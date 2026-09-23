"""Randstad USA auto-apply strategy — the résumé-DROP lane, unblocked by FriendlyCaptcha solving.

Randstad (a staffing agency) exposes a GENERIC, no-account résumé DROP at
`www.randstadusa.com/job-seeker/submit-your-resume/` (a Drupal `join_randstad` webform): fill
name/email/phone + a desired job-title/location typeahead, upload a résumé via the dropzone, submit.
It is fully fillable server-side — the ONE wall is **FriendlyCaptcha** (a browser-integrity
proof-of-work, `bluex-friendly-captcha`/`.frc-captcha`), which NopeCHA cannot solve. With
`captcha_solver` now routing FriendlyCaptcha to 2captcha, the drop is submittable unattended.

A drop is an INBOUND channel (recruiters reach out with matching roles → the persona @takhet.com
Maildir, the same CRM the apply lanes feed), NOT a per-job application; its ground truth is the
on-page thank-you / a recruiter reply. (The per-JOB apply — `randstadusa.com/jobs/apply/<lob>/<ref>/`
— is account/social-login walled per recon, so the drop is the reachable path; a "continue as guest"
button on a per-job flow is recognised by `has_guest_button` but is not the built path.)

This module keeps its pure helpers (form shaping, eligibility, ack matching, the ADVANCE gate)
NETWORK-FREE so they unit-test with no browser. The live driver is `backend.tools.randstad_recon`.
The real submit is gated by env **RANDSTAD_ADVANCE=1** (default OFF → fill + STOP, nothing transmitted).
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

RANDSTAD_DROP_URL = "https://www.randstadusa.com/job-seeker/submit-your-resume/"
RANDSTAD_SUBMIT_URL = "https://www.randstadusa.com/api/form/submit"
RANDSTAD_WEBFORM_ID = "join_randstad"
RANDSTAD_OP = "join randstad"  # the submit button `op` value, verbatim (captured live)


# --- gate -----------------------------------------------------------------------------------------

def advance_enabled() -> bool:
    """True iff RANDSTAD_ADVANCE is truthy — only then does the strategy click the real Submit.
    Default OFF: a run fills every field + uploads the résumé, then STOPS (nothing transmitted)."""
    return os.getenv("RANDSTAD_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


# --- eligibility ----------------------------------------------------------------------------------

# A synthetic persona can't hold a real state license -> skip licensed roles (same policy as the
# Foundever/Taleo lanes: never target a role that demands a fabricated credential).
_LICENSED_RE = re.compile(r"\blicensed\b|\blicense\b|\bP&C\b|property\s*&?\s*casualty|"
                          r"life\s*&?\s*health|series\s*\d", re.I)


def row_is_staffable(title: str) -> bool:
    """Whether a Randstad posting title is one we can honestly drop a synthetic persona toward
    (excludes licensed-credential roles). A résumé drop is role-agnostic, so this is a light guard."""
    return not _LICENSED_RE.search(title or "")


# --- name / contact shaping (self-contained; mirrors the talent-pool reference) --------------------

def split_name(full_name: str) -> tuple[str, str]:
    """('Mary Jane Watson') -> ('Mary', 'Watson'): first token first name, the rest the last name."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


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
    """A clean formatted US phone for Randstad's free-text phone_number field."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:]}"
    return raw or ""


def default_job_title(persona: dict) -> str:
    """A plausible desired job title (the persona's own best title, else Customer Service Rep)."""
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
    """'City, ST' for the desired-location typeahead (from the persona's placed city/state)."""
    prof = persona or {}
    city = (prof.get("city") or "").strip()
    st = _state_abbr(prof.get("state") or "")
    if city and st:
        return f"{city}, {st}"
    return (prof.get("location") or "").strip() or city or "Remote"


def build_drop_form(persona: dict, *, job_title: str | None = None,
                    job_location: str | None = None, resume_file_id: str = "") -> dict:
    """The multipart field dict for `POST /api/form/submit` (join_randstad). `resume_file_id` comes
    from the dropzone upload (empty in a dry run). The FriendlyCaptcha solution the widget needs is set
    on the page's `frc-captcha-solution` field by the solver, not carried here."""
    prof = persona or {}
    first = (prof.get("first_name") or "").strip()
    last = (prof.get("last_name") or "").strip()
    if not (first and last):
        f2, l2 = split_name(prof.get("full_name") or "")
        first = first or f2
        last = last or l2
    return {
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


def missing_required(form: dict) -> list[str]:
    """The join_randstad REQUIRED fields still empty (first/last/location/title/email)."""
    req = ("first_name", "last_name", "job_location", "job_title", "email_address")
    return [k for k in req if not (form.get(k) or "").strip()]


# --- ack / captcha detection off a page string ----------------------------------------------------

_FRIENDLY_RE = re.compile(
    r"bluex-friendly-captcha|\bfrc-captcha\b|friendly-challenge|FriendlyCaptcha", re.I)

_ACK_RE = re.compile(
    r"thank you|we('| ha)ve received|received your|we'?ll be in touch|successfully|confirmation|"
    r"submitted|success", re.I)
_ERR_RE = re.compile(
    r"captcha|verification|browser check|error|invalid|required|please (enter|correct|complete)|"
    r"try again|failed", re.I)


def page_has_friendlycaptcha(html: str) -> bool:
    """True iff the LIVE page content shows a FriendlyCaptcha widget (needs page.content(), whose
    render-time classes captcha.js adds — a static initial fetch won't show them)."""
    return bool(_FRIENDLY_RE.search(html or ""))


def has_guest_button(html: str) -> bool:
    """True iff a per-job flow offers a 'continue as guest' path (recognised, not the built lane)."""
    return bool(re.search(r"continue as guest|apply as guest|guest\s*apply", html or "", re.I))


def is_drop_ack(status: int, body: str) -> bool:
    """True iff the drop response / on-page body reads as an ACCEPTED talent-pool drop.
    Ground truth = a 200 with a thank-you/confirmation and NO error/captcha rejection (a
    'Browser check failed' FriendlyCaptcha error is NOT an ack)."""
    if status != 200:
        return False
    b = body or ""
    if _ERR_RE.search(b) and not _ACK_RE.search(b):
        return False
    if _ACK_RE.search(b):
        return True
    if ('"redirect"' in b or '"settings"' in b or '"messages"' in b) and not _ERR_RE.search(b):
        return True
    return False


# --- browser strategy -----------------------------------------------------------------------------

class RandstadStrategy:
    """Fills the Randstad résumé DROP and (under RANDSTAD_ADVANCE) submits, clearing FriendlyCaptcha
    via `captcha_solver.solve_on_page` (2captcha). The driver navigates to `RANDSTAD_DROP_URL` and
    hands us the page; we own the fill + the gated submit + the FriendlyCaptcha solve loop."""

    DROP_URL = RANDSTAD_DROP_URL

    async def fill(self, page, persona: dict, resume_path: str, report: dict) -> dict:
        """Fill every join_randstad field + upload the résumé (NO submit). Records into `report`.
        Never raises (best-effort per field)."""
        form = build_drop_form(persona)
        report["form"] = form
        filled: list[str] = []
        for name in ("first_name", "last_name", "email_address", "phone_number"):
            if await self._fill_plain(page, name, form.get(name)):
                filled.append(name)
        for name in ("job_location", "job_title"):
            if await self._fill_typeahead(page, name, form.get(name)):
                filled.append(name)
        report["filled_fields"] = filled
        report["resume_uploaded"] = await self._upload_resume(page, resume_path, report)
        report["missing_required"] = missing_required(form)
        try:
            report["friendlycaptcha"] = page_has_friendlycaptcha(await page.content())
        except Exception:
            report["friendlycaptcha"] = None
        return report

    async def submit(self, page, report: dict, *, wait_secs: int = 150) -> dict:
        """Click Submit and clear FriendlyCaptcha via the 2captcha solver, polling for the on-page ack.
        Only call under `advance_enabled()`. Sets report['submitted'/'success'/'captcha_solved']."""
        from backend.applier import captcha_solver

        # Pre-solve FriendlyCaptcha if it's already mounted (its solution field is read on submit).
        try:
            report["captcha_solved"] = bool(await captcha_solver.solve_on_page(page))
        except Exception as exc:  # noqa: BLE001
            logger.warning("randstad pre-submit captcha solve failed: %s", exc)
            report["captcha_solved"] = False
        try:
            btn = page.locator('[name="op"], button.webform-button--submit, #edit-actions-submit, '
                               'button[type="submit"]')
            await btn.first.click(timeout=15000)
            report["submitted"] = True
        except Exception as exc:  # noqa: BLE001
            report["note"] = f"submit click failed: {type(exc).__name__}: {exc}"
            report["submitted"] = False
            return report

        body = ""
        deadline_iters = max(1, wait_secs // 3)
        for _ in range(deadline_iters):
            await page.wait_for_timeout(3000)
            # A FriendlyCaptcha challenge can (re)mount after the click — keep solving it.
            try:
                if await captcha_solver.solve_on_page(page):
                    report["captcha_solved"] = True
            except Exception:
                pass
            try:
                body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                body = ""
            if is_drop_ack(200, body):
                break
        report["success"] = is_drop_ack(200, body)
        report["body_head"] = (body or "")[:500]
        return report

    # -- internals --

    async def _fill_plain(self, page, name: str, value) -> bool:
        try:
            loc = page.locator(f'[name="{name}"]')
            if await loc.count() and value:
                await loc.first.fill(str(value))
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("randstad fill %s: %s", name, exc)
        return False

    async def _fill_typeahead(self, page, name: str, value) -> bool:
        """location / job_title are autocomplete typeaheads — type, wait, pick the first suggestion."""
        try:
            loc = page.locator(f'[name="{name}"]')
            if not (await loc.count()) or not value:
                return False
            el = loc.first
            await el.click()
            await el.fill("")
            await el.type(str(value), delay=60)
            await page.wait_for_timeout(1800)
            for _ in range(2):
                await el.press("ArrowDown")
                await page.wait_for_timeout(300)
            await el.press("Enter")
            await page.wait_for_timeout(500)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("randstad typeahead %s: %s", name, exc)
            return False

    async def _upload_resume(self, page, resume_path: str, report: dict) -> bool:
        """Attach the résumé via dropzone.js's real input (input.dz-hidden-input), else any file input,
        and wait for the hidden resume[uploaded_files] to populate."""
        try:
            fi = page.locator('input.dz-hidden-input')
            if not await fi.count():
                fi = page.locator('input[type="file"]')
            if await fi.count() and resume_path and os.path.exists(resume_path):
                await fi.first.set_input_files(resume_path)
                for _ in range(25):
                    await page.wait_for_timeout(1000)
                    val = await page.evaluate(
                        "() => { const e=document.querySelector('[name=\"resume[uploaded_files]\"]');"
                        " return e ? e.value : ''; }")
                    if val:
                        report["resume_file_id"] = val
                        return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("randstad resume upload: %s", exc)
        return False
