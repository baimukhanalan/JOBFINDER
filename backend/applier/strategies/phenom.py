"""Phenom Mass Hiring ATS family pre-fill strategy — a SPLIT family.

The Mass Hiring board discovers these employers on their Phenom career sites, but the
actual apply backend differs per employer, so this module carries two thin strategies:

  CONDUENT  (`careers.conduent.com/us/en/job/<id>`) — a Phenom career-site wrapper whose
    real ATS is Oracle HCM / Recruiting Cloud. The page JSON says `"ats":"ORACLEHCM"` and
    the apply is a login-less guest flow via Phenom HVH (`"forwardApply":"hvhapply"`,
    `"isHvhishvhjobApply":true`). Clicking "Apply" hands off to the Oracle CX apply — so
    `PhenomStrategy` reuses the whole `OracleORCStrategy` machinery (JET oj-* widgets, EEO
    decline, deterministic truthful screeners, the env-gated wizard walk) and only adds the
    Phenom → Oracle handoff on top. Phenom's own apply-studio reCAPTCHA v2 is CONFIGURED-
    BUT-OFF for Conduent (`captchaConfig.useCaptcha=false`, verified live); the Oracle HCM
    backend runs an INVISIBLE reCAPTCHA v3 itself on submit — wired via `captcha_solver`.

  HUMANA  (`humana.wd5.myworkdayjobs.com/...`) — careers.humana.com is Phenom for DISCOVERY
    only; every apply_url in the board points to Workday (tenants Humana_External_Career_Site
    + CenterWell_External_Career_Site), an account-gated multi-step form. `PhenomWorkdayStrategy`
    extends the stock `WorkdayStrategy` and adds an env-gated account-create + captcha + wizard
    walk. Its default (flag OFF) path is byte-identical to `WorkdayStrategy` — a plain /catalog
    Workday fill is untouched; only Humana's own host routes here, and only the gated path
    diverges. Humana's Create-Account step carries a per-tenant reCAPTCHA v2 / v2-Enterprise —
    also wired via `captcha_solver`.

Like every strategy in this repo, NOTHING here clicks the FINAL Submit — each fills and
STOPS, recording the submit selector. The application is transmitted only when that button
is pressed (by the co-pilot's gated auto-submit, or a human). Walking either wizard past the
first step is itself gated behind a per-ATS env flag (`PHENOM_ADVANCE` / `WORKDAY_ADVANCE`),
so a plain fill / dry-run is entirely side-effect-free at the employer.
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
from backend.applier.strategies.oracle_orc import OracleORCStrategy
from backend.applier.strategies.workday import WorkdayStrategy

logger = logging.getLogger(__name__)


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_advance() -> bool:
    """True only when the Conduent/Phenom→Oracle wizard walk is explicitly enabled — the
    live-submit switch that lets the strategy advance past step 1 (which transmits PII, and
    the final Submit sends the application). OFF by default so a plain fill / dry-run stays
    side-effect-free. `PHENOM_ADVANCE` is the per-ATS gate; `ORC_ADVANCE` also enables it so
    the shared Mass Hiring batch (which sets ORC_ADVANCE) walks a Conduent job too."""
    return _truthy("PHENOM_ADVANCE") or _truthy("ORC_ADVANCE")


def _env_workday_advance() -> bool:
    """True only when the Humana/Phenom→Workday account-create + wizard walk is explicitly
    enabled. OFF by default so `PhenomWorkdayStrategy` behaves byte-identically to the stock
    `WorkdayStrategy` (stop at the account gate). `WORKDAY_ADVANCE` is the per-ATS gate;
    `PHENOM_ADVANCE` also enables it so one batch env walks either Phenom sub-family."""
    return _truthy("WORKDAY_ADVANCE") or _truthy("PHENOM_ADVANCE")


def _gen_password() -> str:
    """A strong password satisfying typical ATS complexity (upper+lower+digit+symbol)."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Jf{body}9!"


# Phenom career-site wrappers whose apply hands off to an Oracle HCM guest flow. Add another
# Phenom→Oracle tenant here (verified `"ats":"ORACLEHCM"` + hvhapply) to route it the same way.
_PHENOM_ORACLE_HOSTS = ("careers.conduent.com",)


class PhenomStrategy(OracleORCStrategy):
    """Conduent: a Phenom wrapper over an Oracle HCM guest apply. Inherits Oracle CX's JET
    widget fill, EEO decline, deterministic screeners and env-gated wizard walk; adds the
    Phenom "Apply" (HVH) → Oracle CX handoff and pre-solves the Oracle submit captcha."""

    name = "phenom"
    # Walk the wizard past step 1 only when PHENOM_ADVANCE/ORC_ADVANCE is set — see _env_advance.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        return any(h in u for h in _PHENOM_ORACLE_HOSTS)

    async def open_form(self, page: Page) -> None:
        # The apply URL IS the Phenom job page; the co-pilot already navigated here. Dismiss
        # the cookie banner FIRST (before any fill / click), then take the Phenom "Apply"
        # (HVH guest) handoff, then — if it landed on the Oracle CX site — run Oracle's own
        # Apply-click so the shared pipeline fills onto the real form.
        await self._dismiss_cookie_banner(page)
        try:
            await self._phenom_apply_handoff(page)
        except Exception as exc:
            logger.debug("phenom: apply handoff raised: %s", exc)
        self._on_oracle = "oraclecloud.com" in (page.url or "").lower()
        if self._on_oracle:
            try:
                await super().open_form(page)   # OracleORCStrategy.open_form
            except Exception as exc:
                logger.debug("phenom: oracle open_form raised: %s", exc)
        else:
            # Inline Phenom apply-studio (HVH renders the form in place instead of redirecting).
            # The Oracle JET gap-fills below simply no-op on this DOM; base.prefill + the generic
            # demographic-decline + radio-screener passes still fill it. A late cookie/consent
            # overlay on the just-opened apply form is dismissed here too.
            await self._dismiss_cookie_banner(page)

    async def _phenom_apply_handoff(self, page: Page) -> None:
        """Click the Phenom "Apply" button (HVH guest apply). Phenom marks the control with a
        `data-ph-at-id` and/or a plain "Apply" label; the click either redirects to the Oracle
        CX site or opens the apply-studio form inline. Then take any guest chooser."""
        clicked = False
        for sel in ('[data-ph-at-id="apply-button"]', '[data-ph-at-id="job-apply-button"]',
                    'a[data-ph-at-id*="apply" i]', 'button[data-ph-at-id*="apply" i]',
                    'a.apply-button', 'button.apply-button',
                    'a:has-text("Apply Now")', 'button:has-text("Apply Now")',
                    'a:has-text("Apply")', 'button:has-text("Apply")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=1000):
                    await btn.click()
                    # HVH apply may redirect to Oracle CX OR render the apply-studio inline —
                    # allow time for either, and for a possible cross-site navigation to settle.
                    await page.wait_for_timeout(3500)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return
        # Some Phenom HVH flows show a guest chooser ("Apply as Guest" / "Continue without an
        # account") before the form — take the guest/manual path (no third-party login).
        for sel in ('button:has-text("Apply as Guest")', 'a:has-text("Apply as Guest")',
                    'button:has-text("Continue as Guest")', 'button:has-text("Apply without")',
                    'button:has-text("Continue")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # OracleORCStrategy.prefill runs our open_form (the Phenom handoff), then the shared
        # pipeline (identity/résumé), the Oracle JET gap-fills + EEO decline + deterministic
        # screeners, and — when advance_wizard is on — walks the wizard to the recorded Submit.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["strategy"] = self.name
        report["phenom_backend"] = "oraclehcm"
        # Captcha at the submit step: Phenom's apply-studio reCAPTCHA v2 is OFF for Conduent,
        # and the Oracle HCM backend runs an INVISIBLE reCAPTCHA v3 on submit. When the wizard
        # has advanced to the final Submit (advance_wizard on), pre-solve+inject any captcha
        # token so the co-pilot's Submit click passes. Graceful no-op without CAPTCHA_SOLVER_KEY,
        # so a dry run is unaffected; the co-pilot re-checks at the actual click.
        if self.advance_wizard and (report.get("wizard_at_submit") or report.get("submit_selector")):
            try:
                report["captcha_solved"] = await captcha_solver.solve_on_page(page)
            except Exception as exc:
                logger.debug("phenom: captcha solve raised: %s", exc)
        return report


# Workday wizard controls (stable data-automation-ids across tenants) + a text fallback.
_WD_NEXT = re.compile(r"^\s*(continue|next|save (and|&) continue|review)\s*$", re.I)
_WD_SUBMIT = re.compile(r"submit|finish|complete|send application", re.I)
_WD_NAV_BTN = ("button[data-automation-id='bottom-navigation-next-button'], "
               "button[data-automation-id='pageFooterNextButton'], "
               "button[data-automation-id='wd-CommandButton'], button")
# Humana workday hosts (both Humana_External_Career_Site + CenterWell_External_Career_Site
# live under this tenant). Kept SEPARATE from the generic WorkdayStrategy so only Humana's
# own host routes to the gated account-create path — every other Workday URL is untouched.
_PHENOM_WORKDAY_HOST_RE = re.compile(r"humana\.[a-z0-9]+\.myworkdayjobs\.com", re.I)


class PhenomWorkdayStrategy(WorkdayStrategy):
    """Humana: careers.humana.com is Phenom for discovery, but the apply is Workday. Extends
    the stock WorkdayStrategy with an env-gated account-create (captcha-wired) + wizard walk.
    Default (WORKDAY_ADVANCE off) is byte-identical to WorkdayStrategy."""

    name = "phenom_workday"
    # Create the account + walk the wizard only when WORKDAY_ADVANCE/PHENOM_ADVANCE is set.
    advance_wizard = _env_workday_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(_PHENOM_WORKDAY_HOST_RE.search(url or ""))

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # DEFAULT PATH (gate off): behave EXACTLY like the stock WorkdayStrategy — a plain
        # /catalog Workday fill must be untouched. super() resolves to the shared base.prefill
        # (WorkdayStrategy has no prefill override), which calls our open_form (identical clicks)
        # and stops at the account gate as login_required.
        if not self.advance_wizard:
            return await super().prefill(
                page, profile_form, resume_path, cover_letter=cover_letter, job=job,
                draft=draft, resume_summary=resume_summary, known_answers=known_answers,
                facts=facts, profile_id=profile_id, niche=niche,
                resume_parser_only=resume_parser_only)

        # GATED LIVE PATH: Apply → Create Account (captcha) → fill → walk the wizard, recording
        # the final Submit WITHOUT clicking it. Every step is best-effort/try-except so a DOM
        # drift never raises into the caller.
        try:
            await self.open_form(page)   # Workday "Apply" / "Apply Manually"
        except Exception as exc:
            logger.debug("phenom_workday: open_form raised: %s", exc)
        try:
            await self._create_account(page, profile_form)
        except Exception as exc:
            logger.debug("phenom_workday: create_account raised: %s", exc)

        # The shared pipeline fills whatever is now reachable (base.prefill re-runs open_form,
        # which no-ops once the Apply button is gone). It returns login_required if the account
        # gate is still up (e.g. no captcha key) — that is the honest "needs the captcha key"
        # signal, surfaced below.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["strategy"] = self.name
        report["phenom_backend"] = "workday"
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            report["note"] = ("phenom_workday: account gate not cleared "
                              "(needs CAPTCHA_SOLVER_KEY + a residential IP)")
            return report
        try:
            await self._fill_workday_gaps(page, profile_form, facts)
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("phenom_workday: gap fill raised: %s", exc)
        try:
            await self._advance_wizard(page, report, profile_form, cover_letter, facts)
        except Exception as exc:
            logger.debug("phenom_workday: wizard advance raised: %s", exc)
        return report

    async def _create_account(self, page: Page, profile_form: dict) -> bool:
        """Best-effort Workday account creation for a synthetic persona. Click "Create Account",
        fill email (the persona's live @takhet.com box) + password (twice), solve the per-tenant
        reCAPTCHA v2/Enterprise on this step via the solver (no-op without a key), and submit.
        A verify-email link (if the tenant sends one) is finished downstream by the co-pilot's
        emailed-code watcher, exactly like the Oracle PIN / GH-Ashby security code."""
        email = (profile_form or {}).get("email") or ""
        if not email:
            return False
        # Switch from the Sign-In panel to Create-Account when Workday defaults to sign-in.
        for sel in ('[data-automation-id="createAccountLink"]',
                    'button[data-automation-id="createAccountLink"]',
                    'a:has-text("Create Account")', 'button:has-text("Create Account")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=1000):
                    await b.click()
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        await self._fill_first(page, ('input[data-automation-id="email"]',
                                      'input[type="email"]'), email)
        await self._fill_first(page, ('input[data-automation-id="password"]',), pw)
        await self._fill_first(page, ('input[data-automation-id="verifyPassword"]',
                                      'input[data-automation-id="confirmPassword"]'), pw)
        # Required "I have read and agree..." create-account checkbox, if present.
        try:
            await fill_required_consent(page)
        except Exception:
            pass
        # Solve the account-create reCAPTCHA (v2 / v2-Enterprise) and inject the token BEFORE
        # submitting the account. Graceful no-op without CAPTCHA_SOLVER_KEY.
        try:
            await captcha_solver.solve_on_page(page)
        except Exception as exc:
            logger.debug("phenom_workday: create-account captcha raised: %s", exc)
        for sel in ('button[data-automation-id="createAccountSubmitButton"]',
                    'button[data-automation-id="signInSubmitButton"]',
                    'button:has-text("Create Account")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=1000):
                    await b.click()
                    await page.wait_for_timeout(3000)
                    return True
            except Exception:
                continue
        return False

    async def _fill_first(self, page: Page, selectors, value: str) -> bool:
        """Fill the first present+visible input from `selectors` with `value` (auto-waits)."""
        if not value:
            return False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.fill(value, timeout=4000)
                    return True
            except Exception:
                continue
        return False

    async def _fill_workday_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        """Decline every EEO/Voluntary-Disclosure demographic (never claiming a protected
        characteristic) and tick required consent on the current Workday step. Workday renders
        these as native selects / radios / checkboxes the shared helpers already handle."""
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty visible controls on the current step, so the report's
        `unfilled` reflects the gap fill and the co-pilot's submit gate stays honest."""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||!!el.closest('[data-automation-id="required"],[aria-required="true"]');
                    if(!req) continue;
                    let empty;
                    if(t==='checkbox'||t==='radio'){const nm=el.name;
                      empty=nm?![...document.querySelectorAll('[name="'+
                        (window.CSS&&CSS.escape?CSS.escape(nm):nm)+'"]')].some(x=>x.checked):!el.checked;}
                    else empty=!(el.value||'').trim();
                    if(!empty) continue;
                    let lab='';const id=el.id;
                    if(id){const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab){const l=el.closest('label')||
                      (el.parentElement&&el.parentElement.querySelector('label'));if(l)lab=l.innerText.trim();}
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||(el.name||'field');
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  } return out;}""")
        except Exception:
            return []

    async def _step_signature(self, page: Page) -> str:
        """Cheap fingerprint of the current Workday step (Workday re-renders in place, often
        the same URL) — to tell whether a Continue click actually advanced."""
        try:
            return await page.evaluate(
                "()=>{const h=document.querySelector("
                "'[data-automation-id=\"jobApplicationProgressBarActiveStep\"],h1,h2,legend');"
                "return h?h.innerText.trim().slice(0,50):(location.href||'');}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """(handle, kind) for the step's primary button: 'submit' on the final Review step,
        'advance' on Continue/Next/Save-and-Continue, else None."""
        try:
            for b in await page.query_selector_all(_WD_NAV_BTN):
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _WD_SUBMIT.search(txt) and not _WD_NEXT.search(txt):
                    return b, "submit"
                if _WD_NEXT.search(txt):
                    return b, "advance"
            sel = await find_submit_button(page)
            if sel:
                b = await page.query_selector(sel)
                if b:
                    txt = ((await b.inner_text()) or "").strip()
                    return b, ("submit" if _WD_SUBMIT.search(txt) else "advance")
        except Exception as exc:
            logger.debug("phenom_workday: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        """Fill an ordinary / EEO / review Workday step: decline demographics + required
        consent, then fill any matched fields the analyzer recognizes."""
        await self._fill_workday_gaps(page, profile_form, facts)
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("phenom_workday: step fill raised: %s", exc)
        await self._fill_workday_gaps(page, profile_form, facts)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the Workday task flow (My Information → Experience → Application Questions →
        Voluntary Disclosures → Self-Identify → Review), clicking Continue while it advances
        and filling each new step. STOP at the final Submit — recording its selector WITHOUT
        clicking it, after pre-solving any captcha that reappears there. If a Continue click
        does NOT advance (a required field is still empty), stop and leave the gaps in
        `unfilled` for the human / next iteration."""
        for _ in range(8):
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                # A per-tenant reCAPTCHA can reappear on the final submit — pre-solve+inject.
                try:
                    report["captcha_solved"] = await captcha_solver.solve_on_page(page)
                except Exception as exc:
                    logger.debug("phenom_workday: submit captcha raised: %s", exc)
                report["submit_selector"] = (
                    "button[data-automation-id='bottom-navigation-next-button'], "
                    "button[data-automation-id='pageFooterNextButton'], "
                    "button:has-text('Submit')")
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(page)
                return
            sig = await self._step_signature(page)
            try:
                await btn.click()
                await page.wait_for_timeout(2500)
            except Exception:
                break
            if await self._step_signature(page) == sig:
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)
