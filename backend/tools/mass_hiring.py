"""Mass Hiring board — REMOTE-only, mass-hiring US jobs the HUMAN applies to manually.

Deliberately SEPARATE from the auto-apply `job_catalog` (catalog_db.py): the employers here
(remote-job aggregators, BPOs, Amazon's remote slice, later Workday) are NOT auto-applyable by
the bot, so they must never enter the auto-apply selection. This is a pure discovery surface:
collect → classify → store history → rank companies by mass-hiring signal → show, with each job
linking out to its own apply page ("подать вручную").

HARD RULES (user-set 2026-08-26): store ONLY (1) REMOTE jobs and (2) MASS-HIRING categories
(high-volume entry roles: customer support/service/success, sales/SDR, data entry, content
moderation, virtual assistant, claims/ops, recruiting coordinator). Anything senior/dev/exec or
on-site is dropped at collection time.

Lives in the same isolated `jobfinder_crm` DB (CRM_PG_DSN). Sync psycopg2, like catalog_db.

    python -m backend.tools.mass_hiring --collect        # pull every source → upsert
    python -m backend.tools.mass_hiring --stats          # top companies by mass_hiring_score
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
import psycopg2.pool

_ENV = Path(__file__).resolve().parents[1] / ".env"
# Accept-Encoding pinned to gzip/deflate: some sources (amazon.jobs) reply zstd, which trips
# httpx's zstd decoder ("cannot use a decompressobj multiple times") and the whole fetch fails.
_UA = {"User-Agent": "Mozilla/5.0 (compatible; JobFinderMassHire/1.0)",
       "Accept-Encoding": "gzip, deflate"}
# A real-browser UA for sources fronted by a WAF that rejects the bot UA above (Cloudflare on
# Maximus/Avature, Akamai on Kelly/mykelly.com).
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _dsn() -> str:
    dsn = os.environ.get("CRM_PG_DSN")
    if dsn:
        return dsn
    try:
        for line in _ENV.read_text().splitlines():
            if line.strip().startswith("CRM_PG_DSN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("CRM_PG_DSN not set (backend/.env or environment)")


_pool = None
_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 6, dsn=_dsn())
    return _pool


@contextmanager
def conn():
    p = _get_pool()
    c = p.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        p.putconn(c)


@contextmanager
def _cur(dict_rows: bool = True):
    with conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            yield cur
        finally:
            cur.close()


# ---- schema --------------------------------------------------------------------
def _missing_columns(cur, table: str, cols) -> list[str]:
    """Which of `cols` the table lacks — via information_schema (no table lock), so the nightly
    ensure_schema never requests an exclusive lock for a column that already exists."""
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s AND column_name = ANY(%s)", (table, list(cols)))
    have = {r[0] for r in cur.fetchall()}
    return [c for c in cols if c not in have]


def ensure_schema() -> None:
    with _cur(dict_rows=False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS mass_hiring_jobs (
          id           BIGSERIAL PRIMARY KEY,
          source       TEXT NOT NULL,          -- remotive / himalayas / amazon / ...
          source_id    TEXT NOT NULL,
          company      TEXT,
          company_key  TEXT,                   -- normalized slug for grouping
          title        TEXT,
          category     TEXT,                   -- normalized mass-hiring category
          location_raw TEXT,
          us_eligible  BOOLEAN DEFAULT FALSE,
          employment_type TEXT,
          seniority    TEXT,
          salary_min   INTEGER,
          salary_max   INTEGER,
          salary_raw   TEXT,
          apply_url    TEXT,
          posted_at    BIGINT DEFAULT 0,
          first_seen   BIGINT DEFAULT 0,
          last_seen    BIGINT DEFAULT 0,
          active       BOOLEAN DEFAULT TRUE,
          UNIQUE (source, source_id)
        );""")
        # A blocked DDL must FAIL FAST, not queue every reader of the table behind it for hours
        # (incident 2026-09-07..09: an idle-in-transaction session held the table, the nightly
        # ALTER waited ~2 days for its ACCESS EXCLUSIVE lock, and /mass-hiring + all 6 apply lanes
        # hung in the lock queue behind it). SET LOCAL scopes it to this transaction.
        cur.execute("SET LOCAL lock_timeout = '15s'")
        # `ADD COLUMN IF NOT EXISTS` still takes the exclusive lock even when the column exists,
        # so check the catalog (a plain SELECT) and only ALTER when something is really missing.
        for col in _missing_columns(cur, "mass_hiring_jobs", ("comp_type", "auto_status")):
            cur.execute(f"ALTER TABLE mass_hiring_jobs ADD COLUMN IF NOT EXISTS {col} TEXT;")
        # Widen pay columns to NUMERIC so a real hourly rate keeps its cents (TTEC "$21.65").
        # Guarded: the ALTER (a table rewrite) runs ONCE, only while still integer.
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='mass_hiring_jobs' AND column_name='salary_min'")
        dt = cur.fetchone()
        if dt and dt[0] == "integer":
            cur.execute("ALTER TABLE mass_hiring_jobs ALTER COLUMN salary_min TYPE NUMERIC(10,2)")
            cur.execute("ALTER TABLE mass_hiring_jobs ALTER COLUMN salary_max TYPE NUMERIC(10,2)")
        cur.execute("CREATE INDEX IF NOT EXISTS mh_company ON mass_hiring_jobs (company_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS mh_cat ON mass_hiring_jobs (category);")
        cur.execute("CREATE INDEX IF NOT EXISTS mh_active ON mass_hiring_jobs (active);")


def backfill_comp_type() -> int:
    """Set comp_type on every existing row from its title/category (deterministic).
    The nightly collect self-heals new rows; this one-shot labels the backlog."""
    ensure_schema()
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT id, title, category FROM mass_hiring_jobs")
        rows = cur.fetchall()
        for _id, title, cat in rows:
            cur.execute("UPDATE mass_hiring_jobs SET comp_type=%s WHERE id=%s",
                        (comp_type(title, cat), _id))
    return len(rows)


def backfill_auto_status() -> int:
    """Set auto_status on every row from its source (recon map). The nightly collect
    self-heals new rows; this one-shot labels the backlog."""
    ensure_schema()
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT DISTINCT source FROM mass_hiring_jobs")
        srcs = [r[0] for r in cur.fetchall()]
        n = 0
        for s in srcs:
            cur.execute("UPDATE mass_hiring_jobs SET auto_status=%s WHERE source=%s",
                        (auto_status(s), s))
            n += cur.rowcount
    return n


def backfill_ttec_pay() -> tuple[int, int]:
    """One-shot: read the posted hourly wage from each active TTEC posting's detail page and
    store it. Returns (rows_with_a_real_rate, rows_scanned). The nightly collect self-heals
    new rows; this labels the backlog now. Gentle thread pool, per-row guarded."""
    ensure_schema()
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT id, apply_url FROM mass_hiring_jobs "
                    "WHERE source='ttec' AND active AND salary_min IS NULL")
        rows = cur.fetchall()

    def _one(rec):
        _id, url = rec
        lo, hi, raw = _ttec_detail_pay(url or "")
        return (_id, lo, hi, raw)

    got = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_one, rows))
    with _cur(dict_rows=False) as cur:
        for _id, lo, hi, raw in results:
            if lo is None:
                continue
            cur.execute("UPDATE mass_hiring_jobs SET salary_min=%s, salary_max=%s, salary_raw=%s "
                        "WHERE id=%s", (lo, hi, raw, _id))
            got += 1
    return (got, len(rows))


# ---- classification (the two HARD RULES) ---------------------------------------
# Mass-hiring categories: title → normalized bucket. A title matching NONE is NOT mass-hiring
# and is DROPPED (that's how "only mass-hiring jobs" is enforced).
_CATEGORIES = [
    ("customer_support", re.compile(
        r"customer (support|service|success|experience|care|advoc|solution|relation|operation)|"
        r"support (specialist|agent|rep|advoc|associate|analyst|engineer)|"
        r"technical support|help ?desk|(call|contact) cent(er|re)|\bcsr\b|"
        r"client (support|success|service)|member (support|service|care|advoc)|player support|"
        r"patient (access|service|care|support|advoc|coordinator)|"
        r"claims (processor|specialist|associate|rep|examiner|agent|adjuster)|"
        r"enrollment (specialist|rep|coordinator|advisor)|intake (specialist|coordinator|rep)|"
        r"(healthcare|insurance|benefits|billing|financial services) (rep\b|representative|agent|associate|advisor)|"
        r"trust (and|&) safety|content (review|moderat)|community (support|moderat)", re.I)),
    ("sales", re.compile(
        r"\bsdr\b|\bbdr\b|sales (development|dev) rep|business development rep|"
        r"inside sales|telesales|appointment setter|lead generation (rep|specialist)", re.I)),
    ("data_entry", re.compile(
        r"data entry|data annotat|data label|transcription|transcriber|annotator|"
        r"image (annotat|label)|content (tagger|labeler)", re.I)),
    ("virtual_assistant", re.compile(
        r"virtual assistant|\bva\b|executive assistant|administrative assistant|admin assistant", re.I)),
    ("operations", re.compile(
        r"claims (processor|specialist|associate)|(back|front) office|verification (agent|specialist)|"
        r"onboarding specialist|operations (associate|coordinator|specialist)|order (processor|management)|"
        r"billing (specialist|associate)|scheduling coordinator", re.I)),
    ("recruiting", re.compile(
        r"recruit(ing|ment) coordinator|talent (coordinator|sourcer)|sourcer", re.I)),
]
# Drop senior / leadership / specialist-engineer titles even if they brush a category — those are
# NOT mass-hiring entry roles.
_NOT_MASS = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president|"
    r"architect|chief|founding|expert|specialist iii|iii\b)\b", re.I)
# also drop obvious dev/engineering roles that slip through loose source categories
_DEV = re.compile(r"\b(software|backend|frontend|full[- ]?stack|devops|data|ml|ai) engineer\b|"
                  r"\bdeveloper\b|\bprogrammer\b", re.I)
# Big health insurers (UnitedHealth/Optum, Humana, Centene, Cigna, Elevance, CVS) post a huge volume
# of CLINICAL / licensed roles alongside their entry CSR pipeline — drop them (a CSR board is not a
# nursing/pharmacy board). Kept specific so it never hits a CSR title ("Pharmacy CARE Rep" has no
# 'pharmacist'; "Clinical ADMIN Coordinator" has no rn/nurse and IS an entry role — see _CARE_EXTRA).
_CLINICAL = re.compile(
    r"\b(rn|lpn|lvn|lcsw)\b|registered nurse|nurse practitioner|\bnurse\b|clinician|therapist|"
    r"physician|pharmacist|medical director|m\.d\.|radiologist|oncolog|dermatolog|cardiolog|"
    r"psychologist|surgeon|\bresident\b|hedis|dosimetrist", re.I)
# Extra mass-hiring ENTRY roles that the base _CATEGORIES miss — the health-insurer member-services
# lexicon (care coordinator/navigator, member/provider advocate, correspondence/claims-resolution rep,
# community health worker, collections/eligibility rep, admin coordinator). Tuned against live titles
# from UnitedHealth/Humana/Centene/Cigna (2026-08-28); _NOT_MASS/_CLINICAL still veto senior/clinical.
_CARE_EXTRA = re.compile(
    r"care (coordinator|navigator|advocat|guide|specialist|associate)|"
    r"care management (support|assistant|associate|coordinator)|"
    r"(inbound|outbound) contacts? (rep\b|representative|associate|specialist|agent)|"
    r"(clinical )?admin(istrative)? coordinator|"
    r"(member|provider|patient|client|consumer) (advocat|navigator|liaison|concierge|contact|"
    r"engagement|experience|resource|support|service)|"
    r"community health worker|"
    r"health (program|plan|guide) (rep\b|representative|coordinator|specialist|advisor|advocate)|"
    r"correspondence (rep\b|representative|specialist)|"
    r"claims (research|resolution)[^,]*(rep\b|representative|specialist|analyst)|"
    r"recovery (and|&) resolution (rep\b|representative|specialist)|"
    r"broker (agent )?service|"
    r"collections (rep\b|representative|specialist)|"
    r"eligibility (rep\b|representative|specialist|coordinator)|"
    r"scheduling (rep\b|representative|coordinator|specialist)", re.I)


def categorize(title: str) -> str | None:
    """Return the mass-hiring category for a title, or None if it is NOT a mass-hiring role."""
    t = title or ""
    if _NOT_MASS.search(t) or _DEV.search(t) or _CLINICAL.search(t):
        return None
    for name, rx in _CATEGORIES:
        if rx.search(t):
            return name
    if _CARE_EXTRA.search(t):
        return "customer_support"
    return None


# ---- compensation type: stable fixed pay vs commission / percent-of-sales ------
# What the human cares about: a stable hourly/salary W-2 role (⭐) vs one paid on
# commission (SDR/BDR/telesales, "base + commission", OTE). Deterministic, title-driven.
_COMMISSION_RE = re.compile(
    r"\bcommission\b|\bote\b|uncapped|base ?\+ ?commission|per[- ]sale|\bquota\b|"
    r"commission[- ]only|100% ?commission|1099 ?commission|\bdraw\b", re.I)


def comp_type(title: str, category: str | None) -> str:
    """'variable' for commission / percent-of-sales roles, else 'fixed' (stable pay)."""
    if category == "sales" or _COMMISSION_RE.search(title or ""):
        return "variable"
    return "fixed"


# ---- auto-apply feasibility per source (recon 2026-08-30) -----------------------
# 'auto'         = our server can auto-submit unattended (no human, no residential IP).
# 'needs_laptop' = automatable but needs the owner's laptop (residential IP / a human to
#                  solve a captcha or WAF challenge) — mark + do later.
# 'blocked'      = a mandatory human video/voice/VJT assessment gates the application.
# unmapped (aggregators remotive/himalayas/remoteok, mixed ATS) -> 'unknown'.
_AUTO_STATUS = {
    "maximus": "auto", "alorica": "auto",
    "kelly": "needs_laptop", "concentrix": "needs_laptop", "cvshealth": "needs_laptop",
    "centene": "needs_laptop", "cigna": "needs_laptop", "ttec": "needs_laptop",
    "unitedhealth": "needs_laptop", "teleperformance": "needs_laptop", "sutherland": "needs_laptop",
    # foundever: SuccessFactors careersection, single-page account-creation apply, NO captcha/résumé.
    # Full-auto server-side via strategies/foundever.py + tools/foundever_recon.py; LIVE-PROVEN
    # 2026-09-19 ("Your Application has been sent" + a SuccessFactors account email in the persona box).
    "foundever": "auto",
    # Transcom = Avature (like Maximus), Percepta = Taleo (like TTEC): the ATS is auto-applyable
    # (no captcha) and reuses the existing strategy, BUT the live lane is tenant-specific so each
    # needs a per-tenant verify pass before wiring — collect-first today. Kept 'needs_laptop' (like
    # the not-yet-wired staffing agencies), NOT 'auto', so the board doesn't show a misleading «Авто»
    # badge. NOT auto-picked by any lane (avature cron = %avature% URL, taleo cron = source='ttec').
    "transcom": "needs_laptop", "percepta": "needs_laptop",
    "humana": "blocked", "conduent": "blocked", "workingsolutions": "blocked", "amazon": "blocked",
    # Staffing agencies (recon 2026-09-20). Randstad: guest apply + résumé + a Friendly-Captcha
    # proof-of-work (self-solving, no image challenge) — the most auto-promising, but not yet
    # proven end-to-end, so 'needs_laptop' until a live ack. Manpower/Experis: no captcha + a
    # guest JobApplyNoAuth endpoint, but apply is a non-URL-addressable client SPA (needs deeper
    # recon). Adecco: mandatory account on a custom candidate SPA. Robert Half: mandatory
    # Salesforce account + reCAPTCHA Enterprise on submit. All four are COLLECT-ONLY today.
    "randstad": "needs_laptop", "manpower": "needs_laptop", "experis": "needs_laptop",
    "adecco": "needs_laptop", "roberthalf": "needs_laptop",
}


def auto_status(source: str) -> str:
    return _AUTO_STATUS.get((source or "").lower(), "unknown")


# ---- hourly pay -----------------------------------------------------------------
_HOURS_PER_YEAR = 2080          # 40h * 52w

# Rough hourly bands for US remote entry mass-hiring roles, by category — a LABELED
# estimate shown only when a posting discloses no pay (most don't). Real posted pay wins.
_HOURLY_EST = {
    "customer_support": (15, 21),
    "operations":       (16, 22),
    "virtual_assistant": (13, 20),
    "data_entry":       (13, 18),
    "recruiting":       (20, 28),
    "sales":            (16, 22),
}


def to_hourly(v) -> float | None:
    """Normalize a stored pay figure to an hourly rate by magnitude:
    <200 already hourly · <10000 monthly · else annual."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v < 200:
        return v
    if v < 10000:
        return v * 12 / _HOURS_PER_YEAR
    return v / _HOURS_PER_YEAR


def hourly_pay(job: dict) -> tuple[float, float, bool] | None:
    """Return (lo, hi, is_estimate) hourly for a job, or None. Posted pay (normalized to
    hourly) wins; otherwise the category estimate."""
    lo = to_hourly(job.get("salary_min"))
    hi = to_hourly(job.get("salary_max"))
    if lo or hi:
        lo, hi = (lo or hi), (hi or lo)
        return (min(lo, hi), max(lo, hi), False)
    est = _HOURLY_EST.get(job.get("category"))
    if est:
        return (float(est[0]), float(est[1]), True)
    return None


# ---- posted-wage parsing (prose) -----------------------------------------------
# Some employers (TTEC) disclose pay only in the DETAIL page prose, e.g.
# "Base hourly wage starting at $21.65" — the collector must read it there, since the
# search-results fragment carries no pay. Reusable for any source whose detail page
# states an hourly rate. Hourly-context-gated so an annual salary / signing bonus /
# relocation figure is never mistaken for an hourly wage.
_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
_HOURLY_NEAR = re.compile(r"(?:per\s+hour|/\s?h(?:ou)?rs?\b|hourly|an\s+hour)", re.I)
_WAGE_RANGE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY, re.I)
_WAGE_ONE = re.compile(_MONEY)


def _wage_num(s: str) -> float | None:
    try:
        v = float((s or "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if 7.0 <= v <= 150.0 else None       # bound to a plausible US hourly rate


def _parse_hourly_wage(text: str) -> tuple[float | None, float | None, str | None]:
    """Extract an hourly wage from prose. Returns (min, max, raw) or (None, None, None).
    Only accepts a figure with nearby hourly context, and each value must fall in a
    plausible hourly band — so annual salaries and bonuses are rejected."""
    if not text:
        return (None, None, None)
    # 1) an explicit range with hourly context in a small window around it
    for m in _WAGE_RANGE.finditer(text):
        if _HOURLY_NEAR.search(text[max(0, m.start() - 40): m.end() + 40]):
            lo, hi = _wage_num(m.group(1)), _wage_num(m.group(2))
            if lo and hi:
                lo, hi = min(lo, hi), max(lo, hi)
                return (lo, hi, f"${lo:g}–${hi:g}/hr")
    # 2) a single value with hourly context nearby ("starting at $X", "$X per hour")
    for m in _WAGE_ONE.finditer(text):
        if _HOURLY_NEAR.search(text[max(0, m.start() - 45): m.end() + 25]):
            v = _wage_num(m.group(1))
            if v:
                return (v, None, f"${v:g}/hr")
    return (None, None, None)


def _ttec_detail_pay(url: str) -> tuple[float | None, float | None, str | None]:
    """Fetch a TTEC detail page and read its posted hourly wage. Fully guarded — any
    failure yields no pay (self-heals on the next run)."""
    if not url:
        return (None, None, None)
    try:
        from bs4 import BeautifulSoup
        r = httpx.get(url, headers=_UA, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return (None, None, None)
        text = BeautifulSoup(r.text, "html.parser").get_text(" ")
        return _parse_hourly_wage(text)
    except Exception as e:
        print(f"[ttec detail {url}] {type(e).__name__}: {e}", file=sys.stderr)
        return (None, None, None)


# US-eligibility from a free-text remote-location field. Three tiers, checked IN ORDER:
#   1. an EXPLICIT US signal (usa / united states / north america / americas) → keep, even when a
#      non-US word also appears ("US or Canada");
#   2. a NON-US region-lock ANYWHERE in the string → reject BEFORE the bare-"remote" allow, so
#      "India (Remote)" / "Philippines, Remote" / "EMEA remote" / "Remote UK" don't leak in on the
#      "remote" token. (The old code tested one broad allow — which INCLUDED bare "remote" — FIRST,
#      so every non-US remote string returned True before the non-US check ran. The non-US regex
#      was also `^`-anchored, which missed a trailing lock like "Remote UK".)
#   3. a generic open signal (anywhere / worldwide / global) or a bare "remote" with no country →
#      keep (unspecified remote on a US-focused board → assume open).
_US_SPECIFIC = re.compile(r"\b(usa?|united states|u\.s\.?|north america|americas)\b", re.I)
_NON_US_ONLY = re.compile(r"\b(europe|emea|apac|uk|united kingdom|india|philippines|latam|"
                          r"latin america|canada|australia|africa|asia)\b", re.I)
_OPEN_REMOTE = re.compile(r"\b(anywhere|worldwide|global|remote)\b", re.I)


def us_eligible(location: str) -> bool:
    loc = (location or "").strip()
    if not loc:
        return True                      # unspecified remote → assume open
    if _US_SPECIFIC.search(loc):
        return True                      # explicit US → keep (wins over any co-occurring non-US word)
    if _NON_US_ONLY.search(loc):
        return False                     # region-locked non-US → reject before the bare-"remote" allow
    if _OPEN_REMOTE.search(loc):
        return True                      # anywhere / worldwide / global / bare remote (no country) → keep
    return False


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())[:40]


# ---- writes --------------------------------------------------------------------
_COLS = ("source", "source_id", "company", "company_key", "title", "category", "comp_type",
         "auto_status", "location_raw", "us_eligible", "employment_type", "seniority",
         "salary_min", "salary_max", "salary_raw", "apply_url", "posted_at", "first_seen",
         "last_seen", "active")


def upsert_jobs(rows: list[dict]) -> int:
    if not rows:
        return 0
    ph = ",".join(["%s"] * len(_COLS))
    # keep first_seen; refresh everything else including last_seen/active. Pay columns are
    # COALESCE'd (new-first): a real posted rate is written, but a transient detail-fetch miss
    # (EXCLUDED NULL) keeps the previously-known rate instead of wiping it.
    _PAY = ("salary_min", "salary_max", "salary_raw")
    upd = ",".join(
        (f"{c}=COALESCE(EXCLUDED.{c},mass_hiring_jobs.{c})" if c in _PAY else f"{c}=EXCLUDED.{c}")
        for c in _COLS if c not in ("source", "source_id", "first_seen"))
    with _cur(dict_rows=False) as cur:
        for r in rows:
            cur.execute(
                f"INSERT INTO mass_hiring_jobs ({','.join(_COLS)}) VALUES ({ph}) "
                f"ON CONFLICT (source, source_id) DO UPDATE SET {upd}",
                [r.get(c) for c in _COLS])
    return len(rows)


def deactivate_stale(source: str, run_start: int) -> int:
    """Mark jobs of this source not refreshed this run as inactive (disappeared at source).
    History is preserved (row stays, last_seen frozen) so hiring-frequency can be computed."""
    with _cur(dict_rows=False) as cur:
        cur.execute("UPDATE mass_hiring_jobs SET active=FALSE "
                    "WHERE source=%s AND active=TRUE AND last_seen < %s", (source, run_start))
        return cur.rowcount


# ---- connectors ----------------------------------------------------------------
def _mk_row(source, source_id, company, title, location, apply_url, *, salary_raw=None,
            salary_min=None, salary_max=None, employment_type=None, seniority=None,
            posted_at=0) -> dict | None:
    """Build a normalized row, applying the two HARD RULES. Returns None if not mass-hiring."""
    cat = categorize(title)
    if not cat:
        return None
    now = int(time.time())
    return {
        "source": source, "source_id": str(source_id), "company": company,
        "company_key": _slug(company), "title": title, "category": cat,
        "comp_type": comp_type(title, cat), "auto_status": auto_status(source),
        "location_raw": location, "us_eligible": us_eligible(location),
        "employment_type": employment_type, "seniority": seniority,
        "salary_min": salary_min, "salary_max": salary_max, "salary_raw": salary_raw,
        "apply_url": apply_url, "posted_at": posted_at or 0,
        "first_seen": now, "last_seen": now, "active": True,
    }


def _iso_epoch(s: str) -> int:
    try:
        from datetime import datetime
        return int(datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def fetch_remotive() -> list[dict]:
    """Remotive public API — all-remote. Pull the mass-hiring-ish categories."""
    rows, cats = [], ["customer-support", "sales", "data-entry", "all-others"]
    for c in cats:
        try:
            r = httpx.get("https://remotive.com/api/remote-jobs",
                          params={"category": c, "limit": 500}, headers=_UA, timeout=30)
            for j in (r.json().get("jobs") or []):
                row = _mk_row("remotive", j.get("id"), j.get("company_name"), j.get("title"),
                              j.get("candidate_required_location"), j.get("url"),
                              salary_raw=j.get("salary") or None,
                              employment_type=j.get("job_type"),
                              posted_at=_iso_epoch(j.get("publication_date")))
                if row:
                    rows.append(row)
        except Exception as e:
            print(f"[remotive/{c}] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


def fetch_himalayas() -> list[dict]:
    """Himalayas public API — all-remote, rich fields (salary, seniority, location restrictions).
    The API returns 20/page regardless of `limit`, so we PAGINATE by offset (the one uncapped
    free source) until a page repeats/empties."""
    rows, seen, off = [], set(), 0
    while off < 4000:
        js = None
        for attempt in range(3):
            try:
                r = httpx.get("https://himalayas.app/jobs/api",
                              params={"limit": 100, "offset": off}, headers=_UA, timeout=30)
                js = r.json().get("jobs") or []
                break
            except Exception as e:
                # himalayas' API intermittently returns non-JSON (rate-limit/5xx) at a RANDOM offset.
                # The old code broke the WHOLE pagination on the first hiccup, collapsing the board's
                # himalayas slice (observed 70→9). Retry this offset with backoff before giving up.
                print(f"[himalayas offset={off} try={attempt}] {type(e).__name__}: {e}", file=sys.stderr)
                time.sleep(1.5 * (attempt + 1))
        if js is None:
            break
        fresh = [j for j in js if j.get("guid") not in seen]
        if not fresh:
            break
        for j in fresh:
            seen.add(j.get("guid"))
            locs = j.get("locationRestrictions") or []
            loc = ", ".join(locs) if isinstance(locs, list) else str(locs or "")
            row = _mk_row("himalayas", j.get("guid"), j.get("companyName"), j.get("title"),
                          loc or "Anywhere", j.get("applicationLink"),
                          salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                          employment_type=j.get("employmentType"), seniority=j.get("seniority"),
                          posted_at=_iso_epoch(j.get("pubDate")))
            if row:
                rows.append(row)
        off += len(js)
    return rows


def fetch_remoteok() -> list[dict]:
    """RemoteOK public API — one big array (first element is a legal notice). US-skewed filter
    drops its many non-US postings."""
    rows = []
    try:
        r = httpx.get("https://remoteok.com/api", headers=_UA, timeout=30)
        for j in r.json():
            if not (isinstance(j, dict) and j.get("position")):
                continue
            row = _mk_row("remoteok", j.get("id") or j.get("slug"), j.get("company"),
                          j.get("position"), j.get("location") or "", j.get("url"),
                          salary_min=j.get("salary_min") or None,
                          salary_max=j.get("salary_max") or None,
                          posted_at=int(j.get("epoch") or 0))
            if row:
                rows.append(row)
    except Exception as e:
        print(f"[remoteok] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


def _amazon_row(j: dict) -> dict | None:
    """One amazon.jobs result → row or None. A remote posting is marked by **city** starting with
    'Virtual' — it takes three forms: 'Virtual', 'Virtual Location - <State>', 'Virtual Contact
    Center-<xx>'. US-eligibility is the **country_code** ('USA'), which is reliable on every row;
    `normalized_location` is 'USA' OR a state-tagged string like 'Texas, USA' (the state-tagged rows
    are exactly where the real mass-hiring CS roles live), so it must NOT be exact-matched against
    'USA'. Re-diagnosed live 2026-08-28 (see fetch_amazon_remote)."""
    city = (j.get("city") or "").strip().lower()
    cc = (j.get("country_code") or "").strip().upper()
    if not (city.startswith("virtual") and cc in ("USA", "US")):
        return None
    url = j.get("url_next_step") or (("https://www.amazon.jobs" + j["job_path"]) if j.get("job_path") else "")
    nloc = j.get("normalized_location") or "USA"
    return _mk_row("amazon", j.get("id"), "Amazon", j.get("title"),
                   f"Virtual, {nloc}", url,
                   posted_at=_iso_epoch(j.get("posted_date") or j.get("updated_time")))


def fetch_amazon_remote() -> list[dict]:
    """Amazon's US virtual slice. Two bugs fixed 2026-08-28 (either alone suppressed all rows):
    (1) result_limit was 200 — the API rejects >100 with {"error":...,"hits":0,"jobs":null}, so the
    connector yielded 0 UNCONDITIONALLY (this is why the board looked 'seasonally empty'); now
    result_limit=100 and we paginate offset. (2) is_us tested normalized_location EXACTLY == 'USA',
    dropping every state-tagged remote row ('Texas, USA', 'Arizona, USA') — i.e. the real CS roles;
    now is_us = country_code and is_remote = city.startswith('virtual'). base_query='virtual' is the
    keyword that surfaces every remote row (a remote posting's city literally contains 'Virtual', so
    it is indexed on it); 'work from home' matches only 4-6 rows and MISSES most CS roles."""
    rows, seen = [], set()
    offset = 0
    while offset < 400:
        try:
            r = httpx.get("https://www.amazon.jobs/en/search.json", headers=_UA, timeout=30,
                          params={"base_query": "virtual", "country": "USA", "result_limit": 100,
                                  "offset": offset, "sort": "recent"})
            d = r.json()
            jobs = d.get("jobs") or []
            hits = int(d.get("hits") or 0)
        except Exception as e:
            print(f"[amazon offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not jobs:
            break
        for j in jobs:
            jid = j.get("id")
            if jid in seen:
                continue
            row = _amazon_row(j)
            if row:
                seen.add(jid)
                rows.append(row)
        offset += 100
        if offset >= hits:
            break
    return rows


# --- BPO connectors (the real remote-US mass-hiring source; endpoints reverse-engineered
#     from each careers SPA's network calls, then verified headless-free with httpx) ---
_REMOTE_RE = re.compile(r"remote|work[- ]?at[- ]?home|work[- ]?from[- ]?home|\bwah\b|virtual|"
                        r"telecommut|home[- ]?based", re.I)


def _is_remote(*texts) -> bool:
    return any(_REMOTE_RE.search(t or "") for t in texts)


# Some employers signal US in a location by a bare 2-letter state code PREFIX ("RI - Work from
# home", "TX - Work from home") or a full state name ("Work At Home-Texas") that us_eligible()'s
# generic USA/remote regex misses. _has_us_state adds that recognition (prefix code OR name) so a
# state-coded work-from-home row still counts as US. Codes are matched only at the string START to
# avoid mid-text traps ("OR"/"IN"/"ME" as English words).
_US_STATE_ABBR = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
                  "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
                  "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
                  "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
_US_STATE_NAME_RE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    r"michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west virginia|wisconsin|wyoming|district of columbia)\b", re.I)


def _has_us_state(loc: str) -> bool:
    l = (loc or "").strip()
    m = re.match(r"([A-Za-z]{2})\b", l)
    if m and m.group(1).upper() in _US_STATE_ABBR:
        return True
    return bool(_US_STATE_NAME_RE.search(l))


def _title_us(title: str) -> bool:
    """US signal in a job TITLE (used where the listing's location field is unreliable, e.g. TTEC
    shows a home-office city for a remote req). True on 'USA'/'United States' or a full state name."""
    t = title or ""
    if re.search(r"\b(usa|u\.s\.a?\.?|united states)\b", t, re.I):
        return True
    return bool(_US_STATE_NAME_RE.search(t))


def fetch_conduent() -> list[dict]:
    """Conduent — Phenom People. POST /widgets, paginate `from` until empty. Remote US CS is the
    genuine target here (healthcare / Medicaid call-center 'Remote US' roles)."""
    rows, frm, size = [], 0, 50
    ref = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    while frm < 800:
        body = {"lang": "en_us", "deviceType": "desktop", "country": "us",
                "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "",
                "subsearch": "", "from": frm, "irs": False, "jobs": True, "counts": False,
                "all_fields": ["category", "country", "state", "city", "type", "remote"],
                "size": size, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page20-ds", "siteType": "external", "keywords": "customer service",
                "global": True, "selected_fields": {}, "locationData": {}}
        try:
            r = httpx.post("https://careers.conduent.com/widgets", json=body, timeout=30,
                           headers={**_UA, "Content-Type": "application/json",
                                    "Accept": "application/json", "User-Agent": ref,
                                    "Referer": "https://careers.conduent.com/us/en/search-results"})
            js = (r.json().get("refineSearch") or {}).get("data", {}).get("jobs") or []
        except Exception as e:
            print(f"[conduent from={frm}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not js:
            break
        for j in js:
            loc = j.get("cityStateCountry") or j.get("location") or ""
            country = j.get("country") or ""
            if not _is_remote(loc, j.get("title")):
                continue                                  # remote-only rule
            if "united states" not in (loc + country).lower() and "us" not in country.lower():
                continue
            row = _mk_row("conduent", j.get("jobId") or j.get("jobSeqNo"), "Conduent",
                          j.get("title"), loc, f"https://careers.conduent.com/us/en/job/{j.get('jobId')}",
                          employment_type=j.get("type"), posted_at=_iso_epoch(j.get("postedDate")))
            if row:
                rows.append(row)
        frm += size
    return rows


def fetch_alorica() -> list[dict]:
    """Alorica — Oracle Recruiting Cloud (ORC). Plain GET, paginate offset. Work-at-home CSR."""
    rows, offset, limit = [], 0, 50
    host = "fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com"
    total = None
    while offset < 600:
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               "?onlyData=true&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
               f"&finder=findReqs;siteNumber=CX_1,limit={limit},offset={offset},"
               "sortBy=POSTING_DATES_DESC,keyword=%22customer%22")
        try:
            r = httpx.get(url, timeout=30, headers={**_UA, "Accept": "application/json"})
            it = (r.json().get("items") or [{}])[0]
            reqs = it.get("requisitionList") or []
            total = it.get("TotalJobsCount", total)
        except Exception as e:
            print(f"[alorica offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not reqs:
            break
        for j in reqs:
            loc = j.get("PrimaryLocation") or ""
            if (j.get("PrimaryLocationCountry") or "").upper() != "US":
                continue
            # remote if the title/location says so, OR it's a bare-country (national) posting
            if not (_is_remote(j.get("Title"), loc) or loc.strip().lower() in ("united states", "us")):
                continue
            row = _mk_row("alorica", j.get("Id"), "Alorica", j.get("Title"), loc or "United States",
                          f"https://{host}/hcmUI/CandidateExperience/en/sites/CX_1/job/{j.get('Id')}",
                          posted_at=_iso_epoch(j.get("PostedDate")))
            if row:
                rows.append(row)
        offset += limit
        if total is not None and offset >= total:
            break
    return rows


# --- Workday CxS (generic) — Concentrix + CVS Health share the same /wday/cxs/<tenant>/<site>/jobs
#     shape. A list row carries only `locationsText` (no country/remote field), so US+remote is read
#     off that string: us_eligible() (USA/remote wording) OR _has_us_state() (state code/name). The
#     bare board's `total` is unreliable (reads 0), so callers narrow with a facet or searchText and
#     we paginate until jobPostings is empty (bounded by offset_cap; limit caps at 20/page). ---
def _workday_row(j: dict, source: str, company: str, host: str, site: str,
                 us_confirmed: bool = False) -> dict | None:
    loc = j.get("locationsText") or ""
    ep = j.get("externalPath") or ""
    # Remote can be encoded in the location OR in the externalPath slug (e.g. a multi-location row
    # shows loc "16 Locations" while the path is /job/Tennessee-Work-at-Home/...).
    if not (_is_remote(loc) or _is_remote(ep.replace("-", " "))):
        return None
    # US is guaranteed when the caller applied a US-country facet (us_confirmed); otherwise (e.g. CVS,
    # narrowed only by a job-family facet) confirm it from the location text (US wording or a state).
    if not (us_confirmed or us_eligible(loc) or _has_us_state(loc)):
        return None
    jid = (j.get("bulletFields") or [None])[0] or ep.rstrip("/").split("_")[-1] or ep
    row = _mk_row(source, jid, company, j.get("title"), loc or "Remote, United States",
                  f"https://{host}/en-US/{site}" + ep)
    if row:
        # US already confirmed above (facet or loc text) — force the flag True so a state-coded /
        # multi-location work-from-home row isn't dropped by collect(us_only=True).
        row["us_eligible"] = True
    return row


def _fetch_workday(source: str, company: str, host: str, tenant: str, site: str, *,
                   search_texts=("",), applied_facets=None, offset_cap: int = 200,
                   us_confirmed: bool = False) -> list[dict]:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    ref = f"https://{host}/{site}"
    rows, seen = [], set()
    for st in search_texts:
        offset = 0
        while offset < offset_cap:
            try:
                r = httpx.post(url, json={"appliedFacets": applied_facets or {}, "limit": 20,
                                          "offset": offset, "searchText": st}, timeout=30,
                               headers={**_UA, "Content-Type": "application/json",
                                        "Accept": "application/json", "Referer": ref})
                js = r.json().get("jobPostings") or []
            except Exception as e:
                print(f"[{source} st={st!r} off={offset}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not js:
                break
            for j in js:
                row = _workday_row(j, source, company, host, site, us_confirmed)
                if row and row["source_id"] not in seen:
                    seen.add(row["source_id"])
                    rows.append(row)
            offset += 20
    return rows


# Concentrix (Workday, tenant cnx). Apply the US locationCountry facet + EMPTY searchText — this
# surfaces ALL 35 US rows (the old searchText='work at home' missed US WAH rows whose text lacks the
# phrase, and the unfaceted `total` reads 0 which broke pagination). `external_global` is Concentrix's
# PROFESSIONAL tier, so most WAH rows are senior and dropped by categorize (~2 mass-hiring entry).
_CNX_US_FACET = "bc33aa3152ec42d4995f4791a106ed09"     # locationCountry = United States of America


def fetch_concentrix() -> list[dict]:
    return _fetch_workday("concentrix", "Concentrix", "cnx.wd1.myworkdayjobs.com", "cnx",
                          "external_global", search_texts=("",),
                          applied_facets={"locationCountry": [_CNX_US_FACET]}, offset_cap=60, us_confirmed=True)


# CVS Health (Workday, tenant cvshealth). The tenant has 8000+ jobs and NO remote/workType facet, so
# searchText does not hard-filter (it only re-ranks — 'work from home' still returns on-site store
# pharmacy techs first). Narrow with the jobFamilyGroup facet 'Customer and Member Services' (57
# rows), then _workday_row keeps the work-from-home / US-state-coded slice (Provider CSR, Service
# Advocate, Technical Support Rep, …). Bounded to 3 pages.
_CVS_FAMILY_CMS = "e65dbadf6a50100168ed7e8f60560002"   # jobFamilyGroup = Customer and Member Services


def fetch_cvs() -> list[dict]:
    return _fetch_workday("cvshealth", "CVS Health", "cvshealth.wd1.myworkdayjobs.com", "cvshealth",
                          "CVS_Health_Careers", search_texts=("",),
                          applied_facets={"jobFamilyGroup": [_CVS_FAMILY_CMS]}, offset_cap=80)


# Teleperformance — custom Umbraco careers API aggregating the underlying iCIMS reqs. node=1780 is
# the US careers opportunitiesId; workFromHome=True + country='United States' are server-side filters,
# so every returned row is US work-from-home by construction. pageSize=500 returns the whole US-WFH
# set in one call.
def _tp_row(r: dict) -> dict | None:
    if str(r.get("workFromHome") or "").strip().lower() not in ("yes", "true", "1"):
        return None
    if (r.get("country") or "").strip().lower() != "united states":
        return None
    loc = r.get("location") or "Remote"
    return _mk_row("teleperformance", r.get("externalId"), "Teleperformance", r.get("title"),
                   f"{loc}, United States", r.get("url"),
                   employment_type=r.get("opportunityType"), posted_at=_iso_epoch(r.get("date")))


def fetch_teleperformance() -> list[dict]:
    rows = []
    try:
        r = httpx.get("https://www.tp.com/Umbraco/Api/Careers/GetCareersBase", headers=_UA, timeout=40,
                      params={"node": 1780, "workFromHome": "True", "country": "United States",
                              "culture": "en-us", "pageSize": 500, "page": 0})
        for j in (r.json().get("resultado") or []):
            row = _tp_row(j)
            if row:
                rows.append(row)
    except Exception as e:
        print(f"[teleperformance] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# TTEC — Radancy TalentBrew. The /search-jobs/results endpoint returns a JSON envelope whose `results`
# key is an HTML fragment (job tiles). CRITICAL: a tile's location span is the requisition HOME OFFICE
# (e.g. an offshore 'Pasay, Philippines' row can carry a "…- Remote" title), so remote-ness AND
# US-eligibility are decided from the TITLE, never the location span.
_TTEC_REMOTE_RE = re.compile(r"remote|work\s*(from|at)\s*home|\bwah\b|virtual|telecommut", re.I)


def _ttec_row(jid: str, title: str, href: str) -> dict | None:
    t = title or ""
    if not (_TTEC_REMOTE_RE.search(t) and _title_us(t)):
        return None
    url = ("https://www.ttecjobs.com" + href) if (href or "").startswith("/") else (href or "")
    return _mk_row("ttec", jid, "TTEC", title, "Remote, United States", url)


def fetch_ttec() -> list[dict]:
    from bs4 import BeautifulSoup
    rows, seen = [], set()
    for kw in ("remote", "work from home"):
        page = 1
        while page <= 5:
            try:
                r = httpx.get("https://www.ttecjobs.com/en/search-jobs/results", headers=_UA, timeout=30,
                              params={"SearchResultsModuleName": "Search Results", "CurrentPage": page,
                                      "RecordsPerPage": 100, "keywords": kw})
                html = r.json().get("results") or ""
            except Exception as e:
                print(f"[ttec kw={kw!r} p={page}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            anchors = BeautifulSoup(html, "html.parser").select("a[data-job-id]")
            new = 0
            for a in anchors:
                jid = a.get("data-job-id")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                h2 = a.select_one("h2")
                row = _ttec_row(jid, h2.get_text(strip=True) if h2 else "", a.get("href"))
                if row:
                    rows.append(row)
            if not anchors or new == 0:
                break
            page += 1
    _enrich_ttec_pay(rows)
    return rows


def _enrich_ttec_pay(rows: list[dict]) -> None:
    """Populate salary_min/max/raw on TTEC rows from each posting's detail page (pay lives
    only in the detail prose). Small thread pool so the nightly collect isn't slowed much;
    a per-row failure just leaves that row's pay None (self-heals next run)."""
    if not rows:
        return
    def _one(row: dict) -> None:
        lo, hi, raw = _ttec_detail_pay(row.get("apply_url") or "")
        if lo is not None:
            row["salary_min"], row["salary_max"], row["salary_raw"] = lo, hi, raw
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(_one, rows))
    except Exception as e:
        print(f"[ttec enrich] {type(e).__name__}: {e}", file=sys.stderr)


# Sutherland — SmartRecruiters public postings API. location.country is lowercase ISO-2 ('us') and
# location.remote is a boolean; filter on those (the fullLocation string still names a US city because
# these are US-homed remote roles). Server-side remote/country params are unreliable, so filter client-side.
def _smartrecruiters_row(j: dict, source: str, company: str) -> dict | None:
    loc = j.get("location") or {}
    if (loc.get("country") or "").lower() != "us" or not loc.get("remote"):
        return None
    full = loc.get("fullLocation") or ", ".join(
        x for x in (loc.get("city"), loc.get("region"), "United States") if x)
    return _mk_row(source, j.get("id"), company, j.get("name"), full or "United States",
                   f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                   posted_at=_iso_epoch(j.get("releasedDate")))


def _fetch_smartrecruiters(source: str, company: str) -> list[dict]:
    rows, offset = [], 0
    while offset < 1000:
        try:
            r = httpx.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                          params={"limit": 100, "offset": offset}, headers=_UA, timeout=30)
            d = r.json()
            content = d.get("content") or []
            total = int(d.get("totalFound") or 0)
        except Exception as e:
            print(f"[{source} offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not content:
            break
        for j in content:
            row = _smartrecruiters_row(j, source, company)
            if row:
                rows.append(row)
        offset += 100
        if offset >= total:
            break
    return rows


def fetch_sutherland() -> list[dict]:
    return _fetch_smartrecruiters("sutherland", "Sutherland")


# Working Solutions — 100%-remote independent-contractor CSR marketplace on an Algolia-backed apply
# portal. Every posting is US/CA remote work-at-home; keep the US ones. The search key is public but
# REFERER-restricted (must send Referer: https://apply.workingsolutions.com/, else 403).
_WS_APP_ID = "UM59DWRPA1"
_WS_API_KEY = "69a3025b68f9c1f44573c9a8b13d7597"        # public referer-restricted Algolia search key


def _ws_row(h: dict) -> dict | None:
    countries = h.get("country") or []
    if isinstance(countries, str):
        countries = [countries]
    if "United States" not in countries:
        return None
    return _mk_row("workingsolutions", h.get("id"), "Working Solutions", h.get("title"),
                   "Remote, United States", f"https://apply.workingsolutions.com/job/{h.get('id')}")


def fetch_working_solutions() -> list[dict]:
    rows = []
    try:
        r = httpx.post("https://UM59DWRPA1-dsn.algolia.net/1/indexes/production_Working%20Solutions_jobs/query",
                       json={"query": "", "hitsPerPage": 100, "facetFilters": [["country:United States"]]},
                       timeout=30, headers={"X-Algolia-Application-Id": _WS_APP_ID,
                                            "X-Algolia-API-Key": _WS_API_KEY,
                                            "Content-Type": "application/json",
                                            "Referer": "https://apply.workingsolutions.com/"})
        for h in (r.json().get("hits") or []):
            row = _ws_row(h)
            if row:
                rows.append(row)
    except Exception as e:
        print(f"[workingsolutions] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# Kelly (KellyConnect) — WordPress WP-REST job_listing feed on www.mykelly.com. The whole host sits
# behind Akamai bot protection that 403s our DATACENTER IP, so route the fetch through the rotating
# proxy pool (its egress passes, verified live). remote + country live in ACF meta (no server-side
# filter), so we page all ~30 pages and filter client-side.
def _kelly_row(j: dict) -> dict | None:
    acf = j.get("acf") or {}
    remote = str(acf.get("remote") or "") == "1"
    us = (acf.get("country_code") or "").upper() == "US" or \
         (acf.get("geolocation_country") or "") == "United States"
    if not (remote and us):
        return None
    import html
    title = html.unescape(((j.get("title") or {}).get("rendered")) or acf.get("job_title") or "")
    loc = acf.get("_job_location") or ""
    url = acf.get("external_apply_url") or j.get("link") or ""
    jid = acf.get("job_id") or j.get("id")
    return _mk_row("kelly", jid, "Kelly", title, f"{loc} (Remote)", url,
                   posted_at=_iso_epoch(j.get("date")))


def _pool_proxy_url() -> str | None:
    """An httpx proxy URL (scheme://user:pass@host:port) for the Kelly COLLECTOR, or None if the
    pool is empty. DATACENTER-ONLY: uses proxy_pool._pool_pick() (the Bright Data datacenter pool
    in data/proxies.json), NOT next_proxy(). next_proxy() short-circuits to a residential/phone
    SOCKS slot first, whose carrier IP Akamai 403s (or which SOCKS-fails) — so mykelly.com's WP-REST
    feed came back empty every collect. The BD datacenter egress is what actually clears Akamai
    (verified: forcing it returned valid rows), matching the documented `mykelly.com ⇒
    BD-datacenter / no-residential` contract."""
    try:
        from backend.tools import proxy_pool
        p = proxy_pool._pool_pick()
    except Exception:
        return None
    if not p or not p.get("server"):
        return None
    server = p["server"]
    if p.get("username"):
        from urllib.parse import quote
        scheme, rest = server.split("://", 1)
        return f"{scheme}://{quote(p['username'])}:{quote(p.get('password') or '')}@{rest}"
    return server


def fetch_kelly() -> list[dict]:
    rows = []
    proxy = _pool_proxy_url()
    if not proxy:
        print("[kelly] no proxy in pool — skipping (Akamai 403s the datacenter IP)", file=sys.stderr)
        return rows
    try:
        with httpx.Client(timeout=45, headers={"User-Agent": _BROWSER_UA}, proxy=proxy) as c:
            page = 1
            while page <= 35:
                r = c.get("https://www.mykelly.com/wp-json/wp/v2/job-listings",
                          params={"per_page": 100, "page": page,
                                  "_fields": "id,link,date,title,acf"})
                if r.status_code >= 400:
                    break
                arr = r.json()
                if not isinstance(arr, list) or not arr:
                    break
                for j in arr:
                    row = _kelly_row(j)
                    if row:
                        rows.append(row)
                if len(arr) < 100:
                    break
                page += 1
    except Exception as e:
        print(f"[kelly] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# Maximus — Avature template-builder portal (portal id 4). NOT Workday. Two-step, no login/captcha:
# (1) GET /careers/Job-Search_US with a cookie jar; the HTML embeds a job-list widget whose
# data-props (nested by device — use the 'desktop' object) carry a STABLE uuid + a PER-SESSION qtvc
# token + formId + link configs; (2) GET /4/_portalList with the SAME cookies + those params. GOTCHAS:
# qtvc rotates every page load (scrape fresh, never hardcode); the context-value keys
# (recordIdContextValues/personIdContextValue/userIdContextValue) make _portalList 500 if sent — so we
# build the querystring from a WHITELIST, not by lifting the whole object. Remote is not a structured
# field → derived from the title/classification text; the portal is US-only.
_MAXIMUS_REMOTE_RE = re.compile(
    r"remote|work[- ]?at[- ]?home|work[- ]?from[- ]?home|\bwah\b|virtual|telework|telecommut|"
    r"home[- ]?based", re.I)
_MAXIMUS_WL = ("uuid", "listType", "searchIndexId", "formId", "qtvc", "searchMode", "layout",
               "allowFilteringFromUrlParams", "hasToIncludePaginationOptions", "allowListSorting",
               "fetchJobIdInPeopleLists", "shouldAddBase64FileFields")
_MAXIMUS_BLOBS = ("firstColumnLinks", "additionalColumnLinks", "links", "conditionalLinkConfig",
                  "dynamicValueConfigs")


def _maximus_params(props: dict) -> dict:
    import json as _json
    p = {}
    for k in _MAXIMUS_WL:
        v = props.get(k)
        p[k] = ("true" if v else "false") if isinstance(v, bool) else ("" if v is None else v)
    for k in _MAXIMUS_BLOBS:
        v = props.get(k)
        p[k] = _json.dumps(v if v is not None else {}, separators=(",", ":"))
    if not p.get("searchMode"):
        p["searchMode"] = "ResultsAndCount"
    if not p.get("layout"):
        p["layout"] = "cards"
    p.update({"sortDirection": "DESC", "filters": "{}", "token": "", "pageUrlParams": "{}"})
    return p


def _maximus_row(res: dict, apply_url: str | None = None) -> dict | None:
    f = res.get("fields") or {}

    def sv(key):
        x = f.get(key)
        return x.get("stringValue") if isinstance(x, dict) else None

    title = sv("schemaField_3_293_3") or ""
    classification = sv("schemaField_3_481_3") or ""
    if not _MAXIMUS_REMOTE_RE.search(f"{title} {classification}"):
        return None
    jloc = f.get("jobLocation") if isinstance(f.get("jobLocation"), dict) else {}
    loc = jloc.get("stringValue") or "Remote"
    country = (((jloc.get("jsonValue") or {}).get("country") or {}).get("name")) or ""
    jid = res.get("id") or sv("jobId")
    loc_str = loc if (country and country.lower() in loc.lower()) else f"{loc}, {country or 'United States'}"
    url = apply_url or f"https://maximus.avature.net/careers/Job-Application?folderId={jid}"
    return _mk_row("maximus", jid, "Maximus", title, loc_str, url, posted_at=_iso_epoch(sv("postedDate")))


def _maximus_collect() -> list[dict]:
    import json as _json
    from bs4 import BeautifulSoup
    rows = []
    try:
        with httpx.Client(timeout=40, headers={"User-Agent": _BROWSER_UA},
                          follow_redirects=True) as c:
            r = c.get("https://maximus.avature.net/careers/Job-Search_US")
            props = None
            for el in BeautifulSoup(r.text, "html.parser").select("[data-props]"):
                dp = el.get("data-props") or ""
                if "qtvc" in dp and "JobList" in dp:
                    try:
                        props = _json.loads(dp)
                    except Exception:
                        props = None
                    break
            if isinstance(props, dict) and "desktop" in props:
                props = props["desktop"]           # data-props is nested by device
            if not props or not props.get("qtvc"):
                print("[maximus] job-list widget / qtvc token not found", file=sys.stderr)
                return rows
            base = _maximus_params(props)
            offset, total = 0, None
            while offset < 600:
                rr = c.get("https://maximus.avature.net/4/_portalList",
                           params={**base, "offset": offset, "recordsPerPage": 50},
                           headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
                try:
                    d = rr.json()
                except Exception:
                    print(f"[maximus offset={offset}] non-JSON (HTTP {rr.status_code})", file=sys.stderr)
                    break
                results = d.get("results") or []
                if not results:
                    break
                t = d.get("total")                 # Avature returns total as a STRING
                if t is not None:
                    try:
                        total = int(t)
                    except (TypeError, ValueError):
                        pass
                links = d.get("additionalLinks") or []
                for i, res in enumerate(results):
                    row = _maximus_row(res, links[i] if i < len(links) else None)
                    if row:
                        rows.append(row)
                offset += len(results)
                if total is not None and offset >= total:
                    break
    except Exception as e:
        print(f"[maximus] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


def fetch_maximus() -> list[dict]:
    # Avature/Cloudflare + the per-session qtvc handshake make this the flakiest source: a transient
    # DNS/connect hiccup returns nothing and would deactivate all rows. Retry the whole two-step flow
    # a few times — maximus always has ~20+ US remote CSR reqs, so an empty result means a transient
    # failure worth retrying, not a genuinely empty board.
    for attempt in range(3):
        rows = _maximus_collect()
        if rows:
            return rows
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return []


# Transcom — Avature careers portal (apply.careers.transcom.com; the CLASSIC server-rendered Avature
# template, NOT Maximus's JS/qtvc `_portalList` flow). `GET /en_US/careers/SearchJobs` returns
# `article.article--result` cards: title `h3 a` → /JobDetail/<slug>/<id>, location `.list-item-location`,
# id `.list-item-jobId`, a description snippet `.article__content`. It is a GLOBAL BPO board (Philippines/
# Europe/LatAm-heavy) whose default page size is flaky (6 vs 30), so US+remote is enforced client-side and
# pagination advances by the count actually returned. Apply is Avature → reuses strategies/avature.py
# after a per-tenant verify pass; the apply_url is on apply.careers.transcom.com (NOT *.avature.net), so
# the Maximus apply cron — scoped to `apply_url ILIKE '%avature%'` — never touches it.
_TRANSCOM_BASE = "https://apply.careers.transcom.com/en_US/careers"


def _transcom_row(jid, title, location, apply_url, desc="") -> dict | None:
    if not (jid and title):
        return None
    loc = location or ""
    if not _is_remote(title, loc, desc):
        return None                                   # remote-only rule
    if not (us_eligible(loc) or _has_us_state(loc) or _title_us(title)):
        return None                                   # US-only rule
    row = _mk_row("transcom", jid, "Transcom", title, loc or "Remote, United States",
                  apply_url or f"{_TRANSCOM_BASE}/SearchJobs")
    if row:
        row["us_eligible"] = True                     # US confirmed above (loc text / state / title)
    return row


def _transcom_cards(client, params) -> list[tuple]:
    """Paginate one SearchJobs query → (jid, title, href, loc, desc) tuples. The portal's page-size
    honoring is inconsistent (returns 6 or 30) and a past-the-end offset can REPEAT the last page, so
    step the offset by the count returned and STOP as soon as a page adds no fresh job (bounded ≤300)."""
    from bs4 import BeautifulSoup
    out, seen, offset = [], set(), 0
    while offset < 300:
        p = {**params, "jobRecordsPerPage": 30, "jobOffset": offset}
        try:
            r = client.get(_TRANSCOM_BASE + "/SearchJobs", params=p)
            arts = BeautifulSoup(r.text, "html.parser").select("article.article--result")
        except Exception as e:
            print(f"[transcom offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not arts:
            break
        new = 0
        for a in arts:
            link = a.select_one("h3 a")
            if not link:
                continue
            href = link.get("href") or ""
            if href in seen:
                continue                              # a repeated page → skip
            seen.add(href)
            new += 1
            id_el = a.select_one(".list-item-jobId")
            loc_el = a.select_one(".list-item-location")
            desc_el = a.select_one(".article__content")
            out.append((
                re.sub(r"\D", "", id_el.get_text()) if id_el else "",
                link.get_text(strip=True), href,
                loc_el.get_text(strip=True) if loc_el else "",
                desc_el.get_text(" ", strip=True) if desc_el else ""))
        if new == 0:                                  # nothing fresh on this page → end of results
            break
        offset += len(arts)
    return out


def fetch_transcom() -> list[dict]:
    # US-remote is a tiny slice of a global board, so query the default board PLUS US/remote-targeting
    # keywords to widen coverage; the client-side _transcom_row filter keeps only US+remote+mass-hiring.
    rows, seen = [], set()
    queries = [{}, {"search": "remote"}, {"search": "work from home"}, {"search": "work at home"},
               {"search": "virtual"}, {"search": "United States"}, {"search": "customer service"}]
    try:
        with httpx.Client(headers={"User-Agent": _BROWSER_UA}, timeout=30,
                          follow_redirects=True) as c:
            for qi, q in enumerate(queries):
                if qi:
                    time.sleep(0.4)                   # light pacing (≤~70 GETs/run for a tiny yield)
                for jid, title, href, loc, desc in _transcom_cards(c, q):
                    if not jid or jid in seen:
                        continue
                    row = _transcom_row(jid, title, loc, href, desc)
                    if row:
                        seen.add(jid)
                        rows.append(row)
    except Exception as e:
        print(f"[transcom] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# Percepta — Ford-affiliated BPO on Taleo. TWO careersections: the US portal is 10300 (portal
# 128160131726); 10400 is the international portal (0 US). Modern FACETED Taleo, so the whole board
# comes from ONE JSON endpoint: POST /careersection/rest/jobboard/searchjobs?lang=en&portal=<id>
# (NOTE: no careersection code in the path — /careersection/rest/…, not /careersection/10300/rest/…).
# GET the jobsearch.ftl page first to set the session cookie. A requisition's `column` array holds
# [title, locations-json]; `linkedColumn` indexes the title column, `locationsColumns` the location
# column(s) whose value is a JSON array of Taleo codes ("US-MI-Dearborn"). Most US CSR reqs are
# SITE-based (Melbourne FL / Dearborn MI) so the remote-only rule keeps just the genuinely-remote ones.
# Apply is Taleo → reuses strategies/taleo.py after a per-tenant verify pass; the TTEC Taleo apply cron
# is scoped to `source='ttec'`, so a source='percepta' row is never picked up by the wrong driver.
_PERCEPTA_PORTAL = "128160131726"          # careersection 10300 = the US portal
_PERCEPTA_EP = "https://percepta.taleo.net/careersection/rest/jobboard/searchjobs"


def _percepta_loc(codes) -> str:
    """Format Taleo location codes ('US-MI-Dearborn') into a readable string."""
    out = []
    for code in codes:
        parts = (code or "").split("-")
        if len(parts) >= 3 and parts[0] == "US":
            out.append(f"{parts[2]}, {parts[1]}, United States")
        elif len(parts) == 2 and parts[0] == "US":
            out.append(f"{parts[1]}, United States")
        elif code:
            out.append(code)
    return " / ".join(out)


def _percepta_row(req: dict) -> dict | None:
    import json
    col = req.get("column") or []
    if not col:
        return None
    li = req.get("linkedColumn", 0)
    title = col[li] if 0 <= li < len(col) else col[0]
    codes = []
    for i in (req.get("locationsColumns") or []):
        if 0 <= i < len(col):
            try:
                v = json.loads(col[i])
                codes += v if isinstance(v, list) else [v]
            except Exception:
                codes.append(col[i])
    is_us = any(
        (c or "").upper().startswith("US-") or (c or "").upper() == "US"
        or "UNITED STATES" in (c or "").upper() for c in codes) or _title_us(title or "")
    loc = _percepta_loc(codes) or ("United States" if is_us else "")
    if not _is_remote(title or "", loc):
        return None                                   # remote-only rule
    # US-only rule. `us_eligible("")` defaults True ("assume open") — safe ONLY because
    # fetch_percepta queries the US-only portal 10300; a global Taleo tenant would need an
    # explicit non-US reject here (don't copy this helper for one without that guard).
    if not (is_us or us_eligible(loc)):
        return None
    jid = req.get("jobId")
    if not (jid and title):
        return None
    apply_url = (f"https://percepta.taleo.net/careersection/10300/jobdetail.ftl?job={jid}&lang=en")
    row = _mk_row("percepta", jid, "Percepta", title, loc or "Remote, United States", apply_url)
    if row and is_us:
        row["us_eligible"] = True
    return row


def fetch_percepta() -> list[dict]:
    def body(pg):
        return {"multilineEnabled": False,
                "sortingSelection": {"ascendingSortingOrder": "false", "sortBySelectionParam": "3"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []}, "pageNo": pg}
    hdr = {"Content-Type": "application/json", "Accept": "application/json", "Tz": "GMT"}
    rows, seen = [], set()
    try:
        with httpx.Client(headers={"User-Agent": _BROWSER_UA}, timeout=30,
                          follow_redirects=True) as c:
            c.get("https://percepta.taleo.net/careersection/10300/jobsearch.ftl")   # session cookie
            pg = 1
            while pg <= 8:
                try:
                    r = c.post(_PERCEPTA_EP, params={"lang": "en", "portal": _PERCEPTA_PORTAL},
                               json=body(pg), headers=hdr)
                    d = r.json()
                except Exception as e:
                    print(f"[percepta pg={pg}] {type(e).__name__}: {e}", file=sys.stderr)
                    break
                reqs = d.get("requisitionList") or []
                if not reqs:
                    break
                for req in reqs:
                    jid = req.get("jobId")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    row = _percepta_row(req)
                    if row:
                        rows.append(row)
                total = (d.get("pagingData") or {}).get("totalCount") or 0
                if len(seen) >= total:
                    break
                pg += 1
    except Exception as e:
        print(f"[percepta] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# The shared Workday US-country facet id (locationCountry / Location_Country = United States of America).
_WD_US_FACET = "bc33aa3152ec42d4995f4791a106ed09"


# UnitedHealth Group / Optum — Radancy TalentBrew (like TTEC): POST /search-jobs/resultspost with a
# FacetFilters array (Remote WorkSetting + United States Country1); the response `results` key is an
# HTML fragment of job tiles (parse a[data-job-id]). Every returned row is US+remote by construction.
def _talentbrew_row(source: str, company: str, jid: str, title: str, href: str, host: str) -> dict | None:
    if not title:
        return None
    url = (host + href) if (href or "").startswith("/") else (href or "")
    return _mk_row(source, jid, company, title, "Remote, United States", url)


def fetch_unitedhealth() -> list[dict]:
    from bs4 import BeautifulSoup
    body = {"ActiveFacetID": "Remote", "CurrentPage": 1, "RecordsPerPage": 100, "SearchType": 5,
            "SearchResultsModuleName": "Search Results", "IsPagination": "True",
            "FacetFilters": [
                {"ID": "Remote", "FacetType": 5, "Count": 686, "Display": "Remote",
                 "IsApplied": True, "FieldName": "custom_fields.WorkSetting"},
                {"ID": "United States", "FacetType": 5, "Count": 5043, "Display": "United States",
                 "IsApplied": True, "FieldName": "custom_fields.Country1"}]}
    rows, seen = [], set()
    page = 1
    while page <= 9:
        body["CurrentPage"] = page
        try:
            r = httpx.post("https://careers.unitedhealthgroup.com/search-jobs/resultspost",
                           json=body, timeout=40,
                           headers={"User-Agent": _BROWSER_UA, "Content-Type": "application/json"})
            html = r.json().get("results") or ""
        except Exception as e:
            print(f"[unitedhealth p={page}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        anchors = BeautifulSoup(html, "html.parser").select("a[data-job-id]")
        new = 0
        for a in anchors:
            jid = a.get("data-job-id")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            new += 1
            h2 = a.select_one("h2")
            row = _talentbrew_row("unitedhealth", "UnitedHealth Group", jid,
                                  h2.get_text(strip=True) if h2 else "", a.get("href"),
                                  "https://careers.unitedhealthgroup.com")
            if row:
                rows.append(row)
        if not anchors or new == 0:
            break
        page += 1
    return rows


# Centene + Cigna — Workday CxS via the generic helper. Neither tenant has a remote/workplace facet
# (remote is encoded IN the location text, e.g. "Remote-AR" / "Tennessee Work at Home"), so we apply
# the US-country facet and let _workday_row keep the remote+US rows. Cigna's country facet parameter
# is `Location_Country` (Centene's is `locationCountry`).
def fetch_centene() -> list[dict]:
    return _fetch_workday("centene", "Centene", "centene.wd5.myworkdayjobs.com", "centene",
                          "Centene_External", search_texts=("",),
                          applied_facets={"locationCountry": [_WD_US_FACET]}, offset_cap=260, us_confirmed=True)


def fetch_cigna() -> list[dict]:
    return _fetch_workday("cigna", "Cigna", "cigna.wd5.myworkdayjobs.com", "cigna", "cignacareers",
                          search_texts=("",),
                          applied_facets={"Location_Country": [_WD_US_FACET]}, offset_cap=360, us_confirmed=True)


# Humana — Phenom (like Conduent). POST /widgets with selected_fields.city=["Remote"] (the exact
# server-side US-remote filter) and page by `from`. isRemote / country come per-row.
def _humana_row(j: dict) -> dict | None:
    if (j.get("country") or "") != "United States of America":
        return None
    if not (j.get("isRemote") == "Yes" or (j.get("city") or "") == "Remote"):
        return None
    loc = j.get("cityStateCountry") or "Remote, United States"
    return _mk_row("humana", j.get("jobId"), "Humana", j.get("title"), loc,
                   j.get("applyUrl"), posted_at=_iso_epoch(j.get("postedDate")))


def fetch_humana() -> list[dict]:
    rows, seen, frm = [], set(), 0
    while frm < 800:
        try:
            r = httpx.post("https://careers.humana.com/widgets", timeout=40,
                           json={"ddoKey": "refineSearch", "from": frm, "jobs": True, "counts": True,
                                 "all_fields": ["category", "country", "state", "city", "type"],
                                 "size": 100, "clearAll": False, "pageName": "search-results",
                                 "selected_fields": {"city": ["Remote"]}},
                           headers={"User-Agent": _BROWSER_UA, "Content-Type": "application/json",
                                    "Accept": "application/json",
                                    "Referer": "https://careers.humana.com/us/en/search-results"})
            jobs = ((r.json().get("refineSearch") or {}).get("data") or {}).get("jobs") or []
        except Exception as e:
            print(f"[humana from={frm}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not jobs:
            break
        for j in jobs:
            jid = j.get("jobId")
            if jid in seen:
                continue
            seen.add(jid)
            row = _humana_row(j)
            if row:
                rows.append(row)
        frm += 100
    return rows


# Foundever (formerly Sitel Group / SYKES) — SuccessFactors Recruiting Marketing (Jobs2Web / "j2w")
# career site at jobs.foundever.com. There is no structured JSON/JSON-LD feed and no server-side
# US-country facet URL, so we read the RESULTS TABLE (/search-jobs/results): each posting is a
# <tr class="data-row"> with the title link (td.colTitle a.jobTitle-link, href /job/<slug>/<id>/),
# a location cell (td.colLocation span.jobLocation, format "<workplace>, <city|Any Location>, <ISO2>"
# e.g. "Remote, Mississippi, US") and a posted-date span. Remote-ness AND US both come off that
# location string: the LAST comma-token is the country code (US/USA or the full "United States…"),
# and a leading "Remote"/"Virtual"/"Work at Home" workplace is the remote signal. The results page
# paginates via the URL query param ?q=<kw>&startrow=N (the /go/ landing pages do NOT), so we query
# a couple of remote keywords, page by startrow, and filter US+remote client-side (like Sutherland).
# One job is PINNED on every page, so end-of-results is detected when a page repeats the previous
# page's id set (not by counting "new" ids, which the pinned/overlapping rows would poison).
_FOUNDEVER_HOST = "https://jobs.foundever.com"


def _foundever_country(loc: str) -> str:
    parts = [p.strip() for p in (loc or "").split(",") if p.strip()]
    return parts[-1] if parts else ""


def _foundever_is_us(loc: str) -> bool:
    return _foundever_country(loc).upper() in ("US", "USA") or "united states" in (loc or "").lower()


def _foundever_date(s: str) -> int:
    """'Sep 2, 2026' (the results-table posted date) -> epoch, or 0 on any parse miss."""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        from datetime import datetime
        return int(datetime.strptime(s, "%b %d, %Y").timestamp())
    except Exception:
        return 0


def _foundever_row(jid, title, loc, href, date="") -> dict | None:
    """One SuccessFactors results-table row → normalized row or None. US+remote decided from the
    location string (country code + workplace); categorize() then enforces the mass-hiring rule."""
    if not jid or not title:
        return None
    if not _foundever_is_us(loc):
        return None                                   # US-only (last-token country code)
    if not _is_remote(loc):
        return None                                   # remote-only (workplace token)
    url = (_FOUNDEVER_HOST + href) if (href or "").startswith("/") else (href or "")
    row = _mk_row("foundever", jid, "Foundever", title, loc, url, posted_at=_foundever_date(date))
    if row:
        # US already confirmed from the location's country code (authoritative) — force the flag so
        # a state-coded "Remote, Mississippi, US" row isn't dropped by collect(us_only=True).
        row["us_eligible"] = True
    return row


def _foundever_parse(html: str) -> list[tuple]:
    """(jid, title, location, href, date) for every job row in a /search-jobs/results page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tr.data-row"):
        a = tr.select_one("td.colTitle a.jobTitle-link")
        if not a:
            continue
        href = a.get("href") or ""
        m = re.search(r"/job/[^/]+/(\d+)/", href)
        loc = tr.select_one("td.colLocation span.jobLocation")
        dt = tr.select_one("span.jobDate")
        out.append((m.group(1) if m else None, a.get_text(" ", strip=True),
                    loc.get_text(" ", strip=True) if loc else "",
                    href, dt.get_text(" ", strip=True) if dt else ""))
    return out


def fetch_foundever() -> list[dict]:
    rows, seen = [], set()
    headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*",
               "Referer": _FOUNDEVER_HOST + "/search-jobs/"}
    try:
        with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as c:
            for kw in ("remote", "work from home"):
                startrow, prev_ids = 0, None
                while startrow < 1000:
                    try:
                        r = c.get(_FOUNDEVER_HOST + "/search-jobs/results",
                                  params={"q": kw, "startrow": startrow})
                        parsed = _foundever_parse(r.text)
                    except Exception as e:
                        print(f"[foundever kw={kw!r} startrow={startrow}] {type(e).__name__}: {e}",
                              file=sys.stderr)
                        break
                    ids = tuple(p[0] for p in parsed)
                    if not parsed or ids == prev_ids:      # empty OR the page repeated → end of results
                        break
                    prev_ids = ids
                    for jid, title, loc, href, date in parsed:
                        if not jid or jid in seen:
                            continue
                        seen.add(jid)
                        row = _foundever_row(jid, title, loc, href, date)
                        if row:
                            rows.append(row)
                    startrow += 10
    except Exception as e:
        print(f"[foundever] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# =====================================================================================
# STAFFING AGENCIES — the fast-placement lane. Big US staffing firms place candidates
# quickly with light screening (a recruiter phone-screen, not a multi-day assessment),
# so their remote entry roles are a high-yield offer source. Each is its own careers
# backend (NOT a shared ATS): endpoints reverse-engineered from the careers SPA's network
# calls, then verified with httpx (recon 2026-09-20). US-only inventory (each is a US
# domain), so US-eligibility is forced True on kept rows; categorize()/_is_remote enforce
# the two HARD RULES. Salary figures are stored raw (to_hourly normalizes by magnitude at
# display time, so an hourly 19.90 and an annual 45000 both render correctly).
# =====================================================================================

# --- Randstad USA (randstadusa.com) — its own React "search-app" over a first-party JSON
#     API. POST /api/search/search-results with searchParams.isRemote=true is a SERVER-SIDE
#     remote filter (the whole domain is US-only inventory). Every hit carries a lob (line of
#     business): lobId 1027 / "Randstad Careers" is Randstad hiring its OWN staff and routes
#     to a DIFFERENT apply host (randstadnorthamerica.workgr8.com) — NOT a staffing PLACEMENT,
#     so it's dropped; the placement lobs (308 Office & Admin, 4 Digital, 337 Allied Health)
#     apply natively at randstadusa.com/jobs/apply/<lob>/<atsReference>/. ---
_RANDSTAD_URL = "https://www.randstadusa.com/api/search/search-results"
# slug-form keywords (the API's `query` is a lowercase-hyphenated slug, not free text)
_RANDSTAD_QUERIES = ("customer-service-representative", "call-center", "data-entry",
                     "administrative-assistant", "sales", "collections", "claims",
                     "scheduling-coordinator", "customer-support")


def _randstad_salary_raw(sal: dict):
    typ = (sal.get("type") or "").lower()
    unit = "hr" if "hour" in typ else ("yr" if "year" in typ else "")
    lo = sal.get("min") if sal.get("min") is not None else sal.get("fixed")
    hi = sal.get("max") if sal.get("max") is not None else sal.get("fixed")
    if not lo and not hi:
        return None, None, None
    lo, hi = (lo if lo is not None else hi), (hi if hi is not None else lo)
    if lo == hi:
        raw = f"${lo:g}/{unit}" if unit else f"${lo:g}"
    else:
        raw = f"${lo:g}–${hi:g}/{unit}" if unit else f"${lo:g}–${hi:g}"
    return lo, hi, raw


def _randstad_row(h: dict) -> dict | None:
    """One randstad search hit → normalized row or None. Remote+US is server-guaranteed
    (isRemote filter, US domain); drop the internal-hire lob (workgr8) and non-remote rows."""
    if not isinstance(h, dict) or h.get("isDeleted"):
        return None
    if not h.get("isRemote"):
        return None                                      # remote-only
    if h.get("lobId") == 1027 or "randstad careers" in (h.get("lobName") or "").lower():
        return None                                      # internal Randstad hire, not a placement
    apply_url = h.get("applyUrl") or h.get("detailsUrl") or ""
    if "workgr8" in apply_url.lower():
        return None                                      # belt-and-suspenders: any workgr8 apply = internal
    jl = h.get("jobLocation") or {}
    city = (jl.get("city") or "").strip()
    st = (jl.get("stateAbbreviation") or "").strip()
    if city and city.lower() != "united states":
        loc = f"Remote, {city}" + (f", {st}" if st else "") + ", United States"
    else:
        loc = "Remote, United States"                    # nationwide-remote rows carry city 'United States'
    lo, hi, sraw = _randstad_salary_raw(h.get("salary") or {})
    jid = h.get("atsReference") or h.get("id")
    row = _mk_row("randstad", jid, "Randstad", h.get("title") or "", loc, apply_url,
                  salary_min=lo, salary_max=hi, salary_raw=sraw,
                  employment_type=h.get("employmentType"),
                  posted_at=int((h.get("createdDate") or 0)) // 1000)  # createdDate is epoch MILLIS
    if row:
        row["us_eligible"] = True                        # US-only domain (authoritative)
    return row


def fetch_randstad() -> list[dict]:
    rows, seen = [], set()
    hdr = {"User-Agent": _BROWSER_UA, "Content-Type": "application/json",
           "Accept": "application/json"}
    for q in _RANDSTAD_QUERIES:
        page, total = 1, None
        while page <= 15:
            body = {"data": {"currentRoute": {"path": "/jobs/:searchParams*", "params": {}},
                             "currentLanguage": "en",
                             "searchParams": {"query": q, "isRemote": True, "page": page}}}
            try:
                r = httpx.post(_RANDSTAD_URL, json=body, headers=hdr, timeout=30)
                d = r.json()
                # searchResults sits at the JSON top level (the request `data` wrapper is NOT
                # echoed back); tolerate a nested `data.searchResults` shape too.
                sr = d.get("searchResults") or (d.get("data") or {}).get("searchResults") or {}
                hits = sr.get("hits") or []
                total = int(sr.get("totalSize") or 0)
            except Exception as e:
                print(f"[randstad q={q!r} p={page}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not hits:
                break
            for h in hits:
                jid = h.get("atsReference") or h.get("id")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                row = _randstad_row(h)
                if row:
                    rows.append(row)
            if len(hits) < 10 or (total and page * 10 >= total):
                break
            page += 1
    return rows


# --- ManpowerGroup: Manpower (manpower.com) + Experis (experis.com) share ONE first-party
#     .NET API `POST /api/services/Jobs/searchjobs`; the brand is resolved server-side from
#     the Host header (US site = US-only inventory). There is NO reliable server-side remote
#     field (the remoteJobs facet is dead), and a blank jobLocation is NOT a remote signal (a
#     blank-location "Service Desk Analyst" was actually HYBRID), so remote is decided
#     TITLE/description-first with a hard `hybrid` veto (same location-first philosophy as
#     applier/regions.py). No structured salary — pay is free text in publicDescription, read
#     via _parse_hourly_wage. Apply is a non-URL-addressable client SPA (guest JobApplyNoAuth
#     exists) so this is COLLECT-ONLY for now (see _AUTO_STATUS / the report). ---
_MG_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
# description-only remote signal, kept tight so an incidental "remote" mention doesn't leak in
_MG_DESC_REMOTE_RE = re.compile(
    r"(fully|100%|completely)\s+remote|work\s*from\s*home|remote\s+(position|role|opportunity|"
    r"work)|location:\s*remote|telecommut|home[- ]?based", re.I)
# "remote"/"work from home" lead the keyword list — a role keyword alone ranks the (few) remote
# postings below pages of on-site ones, so the remote-titled roles were being missed.
_MANPOWER_KEYWORDS = ("remote", "work from home", "customer service", "call center", "data entry",
                      "administrative", "collections", "claims", "customer support", "sales")
_EXPERIS_KEYWORDS = ("remote", "work from home", "help desk", "service desk", "technical support",
                     "desktop support", "support specialist", "customer service")


def _manpowergroup_row(j: dict, source: str, company: str, host: str) -> dict | None:
    title = j.get("jobTitle") or ""
    desc = re.sub(r"<[^>]+>", " ", j.get("publicDescription") or "")
    if _MG_HYBRID_RE.search(f"{title} {desc}"):
        return None                                      # hybrid ≠ remote
    if not (_is_remote(title) or _MG_DESC_REMOTE_RE.search(desc)):
        return None                                      # remote-only (title, else explicit desc signal)
    jurl = j.get("jobURL") or ""
    apply_url = (f"https://{host}" + jurl) if jurl.startswith("/") else jurl
    branch = (j.get("jobLocation") or "").strip()
    loc = f"Remote, {branch}, United States" if branch else "Remote, United States"
    lo, hi, sraw = _parse_hourly_wage(desc)              # pay lives in the description prose
    row = _mk_row(source, j.get("jobID"), company, title, loc, apply_url,
                  salary_min=lo, salary_max=hi, salary_raw=sraw,
                  employment_type=j.get("employmentType"),
                  posted_at=_iso_epoch(j.get("publishfromDate")))
    if row:
        row["us_eligible"] = True                        # US-only domain (implicit)
    return row


def _fetch_manpowergroup(host: str, source: str, company: str, keywords) -> list[dict]:
    rows, seen = [], set()
    url = f"https://{host}/api/services/Jobs/searchjobs"
    hdr = {"User-Agent": _BROWSER_UA, "Content-Type": "application/json",
           "Accept": "application/json", "Referer": f"https://{host}/en/search"}
    for kw in keywords:
        offset, total = 0, None
        while offset < 400:
            body = {"filter": {"searchkeyword": kw, "offset": offset, "totalCount": 0,
                               "limit": 50, "haslocation": False, "language": "en"}}
            try:
                r = httpx.post(url, json=body, headers=hdr, timeout=30)
                d = r.json()
                items = d.get("jobsItems") or []
                total = int((d.get("filters") or {}).get("totalCount") or 0)
            except Exception as e:
                print(f"[{source} kw={kw!r} off={offset}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not items:
                break
            for j in items:
                jid = j.get("jobID")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                row = _manpowergroup_row(j, source, company, host)
                if row:
                    rows.append(row)
            offset += len(items)
            if total is not None and offset >= total:
                break
    return rows


def fetch_manpower() -> list[dict]:
    return _fetch_manpowergroup("www.manpower.com", "manpower", "Manpower", _MANPOWER_KEYWORDS)


def fetch_experis() -> list[dict]:
    return _fetch_manpowergroup("www.experis.com", "experis", "Experis", _EXPERIS_KEYWORDS)


# --- Adecco (adecco.com/en-us) — its own Sitecore/Azure-Search backend whose list API
#     (`jobs/summarized`) is broken (returns 0), so we discover jobs from the SITEMAP and read
#     each posting's public detail API. The US sitemap has ~2600 URLs; a slug pre-filter bounds
#     the per-job detail fetches to the mass-hiring-ish titles before categorize()/isRemote run.
#     Apply is a mandatory account on a custom candidate SPA (candidate.adecco.com) → COLLECT-ONLY. ---
_ADECCO_HOST = "https://www.adecco.com"
_ADECCO_SLUG_HINT = re.compile(
    r"customer-service|customer-care|call-center|contact-center|\bcsr\b|data-entry|"
    r"administrative|admin-assistant|\bclerk\b|receptionist|virtual-assistant|scheduler|"
    r"scheduling|sales|collections|claims|patient|member|enrollment|intake|help-?desk|"
    r"support|coordinator|representative|-rep-|\brep\b|specialist|associate|advocate", re.I)


def _adecco_us_sitemap_url() -> str | None:
    r = httpx.get(_ADECCO_HOST + "/jobsindex.xml", headers={"User-Agent": _BROWSER_UA}, timeout=30)
    m = re.search(r"<loc>\s*([^<\s]*sitemap-jobs-unitedstates-en\.xml)\s*</loc>", r.text, re.I)
    return m.group(1) if m else None


def _adecco_detail(job_tail: str) -> dict | None:
    url = (f"{_ADECCO_HOST}/api/data/jobs/job-description-details/"
           f"{job_tail}/adecco/US/en-US/job-details")
    r = httpx.get(url, headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"}, timeout=25)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _adecco_row(d: dict, url: str) -> dict | None:
    """One adecco job-description-details payload → normalized row or None. Remote + US + OPEN
    are read off the structured detail fields; categorize() enforces the mass-hiring rule."""
    if not isinstance(d, dict) or not d.get("isRemote"):
        return None                                      # remote-only
    if (d.get("countryId") or "").upper() not in ("USA", "US"):
        return None
    if (d.get("jobStatusId") or "OPEN").upper() not in ("OPEN", ""):
        return None
    city = (d.get("cityName") or "").strip()
    st = (d.get("stateName") or "").strip()
    loc = f"Remote, {city}, {st}, United States" if city else "Remote, United States"
    scale = (d.get("salaryTimeScale") or "").lower()
    unit = "hr" if scale.startswith("hour") else ("yr" if scale.startswith(("year", "annu")) else scale)
    smin, smax = d.get("minsalary"), d.get("maxSalary")
    cur = d.get("salaryCurrencySymbol") or "$"
    sraw = None
    if smin:
        sraw = (f"{cur}{smin:g}" + (f"–{cur}{smax:g}" if smax and smax != smin else "")
                + (f"/{unit}" if unit else ""))
    jid = d.get("jobId") or _slug(url)
    row = _mk_row("adecco", jid, "Adecco", d.get("jobName") or "", loc, url,
                  salary_min=smin, salary_max=smax, salary_raw=sraw,
                  employment_type=d.get("contractTypeTitle"),
                  posted_at=_iso_epoch(d.get("postedDate")))
    if row:
        row["us_eligible"] = True                        # US-only sitemap (authoritative)
    return row


def fetch_adecco(max_detail: int = 500) -> list[dict]:
    rows = []
    try:
        sm = _adecco_us_sitemap_url()
        if not sm:
            print("[adecco] US jobs sitemap not found in index", file=sys.stderr)
            return rows
        r = httpx.get(sm, headers={"User-Agent": _BROWSER_UA}, timeout=40)
        urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
    except Exception as e:
        print(f"[adecco sitemap] {type(e).__name__}: {e}", file=sys.stderr)
        return rows
    cand = [u for u in urls if _ADECCO_SLUG_HINT.search(u)][:max_detail]

    def _one(u: str):
        tail = u.rstrip("/").split("/")[-1]
        try:
            d = _adecco_detail(tail)
        except Exception as e:
            print(f"[adecco detail {tail}] {type(e).__name__}: {e}", file=sys.stderr)
            return None
        return _adecco_row(d, u) if d else None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for row in ex.map(_one, cand):
            if row:
                rows.append(row)
    return rows


# --- Robert Half (roberthalf.com/us/en/jobs) — its own AEM-rendered search page (backed by
#     Salesforce). No usable JSON API (the raw /search 403s without server creds), but the SSR
#     page embeds every result as a <rhcl-job-card> custom element and `remote=Remote` is a
#     SERVER-SIDE filter. Apply is a mandatory Salesforce candidate account + reCAPTCHA
#     Enterprise → COLLECT-ONLY. ---
_RH_HOST = "https://www.roberthalf.com"


def _rh_row(card) -> dict | None:
    """One bs4 <rhcl-job-card> element → normalized row or None (remote worksite only)."""
    jid = card.get("job-id")
    if not jid:
        return None
    a = card.select_one('a[slot="headline"]')
    title = a.get_text(strip=True) if a else ""
    href = a.get("href") if a else ""

    def li(name: str) -> str:
        e = card.select_one(f'li[data-subslot="{name}"]')
        return e.get_text(" ", strip=True) if e else ""

    def sp(name: str) -> str:
        e = card.select_one(f'span[data-subslot="{name}"]')
        return e.get_text(strip=True) if e else ""

    if "remote" not in li("worksite").lower():
        return None                                      # remote-only (worksite subslot)
    loc = li("location") or "United States"
    period = sp("salary-period").lower()
    unit = "hr" if period.startswith("hour") else ("yr" if period.startswith("year") else "")

    def _num(s: str):
        try:
            return float((s or "").replace(",", ""))
        except (TypeError, ValueError):
            return None
    lo, hi = _num(sp("salary-min")), _num(sp("salary-max"))
    sraw = None
    if lo:
        sraw = (f"${lo:g}" + (f"–${hi:g}" if hi and hi != lo else "") + (f"/{unit}" if unit else ""))
    row = _mk_row("roberthalf", jid, "Robert Half", title,
                  f"Remote, {loc}, United States", href,
                  salary_min=lo, salary_max=hi, salary_raw=sraw,
                  employment_type=li("type"), posted_at=_iso_epoch(li("date")))
    if row:
        row["us_eligible"] = True
    return row


def fetch_roberthalf() -> list[dict]:
    from bs4 import BeautifulSoup
    rows, seen = [], set()
    page = 1
    while page <= 25:
        try:
            r = httpx.get(f"{_RH_HOST}/us/en/jobs", headers={"User-Agent": _BROWSER_UA}, timeout=30,
                          params={"remote": "Remote", "pagenumber": page})
            if r.status_code >= 400:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("rhcl-job-card")
        except Exception as e:
            print(f"[roberthalf p={page}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not cards:
            break
        new = 0
        for c in cards:
            jid = c.get("job-id")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            new += 1
            row = _rh_row(c)
            if row:
                rows.append(row)
        if new == 0:
            break
        page += 1
    return rows


_SOURCES = {"remotive": fetch_remotive, "himalayas": fetch_himalayas,
            "remoteok": fetch_remoteok, "amazon": fetch_amazon_remote,
            "conduent": fetch_conduent, "alorica": fetch_alorica, "concentrix": fetch_concentrix,
            "teleperformance": fetch_teleperformance, "ttec": fetch_ttec, "cvshealth": fetch_cvs,
            "sutherland": fetch_sutherland, "workingsolutions": fetch_working_solutions,
            "kelly": fetch_kelly, "maximus": fetch_maximus, "unitedhealth": fetch_unitedhealth,
            "centene": fetch_centene, "cigna": fetch_cigna, "humana": fetch_humana,
            "foundever": fetch_foundever,
            # BPOs on already-supported ATSes (apply reuses the tenant's strategy after a verify pass)
            "transcom": fetch_transcom, "percepta": fetch_percepta,
            # staffing agencies (fast-placement lane)
            "randstad": fetch_randstad, "manpower": fetch_manpower, "experis": fetch_experis,
            "adecco": fetch_adecco, "roberthalf": fetch_roberthalf}


def collect(sources: list[str] | None = None, us_only: bool = True) -> dict:
    """Pull every source, upsert, deactivate disappeared jobs. Returns per-source counts."""
    ensure_schema()
    out = {}
    for name in (sources or list(_SOURCES)):
        run_start = int(time.time())
        try:
            rows = _SOURCES[name]()
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if us_only:
            rows = [r for r in rows if r["us_eligible"]]
        # de-dup within this batch on (source, source_id) — keep the last
        seen = {(r["source"], r["source_id"]): r for r in rows}
        rows = list(seen.values())
        n = upsert_jobs(rows)
        stale = deactivate_stale(name, run_start)
        out[name] = {"collected": n, "deactivated": stale}
    # Refresh the large-employer reference panel (E-Verify 10k+ signal) AFTER all job sources.
    # Fully guarded + stale-gated (weekly): a failure here can NEVER affect job collection, and
    # this writes only a SEPARATE cache file (backend/data/everify_employers.json), never the
    # mass_hiring_jobs table. See backend/tools/everify_employers.py.
    try:
        from backend.tools import everify_employers
        out["_everify_reference"] = everify_employers.maybe_refresh_cache()
    except Exception:
        pass
    return out


# ---- reads (for the tab) -------------------------------------------------------
def _hide_spanish() -> bool:
    """Whether to hide Spanish-required jobs from the board (reversible operator setting)."""
    try:
        from backend.tools import mh_settings
        return mh_settings.hide_spanish()
    except Exception:
        return False


def companies(category: str | None = None, limit: int = 100,
              comp: str | None = None) -> list[dict]:
    """Companies ranked by mass_hiring_score: active remote mass-hiring reqs + posting velocity."""
    week = int(time.time()) - 7 * 86400
    where = ["active=TRUE"]
    args: list = []
    if category:
        where.append("category=%s")
        args.append(category)
    if comp:
        where.append("comp_type=%s")
        args.append(comp)
    if _hide_spanish():
        where.append("title NOT ILIKE %s")
        args.append("%spanish%")
    w = " AND ".join(where)
    with _cur() as cur:
        cur.execute(f"""
        SELECT company, company_key,
               count(*) AS active_jobs,
               count(*) FILTER (WHERE category='customer_support') AS cs_jobs,
               count(*) FILTER (WHERE posted_at >= %s) AS posted_7d,
               count(DISTINCT category) AS categories,
               min(salary_min) FILTER (WHERE salary_min>0) AS sal_min,
               max(salary_max) FILTER (WHERE salary_max>0) AS sal_max
        FROM mass_hiring_jobs WHERE {w}
        GROUP BY company, company_key
        ORDER BY active_jobs DESC, posted_7d DESC
        LIMIT %s""", [week] + args + [limit])
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["mass_hiring_score"] = min(100, round(d["active_jobs"] * 3 + d["posted_7d"] * 5))
            out.append(d)
        out.sort(key=lambda x: x["mass_hiring_score"], reverse=True)
        return out


def jobs(company_key: str | None = None, category: str | None = None, limit: int = 100,
         comp: str | None = None) -> list[dict]:
    where, args = ["active=TRUE"], []
    if company_key:
        where.append("company_key=%s"); args.append(company_key)
    if category:
        where.append("category=%s"); args.append(category)
    if comp:
        where.append("comp_type=%s"); args.append(comp)
    if _hide_spanish():
        where.append("title NOT ILIKE %s"); args.append("%spanish%")
    with _cur() as cur:
        cur.execute(f"SELECT * FROM mass_hiring_jobs WHERE {' AND '.join(where)} "
                    f"ORDER BY posted_at DESC NULLS LAST LIMIT %s", args + [limit])
        return [dict(r) for r in cur.fetchall()]


def job_by_id(row_id: int) -> dict | None:
    """A single mass_hiring_jobs row by primary key (for the auto-fill lane)."""
    with _cur() as cur:
        cur.execute("SELECT * FROM mass_hiring_jobs WHERE id=%s", (row_id,))
        r = cur.fetchone()
        return dict(r) if r else None


def stats() -> dict:
    with _cur() as cur:
        cur.execute("SELECT count(*) n, count(*) FILTER (WHERE active) act, "
                    "count(DISTINCT company_key) FILTER (WHERE active) cos FROM mass_hiring_jobs")
        r = cur.fetchone()
        cur.execute("SELECT source, count(*) FILTER (WHERE active) n FROM mass_hiring_jobs "
                    "GROUP BY source ORDER BY n DESC")
        by_src = {row["source"]: row["n"] for row in cur.fetchall()}
        cur.execute("SELECT category, count(*) FILTER (WHERE active) n FROM mass_hiring_jobs "
                    "GROUP BY category ORDER BY n DESC")
        by_cat = {row["category"]: row["n"] for row in cur.fetchall()}
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT max(last_seen) FROM mass_hiring_jobs")
        last = cur.fetchone()[0] or 0
    return {"total": r["n"], "active": r["act"], "companies": r["cos"],
            "by_source": by_src, "by_category": by_cat, "last_collected": int(last)}


CATEGORY_LABELS = {
    "customer_support": "Customer Support", "sales": "Sales", "data_entry": "Data Entry",
    "virtual_assistant": "Virtual Assistant", "operations": "Operations", "recruiting": "Recruiting",
}


if __name__ == "__main__":
    if "--collect" in sys.argv:
        t = time.time()
        res = collect()
        print("collect:", res)
        print(f"stats: {stats()}  ({time.time()-t:.1f}s)")
    elif "--backfill-comptype" in sys.argv:
        print(f"comp_type set on {backfill_comp_type()} rows")
    elif "--backfill-autostatus" in sys.argv:
        print(f"auto_status set on {backfill_auto_status()} rows")
    elif "--backfill-ttec-pay" in sys.argv:
        got, scanned = backfill_ttec_pay()
        print(f"ttec posted hourly wage set on {got}/{scanned} scanned rows")
    elif "--stats" in sys.argv:
        import json
        print(json.dumps(stats(), indent=2))
        print("\nTop companies by mass_hiring_score:")
        for c in companies(limit=20):
            print(f"  {c['mass_hiring_score']:3}  {c['company']:28} "
                  f"active={c['active_jobs']:3} cs={c['cs_jobs']:2} 7d={c['posted_7d']:2}")
    else:
        print(__doc__)
