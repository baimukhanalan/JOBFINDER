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
        r"(healthcare|insurance|benefits|billing|financial services) (representative|agent|associate|advisor)|"
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
        try:
            r = httpx.get("https://himalayas.app/jobs/api",
                          params={"limit": 100, "offset": off}, headers=_UA, timeout=30)
            js = r.json().get("jobs") or []
        except Exception as e:
            print(f"[himalayas offset={off}] {type(e).__name__}: {e}", file=sys.stderr)
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


def fetch_amazon_remote() -> list[dict]:
    """Amazon's (small) remote slice — a job is remote only if its location says Virtual."""
    rows = []
    queries = ["customer service", "customer support", "sales", "data"]
    for q in queries:
        try:
            r = httpx.get("https://www.amazon.jobs/en/search.json", headers=_UA, timeout=30,
                          params={"base_query": q, "country": "USA", "result_limit": 200,
                                  "sort": "recent"})
            for j in (r.json().get("jobs") or []):
                loc = (j.get("normalized_location") or "")
                if "virtual" not in loc.lower():        # remote-only rule
                    continue
                url = "https://www.amazon.jobs" + (j.get("job_path") or "")
                row = _mk_row("amazon", j.get("id"), "Amazon", j.get("title"), loc, url,
                              posted_at=_iso_epoch(j.get("updated_time") or j.get("posted_date")))
                if row:
                    rows.append(row)
        except Exception as e:
            print(f"[amazon/{q}] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


# --- BPO connectors (the real remote-US mass-hiring source; endpoints reverse-engineered
#     from each careers SPA's network calls, then verified headless-free with httpx) ---
_REMOTE_RE = re.compile(r"remote|work[- ]?at[- ]?home|work[- ]?from[- ]?home|\bwah\b|virtual|"
                        r"telecommut|home[- ]?based", re.I)


def _is_remote(*texts) -> bool:
    return any(_REMOTE_RE.search(t or "") for t in texts)


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


def fetch_concentrix() -> list[dict]:
    """Concentrix — Workday CxS. POST jobs, paginate offset. Mostly offshore; we keep only
    the 'USA Work-at-Home' slice (bounded pagination — the US-remote fraction is small)."""
    rows, offset, limit = [], 0, 20
    while offset < 400:
        try:
            r = httpx.post("https://cnx.wd1.myworkdayjobs.com/wday/cxs/cnx/external_global/jobs",
                           json={"appliedFacets": {}, "limit": limit, "offset": offset,
                                 "searchText": "customer service work at home"},
                           timeout=30, headers={**_UA, "Content-Type": "application/json",
                                                "Accept": "application/json",
                                                "Referer": "https://cnx.wd1.myworkdayjobs.com/external_global"})
            d = r.json()
            js = d.get("jobPostings") or []
        except Exception as e:
            print(f"[concentrix offset={offset}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not js:
            break
        for j in js:
            loc = j.get("locationsText") or ""
            if "usa" not in loc.lower() or not _is_remote(loc):
                continue
            ep = j.get("externalPath") or ""
            jid = ep.rstrip("/").split("_")[-1] or ep.rstrip("/").split("/")[-1]
            row = _mk_row("concentrix", jid or ep, "Concentrix", j.get("title"), loc,
                          "https://cnx.wd1.myworkdayjobs.com/en-US/external_global" + ep)
            if row:
                rows.append(row)
        offset += limit
        if offset >= int(d.get("total") or 0):
            break
    return rows


_SOURCES = {"remotive": fetch_remotive, "himalayas": fetch_himalayas,
            "remoteok": fetch_remoteok, "amazon": fetch_amazon_remote,
            "conduent": fetch_conduent, "alorica": fetch_alorica, "concentrix": fetch_concentrix}


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
        LIMIT %s""", args + [week, limit])
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
    return {"total": r["n"], "active": r["act"], "companies": r["cos"],
            "by_source": by_src, "by_category": by_cat}


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
