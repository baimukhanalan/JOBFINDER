"""Auto-fill lane for the Mass Hiring board — currently Maximus (Avature), the one
stable-comp ATS that submits end-to-end without a live captcha (verified 2026-08-28).

Reuses the /catalog building blocks (synth_persona → generate_draft → résumé render) but
reads a `mass_hiring_jobs` row instead of the auto-apply `job_catalog`, and writes the
co-pilot's prefill dir under a distinct 'mh_<id>' jobid namespace. The co-pilot picks
`AvatureStrategy` by URL (maximus.avature.net). The caller drives it in DRY-RUN by default
(`copilot /load` with dry_run=1) — the strategy fills but never submits, so nothing is
transmitted to the employer until we explicitly enable the live path.
"""
from __future__ import annotations

import json
import re

from backend.tools import catalog_drafts, drafts_ui
from backend.tools.catalog_drafts import PREFILL_ROOT

# Maximus/BPO titles carry the work-location city+state in parens, e.g.
# "CSR II Operations (Temporary, Remote Lawrence KS)" / "... Remote McAllen, TX)" /
# "... (Remote - New York, NY)". Some of these jobs require residence within N miles of that
# site (onsite equipment pickup), so the persona must be LOCATED there for the residence
# screener to answer Yes truthfully-by-design.
_TITLE_CITY_RE = re.compile(
    r"[Rr]emote\s*[-–,]?\s*([A-Za-z][A-Za-z .'-]+?),?\s+([A-Z]{2})\b")


def _city_from_title(title: str) -> str:
    m = _TITLE_CITY_RE.search(title or "")
    if not m:
        return ""
    city = m.group(1).strip(" .,-")
    st = m.group(2).strip()
    return f"{city}, {st}, United States"

# Apply hosts that have a working auto-fill strategy on THIS board. Avature (Maximus) only
# for now — the one mass-hiring ATS that completes without a live human captcha/assessment.
SUPPORTED_HOSTS = ("avature.net",)


def is_supported(apply_url: str) -> bool:
    u = (apply_url or "").lower()
    return any(h in u for h in SUPPORTED_HOSTS)


def _job_from_row(row: dict) -> dict:
    """Shape a mass_hiring_jobs row into the job dict synth_persona/generate_draft expect."""
    # Prefer the concrete city+state named in the title (residence screeners need it); fall
    # back to the raw location, then a bare US so _country_of still resolves United States.
    location = (_city_from_title(row.get("title") or "")
                or row.get("location_raw") or "United States")
    return {
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "company_key": row.get("company_key") or "",
        "description": "",                       # the board stores no JD body
        "location": location,
        "regions": ["US"],                       # the board is US-only
        "ats": "avature",
        "external_id": str(row.get("source_id") or row.get("id") or ""),
        "url": row.get("apply_url") or "",
        "questions": [],
    }


def _drafted_from_answers(d: dict) -> dict:
    """Fillable known-answers from the generated draft (skip file/none), same shape the
    co-pilot replays. Avature has no scraped questions, so this is usually empty and the
    strategy answers screeners deterministically."""
    drafted: dict[str, str] = {}
    for a in d.get("answers") or []:
        if not a or a.get("source") in ("file", "none"):
            continue
        v = a.get("value")
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        v = str(v or "").strip()
        lbl = drafts_ui._clean_label(a.get("label"))
        if lbl and v:
            drafted[lbl] = v
    return drafted


def prepare(row: dict, gender: str | None = None) -> tuple[str, str]:
    """Synthesize a US persona for this mass-hiring job, tailor a résumé, and write the
    co-pilot prefill dir. Returns (profile_id, jobid='mh_<id>'). No employer contact here —
    only the local persona/résumé/prefill artifacts are produced."""
    if not is_supported(row.get("apply_url", "")):
        raise ValueError("auto-fill not supported for this source yet")
    from backend.tools.synth_persona import synth_persona

    job = _job_from_row(row)

    # fast on-demand tier for the résumé tailoring (mirrors ensure_and_wire); guarded so a
    # config quirk never breaks the fill (tailoring falls back to the deterministic path).
    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job, gender=gender)

    # Force the persona to LIVE at the job's city/state (parsed from the title) so residence
    # screeners ("do you reside within 75 miles of <site>?") are coherent — synth_persona only
    # knows major cities, so a "Lawrence, KS" job would otherwise land the persona elsewhere.
    place = _city_from_title(job["title"])
    if place:
        from backend.tools.synth_persona import _us_state_full
        city, st_code = place.split(",")[0].strip(), place.split(",")[1].strip()
        prof = cand["profile"]
        prof["city"] = city
        prof["state"] = _us_state_full(st_code) or prof.get("state") or ""
        prof["location"] = f"{city}, {st_code}"
        pi = (prof.get("resume") or {}).get("personal_info")
        if isinstance(pi, dict):
            pi["location"] = f"{city}, {st_code}"

    # live, deliverable @takhet.com mailbox + CRM registration (best-effort, never fatal) so
    # a Maximus "Application Complete" reply lands in a box the CRM shows.
    try:
        from backend.tools import mailcrm
        from backend.tools.provision_mailboxes import provision_email
        prof = cand["profile"]
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""),
                                      prof.get("id", ""))
    except Exception as e:
        print(f"[mh-fill] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)

    profile_id = cand["profile"]["id"]
    jobid = f"mh_{row['id']}"
    out = PREFILL_ROOT / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    out.joinpath("resume.pdf").write_bytes(
        drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")

    drafted = _drafted_from_answers(d)
    # City/country help generic Avature identity fields; the strategy also answers residence
    # screeners from the persona's own country, so this is belt-and-suspenders.
    ploc = (((d.get("resume") or {}).get("personal_info") or {}).get("location") or "").strip()
    city = ploc.split(",")[0].strip()
    country = cand["profile"].get("country") or (
        ploc.rsplit(",", 1)[-1].strip() if "," in ploc else "")
    if city:
        for lbl in ("Location (City)", "Location", "City", "Current location", "City/Town"):
            drafted.setdefault(lbl, city)
    if country:
        for lbl in ("Country", "Country/Region", "Country of residence"):
            drafted.setdefault(lbl, country)

    report = {"apply_url": job["url"], "job_title": job["title"], "company": job["company"],
              "profile": profile_id, "resume_niche": None, "drafted_answers": drafted,
              "submitted": False}
    out.joinpath("report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": cand["profile"], "facts": cand.get("facts") or {}},
                   ensure_ascii=False), encoding="utf-8")
    return profile_id, jobid
