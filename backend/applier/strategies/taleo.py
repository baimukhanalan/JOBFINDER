"""Oracle Taleo Enterprise apply strategy (UnitedHealth `uhg.taleo.net`, TTEC `ttec.taleo.net`).

Taleo is the EASY sibling of Avature/Maximus: an account-gated, server-rendered JSF wizard with
**NO captcha, NO WAF, NO submit-gating assessment** (verified live 2026-09-01 — see
`backend/tools/recon_unitedhealth.py` / `recon_ttec.py`). So it runs fully autonomously in the
headful `:98` browser with a fresh synthetic persona + isolated per-job profile — the same rig as the
Teleperformance lane MINUS the NopeCHA/captcha problem.

`TaleoStrategy` SUBCLASSES `AvatureStrategy` to reuse the whole proven wizard machinery
(`_advance_wizard`, `_fill_current_step`, `_answer_screeners`/`_answer_radio_screeners`,
`_decline_demographics`, `_tick_required_checkboxes`, `_fill_passwords`, `_select_by_label`,
`_step_signature`, `_primary_button`, `_rescan_required`). It overrides only what is Taleo-specific:

  * `matches("taleo.net")`
  * `open_form`  — resolve a Radancy job page → the real `*.taleo.net/careersection/…/jobapply.ftl`
    URL if needed, accept the "Statement Before Authentication" (Privacy Agreement) page, then
    register a NEW USER (Taleo wants a distinct USERNAME + password + email — Avature only needs an
    email), landing on wizard step 1 which the base analyzer then fills.
  * `_fill_avature_gaps` → Taleo gaps: state-of-residence <select>, truthful Yes/No prescreeners,
    required-consent checkboxes. (Passwords are set during registration in `open_form`.)
  * `advance_wizard` gated by env **TALEO_ADVANCE** (default OFF → a plain fill is side-effect-free:
    advancing creates the account + transmits PII on the final Submit).

**LIVE-ITERATION CAVEAT:** the Taleo-classic (akira) selectors below — the "I Accept" button, the
"New User" link, the registration field names, the wizard step Continue buttons — are best-effort from
the recon probes and MUST be verified/tuned against the live JSF DOM on `:98`. They are written
defensively (multi-selector, best-effort, never raise), but this file has NOT been run against a live
Taleo form. Iterate exactly as `icims.py` / `avature.py` were.
"""
from __future__ import annotations

import logging
import os
import re

from playwright.async_api import Page

from backend.applier.strategies.avature import AvatureStrategy, _gen_password

logger = logging.getLogger(__name__)

# The external Taleo apply URL embedded in a Radancy job/listing page. UnitedHealth uses careersection
# 10020 (external); TTEC embeds a section-less jobapply.ftl that 302s to a numbered section — both are
# matched here (host is uhg.taleo.net or ttec.taleo.net).
_TALEO_APPLYURL_RE = re.compile(
    r"https://[a-z0-9.]*taleo\.net/careersection/(?:\d+/)?jobapply\.ftl\?job=[A-Za-z0-9]+", re.I)


def resolve_apply_url(page_html: str, prefer_section: str = "10020") -> str | None:
    """Extract the external Taleo apply URL from a Radancy job page's HTML (UnitedHealth or TTEC).

    Prefer careersection 10020 (UHG external candidates) over 10000 (internal). Returns the
    taleo.net jobapply.ftl URL, or None if the page embeds none.
    """
    hits = _TALEO_APPLYURL_RE.findall(page_html or "")
    if not hits:
        return None
    for u in hits:
        if f"/careersection/{prefer_section}/" in u:
            return u
    # else the first non-internal (10000) hit, else the first
    for u in hits:
        if "/careersection/10000/" not in u:
            return u
    return hits[0]


def _taleo_advance() -> bool:
    """True only when TALEO_ADVANCE is explicitly set — the live-submit switch that lets the wizard
    walk past step 1 (which creates the account + transmits PII on the final Submit)."""
    return os.getenv("TALEO_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class TaleoStrategy(AvatureStrategy):
    name = "taleo"
    advance_wizard = _taleo_advance()

    # Deterministic, truthful-by-design Yes/No prescreeners for a fresh synthetic US persona.
    # Matched by label substring (via the inherited _select_by_label). The residence STATE is a
    # <select> handled separately (set to the persona's own state), so the persona answers any
    # "are you located in <state>?" screener truthfully because it is DESIGNED to reside there.
    _SCREENERS = (
        ("legally authorized to work", "Yes"), ("authorized to work", "Yes"),
        ("right to work", "Yes"), ("eligible to work", "Yes"),
        ("require sponsorship", "No"), ("need sponsorship", "No"), ("visa sponsor", "No"),
        ("18 years", "Yes"), ("at least 18", "Yes"), ("older", "Yes"),
        ("previously worked for", "No"), ("currently employed by", "No"),
        ("ever been employed by", "No"), ("former employee", "No"),
        ("consent to a background", "Yes"), ("background check", "Yes"),
        ("drug screen", "Yes"), ("willing to", "Yes"),
    )

    @classmethod
    def matches(cls, url: str) -> bool:
        return "taleo.net" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        """Reach a clean Taleo wizard step 1 for a FRESH persona: (optionally) resolve a Radancy page
        to the taleo.net apply URL, clear cookies, accept the Privacy Agreement, and register a new
        account. Best-effort + never raises; each sub-step no-ops if its control isn't present."""
        url = page.url or ""
        # 1) If we somehow landed on the Radancy job page (not the Taleo form), resolve + navigate.
        if "taleo.net" not in url.lower():
            try:
                html = await page.content()
                taleo = resolve_apply_url(html)
                if taleo:
                    await page.goto(taleo, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(2000)
            except Exception as exc:
                logger.debug("taleo: radancy->taleo resolve failed: %s", exc)
        # 2) Fresh session — a persisted login from a previous persona would lock the wizard.
        try:
            await page.context.clear_cookies()
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass
        # 3) Privacy Agreement ("Statement Before Authentication") -> I Accept.
        await self._accept_privacy(page)
        # 4) New-User registration (username + password + email). Lands on wizard step 1.
        await self._register_new_user(page)
        await page.wait_for_timeout(1500)

    async def _accept_privacy(self, page: Page) -> None:
        """Click the Taleo 'I Accept' button on the data-privacy page (name ends
        …ContinueButton; text 'I Accept' / 'Accept'). No-op if not present."""
        for sel in ('input[name*="ContinueButton" i]',
                    'button[name*="ContinueButton" i]',
                    'input[value="I Accept"]', 'button:has-text("I Accept")',
                    'a:has-text("I Accept")', 'button:has-text("Accept")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2000)
                    return
            except Exception:
                continue

    async def _register_new_user(self, page: Page) -> None:
        """Drive the Sign-In / New-User page: choose New User, fill a unique username + password
        (both boxes) + the persona email, tick terms, submit. Username = the persona email localpart
        (already globally unique per persona). Best-effort; never raises."""
        # click the "New User" affordance if the page shows a Sign In / New User split
        for sel in ('a:has-text("New User")', 'button:has-text("New User")',
                    'input[value*="New User" i]', 'a:has-text("Register")',
                    'button:has-text("Create Account")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
        email = (getattr(self, "_persona_email", "") or "").strip()
        username = email.split("@", 1)[0] if email else ""
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        # username field (name/id/label contains userName / username / user name)
        if username:
            for sel in ('input[name*="userName" i]', 'input[id*="userName" i]',
                        'input[name*="username" i]', 'input[id*="username" i]',
                        'input[aria-label*="user name" i]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.fill(username, timeout=4000)
                        break
                except Exception:
                    continue
        # email field (skip if the registration form has none — some Taleo tenants use email AS the
        # username; the base analyzer also fills email on the wizard identity step)
        if email:
            for sel in ('input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.fill(email, timeout=4000)
                        break
                except Exception:
                    continue
        # both password inputs (password + confirm) — inherited helper
        try:
            await self._fill_passwords(page)
        except Exception:
            pass
        # required terms/consent checkbox(es) — inherited helper (skips marketing opt-ins)
        try:
            await self._tick_required_checkboxes(page)
        except Exception:
            pass
        # submit the registration (Register / Save / Continue)
        for sel in ('input[value="Register" i]', 'button:has-text("Register")',
                    'input[name*="registerButton" i]', 'button:has-text("Save and Continue")',
                    'input[value*="Save" i]', 'button:has-text("Continue")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5000)
                    await page.wait_for_timeout(2500)
                    return
            except Exception:
                continue

    async def prefill(self, page: Page, profile_form: dict, resume_path: str, *args, **kwargs):
        # Stash the persona email so _register_new_user can derive the username BEFORE the base
        # analyzer runs (open_form is called inside super().prefill).
        self._persona_email = (profile_form or {}).get("email", "")
        return await super().prefill(page, profile_form, resume_path, *args, **kwargs)

    async def _fill_avature_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        """Taleo per-step gap fill (called by the inherited prefill after the base analyzer). Passwords
        are already set during registration; here we handle the residence-state <select>, the truthful
        Yes/No prescreeners, and any required consent checkbox on the current wizard step."""
        try:
            await self._tick_required_checkboxes(page)
        except Exception:
            pass
        for substr, ans in self._SCREENERS:
            try:
                await self._select_by_label(page, substr, ans)
            except Exception:
                pass
        state = (profile_form or {}).get("state", "").strip()
        if state:
            for lbl in ("state/province", "state of residence", "state", "province"):
                try:
                    if await self._select_by_label(page, lbl, state):
                        break
                except Exception:
                    continue
