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
        # "Sales & Service" / "Sales and Service" = the retail-CSR entry title Wayfair-style direct-hire
        # employers use (entry-noun-suffixed so a senior/lead brush is still _NOT_MASS-vetoed).
        r"sales (and|&) service (consultant|rep\b|representative|associate|specialist|agent|advisor)|"
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
    # Hilton = Oracle ORC (same ATS as Alorica) but a DIFFERENT tenant → collect-first: a per-tenant
    # screener verify pass + broadening orc_recon.orc_job_ids past source='alorica' are needed before
    # it's driven, so keep 'needs_laptop' (no misleading «Авто» badge; no lane auto-picks it).
    "hilton": "needs_laptop",
    "kelly": "needs_laptop", "concentrix": "needs_laptop", "cvshealth": "needs_laptop",
    "centene": "needs_laptop", "cigna": "needs_laptop", "ttec": "needs_laptop",
    # Healthcare payers/BPOs on the driven Workday CxS lane (register-captcha probe pending).
    "elevance": "needs_laptop", "highmark": "needs_laptop", "sagility": "needs_laptop",
    # unitedhealth/Optum: careers.unitedhealthgroup.com (Radancy) hands off to Oracle Taleo
    # (uhg.taleo.net careersection 10020) for the apply — NO captcha/WAF, BUT the account-create step
    # (createprofile/register/accessmanagement.ftl) 302s via referrals.unitedhealthgroup.com →
    # login.radancy.net → login.microsoftonline.com (Azure AD SAML). There is NO candidate
    # self-registration, so 0 guest applications are possible (re-verified live 2026-09-22, same as the
    # 2026-09-01 finding + 3 empty-Maildir drives). A hard IDENTITY wall, not solvable with a captcha
    # key or a US IP → genuinely BLOCKED (not 'needs_laptop', which implied a buildable lane).
    "unitedhealth": "blocked", "teleperformance": "needs_laptop", "sutherland": "needs_laptop",
    # Wayfair: DIRECT-HIRE (non-BPO) on the SAME SmartRecruiters lane as Sutherland — the SR apply cron
    # (keyed on the smartrecruiters.com apply_url) drives it full-auto to an ack regardless of this badge;
    # mirrors Sutherland's status. The pre-offer human gate (phone/video + sales assessment) sits AFTER
    # the clean submit, so the ceiling is ack + assessment invite, not an offer.
    "wayfair": "needs_laptop",
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
    # molina: Oracle Recruiting Cloud (hckd.fa.us2.oraclecloud.com), the SAME lane as alorica — but the
    # ORC cron `orc_recon.orc_job_ids()` selects `source='alorica'` only, so Molina is collect-first until
    # that filter is widened + a per-tenant screener verify pass (its ORC form differs from Alorica's).
    # kaiser: Radancy front (kaiserpermanentejobs.org) → Taleo apply (kp.taleo.net); reuses
    # strategies/taleo.py after a per-tenant Basics-mapping verify (TTEC cron is scoped source='ttec').
    # Both kept 'needs_laptop' (NOT 'auto') so the board shows no misleading «Авто» badge — no live lane
    # drives them yet.
    "molina": "needs_laptop", "kaiser": "needs_laptop",
    # gainwell: the SAME SuccessFactors RMK careersection family as foundever (careersection host
    # career41.sapsf.com, company gainwellte) — captcha-free, reuses strategies/foundever.py via
    # tools/gainwell_recon.py. Medicaid/Medicare BPO CSR/member-services.
    "gainwell": "auto",
    # conduent: Phenom career-site wrapper → Oracle HCM guest apply (careers.conduent.com). FULL-AUTO
    # from the datacenter IP (DIRECT, no residential) — no login wall, no interactive captcha (Phenom's
    # apply-studio reCAPTCHA v2 is OFF for Conduent, the Oracle-HCM backend runs an INVISIBLE reCAPTCHA
    # v3 the NopeCHA-armed headful browser passes). Driven by strategies/phenom.py::PhenomStrategy +
    # tools/phenom_recon.py (gated PHENOM_ADVANCE=1). LIVE-PROVEN end-to-end 2026-09-02 (a real "Thank
    # You for Applying at Conduent" ack landed in the persona Maildir); fresh dry-run 2026-09-22 still
    # fills the whole identity form + Terms consent + reaches Submit. The post-application "Required
    # Assessment" is a human skills test (a POST-submit step, like TP/Maximus), not a submit wall.
    "conduent": "auto",
    # humana: careers.humana.com is Phenom for DISCOVERY only; every apply_url points at Workday
    # (humana.wd5.myworkdayjobs.com, Humana_External + CenterWell tenants). Routed via
    # strategies/phenom.py::PhenomWorkdayStrategy → the modern shared Workday CxS create-account lane
    # (mass_hiring_apply_workday_cron --tenant humana). Sits in workday_recon._BLOCKED pending a live
    # register-captcha re-verify (the workday_probe_promote cron auto-promotes it to _LIVE_TENANTS on a
    # confirmed on-page submit, like Concentrix). Seasonal (AEP Oct-Dec). Keep 'needs_laptop' (no
    # misleading «Авто» badge; the probe cron drives it, no manual _LIVE add).
    "humana": "needs_laptop",
    "workingsolutions": "blocked", "amazon": "blocked",
    # Staffing agencies (recon 2026-09-20). Randstad: guest apply + résumé + a Friendly-Captcha
    # proof-of-work (self-solving, no image challenge) — the most auto-promising, but not yet
    # proven end-to-end, so 'needs_laptop' until a live ack. Adecco: mandatory account on a custom
    # candidate SPA. Robert Half: mandatory Salesforce account + reCAPTCHA Enterprise on submit.
    # Manpower/Experis: FULL-AUTO server-side (reverse-engineered 2026-09-21) — the guest apply is a
    # plain multipart POST to `Applicant/JobApplyWithEmail` needing NO auth/CSRF/B2C/captcha/résumé,
    # replicated with httpx by strategies/manpower.py + tools/manpower_recon.py (gated MANPOWER_ADVANCE).
    "randstad": "needs_laptop", "manpower": "auto", "experis": "auto",
    "adecco": "needs_laptop", "roberthalf": "needs_laptop",
    # TLS-fingerprint / JS-interstitial-walled boards cracked 2026-09-21 (curl_cffi Chrome impersonation
    # for iCIMS/Cloudflare; plain httpx for the two mis-recon'd ones). All COLLECT-FIRST: geico = Workday
    # (per-tenant create-account verify), cotiviti = iCIMS (TP lane is tenant-scoped), progressive =
    # Talemetry (no strategy), afni = ADP (no strategy) → no lane auto-drives them, so 'needs_laptop'.
    "geico": "needs_laptop", "afni": "needs_laptop",
    "cotiviti": "needs_laptop", "progressive": "needs_laptop",
    # 2026-09-22 coverage expansion. everise/devoted = Workday BPO/payer (create-account lane after a
    # per-tenant verify); oscar/clover = custom Greenhouse (no wired apply); all collect-first. CANADA:
    # concentrix_ca = Talkpush/Workday CA channels (collect-first); sutherland_ca = the shared SR lane
    # (the SR cron keys on the smartrecruiters.com apply_url host, so it CAN drive these — cosmetic
    # 'needs_laptop', like Wayfair).
    "everise": "needs_laptop", "oscar": "needs_laptop", "clover": "needs_laptop",
    "devoted": "needs_laptop", "concentrix_ca": "needs_laptop", "sutherland_ca": "needs_laptop",
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


# --- Hilton — Oracle Recruiting Cloud (ORC), the SAME REST shape/host family as Alorica -----------
# Recon 2026-09-21: careers.marriott.com is NOT ORC (it fronts Jibe, `marriott.jibeapply.com`), but
# jobs.hilton.com IS Oracle Recruiting Cloud on `efet.fa.us2.oraclecloud.com` (site CX_1, ~4600 global
# reqs across every brand/hotel). Hilton exposes a STRUCTURED `WorkplaceTypeCode`
# (ORA_ON_SITE|ORA_HYBRID|ORA_REMOTE), so remote is read off that (not just title text). HONEST YIELD:
# at a quiet time the whole board has only ~11 US-remote reqs and ALL are corporate/senior (Director /
# Sr Manager / DevOps / Recruiter) which `categorize()` correctly drops → ~0 entry rows TODAY. The
# collector is future-proof: Hilton Reservations & Customer Care (HRCC) work-from-home hiring is
# SEASONAL and ramps for peak, and any Customer-Care-Coordinator-class remote role that passes
# `categorize()` is captured automatically. Apply reuses the Oracle ORC lane (same `OracleORCStrategy`
# host match) but is COLLECT-ONLY for now — a different ORC tenant than Alorica, so it needs a
# per-tenant screener verify pass AND `orc_recon.orc_job_ids` is scoped to source='alorica' (so a
# 'hilton' row is never auto-driven by the wrong screener battery). Hence auto_status='needs_laptop'.
_HILTON_ORC_HOST = "efet.fa.us2.oraclecloud.com"
_HILTON_ORC_SITE = "CX_1"


def _hilton_row(j: dict) -> dict | None:
    """PURE decision for one Hilton Oracle ORC requisition (network-free, unit-tested). Keep only
    US + remote entry mass-hiring roles: US via PrimaryLocationCountry, remote via the structured
    WorkplaceTypeCode (ORA_REMOTE; ORA_ON_SITE/ORA_HYBRID veto) with a title/location fallback when
    the code is absent, and the two HARD RULES (`categorize()` entry bucket + REMOTE) via `_mk_row`."""
    if (j.get("PrimaryLocationCountry") or "").upper() != "US":
        return None
    code = (j.get("WorkplaceTypeCode") or "").upper()
    title = j.get("Title") or ""
    loc = j.get("PrimaryLocation") or ""
    if "ON_SITE" in code or "HYBRID" in code:
        return None                                   # authoritative not-remote (structured signal wins)
    is_remote = ("REMOTE" in code) or _is_remote(title, loc)
    if not is_remote:
        return None
    jid = j.get("Id")
    return _mk_row(
        "hilton", jid, "Hilton", title, loc or "United States",
        f"https://{_HILTON_ORC_HOST}/hcmUI/CandidateExperience/en/sites/{_HILTON_ORC_SITE}/job/{jid}",
        posted_at=_iso_epoch(j.get("PostedDate")))


def fetch_hilton() -> list[dict]:
    """Hilton — Oracle Recruiting Cloud (ORC). Keyword-scoped to the CSR/reservations lexicon (so we
    don't page all ~4600 global reqs) → `_hilton_row`. Work-at-home reservations / customer care."""
    host, site = _HILTON_ORC_HOST, _HILTON_ORC_SITE
    rows: list[dict] = []
    seen: set = set()
    for kw in ("reservations", "customer", "care", "member", "guest", "remote", "sales", "support"):
        offset, limit = 0, 200
        total = None
        while offset < 600:
            url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   "?onlyData=true&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
                   f"&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
                   f"sortBy=POSTING_DATES_DESC,keyword=%22{kw}%22")
            try:
                r = httpx.get(url, timeout=30, headers={**_UA, "Accept": "application/json"})
                it = (r.json().get("items") or [{}])[0]
                reqs = it.get("requisitionList") or []
                total = it.get("TotalJobsCount", total)
            except Exception as e:
                print(f"[hilton kw={kw} offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not reqs:
                break
            for j in reqs:
                jid = j.get("Id")
                if jid in seen:
                    continue
                seen.add(jid)
                row = _hilton_row(j)
                if row:
                    rows.append(row)
            offset += limit
            if total is not None and offset >= total:
                break
    return rows


# --- Oracle Recruiting Cloud (ORC), generic — Molina Healthcare rides the SAME public REST board as
#     Alorica (fetch_alorica above, kept inline for the original lane). `recruitingCEJobRequisitions`
#     returns one requisition list; US comes off `PrimaryLocationCountry`, remote off the title/location
#     (or a bare-country national posting). Apply reuses the Alorica ORC strategy (tools/orc_recon.py +
#     strategies/oracle_orc.py) after a per-tenant verify pass — `orc_recon.orc_job_ids()` currently
#     selects `source='alorica'` only, so a new ORC source is COLLECT-FIRST until that filter is widened
#     (mirrors how percepta/transcom collect-first without being auto-driven). ---
def _orc_row(j: dict, source: str, company: str, host: str) -> dict | None:
    """One Oracle Recruiting Cloud requisition -> normalized row or None. US from
    `PrimaryLocationCountry`, remote from the title/location (or a bare-country national posting);
    categorize() then enforces the mass-hiring entry rule. Network-free (unit-testable)."""
    loc = j.get("PrimaryLocation") or ""
    if (j.get("PrimaryLocationCountry") or "").upper() != "US":
        return None
    if not (_is_remote(j.get("Title"), loc) or loc.strip().lower() in ("united states", "us")):
        return None
    return _mk_row(source, j.get("Id"), company, j.get("Title"), loc or "United States",
                   f"https://{host}/hcmUI/CandidateExperience/en/sites/CX_1/job/{j.get('Id')}",
                   posted_at=_iso_epoch(j.get("PostedDate")))


def _fetch_orc(source: str, company: str, host: str, *, offset_cap: int = 600) -> list[dict]:
    rows, offset, limit, total = [], 0, 50, None
    while offset < offset_cap:
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               "?onlyData=true&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
               f"&finder=findReqs;siteNumber=CX_1,limit={limit},offset={offset},sortBy=POSTING_DATES_DESC")
        try:
            r = httpx.get(url, timeout=30, headers={**_UA, "Accept": "application/json"})
            it = (r.json().get("items") or [{}])[0]
            reqs = it.get("requisitionList") or []
            total = it.get("TotalJobsCount", total)
        except Exception as e:
            print(f"[{source} offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not reqs:
            break
        for j in reqs:
            row = _orc_row(j, source, company, host)
            if row:
                rows.append(row)
        offset += limit
        if total is not None and offset >= total:
            break
    return rows


def fetch_molina() -> list[dict]:
    """Molina Healthcare — Oracle Recruiting Cloud (hckd.fa.us2.oraclecloud.com, siteNumber CX_1;
    LIVE-VERIFIED 2026-09-21: board 341, remote+US 133, remote-US ENTRY CSR ~3 — Pharmacy CSR /
    Pharmacy Customer Service Rep / Provider-Engagement Specialist; the rest is Director/RN/clinical,
    correctly dropped by categorize). NOTE: `careers.molinahealthcare.com` fronts THIS ORC board — the
    `molinahealthcare.wd1.myworkdayjobs.com` Workday host is stale/parked (406/422). Reuses the Alorica
    ORC apply strategy (host swap); collect-first until orc_recon widens to `source IN ('alorica',…)`."""
    return _fetch_orc("molina", "Molina Healthcare", "hckd.fa.us2.oraclecloud.com", offset_cap=400)


# --- Workday CxS (generic) — Concentrix + CVS Health share the same /wday/cxs/<tenant>/<site>/jobs
#     shape. A list row carries only `locationsText` (no country/remote field), so US+remote is read
#     off that string: us_eligible() (USA/remote wording) OR _has_us_state() (state code/name). The
#     bare board's `total` is unreliable (reads 0), so callers narrow with a facet or searchText and
#     we paginate until jobPostings is empty (bounded by offset_cap; limit caps at 20/page). ---
# A Workday CxS multi-location REMOTE job typically shows locationsText "N Locations" (or a primary
# physical office) with NO remote word in the text — its remote-ness lives ONLY in the "Working at
# Home"/"Remote"/"Virtual" LOCATION facet. `_wd_remote_location_ids` discovers those facet value ids
# so `_fetch_workday(remote_location_facet=True)` can apply them and capture that hidden slice. Some
# healthcare payers ALSO post a remote role at a physical office with "100% Virtual" only in the
# TITLE (title_remote). Both are opt-in — the existing callers keep the loc/path-only behaviour.
_WD_REMOTE_LOC_RE = re.compile(
    r"working at home|work[- ]?from[- ]?home|work@home|work at home|\bremote\b|virtual|"
    r"telecommut|home[- ]?based", re.I)
_WD_ONSITE_RE = re.compile(r"\bon-?site\b", re.I)


def _wd_remote_location_ids(host: str, tenant: str, site: str) -> list[str]:
    """The `locations` facet value ids whose label reads remote / work-at-home, so a multi-location
    Workday remote job (locationsText 'N Locations', no remote word in text) is captured. Returns []
    on any error (the text-remote pass still runs)."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    ref = f"https://{host}/{site}"
    ids: list[str] = []
    try:
        r = httpx.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                       timeout=30, headers={**_UA, "Content-Type": "application/json",
                                            "Accept": "application/json", "Referer": ref})
        facets = r.json().get("facets") or []
    except Exception as e:
        print(f"[{tenant} facet-discovery] {type(e).__name__}: {e}", file=sys.stderr)
        return []

    def _walk(v: dict) -> None:
        d = v.get("descriptor") or ""
        i = v.get("id")
        if i and _WD_REMOTE_LOC_RE.search(d):
            ids.append(i)
        for sub in (v.get("values") or []):
            _walk(sub)

    for f in facets:
        if "location" in (f.get("facetParameter") or "").lower():
            for v in (f.get("values") or []):
                _walk(v)
    return ids


def _workday_row(j: dict, source: str, company: str, host: str, site: str,
                 us_confirmed: bool = False, *, title_remote: bool = False,
                 assume_remote: bool = False, us_from_path: bool = False) -> dict | None:
    loc = j.get("locationsText") or ""
    ep = j.get("externalPath") or ""
    title = j.get("title") or ""
    if assume_remote:
        # The caller already filtered to a remote LOCATION facet, so every row is remote — but a
        # title that explicitly says "(Onsite)" is a mislabeled facet-tag, so require a real remote
        # signal for those.
        if _WD_ONSITE_RE.search(title) and not (
                _is_remote(loc) or _is_remote(ep.replace("-", " ")) or _is_remote(title)):
            return None
    else:
        # Remote can be encoded in the location OR the externalPath slug (e.g. a multi-location row
        # shows loc "16 Locations" while the path is /job/Tennessee-Work-at-Home/...); with
        # title_remote, also in the TITLE ("100% Virtual" at a physical office).
        texts = [loc, ep.replace("-", " ")]
        if title_remote:
            texts.append(title)
        if not any(_is_remote(t) for t in texts):
            return None
    # US is guaranteed when the caller applied a US-country facet OR the tenant is a US-only employer
    # (us_confirmed); otherwise (e.g. CVS, narrowed only by a job-family facet) confirm it from the
    # location text (US wording or a state). With `us_from_path` a multi-location remote row whose
    # locationsText is "N Locations" (no US signal) is confirmed via the US state in the externalPath
    # slug or the title (Everise posts "…-Work-from-Home" at a state-named path, loc "29 Locations").
    us_ok = us_confirmed or us_eligible(loc) or _has_us_state(loc)
    if not us_ok and us_from_path:
        ep_txt = ep.replace("-", " ")
        us_ok = _has_us_state(ep_txt) or _title_us(title)
    if not us_ok:
        return None
    jid = (j.get("bulletFields") or [None])[0] or ep.rstrip("/").split("_")[-1] or ep
    row = _mk_row(source, jid, company, title, loc or "Remote, United States",
                  f"https://{host}/en-US/{site}" + ep)
    if row:
        # US already confirmed above (facet or loc text) — force the flag True so a state-coded /
        # multi-location work-from-home row isn't dropped by collect(us_only=True).
        row["us_eligible"] = True
    return row


def _fetch_workday(source: str, company: str, host: str, tenant: str, site: str, *,
                   search_texts=("",), applied_facets=None, offset_cap: int = 200,
                   us_confirmed: bool = False, title_remote: bool = False,
                   remote_location_facet: bool = False, us_from_path: bool = False) -> list[dict]:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    ref = f"https://{host}/{site}"
    rows, seen = [], set()

    def _drain(facets, st, assume_remote) -> None:
        offset = 0
        while offset < offset_cap:
            try:
                r = httpx.post(url, json={"appliedFacets": facets or {}, "limit": 20,
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
                row = _workday_row(j, source, company, host, site, us_confirmed,
                                   title_remote=title_remote, assume_remote=assume_remote,
                                   us_from_path=us_from_path)
                if row and row["source_id"] not in seen:
                    seen.add(row["source_id"])
                    rows.append(row)
            offset += 20

    # Pass B (opt-in): rows tagged to a "Working at Home"/"Remote"/"Virtual" location facet — the
    # only way to capture a multi-location Workday remote job whose text carries no remote word.
    if remote_location_facet:
        ids = _wd_remote_location_ids(host, tenant, site)
        if ids:
            _drain({**(applied_facets or {}), "locations": ids}, "", assume_remote=True)
    # Pass A: text search (a remote signal in the location / externalPath, and with title_remote the
    # title). Unchanged for the existing callers (remote_location_facet + title_remote default off).
    for st in search_texts:
        _drain(applied_facets, st, assume_remote=False)
    return rows


# Concentrix — its US remote ENTRY-level hiring is NOT (mostly) on the corporate Workday board.
# `cnx.wd1/external_global` is a GLOBAL/PROFESSIONAL board (recon 2026-09-21: 1579 jobs, offshore-
# dominated — PH 342 / MY 193 / ES 185 / IN 91 …; the US country facet is only ~27, and those are
# almost all Director/Principal-Architect/Sr-Manager/Developer or on-site — exactly ONE remote entry
# CSR). The real US work-at-home FRONTLINE (Licensed Health Insurance Rep, Customer Service / Tech
# Support Rep, Insurance Agent, Inside Sales Rep, …, ~$21–23/hr, seasonal) lives on Concentrix's own
# first-party careers feed `jobs.concentrix.com/wp-json/jdq/v1/search`, which AGGREGATES BOTH apply
# channels: the Workday corporate reqs (apply_url on `cnx.wd1.myworkdayjobs.com`) AND the Talkpush
# frontline campaigns (apply_url on `concentrix.crew.talkpush.com`). `country="USA"` returns ~68 US
# rows with a structured `remote_type` (fully_remote / hybrid / on_site) and each row's native
# `apply_url` — so the Workday auto-apply cron (which selects `apply_url ILIKE '%myworkdayjobs.com%'`)
# is UNAFFECTED by the Talkpush rows, and the frontline entry roles feed the human/collect board.
# We keep the legacy Workday-board fetch too, so the proven Workday-apply lane's categorized req is
# never lost (jdq's own Workday-apply slice is all senior → 0 entry roles; `collect()` de-dups the
# merge on (source, source_id)). Net: ~1 → ~7 remote-US entry roles.
_CNX_US_FACET = "bc33aa3152ec42d4995f4791a106ed09"     # locationCountry = United States of America
_CNX_JDQ_URL = "https://jobs.concentrix.com/wp-json/jdq/v1/search"


def _cnx_jdq_row(d: dict) -> dict | None:
    """PURE (network-free): decode ONE jobs.concentrix.com jdq row into a mass-hiring row, applying
    the two HARD RULES (US-remote + categorize()). `country="USA"` already guarantees US, so
    us_eligible is forced True; remote is read off the structured `remote_type` (hybrid/on_site are
    dropped), falling back to a title/location text scan when the field is absent."""
    rt = (d.get("remote_type") or "").strip().lower()
    if rt in ("on_site", "onsite", "hybrid"):
        return None                                    # explicitly not fully remote → drop
    loc_txt = " ".join(str(d.get(k) or "") for k in ("street", "address", "city", "state") if d.get(k))
    if rt != "fully_remote" and not _is_remote(d.get("job_title") or "", loc_txt):
        return None
    sid = d.get("ats_external_id") or d.get("campaign_id") or d.get("id")
    if not sid:
        return None
    apply_url = d.get("apply_url") or d.get("landing_page_url")
    # location string that reads as US (a "Work At Home" city carries no US signal on its own).
    street = (d.get("street") or d.get("address") or "").strip()
    city, state = (d.get("city") or "").strip(), (d.get("state") or "").strip()
    if street and re.search(r"\bUSA\b|United States", street):
        location = street
    elif city and city.lower() != "work at home":
        location = f"{city}, {state}, United States" if state else f"{city}, United States"
    else:
        location = "Remote, United States"
    # frontline campaigns disclose the hourly rate only in the description prose ("$21.00 – 23.00/hr").
    desc = re.sub(r"<[^>]+>", " ", d.get("long_description") or d.get("short_description") or "")
    lo, hi, raw = _parse_hourly_wage(desc)
    row = _mk_row("concentrix", sid, "Concentrix", d.get("job_title"), location, apply_url,
                  salary_min=lo, salary_max=hi, salary_raw=raw,
                  employment_type=d.get("job_type"), posted_at=_iso_epoch(d.get("created_at") or ""))
    if row:
        row["us_eligible"] = True                      # country="USA" facet is authoritative
    return row


def _fetch_concentrix_jdq(country: str = "USA") -> list[dict]:
    """Concentrix first-party careers feed (jobs.concentrix.com jdq API). Paginates the whole US set
    (per_page 100, to `meta.total_pages`, safety-capped) → `_cnx_jdq_row`. Fully guarded."""
    rows, page, pages = [], 1, 1
    hdr = {"User-Agent": _BROWSER_UA, "Accept": "application/json",
           "Referer": "https://jobs.concentrix.com/"}
    while page <= pages and page <= 30:                 # 30-page (~3000-row) safety ceiling
        try:
            r = httpx.get(_CNX_JDQ_URL, headers=hdr, timeout=30,
                          params={"country": country, "per_page": 100, "page": page})
            js = r.json()
        except Exception as e:
            print(f"[concentrix jdq page={page}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        for d in (js.get("data") or []):
            row = _cnx_jdq_row(d)
            if row:
                rows.append(row)
        pages = (js.get("meta") or {}).get("total_pages") or 1
        page += 1
    return rows


def fetch_concentrix() -> list[dict]:
    # Primary: the first-party careers feed (Workday corporate + Talkpush frontline, ~7 entry roles).
    rows = _fetch_concentrix_jdq()
    # Safety net: the legacy Workday board keeps the proven Workday-apply lane's categorized req even
    # if the feed ever drops it. `collect()` de-dups the merge on (source, source_id); dedupe here too
    # so a direct caller (mh_ondemand / tests) gets a clean set.
    rows += _fetch_workday("concentrix", "Concentrix", "cnx.wd1.myworkdayjobs.com", "cnx",
                           "external_global", search_texts=("",),
                           applied_facets={"locationCountry": [_CNX_US_FACET]}, offset_cap=60,
                           us_confirmed=True)
    seen, out = set(), []
    for r in rows:
        if r["source_id"] not in seen:
            seen.add(r["source_id"])
            out.append(r)
    return out


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
def _smartrecruiters_row(j: dict, source: str, company: str, *, country: str = "us",
                         force_eligible: bool = False) -> dict | None:
    loc = j.get("location") or {}
    if (loc.get("country") or "").lower() != country or not loc.get("remote"):
        return None
    country_name = "Canada" if country == "ca" else "United States"
    full = loc.get("fullLocation") or ", ".join(
        x for x in (loc.get("city"), loc.get("region"), country_name) if x)
    row = _mk_row(source, j.get("id"), company, j.get("name"), full or country_name,
                  f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                  posted_at=_iso_epoch(j.get("releasedDate")))
    if row and force_eligible:
        # A CA-remote row (country='ca') has a Canadian location → us_eligible() is False → collect()
        # would drop it. There is no CA column on the North-America board, so force the "keep on board"
        # flag True; location_raw still carries the Canada signal for downstream (persona nationality).
        row["us_eligible"] = True
    return row


def _fetch_smartrecruiters(source: str, company: str, *, country: str = "us",
                           force_eligible: bool = False) -> list[dict]:
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
            row = _smartrecruiters_row(j, source, company, country=country,
                                       force_eligible=force_eligible)
            if row:
                rows.append(row)
        offset += 100
        if offset >= total:
            break
    return rows


def fetch_sutherland() -> list[dict]:
    return _fetch_smartrecruiters("sutherland", "Sutherland")


# Wayfair — DIRECT-HIRE (NOT a BPO) remote "Virtual Sales & Service" / customer-service employer on the
# SAME SmartRecruiters public postings API, so it reuses `_smartrecruiters_row` verbatim (remote-US +
# `categorize()` mass-hiring ENTRY). Apply reuses `strategies/smartrecruiters.py`; the SR apply cron
# (`mass_hiring_apply_sr_cron`) keys jobs on the `jobs.smartrecruiters.com` apply_url, NOT `source`, so
# these rows are auto-picked up with no cron change.
def fetch_wayfair() -> list[dict]:
    return _fetch_smartrecruiters("wayfair", "Wayfair")


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


# Kaiser Permanente — Radancy TalentBrew front (`kaiserpermanentejobs.org`, like TTEC/UnitedHealth):
# GET /search-jobs/results returns a JSON envelope whose `results` key is an HTML fragment of job tiles
# (`a[data-job-id]`). Kaiser's `.job-location` cell is a COMMA LIST "City, ST, <workplace>, <schedule>…"
# where <workplace> is Remote / Onsite / Flexible, so remote-ness is read off that token (or the title)
# and the onsite/flexible clinical roles fall out; US is forced True (the site is US-only). APPLY forwards
# to Oracle Taleo (`kp.taleo.net`) — reuses `strategies/taleo.py` after a per-tenant Basics-mapping verify
# pass (the TTEC Taleo cron is scoped to source='ttec', so a source='kaiser' row is never driven by the
# wrong tuning) → COLLECT-FIRST today, like percepta.
def _kaiser_row(jid, title, loc, href) -> dict | None:
    """One Kaiser Permanente Radancy tile -> normalized row or None. Remote from the location's
    workplace token (or the title); categorize() enforces the mass-hiring entry rule; US forced True
    (kaiserpermanentejobs.org is US-only). Network-free (unit-testable)."""
    if not jid or not title:
        return None
    if not _is_remote(title, loc):
        return None                                    # remote-only (the workplace token in the loc)
    url = ("https://www.kaiserpermanentejobs.org" + href) if (href or "").startswith("/") else (href or "")
    row = _mk_row("kaiser", jid, "Kaiser Permanente", title, loc or "Remote, United States", url)
    if row:
        row["us_eligible"] = True                      # US-only careers site → force True
    return row


def fetch_kaiser() -> list[dict]:
    from bs4 import BeautifulSoup
    rows, seen = [], set()
    for kw in ("remote", "work from home"):
        page = 1
        while page <= 5:
            try:
                r = httpx.get("https://www.kaiserpermanentejobs.org/search-jobs/results",
                              headers={"User-Agent": _BROWSER_UA}, timeout=30,
                              params={"SearchResultsModuleName": "Search Results", "CurrentPage": page,
                                      "RecordsPerPage": 100, "Keyword": kw, "SearchType": 5,
                                      "IsPagination": True})
                html = r.json().get("results") or ""
            except Exception as e:
                print(f"[kaiser kw={kw!r} p={page}] {type(e).__name__}: {e}", file=sys.stderr)
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
                spanloc = a.select_one(".job-location")
                row = _kaiser_row(jid, h2.get_text(strip=True) if h2 else "",
                                  spanloc.get_text(strip=True) if spanloc else "", a.get("href"))
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


# Healthcare payers / BPOs on Workday CxS (Elevance/Anthem + Carelon, Highmark, Sagility). Same
# driven Workday lane as Centene/Concentrix (workday_recon + strategies/workday.py). Unlike Centene/
# Cigna these tenants have NO country facet (their facets are timeType/jobFamilyGroup/
# locationMainGroup) and are US-only employers → us_confirmed=True, and remote is read from BOTH:
#   (a) remote_location_facet — the "Working at Home"/"Remote"/"Virtual" LOCATION facet, which is how
#       a multi-location Workday remote job is encoded (its locationsText is "N Locations" or a
#       physical office, with no remote word in the text); and
#   (b) title_remote — a remote/virtual signal in the TITLE ("100% Virtual") for a role posted at a
#       physical office.
# The register-captcha PROBE still gates promotion to a live apply lane — these are added to the
# cron's _BLOCKED (probe-pending), NOT _LIVE_TENANTS, until one drive reaches an on-page
# "Application Submitted" with no register reCAPTCHA (like Concentrix/Centene).
def fetch_elevance() -> list[dict]:
    # Carelon (an Elevance subsidiary) rides the SAME tenant — surfaced via the "Carelon" searchText.
    return _fetch_workday("elevance", "Elevance Health", "elevancehealth.wd1.myworkdayjobs.com",
                          "elevancehealth", "ANT",
                          search_texts=("member service", "customer care", "claims", "Carelon"),
                          us_confirmed=True, title_remote=True, remote_location_facet=True,
                          offset_cap=300)


def fetch_highmark() -> list[dict]:
    return _fetch_workday("highmark", "Highmark Health", "highmarkhealth.wd1.myworkdayjobs.com",
                          "highmarkhealth", "highmark",
                          search_texts=("customer service", "member", "claims"),
                          us_confirmed=True, title_remote=True, remote_location_facet=True,
                          offset_cap=300)


def fetch_sagility() -> list[dict]:
    return _fetch_workday("sagility", "Sagility", "sagility.wd1.myworkdayjobs.com",
                          "sagility", "SagilityUSA",
                          search_texts=("customer service", "member", "appeals", "fulfillment"),
                          us_confirmed=True, title_remote=True, remote_location_facet=True,
                          offset_cap=300)


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


# Gainwell Technologies — Medicaid/Medicare BPO. The SAME SuccessFactors Recruiting Marketing
# (Jobs2Web) careers site family as Foundever, at jobs.gainwelltechnologies.com — the results
# TABLE is byte-identical (<tr class="data-row"> / td.colTitle a.jobTitle-link href /job/<slug>/<id>/
# / td.colLocation span.jobLocation / span.jobDate), so the Foundever parser is reused verbatim.
# TWO differences vs Foundever, recon'd live 2026-09-21:
#   1. LOCATION FORMAT is "<city>, <state-code>, US, <zip>" (e.g. "Any city, DE, US, 99999") — the
#      country code is a MIDDLE token, NOT the last (that's the ZIP), so `_foundever_is_us`'s
#      last-token check is wrong here. `_gainwell_is_us` scans every comma-token for a US signal.
#   2. The REMOTE signal is in the TITLE ("... - Remote MT", "Remote, ...", "... Remote U.S."),
#      NOT in the location string (which never carries a "Remote"/"Virtual" workplace token). So
#      `_is_remote` is fed the TITLE (and the location, harmlessly). categorize() still enforces
#      the mass-hiring entry rule (drops the clinical/pharmacy/lead titles Gainwell also posts).
# Apply = the SuccessFactors careersection career41.sapsf.com/careers?company=gainwellte, driven by
# tools/gainwell_recon.py reusing strategies/foundever.py::SuccessFactorsStrategy (captcha-free).
_GAINWELL_HOST = "https://jobs.gainwelltechnologies.com"


def _gainwell_is_us(loc: str) -> bool:
    """US eligibility from Gainwell's '<city>, <state>, US, <zip>' location: a US country token
    anywhere in the comma list, else a US state code/name (never the last token, which is the ZIP)."""
    parts = [p.strip() for p in (loc or "").split(",") if p.strip()]
    for p in parts:
        if p.upper() in ("US", "USA") or "united states" in p.lower():
            return True
    return _has_us_state(loc)


# The RMK results table is identical to Foundever's, so the parser is shared verbatim.
_gainwell_parse = _foundever_parse


def _gainwell_row(jid, title, loc, href, date="") -> dict | None:
    """One Gainwell RMK results-table row → normalized row or None. US from the location's country
    token; REMOTE from the TITLE (Gainwell's location carries no workplace token). categorize()
    then enforces the mass-hiring entry rule."""
    if not jid or not title:
        return None
    if not _gainwell_is_us(loc):
        return None                                   # US-only (country token in the location)
    if not _is_remote(title, loc):
        return None                                   # remote-only (the signal is in the TITLE)
    url = (_GAINWELL_HOST + href) if (href or "").startswith("/") else (href or "")
    row = _mk_row("gainwell", jid, "Gainwell Technologies", title, loc, url,
                  posted_at=_foundever_date(date))
    if row:
        # US already confirmed from the location's country token (authoritative) — force the flag so
        # collect(us_only=True) keeps it even though the generic us_eligible() regex may not fire.
        row["us_eligible"] = True
    return row


def fetch_gainwell() -> list[dict]:
    rows, seen = [], set()
    headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*",
               "Referer": _GAINWELL_HOST + "/search-jobs/"}
    try:
        with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as c:
            for kw in ("remote", "work from home"):
                startrow, prev_ids = 0, None
                while startrow < 1000:
                    try:
                        r = c.get(_GAINWELL_HOST + "/search-jobs/results",
                                  params={"q": kw, "startrow": startrow})
                        parsed = _gainwell_parse(r.text)
                    except Exception as e:
                        print(f"[gainwell kw={kw!r} startrow={startrow}] {type(e).__name__}: {e}",
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
                        row = _gainwell_row(jid, title, loc, href, date)
                        if row:
                            rows.append(row)
                    startrow += 10
    except Exception as e:
        print(f"[gainwell] {type(e).__name__}: {e}", file=sys.stderr)
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
#     via _parse_hourly_wage. Apply is now FULL-AUTO server-side (reverse-engineered 2026-09-21): the
#     guest submit is a plain multipart POST to `Applicant/JobApplyWithEmail` (NOT the client-listed
#     JobApplyNoAuth, which 404s) needing NO auth/CSRF/B2C/captcha/résumé — replicated with httpx by
#     strategies/manpower.py + tools/manpower_recon.py (gated MANPOWER_ADVANCE). ---
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


# ---- TLS-fingerprint / JS-interstitial-walled boards (recon 2026-09-21) ---------
# Four employers a prior recon marked "not httpx-collectable" turned out to be REACHABLE: the wall is
# the request FINGERPRINT (a non-browser TLS/JA3 ClientHello, or Cloudflare's JS interstitial), NOT a
# geo/IP block — a US-datacenter egress via Bright Data got the IDENTICAL rejection, so US egress does
# NOT help and is not used. Two are cracked from the plain server IP with curl_cffi's Chrome TLS
# impersonation; two were never really walled (a wrong-endpoint guess in the earlier recon):
#   * Cotiviti (iCIMS)          — httpx got 405 "Human Verification"; impersonate="chrome" → 200 HTML.
#   * Progressive (Talemetry SSR, Cloudflare) — impersonate="chrome124" in a WARMED Session → 200 HTML.
#     (progressive.wd5.myworkdayjobs.com is a bot-walled DECOY tenant; the real board is Talemetry.)
#   * GEICO                     — the careers.geico.com React SPA is behind Incapsula, but the JOBS live
#                                 on an UNPROTECTED Workday tenant reachable by PLAIN httpx (the "Phenom
#                                 /widgets" guess was wrong — "See Open Jobs" points at the Workday board).
#   * Afni                      — ADP "myjobs" SPA; the job API is a plain httpx GET once you send the
#                                 public `myjobstoken` minted by the keyless career-site config (the
#                                 "withCredentials-gated" read was wrong — no login/cookie/geo gate).
# curl_cffi is lazily imported + fully guarded so a missing wheel never breaks collect().
def _cffi_session(impersonate):
    """A curl_cffi Session with browser-TLS impersonation, or None if the wheel is missing (guarded)."""
    try:
        from curl_cffi import requests as _cr
        return _cr.Session(impersonate=impersonate)
    except Exception as e:                       # pragma: no cover - env-specific
        print(f"[cffi] curl_cffi unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _cffi_get(session, url, *, referer=None, params=None, retries=3):
    """GET via a curl_cffi Session (browser TLS), retrying transient non-200s (a Cloudflare interstitial
    is intermittent). Returns the response TEXT on a 200, else None. Never raises."""
    if session is None:
        return None
    hdr = {"Accept": "text/html,application/json,*/*"}
    if referer:
        hdr["Referer"] = referer
    for attempt in range(retries):
        try:
            r = session.get(url, headers=hdr, params=params, timeout=45)
            if r.status_code == 200:
                return r.text
        except Exception as e:
            if attempt == retries - 1:
                print(f"[cffi {url[:60]}] {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(3)
    return None


# GEICO — Workday CxS (plain httpx; the SPA's Incapsula does not guard the Workday board).
def fetch_geico() -> list[dict]:
    """GEICO — Workday CxS `geico.wd1.myworkdayjobs.com/geico/External`, PLAIN httpx (no wall, no proxy).
    US-only employer (`us_confirmed`) + `title_remote` catches 'Remote (United States)' / a WFH title.
    HONEST live yield 2026-09-21: 285 postings, ~28 remote — but every remote role is senior/professional
    (Counsel / Senior Engineer / Sales MANAGER / Investigator) → **0 remote-US ENTRY today** (GEICO's
    entry CSR/claims reps are ONSITE). Collect-first future-proof: GEICO runs seasonal remote CSR/sales
    ramps that `categorize()` captures the moment they open. Apply would reuse the Workday create-account
    lane after a per-tenant verify → 'needs_laptop'."""
    return _fetch_workday("geico", "GEICO", "geico.wd1.myworkdayjobs.com", "geico", "External",
                          search_texts=("", "remote"), title_remote=True, us_confirmed=True,
                          offset_cap=320)


# Afni — ADP "myjobs" staffing API (plain httpx + a public myjobstoken).
_AFNI_DOMAIN = "afniexternalcareers"
_AFNI_APPLY_BASE = "https://myjobs.adp.com/afniexternalcareers"


def _afni_loc(req: dict) -> tuple[str, str]:
    """(location string, country codeValue) from an ADP requisition's requisitionLocations[0]."""
    rl = req.get("requisitionLocations") or []
    if not rl:
        return "", ""
    addr = (rl[0] or {}).get("address") or {}
    city = (addr.get("cityName") or "").strip()
    state = ((addr.get("countrySubdivisionLevel1") or {}).get("codeValue") or "").strip()
    country = ((addr.get("country") or {}).get("codeValue") or "").strip()
    loc = ", ".join(x for x in (city, state, "United States") if x) or "Remote, United States"
    return loc, country


def _afni_row(req: dict) -> dict | None:
    """PURE (network-free): one Afni ADP requisition dict → a normalized row or None. Remote is
    TITLE-first (a "Remote"/"Work at Home"/"Virtual" title — the ADP feed has no structured remote
    flag; its bare-city location is the recruiting office, not the work site), US off the requisition
    location country code (USA); a blank country is kept (Afni is a US employer)."""
    title = (req.get("jobTitle") or req.get("publishedJobTitle") or "").strip()
    reqid = req.get("reqId")
    if not title or not reqid:
        return None
    loc, country = _afni_loc(req)
    if country and country != "USA":
        return None
    if not _is_remote(title):
        return None
    url = f"{_AFNI_APPLY_BASE}/cx/job-details/{reqid}"
    row = _mk_row("afni", reqid, "Afni", title, loc, url,
                  posted_at=_iso_epoch(req.get("postingDate") or ""))
    if row:
        row["us_eligible"] = True                # USA requisition → force True
    return row


def fetch_afni() -> list[dict]:
    """Afni (US BPO) — ADP "myjobs" careers SPA. The job list is a PLAIN httpx GET: the keyless public
    career-site config (`myjobs.adp.com/public/staffing/v1/career-site/afniexternalcareers`) mints a
    `myJobsToken`; sending it as the `myjobstoken` header (+ `rolecode: manager` + `orgoid`) to the
    staffing `job-requisitions/apply-custom-filters` endpoint returns every requisition — NO login /
    cookie / geo gate. Recon 2026-09-21. Live yield: 63 reqs → ~12 US-remote ENTRY CSR/insurance-rep
    rows ("Remote Customer Service Representative", "Full-Time Remote Insurance Representative"). ADP has
    no apply strategy → collect-first 'needs_laptop'."""
    rows: list[dict] = []
    try:
        with httpx.Client(headers={"User-Agent": _BROWSER_UA, "Accept": "application/json",
                                   "Referer": "https://myjobs.adp.com/"}, timeout=40) as c:
            cfg = c.get(f"https://myjobs.adp.com/public/staffing/v1/career-site/{_AFNI_DOMAIN}").json()
            token, org = cfg.get("myJobsToken"), cfg.get("orgoid")
            if not token or not org:
                return rows
            sel = ("reqId,jobTitle,publishedJobTitle,type,jobDescription,workLocations,"
                   "clientRequisitionID,postingDate,requisitionLocations")
            r = c.get("https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/"
                      "job-requisitions/apply-custom-filters",
                      headers={"myjobstoken": token, "rolecode": "manager", "orgoid": org},
                      params={"$select": sel, "$top": "300", "$skip": "0", "$filter": "",
                              "tz": "America/New_York"})
            reqs = r.json().get("jobRequisitions") or []
    except Exception as e:
        print(f"[afni] {type(e).__name__}: {e}", file=sys.stderr)
        return rows
    for req in reqs:
        row = _afni_row(req)
        if row:
            rows.append(row)
    return rows


# Cotiviti — iCIMS (curl_cffi Chrome TLS impersonation defeats the 405 "Human Verification").
def _cotiviti_row(jid, title, loc, href) -> dict | None:
    """PURE (network-free): one Cotiviti iCIMS results row → a normalized row or None. iCIMS carries a
    STRUCTURED location token ("US-Remote", or "US-Remote | US-UT-South Jordan"); remote+US come off it.
    `categorize()` enforces the entry rule; US forced True (careers-cotiviti postings are US)."""
    if not jid or not title:
        return None
    if not _is_remote(loc):
        return None
    if "US-" not in (loc or "").upper() and not us_eligible(loc):
        return None
    url = (href.split("?")[0] if href else f"https://careers-cotiviti.icims.com/jobs/{jid}/job")
    if url.startswith("/"):
        url = "https://careers-cotiviti.icims.com" + url
    row = _mk_row("cotiviti", jid, "Cotiviti", title, loc or "US-Remote", url)
    if row:
        row["us_eligible"] = True
    return row


def fetch_cotiviti() -> list[dict]:
    """Cotiviti — iCIMS (careers-cotiviti.icims.com). The datacenter IP gets a 405 "Human Verification"
    from httpx; curl_cffi's Chrome TLS impersonation returns the 200 results HTML from the SAME plain IP
    (the wall is a TLS-fingerprint check, not geo — a US-datacenter egress got the identical 405). Recon
    2026-09-21. Each results row is "Job Locations <loc> ID 2026-<id> Title <title> …"; loc "US-Remote"
    = remote+US. HONEST live: ~83 postings → ~1 US-remote ENTRY today (Cotiviti is mostly senior
    healthcare-analytics; the entry slice is thin) but future-proof. iCIMS apply exists (TP lane) but is
    tenant-specific → collect-first 'needs_laptop'."""
    from bs4 import BeautifulSoup
    session = _cffi_session("chrome")
    if session is None:
        return []
    rows, seen = [], set()
    for pr in range(0, 6):
        html = _cffi_get(session, "https://careers-cotiviti.icims.com/jobs/search",
                         params={"ss": "1", "in_iframe": "1", "pr": str(pr)}, retries=3)
        if not html:
            break
        anchors = [a for a in BeautifulSoup(html, "html.parser").select("a[href]")
                   if re.search(r"/jobs/\d+/[^/]+/job", a.get("href", ""))]
        if not anchors:
            break
        new = 0
        for a in anchors:
            href = a["href"]
            m = re.search(r"/jobs/(\d+)/", href)
            jid = m.group(1) if m else None
            if not jid or jid in seen:
                continue
            seen.add(jid)
            new += 1
            title = re.sub(r"^Title\s+", "", a.get_text(" ", strip=True)).strip()
            row_el = a
            for _ in range(9):
                row_el = row_el.parent
                if row_el is None or "row" in " ".join(row_el.get("class") or []):
                    break
            rowtxt = re.sub(r"\s+", " ", row_el.get_text(" ", strip=True)) if row_el else ""
            lm = re.search(r"Job Locations\s+(.*?)\s+ID\s", rowtxt)
            row = _cotiviti_row(jid, title, lm.group(1).strip() if lm else "", href)
            if row:
                rows.append(row)
        if new == 0:
            break
    return rows


# Progressive — Talemetry SSR (curl_cffi chrome124 in a warmed Session clears Cloudflare).
def _progressive_row(jid, title, href, loc) -> dict | None:
    """PURE (network-free): one Progressive Talemetry card → a normalized row or None. Fetched from the
    REMOTE facet (`/search/remote_work/remote/jobs/`) so remoteness is guaranteed by the source; US is
    forced (a US-only insurer). `categorize()` enforces the entry rule."""
    if not jid or not title or not href:
        return None
    row = _mk_row("progressive", jid, "Progressive", title, loc or "Remote, United States", href)
    if row:
        row["us_eligible"] = True
    return row


def fetch_progressive() -> list[dict]:
    """Progressive — Talemetry SSR careers behind Cloudflare. httpx gets Cloudflare's "Just a moment"
    403; curl_cffi impersonate="chrome124" in a WARMED Session clears it from the plain server IP (a
    US-datacenter egress got the identical 403 — the wall is TLS/CF-JS, not geo; `progressive.wd5.
    myworkdayjobs.com` is a bot-walled DECOY tenant, the real board is careers.progressive.com). Recon
    2026-09-21. The clean REMOTE facet `/search/remote_work/remote/jobs/` returns only remote roles;
    each `.jobs-section__item` → title + `/jobs/<id>-<slug>/`. HONEST live: only ~7 remote roles TOTAL →
    ~1 entry today, but future-proof (Progressive runs large WFH claims/service/sales ramps). Talemetry
    apply has no strategy → collect-first 'needs_laptop'."""
    from bs4 import BeautifulSoup
    session = _cffi_session("chrome124")
    if session is None:
        return []
    try:                                          # warm the session so Cloudflare issues a clearance cookie
        session.get("https://careers.progressive.com/", timeout=45)
    except Exception:
        pass
    time.sleep(2)
    rows, seen = [], set()
    base = "https://careers.progressive.com/search/remote_work/remote/jobs/"
    for page in range(0, 25):
        url = base if page == 0 else f"{base}?page={page + 1}"
        html = _cffi_get(session, url, referer=base, retries=4)
        if not html:
            break
        items = BeautifulSoup(html, "html.parser").select(".jobs-section__item")
        if not items:
            break
        new = 0
        for it in items:
            a = it.select_one("h3 a[href*='/jobs/']")
            if not a:
                continue
            href = a["href"]
            m = re.search(r"/jobs/(\d+)", href)
            jid = m.group(1) if m else None
            if not jid or jid in seen:
                continue
            seen.add(jid)
            new += 1
            cols = it.select("div.columns")
            loc = ""
            if len(cols) >= 2:
                loc = re.sub(r"\s+", " ", cols[1].get_text(" ", strip=True)).replace("Location:", "").strip()
            row = _progressive_row(jid, a.get_text(" ", strip=True), href, loc or "Remote, United States")
            if row:
                rows.append(row)
        if new == 0 or len(items) < 20:
            break
    return rows


# --- Greenhouse boards (healthcare payers with remote member-services) --------------------------
# `boards-api.greenhouse.io/v1/boards/<token>/jobs` is a keyless public JSON list (id/title/
# location.name/absolute_url). Used for US health-insurer/payer tenants that post remote member-
# services CSR alongside clinical roles (categorize() drops the clinical/senior ones). Apply is NOT
# wired (these embed a custom Greenhouse form — collect-first) → 'needs_laptop'.
def _greenhouse_row(j: dict, source: str, company: str) -> dict | None:
    """PURE (network-free): one Greenhouse board job → a normalized row or None. Remote from the
    title/location, US from the location wording / a state / a US title (these tenants are US-only
    payers, so a bare "Remote" is US). categorize() enforces the mass-hiring entry rule."""
    title = j.get("title") or ""
    loc = ((j.get("location") or {}).get("name") or "").strip()
    if not _is_remote(title, loc):
        return None
    if not (us_eligible(loc) or _has_us_state(loc) or _title_us(title)):
        return None
    return _mk_row(source, j.get("id"), company, title, loc or "Remote, United States",
                   j.get("absolute_url") or "", posted_at=_iso_epoch(j.get("updated_at") or ""))


def _fetch_greenhouse(source: str, company: str, board: str) -> list[dict]:
    rows: list[dict] = []
    try:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                      params={"content": "false"}, headers={**_UA, "Accept": "application/json"},
                      timeout=30)
        for j in (r.json().get("jobs") or []):
            row = _greenhouse_row(j, source, company)
            if row:
                rows.append(row)
    except Exception as e:
        print(f"[{source}] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


def fetch_oscar() -> list[dict]:
    """Oscar Health — Greenhouse board `oscar` (~290 reqs). US health insurer; remote member-services /
    verification / claims CSR. HONEST live 2026-09-22: ~1 remote entry today (COB Verification
    Specialist); the rest is senior/eng/clinical (categorize drops it). Future-proof — Oscar ramps
    remote member-services seasonally. Collect-first (custom GH form) → 'needs_laptop'."""
    return _fetch_greenhouse("oscar", "Oscar Health", "oscar")


def fetch_clover() -> list[dict]:
    """Clover Health — Greenhouse board `cloverhealth` (~64 reqs). US Medicare-Advantage insurer;
    remote provider-engagement / member-services. HONEST live 2026-09-22: ~1 remote entry today
    (CPH - Provider Engagement). Future-proof. Collect-first → 'needs_laptop'."""
    return _fetch_greenhouse("clover", "Clover Health", "cloverhealth")


# --- Everise — a US remote-CSR BPO on Workday (weareeverise.wd1) ---------------------------------
# LIVE-VERIFIED 2026-09-22: board ~40 reqs, ~17 US-remote, ~6 remote-US ENTRY CSR NOW ("Healthcare
# Customer Service Representative - Work From Home", "Licensed Health Insurance Agent- Work from
# Home"). Everise is GLOBAL (US/PH/Guatemala/Malaysia), so NOT us_confirmed — US is read per row:
# most remote reqs carry the US state in the externalPath ("…Work-from-Home-Alabama/…") even when
# locationsText is a bare "29 Locations", so `us_from_path=True` (+ `title_remote` for a WFH title).
# Apply would reuse the Workday create-account lane after a per-tenant verify → 'needs_laptop'.
def fetch_everise() -> list[dict]:
    return _fetch_workday("everise", "Everise", "weareeverise.wd1.myworkdayjobs.com",
                          "weareeverise", "everiseCareers", search_texts=("",),
                          title_remote=True, us_from_path=True, offset_cap=200)


# --- Devoted Health — Workday (devoted.wd1), US-only Medicare-Advantage insurer -------------------
# LIVE-VERIFIED 2026-09-22: board ~76 reqs, mostly senior/clinical → ~0 remote ENTRY today
# (categorize drops Product Leader / QA Manager / Clinical Documentation). US-only employer
# (us_confirmed). Future-proof — Devoted ramps remote member/guide services seasonally. Collect-
# first → 'needs_laptop'.
def fetch_devoted() -> list[dict]:
    return _fetch_workday("devoted", "Devoted Health", "devoted.wd1.myworkdayjobs.com",
                          "devoted", "Devoted", search_texts=("", "remote"),
                          title_remote=True, us_confirmed=True, offset_cap=200)


# --- CANADA remote-CSR (the biggest coverage gap) ------------------------------------------------
# The mass_hiring board has only a `us_eligible` boolean (no CA column), and collect(us_only=True)
# drops any row with us_eligible=False. So a CA source FORCES us_eligible=True to persist on the
# North-America board; location_raw still carries the Canada signal (persona nationality reads the
# location, not this flag). Distinct source names keep the CA slice separable in stats/apply.
def _cnx_ca_row(d: dict) -> dict | None:
    """PURE (network-free): one jobs.concentrix.com jdq row (country='Canada') → a CA-remote mass-
    hiring row or None. Same shape as `_cnx_jdq_row` but builds a Canadian location and forces the
    board-keep flag. Remote off the structured `remote_type` (drop on_site/hybrid), then categorize()."""
    rt = (d.get("remote_type") or "").strip().lower()
    if rt in ("on_site", "onsite", "hybrid"):
        return None
    loc_txt = " ".join(str(d.get(k) or "") for k in ("street", "address", "city", "state") if d.get(k))
    if rt != "fully_remote" and not _is_remote(d.get("job_title") or "", loc_txt):
        return None
    sid = d.get("ats_external_id") or d.get("campaign_id") or d.get("id")
    if not sid:
        return None
    apply_url = d.get("apply_url") or d.get("landing_page_url")
    city, state = (d.get("city") or "").strip(), (d.get("state") or "").strip()
    if city and city.lower() not in ("work at home", "work_from_home_canada", "can", "canada"):
        location = f"{city}, {state}, Canada" if state else f"{city}, Canada"
    else:
        location = "Remote, Canada"
    desc = re.sub(r"<[^>]+>", " ", d.get("long_description") or d.get("short_description") or "")
    lo, hi, raw = _parse_hourly_wage(desc)
    row = _mk_row("concentrix_ca", sid, "Concentrix", d.get("job_title"), location, apply_url,
                  salary_min=lo, salary_max=hi, salary_raw=raw,
                  employment_type=d.get("job_type"), posted_at=_iso_epoch(d.get("created_at") or ""))
    if row:
        row["us_eligible"] = True                      # keep on the NA board (no CA column)
    return row


def fetch_concentrix_canada() -> list[dict]:
    """Concentrix Canada — the same first-party jdq feed as US, `country=Canada`. LIVE-VERIFIED
    2026-09-22: ~37 CA rows, ~10 remote ENTRY CSR (fully_remote WFH + bilingual EN/FR). Reuses the
    Talkpush/Workday apply channels; collect-first CA feed → 'needs_laptop'."""
    rows, page, pages = [], 1, 1
    hdr = {"User-Agent": _BROWSER_UA, "Accept": "application/json",
           "Referer": "https://jobs.concentrix.com/"}
    while page <= pages and page <= 10:
        try:
            r = httpx.get(_CNX_JDQ_URL, headers=hdr, timeout=30,
                          params={"country": "Canada", "per_page": 100, "page": page})
            js = r.json()
        except Exception as e:
            print(f"[concentrix_ca page={page}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        for d in (js.get("data") or []):
            row = _cnx_ca_row(d)
            if row:
                rows.append(row)
        pages = (js.get("meta") or {}).get("total_pages") or 1
        page += 1
    return rows


def fetch_sutherland_canada() -> list[dict]:
    """Sutherland Canada — the SAME SmartRecruiters postings API as US Sutherland, `country=ca`.
    LIVE-VERIFIED 2026-09-22: ~2 CA-remote, ~1 CA-remote entry today (tiny, future-proof). Forces the
    board-keep flag (Canadian location). The SR apply cron keys on the jobs.smartrecruiters.com host,
    so these are the same shared SR lane after a per-tenant verify → cosmetic 'needs_laptop'."""
    return _fetch_smartrecruiters("sutherland_ca", "Sutherland", country="ca", force_eligible=True)


_SOURCES = {"remotive": fetch_remotive, "himalayas": fetch_himalayas,
            "remoteok": fetch_remoteok, "amazon": fetch_amazon_remote,
            "conduent": fetch_conduent, "alorica": fetch_alorica, "hilton": fetch_hilton,
            "concentrix": fetch_concentrix,
            "teleperformance": fetch_teleperformance, "ttec": fetch_ttec, "cvshealth": fetch_cvs,
            "sutherland": fetch_sutherland, "wayfair": fetch_wayfair,
            "workingsolutions": fetch_working_solutions,
            "kelly": fetch_kelly, "maximus": fetch_maximus, "unitedhealth": fetch_unitedhealth,
            "centene": fetch_centene, "cigna": fetch_cigna, "humana": fetch_humana,
            "elevance": fetch_elevance, "highmark": fetch_highmark, "sagility": fetch_sagility,
            "molina": fetch_molina, "kaiser": fetch_kaiser,
            "foundever": fetch_foundever,
            # BPOs on already-supported ATSes (apply reuses the tenant's strategy after a verify pass)
            "transcom": fetch_transcom, "percepta": fetch_percepta,
            "foundever": fetch_foundever, "gainwell": fetch_gainwell,
            # staffing agencies (fast-placement lane)
            "randstad": fetch_randstad, "manpower": fetch_manpower, "experis": fetch_experis,
            "adecco": fetch_adecco, "roberthalf": fetch_roberthalf,
            # TLS-fingerprint / JS-interstitial-walled boards, cracked 2026-09-21 (see the connector list)
            "geico": fetch_geico, "afni": fetch_afni,
            "cotiviti": fetch_cotiviti, "progressive": fetch_progressive,
            # BPO + healthcare-payer coverage expansion (2026-09-22)
            "everise": fetch_everise, "oscar": fetch_oscar, "clover": fetch_clover,
            "devoted": fetch_devoted,
            # CANADA remote-CSR (the biggest gap)
            "concentrix_ca": fetch_concentrix_canada, "sutherland_ca": fetch_sutherland_canada}


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
