"""Mass-hiring connectors for the big US health/life insurance AEP seasonal ramps.

The Annual Enrollment Period (Oct–Dec) drives huge REMOTE licensed-agent / telesales / member-
services hiring at the direct-to-consumer insurance marketplaces (SelectQuote, HealthMarkets,
Spring Venture Group, …). These employers run remote call-center models, so their AEP inventory is
the single largest untapped remote-US entry pool for the mass-hiring board.

This module is SELF-CONTAINED: it only IMPORTS the reusable row-decision contract from
``mass_hiring`` (the two HARD RULES — ``_is_remote``/location must be REMOTE, and ``categorize()``
must return a mass-hiring ENTRY bucket — are enforced by routing every candidate through
``_mk_row``). Each ``fetch_<source>()`` hits a live, keyless JSON/HTML endpoint (recon 2026-09-23,
httpx from the plain datacenter IP — no headful browser); each ``_<source>_row(...)`` is a PURE,
network-free decision that is unit-tested.

REGISTRATION (owner wires these into ``mass_hiring.collect()`` + ``_AUTO_STATUS``):
    fetch_selectquote()      _AUTO_STATUS["selectquote"]   = "needs_laptop"
    fetch_healthmarkets()    _AUTO_STATUS["healthmarkets"] = "needs_laptop"
    fetch_springventure()    _AUTO_STATUS["springventure"] = "needs_laptop"

IMPORTANT (categorize gap — the AEP unlock): the current ``mass_hiring.categorize()`` recognises
"Insurance Agent / Advisor / Representative" and "Customer Service Rep" (member-services), but NOT
the licensed-SALES titles these ramps use most — "Licensed Insurance Sales Agent", "Medicare Sales
Agent", "Sales Development Advisor", "Business Development Sales Representative", "Final Expense
Sales Agent". Those live rows are fetched here but DROPPED by ``_mk_row`` until ``categorize()`` is
widened (owner-owned). See ``AEP_SALES_TITLES_DROPPED`` for the live evidence and the suggested
regex bucket in this module's docstring notes. Once categorize is widened, these connectors yield
the full AEP pool with ZERO connector change.
"""
from __future__ import annotations

import sys

import httpx
from bs4 import BeautifulSoup

from backend.tools.mass_hiring import (
    _BROWSER_UA,
    _UA,
    _is_remote,
    _iso_epoch,
    _mk_row,
    _slug,
    _fetch_smartrecruiters,
)

# Live remote-US titles (recon 2026-09-23) that categorize() currently DROPS — the AEP sales pool.
# Kept as documentation/evidence for the owner's categorize() widening decision.
AEP_SALES_TITLES_DROPPED = (
    "Licensed Term Life Insurance Sales Agent",
    "Licensed Final Expense Sales Agent",
    "Sales Agent - SQL",
    "Sales Development Advisor",
    "Business Development Sales Representative",
    "Pre-Sales Transfer Agent",
    "Sales Qualification Associate",
    "Medicare Sales Agent",
)


# ---------------------------------------------------------------------------------------------
# SelectQuote — Jibe career-site JSON API.
# careers.selectquote.com is a Jibe (jobsync) front; the job feed is a keyless JSON GET at
# selectquoteinc.jibeapply.com/api/jobs?page=N&limit=100 (totalCount for pagination). Each job's
# `data` block carries title / full_location / state / country_code / salary_*_value / apply_url
# (an iCIMS careers-selectquoteinc.icims.com URL). Recon 2026-09-23: 126 postings, ~15 remote-US.
# Apply = iCIMS (tenant-specific, like the Cotiviti/TP lane) → collect-first 'needs_laptop'.
# ---------------------------------------------------------------------------------------------
_JIBE_SELECTQUOTE = "https://selectquoteinc.jibeapply.com/api/jobs"


def _selectquote_row(d: dict) -> dict | None:
    """PURE (network-free): one Jibe `data` block → a normalized row or None. Remote from the
    title / full_location / short_location text or an explicit state=="Remote"; US from the
    country code (SelectQuote is a US employer). categorize() enforces the entry rule."""
    if not isinstance(d, dict):
        return None
    title = (d.get("title") or "").strip()
    if not title:
        return None
    full = (d.get("full_location") or "").strip()
    short = (d.get("short_location") or "").strip()
    state = (d.get("state") or "").strip()
    is_remote = _is_remote(title, full, short, state) or state.lower() == "remote"
    if not is_remote:
        return None
    cc = (d.get("country_code") or "").upper()
    country = (d.get("country") or "").strip()
    if cc and cc != "US":
        return None
    if not cc and country and country != "United States":
        return None
    sid = d.get("req_id") or d.get("slug")
    if not sid:
        return None
    loc = full or short or (f"{state}, United States" if state else "Remote, United States")
    apply_url = (d.get("apply_url") or "").strip() or f"https://selectquoteinc.jibeapply.com/jobs/{d.get('slug')}"

    def _num(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    row = _mk_row(
        "selectquote", sid, "SelectQuote", title, loc, apply_url,
        salary_min=_num(d.get("salary_min_value")),
        salary_max=_num(d.get("salary_max_value")),
        employment_type=d.get("employment_type"),
        posted_at=_iso_epoch(str(d.get("posted_date") or d.get("create_date") or "")),
    )
    if row:
        row["us_eligible"] = True         # US employer + remote → keep on the board
    return row


def fetch_selectquote() -> list[dict]:
    """SelectQuote — Jibe JSON API (selectquoteinc.jibeapply.com/api/jobs). Keyless httpx GET,
    paginate by `page` until `totalCount`. Recon 2026-09-23: 126 postings, ~15 remote-US; the AEP
    licensed-sales titles (Licensed Term Life / Final Expense Sales Agent, Sales Development
    Advisor, Business Development Sales Representative) are the bulk but are currently DROPPED by
    categorize() (see the module docstring) → only ~2 pass today (Pharmacy Data-Entry / Billing).
    Widening categorize() to a licensed-insurance-sales bucket unlocks the full AEP volume with no
    connector change. Apply = iCIMS → collect-first 'needs_laptop'."""
    rows: list[dict] = []
    page, total = 1, None
    while page <= 20:
        try:
            r = httpx.get(_JIBE_SELECTQUOTE, params={"page": page, "limit": 100},
                          headers={**_UA, "Accept": "application/json"}, timeout=40)
            d = r.json()
            jobs = d.get("jobs") or []
            total = d.get("totalCount", total)
        except Exception as e:
            print(f"[selectquote page={page}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        if not jobs:
            break
        for jw in jobs:
            row = _selectquote_row((jw or {}).get("data") or {})
            if row:
                rows.append(row)
        page += 1
        if total is not None and (page - 1) * 100 >= total:
            break
    return rows


# ---------------------------------------------------------------------------------------------
# HealthMarkets — Radancy TalentBrew (careers.healthmarkets.com), same front family as TTEC /
# UnitedHealth / Kaiser. The results endpoint returns a JSON envelope whose `results` is an HTML
# fragment of `a[data-job-id]` cards (title `h2`, location `.job-location`). HealthMarkets runs a
# LOCAL field-agent model (most postings are city-based "Insurance Advisor" / "Insurance
# Representative" roles), so the genuinely-remote slice is thin today — but the titles pass
# categorize(), so any remote WFH agent role it opens for AEP is captured. Apply = Radancy →
# iCIMS/Taleo (tenant-specific) → collect-first 'needs_laptop'.
# ---------------------------------------------------------------------------------------------
_HM_HOST = "https://careers.healthmarkets.com"
_HM_RESULTS = _HM_HOST + "/en/search-jobs/results"


def _healthmarkets_row(jid, title, loc, href) -> dict | None:
    """PURE (network-free): one HealthMarkets Radancy card → a normalized row or None. Remote from
    the title / location text; US forced True (HealthMarkets is a US-only insurer). categorize()
    enforces the entry rule."""
    title = (title or "").strip()
    if not jid or not title:
        return None
    if not _is_remote(title, loc or ""):
        return None
    url = href or ""
    if url.startswith("/"):
        url = _HM_HOST + url
    if not url:
        url = f"{_HM_HOST}/en/job/{jid}"
    row = _mk_row("healthmarkets", jid, "HealthMarkets", title, loc or "Remote, United States", url)
    if row:
        row["us_eligible"] = True
    return row


def fetch_healthmarkets() -> list[dict]:
    """HealthMarkets — Radancy TalentBrew results JSON (careers.healthmarkets.com/en/search-jobs/
    results?...SearchType=5), httpx + a browser UA, paginate `CurrentPage`. `results` is an HTML
    fragment (bs4 `a[data-job-id]`). Recon 2026-09-23: ~100+ postings ("Insurance Advisor",
    "Insurance Representative") but a LOCAL field-agent model → ~0 genuinely-remote entry today;
    reachable + structured + future-proof (captures any remote WFH agent role it opens for AEP).
    Apply = Radancy → tenant-specific → collect-first 'needs_laptop'."""
    rows, seen = [], set()
    for page in range(1, 8):
        params = {
            "ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 100, "Distance": 50,
            "RadiusUnitType": 0, "Keywords": "", "Location": "", "ShowRadius": "False",
            "IsPagination": "True", "CustomFacetName": "", "FacetTerm": "", "FacetType": 0,
            "SearchResultsModuleName": "Search Results", "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": 0, "SortDirection": 0, "SearchType": 5,
        }
        try:
            r = httpx.get(_HM_RESULTS, params=params, timeout=30,
                          headers={"User-Agent": _BROWSER_UA, "X-Requested-With": "XMLHttpRequest",
                                   "Accept": "application/json, text/javascript, */*"})
            res = (r.json() or {}).get("results") or ""
        except Exception as e:
            print(f"[healthmarkets page={page}] {type(e).__name__}: {e}", file=sys.stderr)
            break
        anchors = BeautifulSoup(res, "html.parser").select("a[data-job-id]")
        if not anchors:
            break
        new = 0
        for a in anchors:
            jid = a.get("data-job-id")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            new += 1
            ti = a.select_one("h2, h3, .job-title")
            lo = a.select_one(".job-location, .location")
            row = _healthmarkets_row(
                jid,
                ti.get_text(strip=True) if ti else a.get_text(" ", strip=True),
                lo.get_text(strip=True) if lo else "",
                a.get("href", ""))
            if row:
                rows.append(row)
        if new == 0:
            break
    return rows


# ---------------------------------------------------------------------------------------------
# Spring Venture Group — SmartRecruiters (public postings API, slug `springventuregroup1`). A big
# Medicare telesales employer; reuses the shared `_fetch_smartrecruiters` reader verbatim (remote-US
# + categorize()), then relabels the company (the SR slug is not a display name). Recon 2026-09-23:
# 4 live postings, 0 entry today (senior CRO / Compliance), future-proof for AEP licensed-agent
# ramps. Apply reuses `strategies/smartrecruiters.py`; the SR apply cron keys on the
# jobs.smartrecruiters.com apply_url HOST (not `source`), so these rows are host-drivable after a
# per-tenant verify (like Wayfair/Experian) — cosmetic 'needs_laptop'.
# ---------------------------------------------------------------------------------------------
_SV_SR_SLUG = "springventuregroup1"


def fetch_springventure() -> list[dict]:
    """Spring Venture Group — SmartRecruiters (`springventuregroup1`), reuses `_fetch_smartrecruiters`.
    Recon 2026-09-23: totalFound=4, 0 remote-US ENTRY today; future-proof. Host-drivable by the SR
    apply cron after a per-tenant verify → cosmetic 'needs_laptop'."""
    rows = _fetch_smartrecruiters("springventure", _SV_SR_SLUG)
    for r in rows:
        r["company"] = "Spring Venture Group"
        r["company_key"] = _slug("Spring Venture Group")
    return rows


_ALL = {
    "selectquote": fetch_selectquote,
    "healthmarkets": fetch_healthmarkets,
    "springventure": fetch_springventure,
}


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Recon the insurance-AEP mass-hiring connectors.")
    ap.add_argument("--source", choices=sorted(_ALL), help="one connector (default: all)")
    ap.add_argument("--list", action="store_true", help="print each fetched row")
    args = ap.parse_args()
    srcs = [args.source] if args.source else list(_ALL)
    for s in srcs:
        try:
            rows = _ALL[s]()
        except Exception as e:  # pragma: no cover - CLI convenience
            print(f"{s}: ERROR {type(e).__name__}: {e}")
            continue
        print(f"{s}: {len(rows)} mass-hiring rows")
        if args.list:
            for r in rows:
                print(f"   - {r['title']} | {r['location_raw']} | {r['category']} | {r['apply_url'][:60]}")


if __name__ == "__main__":  # pragma: no cover
    _cli()
