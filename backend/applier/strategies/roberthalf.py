"""Robert Half apply strategy — a Salesforce candidate-community account wall.

Robert Half's per-job "Apply" (roberthalf.com/us/en/job/…) hands off to a Salesforce
candidate community that gates the application behind a real ACCOUNT (email + password) +
an emailed verification code, with a **reCAPTCHA Enterprise** on the register/submit step:

    roberthalf.com/us/en/job/<loc>/<slug>   -- Apply -->  Salesforce candidate community
      create account (email + password)  — reCAPTCHA Enterprise on register
      -> emailed OTP  (verify_code.read_code from the persona @takhet.com Maildir)
      -> authenticated apply form (contact / résumé / eligibility screeners / EEO / Submit)

Every solved primitive is REUSED, not re-declared as a wall:
  * reCAPTCHA (v2/v3, incl. Enterprise) → captcha_solver.solve_on_page (NopeCHA free path
    in-browser + CapSolver paid escalation, keys in backend/.env).
  * email OTP → verify_code.read_code (the account IS creatable server-side; the persona
    mailbox is provisioned by the recon driver so the code is receivable).
  * the authenticated form's screeners/EEO → the shared base.prefill pipeline
    (deterministic truthful choices + fill_demographics_decline + fill_required_consent).

LIVE actions (create the account, transmit PII, click the final Submit) are GATED behind
env ROBERTHALF_ADVANCE — mirrors AMAZON_ADVANCE / AVATURE_ADVANCE / ORC_ADVANCE. With the
gate OFF (the default), a plain fill / dry-run only lands on the Salesforce account wall
(reported `needs_account`); nothing is created and no PII is transmitted. Even with the gate
ON, the account is created only when the register button is pressed and the application only
when the recorded final Submit is pressed (recorded, never auto-clicked here) — the driver
clicks Submit only when the wizard reached it with `unfilled==[]` (mirrors the other lanes).
"""
import logging
import os
import re
import secrets

from playwright.async_api import Page

from backend.applier import captcha_solver
from backend.applier.analyzer import analyze_page, find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.filler import fill_form
from backend.applier.strategies.amazon_apply import AmazonStrategy
from backend.applier.strategies.base import ApplyStrategy

logger = logging.getLogger(__name__)

# Salesforce community password policy is typically >=8 with mixed classes — reuse a strong one.
_PASSWORD_SPECIALS = "!@#$%^&*?_-"

# The account wall (Salesforce candidate community login/register) vs the apply form.
_WALL_TEXT_RE = re.compile(
    r"create (?:an )?account|create your account|sign in|register|log ?in|"
    r"forgot password|verification code|verify your email", re.I)
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review|proceed)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
_WIZARD_BTN = "button, a[role='button'], [data-testid*='submit' i], [type='submit']"


def _env_advance() -> bool:
    """True only when ROBERTHALF_ADVANCE is explicitly set — the live switch that lets the
    strategy create the Salesforce account (verify the emailed OTP) and walk the apply form.
    OFF by default so a plain fill / dry-run never creates an account or transmits PII."""
    return os.getenv("ROBERTHALF_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _gen_password() -> str:
    """A strong password (upper+lower+digit+special, len>=10) for the Salesforce account."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Rh{body}7{secrets.choice(_PASSWORD_SPECIALS)}"


class RobertHalfStrategy(ApplyStrategy):
    name = "roberthalf"
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        # the job page, the Salesforce candidate community, and any apply surface on the host.
        return ("roberthalf.com" in u
                or "rhcandidate" in u
                or ("roberthalf" in u and "force.com" in u)
                or ("roberthalf" in u and ("my.site.com" in u or "salesforce" in u)))

    # ---- lifecycle ----------------------------------------------------------
    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        self._pf = profile_form or {}
        self._facts = facts or {}
        self._profile_id = profile_id
        self._resume_path = resume_path
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["account_password"] = getattr(self, "_account_pw", "")
        try:
            on_wall = await self._on_account_wall(page)
        except Exception:
            on_wall = False
        if on_wall or report.get("page_type") in ("login_required", "captcha", "expired"):
            report["needs_account"] = True
            if report.get("page_type") not in ("captcha", "expired"):
                report["page_type"] = "login_required"
            return report
        try:
            await self._fill_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("roberthalf: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("roberthalf: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("roberthalf: wizard advance raised: %s", exc)
        return report

    async def open_form(self, page: Page) -> None:
        await self._dismiss_cookie_banner(page)
        # Reveal the apply flow: the job page's "Apply" hands off to the Salesforce community.
        await self._click_apply(page)
        if not self.advance_wizard:
            # dry-run: stop on whatever wall we reached (analyze_page reports login_required).
            return
        try:
            await self._bootstrap_account(page)
        except Exception as exc:
            logger.debug("roberthalf: account bootstrap raised: %s", exc)
        await self._dismiss_cookie_banner(page)

    async def _click_apply(self, page: Page) -> None:
        """Click the job page's Apply so the Salesforce candidate community loads (best-effort)."""
        for sel in ('a:has-text("Apply Now")', 'button:has-text("Apply Now")',
                    'a:has-text("Apply for this job")', 'button:has-text("Apply")',
                    'a:has-text("Apply")', 'a[href*="apply" i]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1200):
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2500)
                    return
            except Exception:
                continue

    # ---- account bootstrap (LIVE, gated) ------------------------------------
    async def _on_account_wall(self, page: Page) -> bool:
        """True when we're on the Salesforce candidate account wall, not the apply form."""
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if any(h in u for h in ("force.com", "my.site.com", "rhcandidate", "/login", "/register")):
            return True
        try:
            txt = ((await page.inner_text("body"))[:4000] or "")
        except Exception:
            txt = ""
        # an account wall shows the register/sign-in affordances AND a password field.
        has_pw = False
        try:
            has_pw = bool(await page.locator('input[type="password"]').count())
        except Exception:
            pass
        return bool(_WALL_TEXT_RE.search(txt)) and has_pw

    async def _bootstrap_account(self, page: Page) -> None:
        """Create the Salesforce candidate account for the persona + verify the emailed OTP so the
        apply form becomes reachable. Side-effect-free until the register button is pressed."""
        if not await self._on_account_wall(page):
            return
        email = self._account_email()
        if not email:
            return
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        import time
        start_ts = time.time()

        # If we landed on Sign-In, switch to Create-account first.
        await self._click_first(page, [
            'a:has-text("Create Account")', 'button:has-text("Create Account")',
            'a:has-text("Register")', 'button:has-text("Register")',
            'a:has-text("Sign up")', 'a:has-text("New user")'])
        await page.wait_for_timeout(1500)

        name = self._persona_name()
        nparts = name.split()
        await self._fill_first(page, [
            'input[type="email"]', 'input[name*="email" i]', 'input[autocomplete="email"]',
            'input[placeholder*="email" i]'], email)
        if nparts:
            await self._fill_first(page, [
                'input[name*="first" i]', 'input[placeholder*="first" i]',
                'input[autocomplete="given-name"]'], nparts[0])
            await self._fill_first(page, [
                'input[name*="last" i]', 'input[placeholder*="last" i]',
                'input[autocomplete="family-name"]'], nparts[-1] if len(nparts) > 1 else nparts[0])
        await self._fill_first(page, [
            'input[name*="name" i]:not([name*="first" i]):not([name*="last" i])',
            'input[autocomplete="name"]', 'input[placeholder*="full name" i]'], name)
        try:
            boxes = page.locator('input[type="password"]')
            for i in range(await boxes.count()):
                try:
                    await boxes.nth(i).fill(pw, timeout=4000)
                except Exception:
                    continue
        except Exception:
            pass
        await self._tick_required_checkboxes(page)

        # Solve the reCAPTCHA (NopeCHA free path already runs in-browser; this is the paid escalation).
        try:
            await captcha_solver.solve_on_page(page)
        except Exception:
            pass

        await self._click_first(page, [
            'button:has-text("Create Account")', 'button:has-text("Register")',
            'button:has-text("Sign up")', 'button:has-text("Continue")',
            'button[type="submit"]', 'input[type="submit"]'])
        await page.wait_for_timeout(3000)

        # emailed OTP
        code = await self._await_email_code(email, start_ts)
        if code:
            await self._fill_first(page, [
                'input[name*="code" i]', 'input[autocomplete="one-time-code"]',
                'input[placeholder*="code" i]', 'input[inputmode="numeric"]'], code)
            await self._click_first(page, [
                'button:has-text("Verify")', 'button:has-text("Confirm")',
                'button:has-text("Continue")', 'button:has-text("Submit")',
                'button[type="submit"]'])
            await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    # ---- helpers (shared shape with the Amazon lane) ------------------------
    def _account_email(self) -> str:
        pf = getattr(self, "_pf", {}) or {}
        return (pf.get("email") or pf.get("application_email") or "").strip()

    def _persona_name(self) -> str:
        pf = getattr(self, "_pf", {}) or {}
        return (pf.get("full_name") or pf.get("name") or "").strip()

    async def _await_email_code(self, email: str, since_ts: float,
                                attempts: int = 12, interval: float = 6.0) -> str:
        try:
            from backend.tools.verify_code import read_code
        except Exception:
            return ""
        import asyncio
        for _ in range(max(1, attempts)):
            try:
                code = read_code(email, since_ts)
            except Exception:
                code = None
            if code:
                return code
            await asyncio.sleep(interval)
        return ""

    async def _fill_first(self, page: Page, selectors, value: str) -> bool:
        if not value:
            return False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                if (await loc.input_value()).strip():
                    return True
                await loc.fill(value, timeout=4000)
                return True
            except Exception:
                continue
        return False

    async def _click_first(self, page: Page, selectors) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    async def _tick_required_checkboxes(self, page: Page) -> None:
        try:
            boxes = page.locator('input[type="checkbox"]')
            for i in range(await boxes.count()):
                cb = boxes.nth(i)
                try:
                    req = await cb.evaluate("e=>e.required||e.getAttribute('aria-required')==='true'")
                    if not req or await cb.is_checked():
                        continue
                    ctx = (await cb.evaluate(
                        "e=>{const c=e.closest('div,li,fieldset,form');return c?c.innerText:'';}")
                        or "").lower()
                    if re.search(r"newsletter|marketing|promotional|subscribe|opt.?in|"
                                 r"talent community|contact you about", ctx):
                        continue
                    try:
                        await cb.check(timeout=2500)
                    except Exception:
                        await cb.evaluate(
                            "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                            "e.dispatchEvent(new Event('change',{bubbles:true}));}")
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("roberthalf: checkbox tick raised: %s", exc)

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        for name in ("Accept All Cookies", "Accept All", "Accept Cookies", "I Accept",
                     "Reject All", "Got it", "Agree"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    # ---- authenticated-form gap fill + wizard walk --------------------------
    async def _fill_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        # Deterministic truthful eligibility/CS screeners (reuse the shared, well-tested table).
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("roberthalf: screeners raised: %s", exc)

    # Reuse Amazon's proven widget + screener helpers (call, never edit shared code). The copied
    # function objects keep amazon_apply's module globals, so their `re`/`logger`/`analyze_page`
    # references still resolve; only `self` is rebound to a RobertHalfStrategy instance.
    _screener_answer = staticmethod(AmazonStrategy._screener_answer)
    _opt_match = staticmethod(AmazonStrategy._opt_match)
    _select_by_label = AmazonStrategy._select_by_label
    _click_radio = AmazonStrategy._click_radio
    _tick_acknowledge = AmazonStrategy._tick_acknowledge
    _answer_screeners = AmazonStrategy._answer_screeners
    _answer_select_screeners = AmazonStrategy._answer_select_screeners
    _answer_radio_screeners = AmazonStrategy._answer_radio_screeners
    _rescan_required = AmazonStrategy._rescan_required

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        await self._dismiss_cookie_banner(page)
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("roberthalf: step fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception:
            pass

    async def _step_signature(self, page: Page) -> str:
        try:
            return await page.evaluate(
                "()=>{const h=document.querySelector('h1,h2,legend,[class*=step-title],"
                "[class*=section-title]');return (h?h.innerText.trim().slice(0,50):'')+'|'+"
                "location.pathname;}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        try:
            for b in await page.query_selector_all(_WIZARD_BTN):
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _SUBMIT_RE.search(txt) and not _ADVANCE_RE.search(txt):
                    return b, "submit"
                if _ADVANCE_RE.search(txt):
                    return b, "advance"
            sel = await find_submit_button(page)
            if sel:
                b = await page.query_selector(sel)
                if b:
                    txt = ((await b.inner_text()) or "").strip()
                    return b, ("submit" if _SUBMIT_RE.search(txt) else "advance")
        except Exception as exc:
            logger.debug("roberthalf: primary_button raised: %s", exc)
        return None, None

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        for _ in range(7):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                try:
                    await captcha_solver.solve_on_page(page)
                except Exception:
                    pass
                report["submit_selector"] = (
                    "button:has-text('Submit application'), button:has-text('Submit'), "
                    "button[type='submit'], input[type='submit']")
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(page)
                return
            sig = await self._step_signature(page)
            try:
                await btn.click()
                await page.wait_for_timeout(2000)
            except Exception:
                break
            if await self._step_signature(page) == sig:
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)
