"""Seasonal / high-volume remote-US(+CA) mass-hiring connectors (recon 2026-09-23).

Aggressive coverage expansion for the Q4 holiday-retail customer-service ramp, insurance AEP, and
future tax-season hiring — big-brand employers whose ATS is reachable KEYLESS from the plain
datacenter IP (httpx, or curl_cffi Chrome-TLS impersonation for the iCIMS/Cloudflare tenants). This
module is SELF-CONTAINED: it only IMPORTS the reusable row-decision contract + ATS readers from
``mass_hiring`` (the two HARD RULES — ``_is_remote``/location must be REMOTE, and ``categorize()``
must return a mass-hiring ENTRY bucket — are enforced inside those readers / by routing every
candidate through ``_mk_row``). Every ``fetch_<source>()`` hits a live keyless endpoint; every
``_<source>_row(...)`` is a PURE, network-free decision that is unit-tested.

REGISTRATION (owner wires these into ``mass_hiring._SOURCES`` + ``_AUTO_STATUS``) — the maintainer
imports the fetchers FROM this module, exactly like ``connectors_insurance`` / ``connectors_extra``:

    from backend.tools import connectors_seasonal as _cs
    _SOURCES["macys"]       = _cs.fetch_macys
    _SOURCES["ulta"]        = _cs.fetch_ulta
    _SOURCES["nordstrom"]   = _cs.fetch_nordstrom
    _SOURCES["ibex"]        = _cs.fetch_ibex
    _SOURCES["mercury"]     = _cs.fetch_mercury
    _SOURCES["current"]     = _cs.fetch_current
    _SOURCES["rocketmoney"] = _cs.fetch_rocketmoney
    _SOURCES["lendingtree"] = _cs.fetch_lendingtree
    _SOURCES["angi"]        = _cs.fetch_angi
    _SOURCES["ro"]          = _cs.fetch_ro
    _AUTO_STATUS["macys"] = _AUTO_STATUS["ulta"] = _AUTO_STATUS["nordstrom"] = \
        _AUTO_STATUS["ibex"] = _AUTO_STATUS["mercury"] = _AUTO_STATUS["current"] = \
        _AUTO_STATUS["rocketmoney"] = _AUTO_STATUS["lendingtree"] = _AUTO_STATUS["angi"] = \
        _AUTO_STATUS["ro"] = "needs_laptop"

All are COLLECT-FIRST (``auto_status='needs_laptop'``): none has a wired mass-hiring apply strategy
yet (Macy's/Nordstrom/Ulta/IBEX are Oracle-ORC / Workday / Jibe→iCIMS — a per-tenant verify pass is
needed before a lane drives them; the Greenhouse/Ashby/Lever fintech boards have no wired mass-apply).

Also ships a GENERIC ``_fetch_dayforce`` / ``_dayforce_row`` reader (requested for TRANZACT). It is
UNVALIDATED against a live tenant — no reachable TRANZACT/insurance Dayforce namespace resolved this
pass (``tranzact.dayforcehcm.com`` does not resolve; ``www.tranzact.net/careers`` is an info page with
no ATS link). The reader implements the documented Dayforce CandidatePortal endpoint
``GET https://<client>.dayforcehcm.com/CandidatePortal/en-US/<client>/api/Postings`` (JSON
``{"Postings":[...]}``); the pure row decision IS unit-tested. It is NOT registered in ``_SOURCES``
(no confirmed live source). The maintainer wires ``fetch_dayforce(source, company, client)`` once a
real Dayforce namespace is known.
"""
from __future__ import annotations

import re
import sys

import httpx

from backend.tools.mass_hiring import (
    _UA,
    _ashby_row,
    _cffi_get,
    _cffi_session,
    _fetch_ashby,
    _fetch_greenhouse,
    _fetch_lever,
    _fetch_workday,
    _greenhouse_row,
    _has_us_state,
    _is_remote,
    _iso_epoch,
    _lever_row,
    _mk_row,
    _orc_row,
    _title_us,
    _workday_row,
    us_eligible,
)


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# =================================================================================================
# Jibe career-site JSON reader (generic, keyless). careers.<co>.com fronts a Jibe (jobsync) SPA whose
# job feed is a keyless GET at `<host>/api/jobs?page=N&limit=100` (totalCount for pagination). Each
# job's `data` block carries title / full_location / short_location / state / country_code / country /
# apply_url / req_id / slug / salary_*_value. Same family SelectQuote uses (connectors_insurance).
# =================================================================================================
def _jibe_row(d: dict, source: str, company: str, host: str) -> dict | None:
    """PURE (network-free): one Jibe `data` block → a normalized remote-US mass-hiring row or None.
    Remote from the title / location text / `state`=="Remote" / `location_type`=="Remote"; US from the
    country code (or a blank code + `country`=="United States"). `categorize()` enforces the entry rule."""
    if not isinstance(d, dict):
        return None
    title = (d.get("title") or "").strip()
    if not title:
        return None
    full = (d.get("full_location") or "").strip()
    short = (d.get("short_location") or "").strip()
    state = (d.get("state") or "").strip()
    ltype = (d.get("location_type") or "").strip()
    if not (_is_remote(title, full, short, state, ltype)
            or state.lower() == "remote" or ltype.lower() == "remote"):
        return None
    cc = (d.get("country_code") or "").upper()
    country = (d.get("country") or "").strip()
    if cc and cc != "US":
        return None
    if not cc and country and country != "United States":
        return None
    sid = d.get("req_id") or d.get("slug") or d.get("id")
    if not sid:
        return None
    loc = full or short or (f"{state}, United States" if state else "Remote, United States")
    apply_url = (d.get("apply_url") or "").strip() or f"https://{host}/jobs/{d.get('slug') or sid}"
    row = _mk_row(source, sid, company, title, loc, apply_url,
                  salary_min=_num(d.get("salary_min_value")),
                  salary_max=_num(d.get("salary_max_value")),
                  employment_type=d.get("employment_type"),
                  posted_at=_iso_epoch(str(d.get("posted_date") or d.get("create_date") or "")))
    if row:
        row["us_eligible"] = True                    # US employer + remote → keep on the board
    return row


def _fetch_jibe(source: str, company: str, host: str, *, keywords=("remote", "work from home"),
                pages_per_kw: int = 5) -> list[dict]:
    """Keyless Jibe fetch: paginate a small set of remote-signal keyword searches (the board can be
    huge — Ulta is ~10k rows — so a bare enumerate is wasteful; the WFH titles all carry a remote
    keyword). Dedupe on the Jibe req_id. Non-remote leak-ins are dropped by `_jibe_row`."""
    rows, seen = [], set()
    for kw in keywords:
        for page in range(1, pages_per_kw + 1):
            try:
                r = httpx.get(f"https://{host}/api/jobs",
                              params={"page": page, "limit": 100, "keywords": kw},
                              headers={**_UA, "Accept": "application/json"}, timeout=40)
                d = r.json()
                jobs = d.get("jobs") or []
                total = d.get("totalCount") or 0
            except Exception as e:
                print(f"[{source} kw={kw!r} p={page}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not jobs:
                break
            for jw in jobs:
                row = _jibe_row((jw or {}).get("data") or {}, source, company, host)
                if row and row["source_id"] not in seen:
                    seen.add(row["source_id"])
                    rows.append(row)
            if page * 100 >= total:
                break
    return rows


# Ulta Beauty — jibe `ulta.jibeapply.com` (LIVE-VERIFIED 2026-09-23: totalCount ~9974, iCIMS apply).
# Ulta runs a Guest Services CONTACT CENTER (seasonal remote CSR) alongside ~9800 in-store beauty
# roles. HONEST live yield 2026-09-23: 0 remote-US ENTRY today (the only "(Remote)" roles are
# Sr Engineer / Director / Architect — categorize() correctly drops them). Future-proof: any remote
# Guest Services / Contact Center CSR role Ulta opens for peak is captured automatically. Apply =
# iCIMS (fdcnm-/cdcm-careers-ulta.icims.com, tenant-specific) → collect-first 'needs_laptop'.
def _ulta_row(d: dict) -> dict | None:
    return _jibe_row(d, "ulta", "Ulta Beauty", "ulta.jibeapply.com")


def fetch_ulta() -> list[dict]:
    return _fetch_jibe("ulta", "Ulta Beauty", "ulta.jibeapply.com")


# =================================================================================================
# Macy's — Oracle Recruiting Cloud (ORC), host `ebwh.fa.us2.oraclecloud.com`, site `CX_1001`
# (macysjobs.com redirects here). The SAME ORC REST shape as Alorica/Molina, but a DIFFERENT siteNumber
# (CX_1001, not CX_1), so it reuses `_orc_row` for the DECISION and rewrites the apply_url to the right
# site. LIVE-VERIFIED 2026-09-23: keyword-scoped board (remote/customer) → 1 remote-US ENTRY today
# ("Marketplace Seller Support Specialist (REMOTE)"); the rest is store beauty/sales/asset-protection,
# correctly dropped. Future-proof — Macy's runs seasonal remote Customer Service ramps. Apply would
# reuse the Oracle-ORC lane after a per-tenant screener verify → collect-first 'needs_laptop'.
# =================================================================================================
_MACYS_HOST = "ebwh.fa.us2.oraclecloud.com"
_MACYS_SITE = "CX_1001"


def _macys_row(j: dict) -> dict | None:
    """PURE (network-free): one Macy's ORC requisition → a row or None (reuses `_orc_row`; fixes the
    apply_url to Macy's CX_1001 site)."""
    row = _orc_row(j, "macys", "Macy's", _MACYS_HOST)
    if row:
        row["apply_url"] = row["apply_url"].replace("/sites/CX_1/job/", f"/sites/{_MACYS_SITE}/job/")
    return row


def _fetch_orc_site(source: str, company: str, host: str, site: str, *,
                    keywords=("remote", "customer"), offset_cap: int = 400) -> list[dict]:
    """ORC fetch for a non-CX_1 site (the shared `mass_hiring._fetch_orc` hardcodes CX_1). Keyword-
    scoped + deduped so a huge store board isn't fully paged; decision + apply_url via `_orc_row`."""
    rows, seen = [], set()
    for kw in keywords:
        offset, total = 0, None
        while offset < offset_cap:
            url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   "?onlyData=true&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
                   f"&finder=findReqs;siteNumber={site},limit=50,offset={offset},"
                   f"sortBy=POSTING_DATES_DESC,keyword=%22{kw}%22")
            try:
                r = httpx.get(url, timeout=30, headers={**_UA, "Accept": "application/json"})
                it = (r.json().get("items") or [{}])[0]
                reqs = it.get("requisitionList") or []
                total = it.get("TotalJobsCount", total)
            except Exception as e:
                print(f"[{source} kw={kw} off={offset}] {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not reqs:
                break
            for j in reqs:
                row = _orc_row(j, source, company, host)
                if not row:
                    continue
                row["apply_url"] = row["apply_url"].replace("/sites/CX_1/job/", f"/sites/{site}/job/")
                if row["source_id"] not in seen:
                    seen.add(row["source_id"])
                    rows.append(row)
            offset += 50
            if total is not None and offset >= total:
                break
    return rows


def fetch_macys() -> list[dict]:
    return _fetch_orc_site("macys", "Macy's", _MACYS_HOST, _MACYS_SITE)


# =================================================================================================
# Nordstrom — Workday `nordstrom.wd501.myworkdayjobs.com/nordstrom/nordstrom_careers`. Nordstrom runs a
# remote Customer Care Center (seasonal WFH CSR) alongside its (huge) store board. Reuses `_fetch_workday`
# with `title_remote=True`; NOT us_confirmed (Nordstrom has Canada stores), so US is read off the
# location text / a state (default `_workday_row` behaviour). HONEST live yield 2026-09-23: 0 remote-US
# ENTRY today (the board is store-heavy; the few "United States" remote roles are Engineers, dropped).
# Future-proof — captures Nordstrom's seasonal remote Customer Care ramp automatically. Apply would reuse
# the Workday create-account lane after a per-tenant verify → collect-first 'needs_laptop'.
# =================================================================================================
_NORD_HOST = "nordstrom.wd501.myworkdayjobs.com"
_NORD_TENANT = "nordstrom"
_NORD_SITE = "nordstrom_careers"


def _nordstrom_row(j: dict) -> dict | None:
    """PURE (network-free): one Nordstrom Workday jobPosting → a remote-US mass-hiring row or None."""
    return _workday_row(j, "nordstrom", "Nordstrom", _NORD_HOST, _NORD_SITE, title_remote=True)


def fetch_nordstrom() -> list[dict]:
    return _fetch_workday("nordstrom", "Nordstrom", _NORD_HOST, _NORD_TENANT, _NORD_SITE,
                          search_texts=("remote", "work from home", "virtual", "work at home"),
                          title_remote=True, offset_cap=120)


# =================================================================================================
# IBEX — a US remote-CSR BPO on iCIMS (`careers-ibex.icims.com`; the US careersection is
# `uscareers-ibex.icims.com`, which the results rows link to). Like Cotiviti, httpx gets the iCIMS
# "Human Verification" wall; curl_cffi's Chrome TLS impersonation returns the 200 results HTML from the
# SAME plain IP. Each results row is "Location <loc> ID 2026-<id> Title <title> …" — IBEX uses a bare
# "Location US" token (NOT Cotiviti's "Job Locations US-Remote"), and its remote signal is in the TITLE
# ("Customer Service Representative - Work at Home"). HONEST live yield 2026-09-23: ~3 remote-US ENTRY
# today (WAH Customer Service Reps; the rest are onsite / offshore, dropped). IBEX is a global BPO
# (US/PH/Pakistan/Jamaica/Nicaragua) so US is REQUIRED per row (loc "US"/us_eligible OR a US title).
# iCIMS apply exists (the TP lane) but is tenant-specific → collect-first 'needs_laptop'.
# =================================================================================================
_IBEX_HOST = "careers-ibex.icims.com"
_ICIMS_LOC_RE = re.compile(r"(?:Job Locations|Location)\s+(.*?)\s+ID\s", re.I)


def _ibex_row(jid, title, loc, href) -> dict | None:
    """PURE (network-free): one IBEX iCIMS results row → a normalized remote-US row or None. Remote
    from the title / location; US from the location ("US"/us_eligible/a state) OR a US-signal title.
    categorize() enforces the entry rule."""
    title = (title or "").strip()
    if not jid or not title:
        return None
    if not _is_remote(title, loc or ""):
        return None
    lu = (loc or "").strip().upper()
    if not (lu in ("US", "USA", "UNITED STATES") or us_eligible(loc or "")
            or _has_us_state(loc or "") or _title_us(title)):
        return None
    url = (href.split("?")[0] if href else f"https://{_IBEX_HOST}/jobs/{jid}/job")
    if url.startswith("/"):
        url = "https://" + _IBEX_HOST + url
    row = _mk_row("ibex", jid, "IBEX", title, loc or "Remote, United States", url)
    if row:
        row["us_eligible"] = True
    return row


def fetch_ibex() -> list[dict]:
    from bs4 import BeautifulSoup
    session = _cffi_session("chrome")
    if session is None:
        return []
    rows, seen = [], set()
    for pr in range(0, 6):
        html = _cffi_get(session, f"https://{_IBEX_HOST}/jobs/search",
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
            lm = _ICIMS_LOC_RE.search(rowtxt)
            row = _ibex_row(jid, title, lm.group(1).strip() if lm else "", href)
            if row:
                rows.append(row)
        if new == 0:
            break
    return rows


# =================================================================================================
# Keyless-board fintech / marketplace employers with genuine remote-US entry CSR/sales/ops TODAY.
# All US-only (or explicitly US-remote) companies, so the shared reader's US filter is clean here
# (unlike remote.com, whose "Remote-<country>" strings leak past the generic filter — deliberately
# NOT added). Apply = the tenant's custom board (no wired mass-hiring lane) → collect-first.
# =================================================================================================
# --- Greenhouse (reuse _fetch_greenhouse / _greenhouse_row) --------------------------------------
def _mercury_row(j: dict) -> dict | None:
    """PURE: one Mercury Greenhouse board job → a remote-US mass-hiring row or None."""
    return _greenhouse_row(j, "mercury", "Mercury")


def fetch_mercury() -> list[dict]:
    """Mercury — Greenhouse board `mercury` (~63 reqs). US banking fintech; remote Customer Support /
    SDR hiring. LIVE 2026-09-23: ~2 remote-US ENTRY today (Customer Support Specialist, SDR).
    Collect-first 'needs_laptop'."""
    return _fetch_greenhouse("mercury", "Mercury", "mercury")


def _current_row(j: dict) -> dict | None:
    """PURE: one Current Greenhouse board job → a remote-US mass-hiring row or None."""
    return _greenhouse_row(j, "current", "Current")


def fetch_current() -> list[dict]:
    """Current — Greenhouse board `current` (~8 reqs). US neobank; remote Client Experience support.
    LIVE 2026-09-23: ~1 remote-US ENTRY today (Producer, Client Experience & Growth — Remote US).
    Future-proof. Collect-first 'needs_laptop'."""
    return _fetch_greenhouse("current", "Current", "current")


def _rocketmoney_row(j: dict) -> dict | None:
    """PURE: one Rocket Money Greenhouse board job → a remote-US mass-hiring row or None."""
    return _greenhouse_row(j, "rocketmoney", "Rocket Money")


def fetch_rocketmoney() -> list[dict]:
    """Rocket Money (fka Truebill) — Greenhouse board `truebill` (~17 reqs). US personal-finance
    fintech; remote Operations / Customer Support. LIVE 2026-09-23: ~3 remote-US ENTRY today
    (Operations Associate ×2 Eastern/Pacific, Team Leader Operations - Customer Support). Collect-first
    'needs_laptop'. NB the GH board SLUG is `truebill` (pre-rebrand); the source/company is Rocket Money."""
    return _fetch_greenhouse("rocketmoney", "Rocket Money", "truebill")


def _lendingtree_row(j: dict) -> dict | None:
    """PURE: one LendingTree Greenhouse board job → a remote-US mass-hiring row or None."""
    return _greenhouse_row(j, "lendingtree", "LendingTree")


def fetch_lendingtree() -> list[dict]:
    """LendingTree — Greenhouse board `lendingtree` (~18 reqs). US online-loan marketplace; remote
    call-center / appointment-setter hiring. LIVE 2026-09-23: ~1 remote-US ENTRY today (Call Center
    Representative — Appointment Setter, Remote). Future-proof. Collect-first 'needs_laptop'."""
    return _fetch_greenhouse("lendingtree", "LendingTree", "lendingtree")


# --- Ashby (reuse _fetch_ashby / _ashby_row) -----------------------------------------------------
def _angi_row(j: dict) -> dict | None:
    """PURE: one Angi Ashby posting → a remote-US mass-hiring row or None."""
    return _ashby_row(j, "angi", "Angi")


def fetch_angi() -> list[dict]:
    """Angi — Ashby org `angi` (~53 reqs). US home-services marketplace; remote Inside Sales / support.
    LIVE 2026-09-23: ~1 remote-US ENTRY today (Inside Sales Representative — Remote, United States).
    Future-proof. Collect-first 'needs_laptop'."""
    return _fetch_ashby("angi", "Angi", "angi")


# --- Lever (reuse _fetch_lever / _lever_row) -----------------------------------------------------
def _ro_row(j: dict) -> dict | None:
    """PURE: one Ro Lever posting → a remote-US mass-hiring row or None."""
    return _lever_row(j, "ro", "Ro")


def fetch_ro() -> list[dict]:
    """Ro — Lever org `ro` (~53 reqs). US telehealth; remote Patient/Care support + recruiting-coord.
    LIVE 2026-09-23: ~1 remote-US ENTRY today (Talent Coordinator, Temp — NY or Remote). Future-proof
    (Ro ramps remote Care Guide / patient-support hiring). Collect-first 'needs_laptop'."""
    return _fetch_lever("ro", "Ro", "ro")


# =================================================================================================
# GENERIC Dayforce CandidatePortal reader (requested for TRANZACT — UNVALIDATED against a live tenant).
# Documented endpoint: GET https://<client>.dayforcehcm.com/CandidatePortal/en-US/<client>/api/Postings
# → JSON {"Postings":[{"Title","JobDetailUrl","ParentRequisitionCode","Locations":[{"City","State",
# "Country"}], "PostedDate", ...}]}. No TRANZACT/insurance Dayforce namespace resolved this pass, so it
# is NOT registered in _SOURCES; the maintainer wires fetch_dayforce(source, company, client) once a
# live namespace is known. The pure `_dayforce_row` decision IS unit-tested.
# =================================================================================================
def _dayforce_row(p: dict, source: str, company: str, host: str) -> dict | None:
    """PURE (network-free): one Dayforce CandidatePortal posting → a remote-US mass-hiring row or None.
    Remote from the title / location; US from the location wording / a state / a US-signal title.
    Handles Locations as a list of {City,State,Country} dicts OR a flat string. categorize() enforces
    the entry rule."""
    if not isinstance(p, dict):
        return None
    title = (p.get("Title") or p.get("title") or "").strip()
    if not title:
        return None
    loc = ""
    ls = p.get("Locations") or p.get("location") or p.get("Location")
    if isinstance(ls, list) and ls:
        parts = []
        for l in ls:
            if isinstance(l, dict):
                parts.append(", ".join(x for x in (l.get("City"), l.get("State"),
                                                    l.get("Country")) if x))
            elif isinstance(l, str):
                parts.append(l)
        loc = "; ".join(p2 for p2 in parts if p2)
    elif isinstance(ls, str):
        loc = ls
    if not loc:
        loc = " ".join(str(p.get(k) or "") for k in
                       ("City", "State", "Country", "ClientSiteName") if p.get(k)).strip()
    if not _is_remote(title, loc):
        return None
    if not (us_eligible(loc) or _has_us_state(loc) or _title_us(title)):
        return None
    ref = (p.get("ParentRequisitionCode") or p.get("Reference") or p.get("Id")
           or p.get("JobId") or p.get("id"))
    if not ref:
        return None
    detail = (p.get("JobDetailUrl") or p.get("Url") or "").strip()
    if detail.startswith("/"):
        detail = f"https://{host}" + detail
    apply_url = detail or f"https://{host}/CandidatePortal/en-US/Posting/{ref}"
    row = _mk_row(source, ref, company, title, loc or "Remote, United States", apply_url,
                  posted_at=_iso_epoch(str(p.get("PostedDate") or p.get("PostingDate") or "")))
    if row:
        row["us_eligible"] = True
    return row


def _fetch_dayforce(source: str, company: str, client: str, *, host: str | None = None) -> list[dict]:
    """Keyless Dayforce CandidatePortal fetch. `host` defaults to `<client>.dayforcehcm.com`."""
    host = host or f"{client}.dayforcehcm.com"
    url = f"https://{host}/CandidatePortal/en-US/{client}/api/Postings"
    rows: list[dict] = []
    try:
        r = httpx.get(url, headers={**_UA, "Accept": "application/json"}, timeout=30)
        js = r.json()
        posts = js.get("Postings") if isinstance(js, dict) else js
        for p in (posts or []):
            row = _dayforce_row(p, source, company, host)
            if row:
                rows.append(row)
    except Exception as e:
        print(f"[{source}] {type(e).__name__}: {e}", file=sys.stderr)
    return rows


def fetch_dayforce(source: str, company: str, client: str, *, host: str | None = None) -> list[dict]:
    """Public wrapper for the maintainer to wire a specific Dayforce tenant once a live namespace is
    known, e.g. `fetch_dayforce("tranzact", "TRANZACT", "<client-namespace>")`."""
    return _fetch_dayforce(source, company, client, host=host)


# ---- CLI (recon convenience) --------------------------------------------------------------------
_ALL = {
    "macys": fetch_macys, "ulta": fetch_ulta, "nordstrom": fetch_nordstrom, "ibex": fetch_ibex,
    "mercury": fetch_mercury, "current": fetch_current, "rocketmoney": fetch_rocketmoney,
    "lendingtree": fetch_lendingtree, "angi": fetch_angi, "ro": fetch_ro,
}


def _cli() -> None:  # pragma: no cover - CLI convenience
    import argparse
    ap = argparse.ArgumentParser(description="Recon the seasonal mass-hiring connectors.")
    ap.add_argument("--source", choices=sorted(_ALL), help="one connector (default: all)")
    ap.add_argument("--list", action="store_true", help="print each fetched row")
    args = ap.parse_args()
    for s in ([args.source] if args.source else list(_ALL)):
        try:
            rows = _ALL[s]()
        except Exception as e:
            print(f"{s}: ERROR {type(e).__name__}: {e}")
            continue
        print(f"{s}: {len(rows)} mass-hiring rows")
        if args.list:
            for r in rows:
                print(f"   - {r['title']} | {r['location_raw']} | {r['category']} | {r['apply_url'][:60]}")


if __name__ == "__main__":  # pragma: no cover
    _cli()
