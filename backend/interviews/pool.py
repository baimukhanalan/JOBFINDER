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
           mailbox, candidate, subject, from_email, from_name, date_ts, path_hash
      FROM mail_index
     WHERE kind='interview' AND NOT outbound
     ORDER BY mailbox, date_ts DESC, path_hash DESC
)
SELECT l.mailbox, l.candidate, l.subject, l.from_email, l.from_name, l.date_ts,
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


def _base_meta(email: str) -> dict:
    """{sex, jobid} for one persona email — prefill `profile.sex` first, else the name-bank
    gender; jobid from the persona dir when its prefill artifact still exists."""
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
        base = {e: _base_meta(e) for e in missing}
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


def _match(row: dict, q: str | None, gender: str | None, direction: str | None) -> bool:
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
    _POOL_CACHE.update(t=now, rows=rows)
    return rows


def unallocated(limit: int | None = 500, offset: int = 0, q: str | None = None,
                gender: str | None = None, direction: str | None = None) -> list[dict]:
    """The free interview pool, newest-invitation first, optionally FILTERED by email/name
    search `q` (email is the primary key), `gender` (male|female) and `direction`
    (it|nonit|other). Each row: {mailbox, candidate, subject, from_email, from_name, date_ts,
    source_hash, company, sex, jobid, role_category, direction}."""
    rows = [r for r in _all_unallocated() if _match(r, q, gender, direction)]
    if limit is not None:
        rows = rows[int(offset):int(offset) + int(limit)]
    return rows


def count_unallocated(q: str | None = None, gender: str | None = None,
                      direction: str | None = None) -> int:
    """How many interviews match the filter in the free pool right now."""
    return sum(1 for r in _all_unallocated() if _match(r, q, gender, direction))


def facets() -> dict:
    """Availability cross-tab for the split UI: {'total', 'gender':{male,female,unknown},
    'direction':{it,nonit,other}, 'cross':{(gender,direction): n}} over the whole free pool —
    so the admin can see e.g. how many IT female interviews are available before splitting."""
    rows = _all_unallocated()
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
    if row is None:
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
