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
        cur.execute("CREATE INDEX IF NOT EXISTS mh_company ON mass_hiring_jobs (company_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS mh_cat ON mass_hiring_jobs (category);")
        cur.execute("CREATE INDEX IF NOT EXISTS mh_active ON mass_hiring_jobs (active);")


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


def categorize(title: str) -> str | None:
    """Return the mass-hiring category for a title, or None if it is NOT a mass-hiring role."""
    t = title or ""
    if _NOT_MASS.search(t) or _DEV.search(t):
        return None
    for name, rx in _CATEGORIES:
        if rx.search(t):
            return name
    return None


# US-eligibility from a free-text remote-location field. Accept when US is allowed (explicitly, or
# via anywhere/worldwide/global/americas/north america). Reject region-locked non-US.
_US_OK = re.compile(r"\b(usa?|united states|u\.s\.?|north america|americas|anywhere|worldwide|"
                    r"global|remote)\b", re.I)
_NON_US_ONLY = re.compile(r"^\s*(europe|emea|apac|uk|united kingdom|india|philippines|latam|"
                          r"latin america|canada|australia|africa|asia)\b", re.I)


def us_eligible(location: str) -> bool:
    loc = (location or "").strip()
    if not loc:
        return True                      # unspecified remote → assume open
    if _US_OK.search(loc):
        return True
    if _NON_US_ONLY.search(loc):
        return False
    return False


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())[:40]


# ---- writes --------------------------------------------------------------------
_COLS = ("source", "source_id", "company", "company_key", "title", "category", "location_raw",
         "us_eligible", "employment_type", "seniority", "salary_min", "salary_max", "salary_raw",
         "apply_url", "posted_at", "first_seen", "last_seen", "active")


def upsert_jobs(rows: list[dict]) -> int:
    if not rows:
        return 0
    ph = ",".join(["%s"] * len(_COLS))
    # keep first_seen; refresh everything else including last_seen/active
    upd = ",".join(f"{c}=EXCLUDED.{c}" for c in _COLS if c not in ("source", "source_id", "first_seen"))
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
def _workday_row(j: dict, source: str, company: str, host: str, site: str) -> dict | None:
    loc = j.get("locationsText") or ""
    if not (_is_remote(loc) and (us_eligible(loc) or _has_us_state(loc))):
        return None
    ep = j.get("externalPath") or ""
    jid = (j.get("bulletFields") or [None])[0] or ep.rstrip("/").split("_")[-1] or ep
    row = _mk_row(source, jid, company, j.get("title"), loc,
                  f"https://{host}/en-US/{site}" + ep)
    if row:
        # We already confirmed US above (us_eligible OR a state code/name like "RI - Work from
        # home", which _mk_row's generic us_eligible() misses) — so force the flag True, else
        # collect(us_only=True) would drop these state-coded work-from-home rows.
        row["us_eligible"] = True
    return row


def _fetch_workday(source: str, company: str, host: str, tenant: str, site: str, *,
                   search_texts=("",), applied_facets=None, offset_cap: int = 200) -> list[dict]:
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
                row = _workday_row(j, source, company, host, site)
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
                          applied_facets={"locationCountry": [_CNX_US_FACET]}, offset_cap=60)


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
    return rows


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


_SOURCES = {"remotive": fetch_remotive, "himalayas": fetch_himalayas,
            "remoteok": fetch_remoteok, "amazon": fetch_amazon_remote,
            "conduent": fetch_conduent, "alorica": fetch_alorica, "concentrix": fetch_concentrix,
            "teleperformance": fetch_teleperformance, "ttec": fetch_ttec, "cvshealth": fetch_cvs,
            "sutherland": fetch_sutherland, "workingsolutions": fetch_working_solutions}


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
    return out


# ---- reads (for the tab) -------------------------------------------------------
def companies(category: str | None = None, limit: int = 100) -> list[dict]:
    """Companies ranked by mass_hiring_score: active remote mass-hiring reqs + posting velocity."""
    week = int(time.time()) - 7 * 86400
    where = ["active=TRUE"]
    args: list = []
    if category:
        where.append("category=%s")
        args.append(category)
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


def jobs(company_key: str | None = None, category: str | None = None, limit: int = 100) -> list[dict]:
    where, args = ["active=TRUE"], []
    if company_key:
        where.append("company_key=%s"); args.append(company_key)
    if category:
        where.append("category=%s"); args.append(category)
    with _cur() as cur:
        cur.execute(f"SELECT * FROM mass_hiring_jobs WHERE {' AND '.join(where)} "
                    f"ORDER BY posted_at DESC NULLS LAST LIMIT %s", args + [limit])
        return [dict(r) for r in cur.fetchall()]


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
    elif "--stats" in sys.argv:
        import json
        print(json.dumps(stats(), indent=2))
        print("\nTop companies by mass_hiring_score:")
        for c in companies(limit=20):
            print(f"  {c['mass_hiring_score']:3}  {c['company']:28} "
                  f"active={c['active_jobs']:3} cs={c['cs_jobs']:2} 7d={c['posted_7d']:2}")
    else:
        print(__doc__)
