"""The interview POOL for the manager-delegation tier.

An "interview" that the admin divides among managers is a persona MAILBOX that has an
interview invitation — i.e. a mailbox whose furthest inbound mail stage is `interview`
(mail_index kind='interview'; the same funnel ranking as tools/stats.py). A mailbox is
"unallocated" (in the free pool the admin can split) until it has a non-cancelled
iv_interviews row — a manager delegation row OR a direct «Собес» booking — so the pool
and the direct-booking path never serve the same interview twice.

This module only READS mail_index (via the shared mail_db pool) to enumerate the pool +
the latest interview message's metadata (subject / recruiter / hash) for display and for
linking the interview thread; the delegation rows themselves live in `db` (iv_interviews).
No stack names, neutral display. Nothing here mutates mail_index.
"""
from __future__ import annotations

import glob
import json
import os
from html import escape

from backend.interviews import db
from backend.tools import mail_db

_PREFILL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        "uploads", "prefill")
_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "demo_personas.json")

# The per-candidate funnel ranking, reused so the pool agrees with the grouped inbox's
# «Собес» stage (a candidate that progressed PAST interview, e.g. to an offer, is not in
# the interview pool).
_STAGE_SQL = mail_db._FURTHEST_STAGE_SQL

# One SELECT: mailboxes at the interview stage, with the latest interview message's meta,
# minus any already-handled (non-cancelled iv_interviews row) mailbox. Ordered newest-first.
_UNALLOCATED_SQL = f"""
WITH stage AS (
    SELECT mailbox, {_STAGE_SQL} AS stage
      FROM mail_index GROUP BY mailbox
),
latest AS (
    SELECT DISTINCT ON (mailbox)
           mailbox, candidate, subject, snippet, from_email, from_name, date_ts, path_hash
      FROM mail_index
     WHERE kind='interview' AND NOT outbound
     ORDER BY mailbox, date_ts DESC, path_hash DESC
)
SELECT l.mailbox, l.candidate, l.subject, l.snippet, l.from_email, l.from_name, l.date_ts,
       l.path_hash AS source_hash
  FROM stage s
  JOIN latest l ON l.mailbox = s.mailbox
  LEFT JOIN (SELECT DISTINCT mailbox FROM iv_interviews WHERE status <> 'cancelled') iv
         ON iv.mailbox = s.mailbox
 WHERE s.stage = 'interview' AND iv.mailbox IS NULL
 ORDER BY l.date_ts DESC, l.mailbox
"""

# Common ATS / no-reply domains whose label is NOT the hiring company — don't show them
# as the "company" (better to show nothing than a misleading ATS name).
_ATS_DOMAINS = {
    "greenhouse", "greenhouse-mail", "ashbyhq", "hire", "lever", "myworkday",
    "myworkdayjobs", "icims", "smartrecruiters", "workable", "workablemail",
    "us", "taleo", "oraclecloud", "avature", "gmail", "googlemail", "notifications",
    "bounce", "no-reply", "noreply", "e", "mail", "email", "mkt", "info",
}


def company_hint(from_email: str, from_name: str = "") -> str:
    """A best-effort neutral company label for a pool row: the recruiter's org from the
    email domain, unless it is a generic ATS/no-reply domain (then blank). Never a stack
    name, never misleading — blank is fine, the subject already identifies the interview."""
    dom = (from_email or "").split("@")[-1].strip().lower()
    if not dom or "." not in dom:
        return ""
    label = dom.split(".")[0]
    # a subdomain like "jobs.acme.com" → prefer the registrable label "acme"
    parts = [p for p in dom.split(".") if p]
    if len(parts) >= 3 and parts[0] in _ATS_DOMAINS:
        label = parts[1]
    if label in _ATS_DOMAINS or len(label) <= 2:
        return ""
    return label[:40]


# ---- persona enrichment: GENDER (М/Ж) + DIRECTION (IT / non-IT / other) -----------
# Gender lives in the persona artifact `uploads/prefill/<demo_id>/<jobid>/persona.json`
# (`profile.sex`); when that's been pruned by prefill_retention we fall back to the SYNTH
# name banks (the first name is gendered). Direction = the applied job's `role_category`
# (job_catalog, 13 buckets) mapped to IT vs non-IT; jobid comes from the persona dir.
# IT = the software/eng/data/tech-product buckets; everything else is non-IT; unknown/Other
# → "other" (never dropped). `email → demo_id` is the demo_personas.json registry.
IT_CATEGORIES = {"Engineering", "Data & ML", "Product", "Design"}
NONIT_CATEGORIES = {
    "Sales / GTM", "Customer Support & Success", "Marketing & Comms", "People & Recruiting",
    "Finance & Accounting", "Operations", "Legal & Compliance", "Executive / Leadership",
}
DIRECTIONS = ("it", "nonit", "other")
GENDERS = ("male", "female")

_REGISTRY: dict | None = None
_NAME_GENDER: dict | None = None
_META: dict = {}   # email -> {"sex","jobid","role_category","direction"} (immutable, cached)


def direction_of(role_category: str | None) -> str:
    if role_category in IT_CATEGORIES:
        return "it"
    if role_category in NONIT_CATEGORIES:
        return "nonit"
    return "other"


# ---- direction legend (ONE source of truth for the user-facing IT/Не-IT/Другое explainer) --
# Reused by every surface that shows a «направление» filter or an IT/Не-IT/Другое cross-tab
# (users_ui delegation card, candidates_inbox Собес priority surface, manage_ui filter). Keep
# in sync with IT_CATEGORIES/NONIT_CATEGORIES above — update this text if those sets change.
# Neutral RU, no stack disclosure.
def direction_legend() -> str:
    """Plain-text (no markup) explanation of the IT / Не-IT / Другое split, suitable for
    dropping straight into a `_page_head(..., info=...)` popover or any other ⓘ tooltip."""
    return (
        "IT — сложные технические направления: Engineering, Data & ML, Product, Design. "
        "Не‑IT — Sales, Customer Support & Success (колл-центр/саппорт), Marketing, "
        "People/Recruiting, Finance, Operations, Legal, Executive. "
        "Другое — роль не распознана или не входит ни в один из бакетов выше."
    )


def direction_legend_html(aria_label: str = "Что означает направление") -> str:
    """Ready-to-embed ⓘ popover for the legend text, reusing the same `.ph-info-d`/`.ph-pop`
    markup+CSS as `mailcrm_ui._page_head`'s `info=` popover (that CSS ships globally on every
    page via `mailcrm_ui._CSS`, so this renders correctly wherever it's dropped — no extra
    styling needed). Use this directly next to a «направление» select/filter or an IT/Не-IT/
    Другое cross-tab that ISN'T rendered through `_page_head`."""
    return (f'<details class="ph-info-d dir-legend"><summary aria-label="{escape(aria_label, quote=True)}">ⓘ</summary>'
            f'<div class="ph-pop">{escape(direction_legend())}</div></details>')


def _registry() -> dict:
    """demo_personas.json {email: {id, name}} — loaded once (email is the unique key)."""
    global _REGISTRY
    if _REGISTRY is None:
        try:
            with open(_REGISTRY_PATH, encoding="utf-8") as fh:
                _REGISTRY = json.load(fh)
        except Exception:
            _REGISTRY = {}
    return _REGISTRY


def _name_gender() -> dict:
    """{first_name_lower: 'male'|'female'} from the synth name banks — the gender fallback
    when a persona's prefill artifact (with `profile.sex`) has been pruned. Names present in
    BOTH banks are ambiguous and dropped."""
    global _NAME_GENDER
    if _NAME_GENDER is None:
        males: set = set()
        females: set = set()
        try:
            from backend.tools import synth_persona as sp
            for banks in (getattr(sp, "_NAMES", {}) or {}).values():
                if isinstance(banks, dict):
                    for n in banks.get("male", []):
                        males.add(str(n).strip().lower())
                    for n in banks.get("female", []):
                        females.add(str(n).strip().lower())
        except Exception:
            pass
        both = males & females
        _NAME_GENDER = {}
        for n in males - both:
            _NAME_GENDER[n] = "male"
        for n in females - both:
            _NAME_GENDER[n] = "female"
    return _NAME_GENDER


def _jobid_from_status(demo_id: str) -> str | None:
    """Recover a persona's jobid from uploads/prefill/<demo_id>/status.json when its per-job
    persona.json artifacts have been pruned by prefill_retention — status.json (a flat
    {jobid: {status, ts}} map) SURVIVES that pruning, so it keeps the persona→job link durable
    for the interview cohort (almost all older than the 20-day retention window). Prefer a
    submitted job, else the most recent."""
    try:
        with open(os.path.join(_PREFILL, demo_id, "status.json"), encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    rows = []
    for jid, info in d.items():
        if not str(jid).isdigit():
            continue
        info = info if isinstance(info, dict) else {}
        # `ts` is an ISO-8601 STRING here ("2026-08-27T05:42:02+00:00"), so sort it as a string
        # (ISO strings order chronologically) — never float() it, that raises + kills the fallback.
        rows.append((1 if info.get("status") == "submitted" else 0,
                     str(info.get("ts") or ""), str(jid)))
    if not rows:
        return None
    rows.sort(reverse=True)
    return rows[0][2]


def _base_meta(email: str) -> dict:
    """{sex, jobid} for one persona email — prefill `profile.sex` first, else the name-bank
    gender; jobid from the persona dir, else from the durable status.json (which outlives the
    prefill artifacts prefill_retention prunes) so an older interview still resolves its job."""
    ent = _registry().get(email) or {}
    demo_id = ent.get("id")
    sex = None
    jobid = None
    if demo_id:
        for pj in glob.glob(os.path.join(_PREFILL, demo_id, "*", "persona.json")):
            try:
                with open(pj, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            sex = ((d.get("profile") or {}).get("sex") or d.get("sex") or sex)
            jd = os.path.basename(os.path.dirname(pj))
            if jd.isdigit():
                jobid = jd
            if sex and jobid:
                break
        if not jobid:                       # persona.json pruned → recover from status.json
            jobid = _jobid_from_status(demo_id)
    if not sex:
        fn = (ent.get("name") or "").split(" ")[0].strip().lower()
        sex = _name_gender().get(fn)
    return {"sex": (sex if sex in GENDERS else "unknown"), "jobid": jobid}


def _role_categories(jobids) -> dict:
    """{int jobid: role_category} in one job_catalog query. Best-effort → {}."""
    ids = sorted({int(j) for j in jobids if j and str(j).isdigit()})
    if not ids:
        return {}
    try:
        from backend.tools import catalog_db
        jobs = catalog_db.jobs_by_ids(ids)
        return {jid: (j or {}).get("role_category") for jid, j in jobs.items()}
    except Exception:
        return {}


def enrich(rows: list[dict]) -> list[dict]:
    """Add `sex` (male|female|unknown), `jobid`, `role_category`, `direction` (it|nonit|other)
    to each pool row (keyed on `mailbox`). Cached per-email (immutable), one batched
    job_catalog lookup for the misses."""
    missing = [r["mailbox"] for r in rows if r["mailbox"] not in _META]
    if missing:
        # per-email try so one malformed prefill artifact can't abort the whole batch (which
        # would empty the delegation + priority cards behind routes_users' blanket except).
        def _safe_meta(e):
            try:
                return _base_meta(e)
            except Exception:
                return {"sex": "unknown", "jobid": None}
        base = {e: _safe_meta(e) for e in missing}
        cats = _role_categories([b["jobid"] for b in base.values()])
        for email, b in base.items():
            cat = cats.get(int(b["jobid"])) if (b["jobid"] and b["jobid"].isdigit()) else None
            _META[email] = {"sex": b["sex"], "jobid": b["jobid"],
                            "role_category": cat, "direction": direction_of(cat)}
    for r in rows:
        m = _META.get(r["mailbox"], {})
        r["sex"] = m.get("sex", "unknown")
        r["jobid"] = m.get("jobid")
        r["role_category"] = m.get("role_category")
        r["direction"] = m.get("direction", "other")
    return rows


def enrich_iv_rows(rows: list[dict]) -> list[dict]:
    """Add `sex` + `direction` to iv_interviews rows (manager portal). Gender from the
    mailbox (registry/name-bank, no prefill needed); direction from the row's stored `jobid`
    → role_category. Mutates + returns the rows."""
    cats = _role_categories([r.get("jobid") for r in rows])
    for r in rows:
        mb = r.get("mailbox") or ""
        m = _META.get(mb)
        r["sex"] = (m or {}).get("sex") or _base_meta(mb)["sex"]
        jid = r.get("jobid")
        cat = cats.get(int(jid)) if (jid and str(jid).isdigit()) else None
        r["role_category"] = cat
        r["direction"] = direction_of(cat)
    return rows


def _match(row: dict, q: str | None, gender: str | None, direction: str | None,
           include_expired: bool = False) -> bool:
    # an EXPIRED booking window (deadline strictly past) is not delegatable — exclude it from
    # the free pool / facets / split unless a caller explicitly wants the full set (the /users
    # priority card, which still SHOWS them at the bottom, dimmed + «делегировать нельзя»).
    if not include_expired and row.get("expired"):
        return False
    if gender in GENDERS and row.get("sex") != gender:
        return False
    if direction in DIRECTIONS and row.get("direction") != direction:
        return False
    if q:
        ql = q.strip().lower()
        if ql and ql not in (row.get("mailbox") or "").lower() \
                and ql not in (row.get("candidate") or "").lower():
            return False
    return True


_POOL_CACHE: dict = {"t": 0.0, "rows": None}


def _invalidate() -> None:
    """Drop the short-lived whole-pool cache — called after an allocation so the next render
    reflects it immediately (the SQL already excludes handled mailboxes)."""
    _POOL_CACHE["rows"] = None


def _all_unallocated() -> list[dict]:
    """The WHOLE free pool, enriched (company + gender + direction). Not paginated — the pool
    is small (~hundreds) and filtering happens in Python across the whole set. TTL-cached 20s
    so a single render (facets + list + datalist) shares ONE heavy SQL; invalidated on
    allocate."""
    import time
    now = time.time()
    if _POOL_CACHE["rows"] is not None and now - _POOL_CACHE["t"] < 20:
        return _POOL_CACHE["rows"]
    try:
        with mail_db._cur() as cur:
            cur.execute(_UNALLOCATED_SQL)
            rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    for r in rows:
        r["company"] = company_hint(r.get("from_email") or "", r.get("from_name") or "")
    enrich(rows)
    # deadline + salary + refined direction + an `expired` flag (durable email parse, cached by
    # message hash) so the DELEGATABLE pool reflects only still-bookable interviews and every row
    # carries a salary/priority for the /users priority card.
    try:
        from backend.tools import interview_priority
        interview_priority.enrich_interview_groups(rows, hash_key="source_hash")
        for r in rows:
            r["expired"] = interview_priority.is_expired(r)
    except Exception:
        for r in rows:
            r.setdefault("expired", False)
    _POOL_CACHE.update(t=now, rows=rows)
    return rows


def unallocated(limit: int | None = 500, offset: int = 0, q: str | None = None,
                gender: str | None = None, direction: str | None = None,
                include_expired: bool = False) -> list[dict]:
    """The free interview pool, newest-invitation first, optionally FILTERED by email/name
    search `q` (email is the primary key), `gender` (male|female) and `direction`
    (it|nonit|other). EXPIRED interviews are excluded by default (not delegatable); pass
    include_expired=True for the priority view that shows them at the bottom. Each row:
    {mailbox, candidate, subject, from_email, from_name, date_ts, source_hash, company, sex,
    jobid, role_category, direction, deadline_ts, deadline_days, salary_label, expired}."""
    rows = [r for r in _all_unallocated() if _match(r, q, gender, direction, include_expired)]
    if limit is not None:
        rows = rows[int(offset):int(offset) + int(limit)]
    return rows


def count_unallocated(q: str | None = None, gender: str | None = None,
                      direction: str | None = None) -> int:
    """How many STILL-BOOKABLE interviews match the filter in the free pool right now (expired
    ones are not counted — they can no longer be delegated)."""
    return sum(1 for r in _all_unallocated() if _match(r, q, gender, direction))


def facets() -> dict:
    """Availability cross-tab for the split UI: {'total', 'gender':{male,female,unknown},
    'direction':{it,nonit,other}, 'cross':{(gender,direction): n}} over the still-bookable free
    pool (EXPIRED excluded) — so the admin splits only interviews that can actually be delegated."""
    rows = [r for r in _all_unallocated() if not r.get("expired")]
    g = {"male": 0, "female": 0, "unknown": 0}
    d = {"it": 0, "nonit": 0, "other": 0}
    cross: dict = {}
    for r in rows:
        sx = r.get("sex") if r.get("sex") in g else "unknown"
        di = r.get("direction") if r.get("direction") in d else "other"
        g[sx] += 1
        d[di] += 1
        cross[(sx, di)] = cross.get((sx, di), 0) + 1
    return {"total": len(rows), "gender": g, "direction": d, "cross": cross}


def allocate_specific(mailbox: str, manager_id: int) -> bool:
    """Send ONE specific pool interview to a manager. No-op (returns False) if the mailbox
    is not currently in the free pool (already handled / not an interview)."""
    row = next((r for r in _all_unallocated() if r["mailbox"] == mailbox), None)
    if row is None or row.get("expired"):   # an expired booking window is not delegatable
        return False
    db.allocate_interview(
        mailbox=row["mailbox"], manager_id=manager_id,
        company=row.get("company") or "", jobid=row.get("jobid") or "",
        subject=(row.get("subject") or "")[:400],
        source_message_hash=row.get("source_hash") or "")
    _invalidate()
    return True


def split(manager_counts: dict[int, int], gender: str | None = None,
          direction: str | None = None) -> dict[int, int]:
    """Divide the free pool across managers, honouring the gender + direction filter: allocate
    `manager_counts[mid]` MATCHING interviews to each manager (blocks, newest-first). Returns
    {manager_id: number actually allocated} (capped by the matching pool). Admin-only tool."""
    wanted = {int(mid): max(0, int(n)) for mid, n in (manager_counts or {}).items() if int(n) > 0}
    total = sum(wanted.values())
    if total <= 0:
        return {}
    rows = unallocated(limit=total, gender=gender, direction=direction)
    out: dict[int, int] = {mid: 0 for mid in wanted}
    i = 0
    for mid, n in wanted.items():
        for _ in range(n):
            if i >= len(rows):
                return out
            r = rows[i]
            db.allocate_interview(
                mailbox=r["mailbox"], manager_id=mid,
                company=r.get("company") or "", jobid=r.get("jobid") or "",
                subject=(r.get("subject") or "")[:400],
                source_message_hash=r.get("source_hash") or "")
            out[mid] += 1
            i += 1
    _invalidate()
    return out
