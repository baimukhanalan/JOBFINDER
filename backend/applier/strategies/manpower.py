"""ManpowerGroup (Manpower + Experis) guest-apply — a SERVER-SIDE httpx lane (no browser).

Reverse-engineered 2026-09-21 from the live `www.manpower.com` React SPA bundle + a headless
request capture. The apply page (`/en/candidate/jobapply?id=<jobItemID>`) is a client SPA, but the
guest submit it fires is a plain multipart POST that needs **NO auth token, NO CSRF, NO Azure-B2C
session, NO captcha** — so the whole application replicates server-side with httpx:

    POST https://<brand-host>/api/services/Applicant/JobApplyWithEmail        (el="/" + the route)
    Content-Type: multipart/form-data
      part name="profileData"  = JSON  (PersonalInfo + Consent + EditExpertiseAndSkills)
      part name="jobDetails"   = JSON  ({jobId, jobItemID, utm*, referer})
      (an optional résumé file part, appended under its upload `type` — NOT used here: the form's
       "NO RESUME" toggle makes a résumé optional, and the guest path submits fine without one)

The backend (`cd.manpower.com`) answers `{"status":<code>, "data":{"entityID":...}, "message":...}`.
A successful submit returns `status === 1000` (`SUCCESS_STATUS`) with a created `entityID`; a malformed
/ rejected body returns `status === 0`. `JobApplyNoAuth` (the endpoint the client constants also list)
is NOT actually routed (404 at cd.manpower.com) — `JobApplyWithEmail` is the live guest endpoint.

The **real POST is gated by env `MANPOWER_ADVANCE=1`** (default OFF) — with it off, `submit()` builds and
returns the exact payload it WOULD send but transmits nothing (side-effect-free, like the other lanes'
ADVANCE gate). Experis shares the same code path on its own host (`www.experis.com`).

All the payload-shaping logic here is PURE (no network) so it is unit-tested with no live calls; only
`submit()` touches the wire (via an injectable httpx client).
"""
from __future__ import annotations

import json
import os
import re

# The live guest apply route (host-relative; el="/" in the bundle). NOT JobApplyNoAuth (404).
_APPLY_PATH = "api/services/Applicant/JobApplyWithEmail"
SUCCESS_STATUS = 1000                       # `nl` in the bundle: data.status===1000 => applied

# brand -> host (the apply POST is same-origin as the job page)
_BRAND_HOST = {"manpower": "www.manpower.com", "experis": "www.experis.com"}

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")


def apply_host(source: str) -> str:
    return _BRAND_HOST.get((source or "").lower(), "www.manpower.com")


def apply_url(source: str) -> str:
    return f"https://{apply_host(source)}/{_APPLY_PATH}"


def manpower_advance() -> bool:
    return os.getenv("MANPOWER_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def phone_local(phone: str) -> str:
    """10-digit local part of a US phone (the form's `personalContact` is the bare number)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


def default_skills(persona: dict, job_title: str = "") -> list[str]:
    """At least one skill is REQUIRED by the apply form. Prefer the persona's own résumé skills;
    fall back to a CSR-relevant default so the (mandatory) skill token is never empty."""
    sk: list[str] = []
    resume = (persona or {}).get("resume") or {}
    grouped = resume.get("skills_grouped") or {}
    for vals in grouped.values():
        for v in (vals or []):
            v = str(v).strip()
            if v and v not in sk:
                sk.append(v)
    facts_tools = (persona or {}).get("_tools") or []
    for v in facts_tools:
        v = str(v).strip()
        if v and v not in sk:
            sk.append(v)
    if not sk:
        sk = ["Customer Service"]
    return sk[:8]


def build_profile_data(persona: dict, *, skills: list[str] | None = None) -> dict:
    """The `profileData` JSON the SPA builds for a guest apply (captured shape, 2026-09-21).

    PersonalInfo.address {city,state,zip} comes from the form's zip→city/state geo-autocomplete, so
    the caller supplies a coherent real US city/state/zip triple. `personalContact` = the bare phone.
    """
    p = persona or {}
    full = str(p.get("full_name") or p.get("name") or "").strip()
    parts = full.split()
    first = str(p.get("first_name") or (parts[0] if parts else "")).strip()
    last = str(p.get("last_name") or (parts[-1] if len(parts) > 1 else "")).strip()
    city = str(p.get("city") or "").strip()
    state = str(p.get("state") or "").strip()
    zipc = str(p.get("zip") or p.get("zip_code") or p.get("postal_code") or "").strip()
    return {
        "Consent": {"NAConsentCheck": "false"},
        "PersonalInfo": {
            "address": {"city": city, "state": state, "zip": zipc},
            "country": str(p.get("country") or "United States"),
            "firstName": first, "lastName": last,
            "email": str(p.get("email") or "").strip(),
            "personalContact": phone_local(p.get("phone") or ""),
        },
        "EditExpertiseAndSkills": {"skills": skills or default_skills(p)},
    }


def build_job_details(job_id, job_item_id, *, referer: str = "direct") -> dict:
    """The `jobDetails` JSON — the numeric jobId + the GUID jobItemID identify the posting."""
    return {"utmSource": "", "utmCampaign": "", "utmMedium": "", "utmTerm": "", "utmContent": "",
            "referer": referer, "jobId": str(job_id or ""), "jobItemID": str(job_item_id or "")}


def is_success(resp_json: dict) -> bool:
    """A real apply returns data.status===1000 (the bundle's `nl`). The wrapper sometimes puts it at
    the top level and sometimes under `data`, so accept either."""
    if not isinstance(resp_json, dict):
        return False
    st = resp_json.get("status")
    if st is None:
        st = (resp_json.get("data") or {}).get("status") if isinstance(resp_json.get("data"), dict) else None
    try:
        return int(st) == SUCCESS_STATUS
    except (TypeError, ValueError):
        return False


def entity_id(resp_json: dict):
    """The created application id from a successful response (best-effort; may be nested)."""
    if not isinstance(resp_json, dict):
        return None
    for node in (resp_json, resp_json.get("data") if isinstance(resp_json.get("data"), dict) else {}):
        for k in ("entityID", "changedEntityId", "changedEntityID", "entityId"):
            v = (node or {}).get(k)
            if v:
                return v
    return None


_JOBITEM_NEAR_RE = None


def extract_job_item_id(html: str, job_id: str | int) -> str | None:
    """The apply POST needs the posting's GUID `jobItemID` (in the job-page JSON + the search API +
    the /jobapply?id= URL). Pull the one adjacent to this numeric jobID; else the first GUID."""
    if not html:
        return None
    jid = str(job_id or "")
    if jid:
        m = re.search(r'"jobID":"' + re.escape(jid) + r'"[^{}]*?"jobItemID":"([0-9a-fA-F-]{32,40})"', html)
        if m:
            return m.group(1)
    m = re.search(r'"jobItemID":"([0-9a-fA-F-]{32,40})"', html)
    return m.group(1) if m else None


def _multipart(profile_data: dict, job_details: dict):
    """The two string parts, encoded like the browser (name= parts, no filename)."""
    return {
        "profileData": (None, json.dumps(profile_data, separators=(",", ":"))),
        "jobDetails": (None, json.dumps(job_details, separators=(",", ":"))),
    }


def submit(source: str, job_id, job_item_id, persona: dict, *,
           client=None, advance: bool | None = None, skills: list[str] | None = None,
           timeout: float = 30.0) -> dict:
    """Submit the guest application to JobApplyWithEmail (server-side, no browser).

    Returns a report dict. **Transmits nothing unless `advance` (default = env MANPOWER_ADVANCE)** —
    with advance off it returns the exact payload it WOULD send (dry run). `client` is an injectable
    httpx.Client (with cookies from a prior job-page GET); a fresh one is made if omitted.
    """
    if advance is None:
        advance = manpower_advance()
    profile_data = build_profile_data(persona, skills=skills)
    job_details = build_job_details(job_id, job_item_id)
    host = apply_host(source)
    report = {
        "source": source, "job_id": str(job_id or ""), "job_item_id": str(job_item_id or ""),
        "url": apply_url(source), "advanced": bool(advance),
        "profile_data": profile_data, "job_details": job_details,
        "submitted": False, "api_status": None, "success": False, "entity_id": None,
        "http_status": None, "message": None, "note": "",
    }
    # honesty gate: never claim an apply for a persona missing the required identity fields
    pi = profile_data["PersonalInfo"]
    missing = [k for k in ("firstName", "lastName", "email") if not pi.get(k)]
    if not pi["address"].get("zip"):
        missing.append("zip")
    if missing:
        report["note"] = "missing required fields: " + ",".join(missing)
        report["unfilled"] = missing
        return report
    report["unfilled"] = []
    if not job_item_id:
        report["note"] = "missing jobItemID (could not resolve the posting GUID)"
        return report
    if not advance:
        report["note"] = "dry run (MANPOWER_ADVANCE off) — nothing transmitted"
        return report

    import httpx
    own = client is None
    if own:
        client = httpx.Client(timeout=timeout, follow_redirects=True,
                              headers={"User-Agent": _BROWSER_UA})
    try:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US",
            "Origin": f"https://{host}",
            "Referer": f"https://{host}/en/candidate/jobapply?id={job_item_id}",
        }
        r = client.post(apply_url(source), files=_multipart(profile_data, job_details),
                        headers=headers, timeout=timeout)
        report["http_status"] = r.status_code
        report["submitted"] = True
        try:
            j = r.json()
        except Exception:
            j = {}
        report["api_status"] = (j.get("status") if isinstance(j, dict) else None)
        report["message"] = (j.get("message") or j.get("errorMessage")) if isinstance(j, dict) else None
        report["success"] = is_success(j)
        report["entity_id"] = entity_id(j)
        if not report["success"]:
            report["note"] = f"api status={report['api_status']} (success needs {SUCCESS_STATUS})"
    except Exception as exc:  # noqa: BLE001
        report["note"] = f"submit error: {type(exc).__name__}: {str(exc)[:160]}"
    finally:
        if own:
            try:
                client.close()
            except Exception:
                pass
    return report
