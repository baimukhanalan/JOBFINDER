"""No-account ATS job-board fetchers, normalized to one shape.

Every function here lists a company's open jobs from its ATS's PUBLIC endpoint —
no login, no account, no API key — the same "just read the board" access Ashby's
posting-api gives us. Each fetcher normalizes the ATS's native JSON into ONE job
dict so the rest of the app (roles_dashboard `_row`, batch `_online_roles`) is
ATS-agnostic:

    {
      "title":            str,
      "applyUrl":         str,   # the public apply page (no account to open it)
      "jobUrl":           str,
      "workplaceType":    "Remote" | "Hybrid" | "OnSite",
      "isRemote":         bool,
      "location":         str,
      "department":       str,
      "descriptionHtml":  str,   # rendered HTML (may be "")
      "descriptionPlain": str,   # tag-stripped text
    }

Supported today (all no-account, all with a similar prefill-and-submit form):
  * ashby       — https://api.ashbyhq.com/posting-api/job-board/<slug>
  * greenhouse  — https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
  * lever       — https://api.lever.co/v0/postings/<slug>?mode=json

Deliberately EXCLUDED: Workday and iCIMS — they force candidates to create an
account before applying, which breaks the "no registration" requirement.
"""
from __future__ import annotations

import html

import httpx

from backend.applier.boards import _strip_html

SUPPORTED = ("ashby", "greenhouse", "lever")
_TIMEOUT = 30


def _wp_norm(raw: str, is_remote: bool) -> tuple[str, bool]:
    """Normalize any ATS workplace label -> ('Remote'|'Hybrid'|'OnSite', is_remote)."""
    r = (raw or "").strip().lower()
    if r in ("remote", "fully remote"):
        return "Remote", True
    if r == "hybrid":
        return "Hybrid", False
    if r in ("on-site", "onsite", "on site", "in-office", "in office"):
        return "OnSite", False
    return ("Remote", True) if is_remote else ("OnSite", False)


# ---- Ashby ---------------------------------------------------------------------
def _fetch_ashby(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    jobs = httpx.get(url, timeout=_TIMEOUT).json().get("jobs", [])
    # Ashby's native shape already matches ours — pass through, filling gaps.
    out = []
    for j in jobs:
        wt, rem = _wp_norm(j.get("workplaceType", ""), bool(j.get("isRemote")))
        out.append({
            "title": j.get("title", ""),
            "applyUrl": j.get("applyUrl") or j.get("jobUrl") or "",
            "jobUrl": j.get("jobUrl") or j.get("applyUrl") or "",
            "workplaceType": wt, "isRemote": rem,
            "location": j.get("location") or "",
            "department": j.get("department") or "",
            "descriptionHtml": j.get("descriptionHtml") or "",
            "descriptionPlain": j.get("descriptionPlain") or "",
        })
    return out


# ---- Greenhouse ----------------------------------------------------------------
def _fetch_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    jobs = httpx.get(url, timeout=_TIMEOUT).json().get("jobs", [])
    out = []
    for j in jobs:
        loc = ((j.get("location") or {}).get("name") or "").strip()
        offices = " ".join(o.get("name", "") for o in (j.get("offices") or []))
        # Greenhouse has no workplace flag — infer "remote" from location/offices.
        blob = f"{loc} {offices} {j.get('title','')}".lower()
        is_remote = "remote" in loc.lower() or ("remote" in blob and "non-remote" not in blob)
        wt, rem = _wp_norm("remote" if is_remote else "onsite", is_remote)
        # `content` is HTML-escaped HTML; unescape for display, strip for matching.
        raw = j.get("content", "") or ""
        desc_html = html.unescape(raw)
        dept = ", ".join(d.get("name", "") for d in (j.get("departments") or []) if d.get("name"))
        out.append({
            "title": j.get("title", ""),
            "applyUrl": j.get("absolute_url", ""),
            "jobUrl": j.get("absolute_url", ""),
            "workplaceType": wt, "isRemote": rem,
            "location": loc,
            "department": dept,
            "descriptionHtml": desc_html,
            "descriptionPlain": _strip_html(raw),
        })
    return out


# ---- Lever ---------------------------------------------------------------------
def _fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = httpx.get(url, timeout=_TIMEOUT).json()
    if not isinstance(data, list):  # {'ok': False, 'error': ...} for unknown slugs
        return []
    out = []
    for p in data:
        cats = p.get("categories") or {}
        wt, rem = _wp_norm(p.get("workplaceType", ""), False)
        desc_html = (p.get("descriptionBody") or p.get("description") or "")
        add = p.get("additional") or ""
        if add:
            desc_html = f"{desc_html}{add}"
        plain = (p.get("descriptionPlain") or p.get("descriptionBodyPlain") or "")
        dept = cats.get("department") or cats.get("team") or ""
        out.append({
            "title": p.get("text", ""),
            "applyUrl": p.get("applyUrl") or p.get("hostedUrl") or "",
            "jobUrl": p.get("hostedUrl") or p.get("applyUrl") or "",
            "workplaceType": wt, "isRemote": rem,
            "location": cats.get("location") or "",
            "department": dept,
            "descriptionHtml": desc_html,
            "descriptionPlain": plain or _strip_html(desc_html),
        })
    return out


_FETCHERS = {
    "ashby": _fetch_ashby,
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
}


def fetch_board(ats: str, slug: str) -> list[dict]:
    """List one company's jobs from its ATS, normalized. Unknown ATS -> ValueError."""
    fetcher = _FETCHERS.get((ats or "ashby").lower())
    if fetcher is None:
        raise ValueError(f"unsupported ATS {ats!r} (have: {', '.join(SUPPORTED)})")
    return fetcher(slug)
