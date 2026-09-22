"""Talent-pool résumé-DROP recon + PURE payload helpers (a NEW inbound offer channel).

Instead of applying to a specific job (ATS → assessment → offer), a talent-pool drop posts a
persona's résumé + contact into a staffing/BPO recruiter POOL; recruiters then reach out with
matching roles, and their mail lands in the persona's @takhet.com Maildir (the SAME CRM pipeline
that already ingests recruiter replies). It bypasses per-job ATS + assessments entirely.

This module is NETWORK-FREE and PURE (payload shaping + ack matching + page parsing off a string),
so it unit-tests with no HTTP. The live driver is `talent_pool_drop.py`.

=================================================================================================
RECON FINDINGS (live, 2026-09-22) — which pools expose a GENERIC (not-per-job) résumé drop that is
reachable server-side WITHOUT an account, and whether a captcha walls it:

  RANDSTAD ....... VIABLE (payload-mappable server-side) BUT invisible-reCAPTCHA-gated.
     `www.randstadusa.com/job-seeker/submit-your-resume/` is a Drupal WEBFORM `join_randstad`
     (NOT a per-job apply — the CLAUDE.md note that this generic form uses "Friendly Captcha" is
     CORRECTED here: the served form carries an INVISIBLE reCAPTCHA v2 widget
     `data-captcha-widget-id="cms_captcha" data-size="invisible"`). Two-step, NO account:
       (1) résumé upload  → multipart `POST /dropzonejs/upload?token=<fresh, scraped per page load>`
           (field name `file`), returns a file id that populates `resume[uploaded_files]`;
       (2) form submit    → `POST /api/form/submit` (see `build_randstad_form` for the exact fields).
     A bare httpx POST is thus NOT captcha-free — it needs a reCAPTCHA token (CapSolver
     `ReCaptchaV2TaskProxyLess`, already wired in `applier/capsolver.py`, or a headless grecaptcha
     exec). The sitekey is injected by the site's `captcha.js` at render (not in the initial HTML),
     so it must be discovered at drive time. Session cookies (cms_user_id/userSessionID) are set on
     the GET and must be carried into both POSTs.

  KELLY .......... NOT REACHABLE. `mykelly.com` 403s our IP (Akamai); talent-network URLs 404.
  ADECCO ......... ACCOUNT/SPA-WALLED. Talent-community URL redirects to the `/en-us` React SPA; reCAPTCHA present.
  ROBERT HALF .... ACCOUNT/CAPTCHA-WALLED. Salesforce candidate flow + reCAPTCHA on the page.
  TTEC ........... ACCOUNT-WALLED. "Talent community" → Taleo general profile `ttec.taleo.net/careersection/2/profile.ftl` (register/login).
  TELEPERFORMANCE  reCAPTCHA LEAD FORM, NO RÉSUMÉ. Sitecore/Marketo GUID-field form + reCAPTCHA — email lead capture, no file upload.
  CONCENTRIX ..... SPA. Workday/Marketo/Salesforce client-rendered; no server résumé drop.
  FOUNDEVER ...... EMAIL-ALERT ONLY, NO RÉSUMÉ. SuccessFactors RMK `/talentcommunity/subscribe/` is a
                   JS-rendered job-ALERT subscription (name/email/category), not a résumé drop.

HONEST HEADLINE: of the surveyed pools, ONLY Randstad exposes a generic résumé DROP reachable
server-side without an account — and it is invisible-reCAPTCHA-gated, so it is NOT a pure captcha-
free httpx lane like the Manpower per-job `JobApplyWithEmail` lane. Every other BPO/staffing
"talent community" is an email-alert subscription (no résumé), account-walled (Taleo/Salesforce),
or a reCAPTCHA lead form. A talent-pool drop yields PASSIVE recruiter outreach (lower/slower
conversion than a direct apply); its value is a new top-of-funnel that costs no assessment.
=================================================================================================
"""
from __future__ import annotations

import re

# --- pool registry (recon verdicts, for the driver + reporting) -----------------------------------

# reachable_serverside: a generic résumé/profile drop can be POSTed without a browser account flow.
# captcha: the anti-bot on that drop ("" = none observed).
# resume_drop: the drop actually accepts a résumé FILE (vs an email-alert subscription).
POOLS: dict[str, dict] = {
    "randstad": {
        "label": "Randstad",
        "url": "https://www.randstadusa.com/job-seeker/submit-your-resume/",
        "backend": "drupal_webform:join_randstad",
        "reachable_serverside": True,
        "resume_drop": True,
        "account_required": False,
        "captcha": "recaptcha_v2_invisible",
        "viable": True,
        "note": "Server-reachable Drupal webform (dropzone upload + /api/form/submit). "
                "Invisible reCAPTCHA v2 → needs a solved token (CapSolver/headless), not captcha-free.",
    },
    "kelly": {
        "label": "Kelly", "url": "https://www.mykelly.com/", "backend": "akamai",
        "reachable_serverside": False, "resume_drop": False, "account_required": True,
        "captcha": "unknown", "viable": False,
        "note": "mykelly.com 403s our IP (Akamai); no reachable talent-network résumé form.",
    },
    "adecco": {
        "label": "Adecco", "url": "https://www.adecco.com/en-us", "backend": "react_spa",
        "reachable_serverside": False, "resume_drop": False, "account_required": True,
        "captcha": "recaptcha", "viable": False,
        "note": "Talent-community URL redirects to the /en-us React SPA; reCAPTCHA present.",
    },
    "roberthalf": {
        "label": "Robert Half", "url": "https://www.roberthalf.com/us/en", "backend": "salesforce",
        "reachable_serverside": False, "resume_drop": False, "account_required": True,
        "captcha": "recaptcha_enterprise", "viable": False,
        "note": "Salesforce candidate account + reCAPTCHA.",
    },
    "ttec": {
        "label": "TTEC", "url": "https://ttec.taleo.net/careersection/2/profile.ftl", "backend": "taleo",
        "reachable_serverside": False, "resume_drop": True, "account_required": True,
        "captcha": "unknown", "viable": False,
        "note": "Talent community = Taleo general profile (register/login required).",
    },
    "teleperformance": {
        "label": "Teleperformance", "url": "https://www.tp.com/en-us/careers/", "backend": "sitecore_marketo",
        "reachable_serverside": True, "resume_drop": False, "account_required": False,
        "captcha": "recaptcha", "viable": False,
        "note": "Sitecore/Marketo lead form + reCAPTCHA; email capture, NO résumé file.",
    },
    "concentrix": {
        "label": "Concentrix", "url": "https://jobs.concentrix.com/", "backend": "workday_marketo_spa",
        "reachable_serverside": False, "resume_drop": False, "account_required": True,
        "captcha": "unknown", "viable": False,
        "note": "Workday/Marketo/Salesforce client-rendered SPA; no server résumé drop.",
    },
    "foundever": {
        "label": "Foundever", "url": "https://jobs.foundever.com/talentcommunity/subscribe/",
        "backend": "successfactors_rmk",
        "reachable_serverside": True, "resume_drop": False, "account_required": False,
        "captcha": "unknown", "viable": False,
        "note": "SuccessFactors RMK talent community = job-ALERT email subscription, NO résumé file.",
    },
}


def viable_pools() -> list[str]:
    """Pool keys whose generic résumé drop is reachable server-side without an account (captcha aside)."""
    return [k for k, v in POOLS.items() if v.get("viable")]


# --- name shaping ---------------------------------------------------------------------------------

def split_name(full_name: str) -> tuple[str, str]:
    """('Mary Jane Watson') -> ('Mary', 'Watson'); first token is first name, the rest the last."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


# --- Randstad (join_randstad Drupal webform) ------------------------------------------------------

RANDSTAD_PAGE_URL = "https://www.randstadusa.com/job-seeker/submit-your-resume/"
RANDSTAD_SUBMIT_URL = "https://www.randstadusa.com/api/form/submit"
RANDSTAD_UPLOAD_BASE = "https://www.randstadusa.com"  # + the scraped /dropzonejs/upload?token=... path
RANDSTAD_ORIGIN = "https://www.randstadusa.com"
RANDSTAD_WEBFORM_ID = "join_randstad"
RANDSTAD_OP = "join randstad"  # the submit button `op` value, verbatim


def default_job_title(persona: dict) -> str:
    """A plausible free-text 'desired job title' for the pool (the persona's own best title else CSR)."""
    prof = persona or {}
    for key in ("title", "current_title", "desired_title"):
        v = (prof.get(key) or "").strip()
        if v:
            return v
    exp = prof.get("experience") or []
    if exp and isinstance(exp, list) and isinstance(exp[0], dict):
        v = (exp[0].get("title") or "").strip()
        if v:
            return v
    return "Customer Service Representative"


def default_job_location(persona: dict) -> str:
    """'City, ST' for the pool's desired-location field (from the persona's placed city/state)."""
    prof = persona or {}
    city = (prof.get("city") or "").strip()
    state = (prof.get("state") or "").strip()
    from_state = _state_abbr(state)
    if city and from_state:
        return f"{city}, {from_state}"
    loc = (prof.get("location") or "").strip()
    return loc or (city or "Remote")


_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _state_abbr(state: str) -> str:
    s = (state or "").strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _STATE_ABBR.get(s.lower(), "")


def phone_digits(raw: str) -> str:
    """Randstad's phone_number is free text; keep a clean formatted US number if we can."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:]}"
    return raw or ""


def build_randstad_form(persona: dict, *, job_title: str | None = None,
                        job_location: str | None = None, resume_file_id: str = "",
                        sms_consent: bool = False,
                        recaptcha_token: str = "") -> dict:
    """The EXACT multipart field dict for `POST /api/form/submit` (join_randstad).

    Captured live 2026-09-22. `resume_file_id` comes from the dropzone upload (empty in a dry run);
    `recaptcha_token` is the invisible-reCAPTCHA solution (empty ⇒ a bare submit that the server will
    reject — the honest captcha wall). `validation_input` is served empty (a JS/honeypot field).
    """
    prof = persona or {}
    first = (prof.get("first_name") or "").strip()
    last = (prof.get("last_name") or "").strip()
    if not (first and last):
        f2, l2 = split_name(prof.get("full_name") or "")
        first = first or f2
        last = last or l2
    form = {
        "first_name": first,
        "last_name": last,
        "job_location": job_location or default_job_location(prof),
        "job_title": job_title or default_job_title(prof),
        "email_address": (prof.get("email") or "").strip(),
        "phone_number": phone_digits(prof.get("phone") or ""),
        "resume[uploaded_files]": resume_file_id or "",
        "op": RANDSTAD_OP,
        "webform_id": RANDSTAD_WEBFORM_ID,
        "validation_input": "",
    }
    if sms_consent:
        form["sms_consent[true]"] = "true"
    if recaptcha_token:
        # The token field is JS-injected on the live page; the Drupal captcha module keys it by the
        # widget id. Send both spellings (belt-and-suspenders) — the exact one is a live-verify item.
        form["cms_captcha"] = recaptcha_token
        form["g-recaptcha-response"] = recaptcha_token
    return form


def randstad_missing_fields(form: dict) -> list[str]:
    """The join_randstad REQUIRED fields that are empty (first/last/location/title/email)."""
    req = ("first_name", "last_name", "job_location", "job_title", "email_address")
    return [k for k in req if not (form.get(k) or "").strip()]


_UPLOAD_TOKEN_RE = re.compile(r'data-upload-path="([^"]*?/dropzonejs/upload\?token=[^"]+)"', re.I)
_CAPTCHA_WIDGET_RE = re.compile(
    r'<div[^>]*data-captcha-widget-id="([^"]+)"[^>]*>', re.I)
_CAPTCHA_SIZE_RE = re.compile(r'data-size="([^"]+)"', re.I)
_SITEKEY_RE = re.compile(r'6L[0-9A-Za-z_\-]{38}')


def parse_upload_path(html: str) -> str | None:
    """The fresh dropzone upload path (`/dropzonejs/upload?token=...`) scraped from the page HTML."""
    m = _UPLOAD_TOKEN_RE.search(html or "")
    return m.group(1) if m else None


def parse_captcha(html: str) -> dict:
    """Characterize the anti-bot widget on the served form: {present, widget_id, size, kind, sitekey}."""
    out = {"present": False, "widget_id": None, "size": None, "kind": None, "sitekey": None}
    m = _CAPTCHA_WIDGET_RE.search(html or "")
    if m:
        out["present"] = True
        out["widget_id"] = m.group(1)
        sm = _CAPTCHA_SIZE_RE.search(m.group(0))
        out["size"] = sm.group(1) if sm else None
        # cms_captcha (Randstad) => invisible reCAPTCHA v2 per the site captcha.js
        out["kind"] = "recaptcha_v2_invisible" if out["size"] == "invisible" else "recaptcha_v2"
    sk = _SITEKEY_RE.search(html or "")
    if sk:
        out["sitekey"] = sk.group(0)
    return out


# Drupal's /api/form/submit returns JSON; a happy submit carries a redirect/settings-confirmation or
# a thank-you message. A validation/captcha failure carries an error/message-list. Be tolerant.
_ACK_RE = re.compile(
    r"thank you|we('| ha)ve received|received your|we'?ll be in touch|successfully|confirmation|"
    r"submitted|success", re.I)
_ERR_RE = re.compile(
    r"captcha|recaptcha|verification|error|invalid|required|please (enter|correct|complete)|"
    r"try again|failed", re.I)


def randstad_ack(status: int, body: str) -> bool:
    """True iff the /api/form/submit response reads as an accepted talent-pool drop.

    Ground truth = HTTP 200 + a thank-you/confirmation body with NO error/captcha rejection. A 200
    whose body is a captcha/validation error is NOT an ack.
    """
    if status != 200:
        return False
    b = body or ""
    if _ERR_RE.search(b) and not _ACK_RE.search(b):
        return False
    # a JSON redirect/settings envelope with no error also counts as accepted
    if _ACK_RE.search(b):
        return True
    if ('"redirect"' in b or '"settings"' in b or '"messages"' in b) and not _ERR_RE.search(b):
        return True
    return False
