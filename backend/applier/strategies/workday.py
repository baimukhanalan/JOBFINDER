"""Workday ATS pre-fill (+ Mass-Hiring auto-apply) strategy.

Workday hosts applications on {tenant}.myworkdayjobs.com (CxS) as a multi-step wizard. This
module carries TWO classes:

  * WorkdayStrategy — the STOCK strategy (unchanged /catalog behaviour). It matches every
    Workday host, clicks Apply toward the form, and when Workday gates it behind account
    creation / sign-in the analyzer reports page_type=login_required and the run stops cleanly
    for a human (the semi-auto, human-submit model). First-page fields that ARE reachable get
    pre-filled. It has NO `prefill` override — `strategy.prefill` resolves to the shared
    base.ApplyStrategy pipeline, exactly as before. On top of the original stub it now also
    carries the Workday-specific gap-fill / wizard-walk HELPER methods (account creation,
    button[aria-haspopup=listbox] selects, questionnaire radios, demographic decline, the
    wizard walker). Those helpers are inert for a plain /catalog fill (open_form/matches/
    inherited prefill are byte-identical to the original stub) but are shared by the subclasses
    below (this one and PhenomWorkdayStrategy in strategies/phenom.py).

  * WorkdayMassHiringStrategy — the MASS-HIRING auto-apply strategy for the four validated
    tenants (Concentrix / CVS Health / Centene / Cigna — all one CxS ATS). Registered BEFORE
    WorkdayStrategy so those tenants route here. Gated behind env WORKDAY_ADVANCE (default OFF —
    see _env_advance): OFF it is byte-identical to the shared pipeline (fill reachable, stop at
    the account gate); ON (set by the Mass Hiring batch) it creates the guest account inline
    (email + generated password), solving the register-step reCAPTCHA via the shared captcha
    solver — that register step is the ONLY captcha in the Workday flow; the final CxS Submit
    carries none — then walks the wizard (My Information → My Experience → Application Questions
    → Voluntary Disclosures / Self Identify → Review), filling every step, and STOPS at the
    final Submit, RECORDING its selector WITHOUT clicking it (the co-pilot's gated auto-submit /
    a human clicks it). The post-submit SHL / Modern-Hire / HireVue assessment that gates the
    HIRE is an email-invited stage and does NOT block the application submit — out of scope.

Mirrors the Avature / Oracle-ORC reference strategies. A plain fill / dry-run is entirely
side-effect-free at the employer: nothing clicks the final Submit, and walking the wizard
(which transmits PII + creates the account) only happens under WORKDAY_ADVANCE.
"""
import logging
import os
import re
import secrets
import time

from playwright.async_api import Page

from backend.applier import captcha_solver
from backend.applier.analyzer import analyze_page, find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.filler import fill_form
from backend.applier.strategies.base import ApplyStrategy

logger = logging.getLogger(__name__)

# Workday CxS renders the wizard's primary button as a
# <button data-automation-id="pageFooterNextButton"> (older tenants:
# "bottom-navigation-next-button") whose LABEL is "Save and Continue"/"Continue"/"Next" on
# intermediate steps and "Submit" on the final Review step. We advance on continue/next and
# STOP (record the selector) on submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review|save)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
# Workday's canonical footer action button first, then plain <button>/role=button fallbacks.
_FOOTER_BTNS = (
    'button[data-automation-id="pageFooterNextButton"]',
    'button[data-automation-id="bottom-navigation-next-button"]',
    'button[data-automation-id="wizardSubmitButton"]',
)
_WIZARD_BTN = ("button[data-automation-id='pageFooterNextButton'], "
               "button[data-automation-id='bottom-navigation-next-button'], "
               "button, a[role='button']")
# The four validated Mass-Hiring Workday tenants (Concentrix / CVS Health / Centene / Cigna).
# Precise on purpose (NOT a blanket myworkdayjobs.com) so only vetted tenants route to the
# account-create + wizard-walk path — every other Workday URL falls through to WorkdayStrategy.
_MASSHIRING_HOST_RE = re.compile(
    r"(?:cnx\.wd1|cvshealth\.wd1|centene\.wd5|cigna\.wd5)\.myworkdayjobs\.com", re.I)
# A representative, real, in-state ZIP per US state (a major-city ZIP). Workday validates
# 'X is not a valid postal code for <State>', so a synthetic persona's random ZIP (which may not
# belong to its state) is overridden with the state's ZIP here to keep postal-vs-state consistent.
_US_STATE_ZIP = {
    "alabama": "35203", "alaska": "99501", "arizona": "85004", "arkansas": "72201",
    "california": "90012", "colorado": "80202", "connecticut": "06103", "delaware": "19801",
    "florida": "32202", "georgia": "30303", "hawaii": "96813", "idaho": "83702",
    "illinois": "60602", "indiana": "46204", "iowa": "50309", "kansas": "66603",
    "kentucky": "40507", "louisiana": "70112", "maine": "04101", "maryland": "21201",
    "massachusetts": "02108", "michigan": "48226", "minnesota": "55401", "mississippi": "39201",
    "missouri": "63101", "montana": "59601", "nebraska": "68102", "nevada": "89101",
    "new hampshire": "03301", "new jersey": "07102", "new mexico": "87501", "new york": "10007",
    "north carolina": "27601", "north dakota": "58501", "ohio": "43215", "oklahoma": "73102",
    "oregon": "97204", "pennsylvania": "19107", "rhode island": "02903", "south carolina": "29201",
    "south dakota": "57501", "tennessee": "37219", "texas": "78701", "utah": "84111",
    "vermont": "05602", "virginia": "23219", "washington": "98104", "west virginia": "25301",
    "wisconsin": "53703", "wyoming": "82001", "district of columbia": "20001",
}
# Workday demographic (Voluntary Disclosures / Self-Identify) fields the shared
# fill_demographics_decline can't reach: they're button[aria-haspopup=listbox] selects, not
# react-selects / native <select> / radios / role=combobox. Decline them here.
_DEMOGRAPHIC_RE = re.compile(
    r"gender|rac(e|ial)|ethnic|hispanic|latin[ox]?\b(?!\s*americ)|disabilit|veteran|"
    r"armed forces|self-?identif|self-?classif|sexual orientation|pronoun", re.I)
# Non-disclosure option wording, used when declining a demographic select.
_DECLINE_VALUES = ("I do not wish to answer", "I don't wish to answer",
                   "I do not want to answer", "Prefer not to answer",
                   "Prefer not to say", "Decline to self-identify",
                   "Decline to answer", "Choose not to disclose", "Do not wish")


def _env_advance() -> bool:
    """True only when WORKDAY_ADVANCE is explicitly set — the live-submit switch that lets the
    mass-hiring strategy create the guest account and walk the wizard past step 1 (which
    transmits PII, and the final Submit sends the application). OFF by default: a plain fill
    (co-pilot dry-run / human review) stays entirely side-effect-free at the employer AND the
    /catalog Workday path is byte-identical to the original login-gate handoff. Mirrors Avature's
    AVATURE_ADVANCE and Oracle-ORC's ORC_ADVANCE gates."""
    return os.getenv("WORKDAY_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _gen_password() -> str:
    """A strong password that satisfies typical ATS complexity (upper+lower+digit+symbol)."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Jf{body}9!"


def _workday_activation_link(email: str, since_ts: float) -> str | None:
    """The Workday '/activate/<token>' candidate-account activation URL from the persona's Maildir
    (the 'Verify your candidate account' mail from *.otp.workday.com), received at/after since_ts."""
    import email as _email
    import glob as _glob
    import os as _os
    import re as _re
    local = (email or "").split("@", 1)[0]
    if not local:
        return None
    base = f"/var/mail/vhosts/takhet.com/{local}"
    files = _glob.glob(base + "/new/*") + _glob.glob(base + "/cur/*")
    for f in sorted(files, key=lambda p: _os.path.getmtime(p) if _os.path.exists(p) else 0, reverse=True):
        try:
            if _os.path.getmtime(f) < since_ts - 30:
                continue
            m = _email.message_from_binary_file(open(f, "rb"))
        except Exception:
            continue
        if "workday" not in str(m.get("From", "")).lower():
            continue
        body = ""
        for p in (m.walk() if m.is_multipart() else [m]):
            if p.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += p.get_payload(decode=True).decode("utf-8", "ignore")
                except Exception:
                    pass
        mm = _re.search(
            r"https?://[a-z0-9.]*myworkdayjobs\.com/[^\s\"'<>]*/activate/[^\s\"'<>]+", body, _re.I)
        if mm:
            return mm.group(0)
    return None


class WorkdayStrategy(ApplyStrategy):
    """Stock Workday strategy — unchanged /catalog behaviour + shared gap-fill / wizard-walk
    helpers (inherited by WorkdayMassHiringStrategy and phenom.PhenomWorkdayStrategy). It
    deliberately has NO `prefill` override, so a plain fill resolves to the shared base
    pipeline and stops at the account gate, exactly as before."""
    name = "workday"
    # Default for subclasses that don't set their own. The stock class has no prefill override,
    # so this attribute is inert for a /catalog fill (base.prefill never reads it).
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        return "myworkdayjobs.com" in u or ".workday.com" in u

    async def open_form(self, page: Page) -> None:
        # Workday's "Apply" then (sometimes) "Apply Manually" reveal the form / account gate.
        for sel in [
            'a[data-automation-id="adventureButton"]',
            'button:has-text("Apply Manually")', 'a:has-text("Apply Manually")',
            'button:has-text("Apply")', 'a:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(3000)  # multi-step SPA
                    break
            except Exception:
                continue

    # ---- account creation (used by the mass-hiring subclass; WORKDAY_ADVANCE=1) --------------
    async def _start_and_create_account(self, page: Page) -> None:
        """From the job page's post-Apply state, take the guest 'Apply Manually' path and create
        the account: fill email + password, tick the required Terms box, solve the register-step
        reCAPTCHA (the only captcha in the flow), and submit. Then handle an emailed verification
        code if the tenant requires one. Best-effort throughout — a failure just leaves the gate
        up, which the shared pipeline reports as login_required (stops for a human)."""
        await self._dismiss_cookie_banner(page)
        # 1) The "Start Your Application" chooser: prefer the guest email path (Apply Manually);
        #    fall back to Autofill-with-Resume (also guest). Deliberately NO generic "Continue"
        #    so we never advance past a step we haven't filled.
        for sel in ('[data-automation-id="applyManually"]',
                    'a:has-text("Apply Manually")', 'button:has-text("Apply Manually")',
                    '[data-automation-id="autofillWithResume"]'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=1200):
                    await b.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
        await self._dismiss_cookie_banner(page)
        await self._dbg_shot(page, "05_after_apply_manually")
        # Workday renders the sign-in / create-account form via XHR after the Apply-Manually
        # navigation — wait for it to hydrate (email field OR the Create Account toggle) before we
        # decide which form is showing, else we fill a blank shell.
        try:
            await page.wait_for_selector(
                'input[data-automation-id="email"], input[type="email"], '
                'button[data-automation-id="createAccountLink"], a[data-automation-id="createAccountLink"]',
                timeout=15000, state="visible")
        except Exception:
            pass
        # 2) The sign-in page defaults to "Sign In"; switch to the Create Account form ONLY when
        #    its email field isn't already showing (some tenants land straight on it). Clicking
        #    the toggle when the form is already visible would fire the SUBMIT button instead.
        if not await self._email_field_visible(page):
            for sel in ('button[data-automation-id="createAccountLink"]',
                        'a[data-automation-id="createAccountLink"]',
                        'button:has-text("Create Account")', 'a:has-text("Create Account")'):
                try:
                    b = page.locator(sel).first
                    if await b.count() and await b.is_visible(timeout=1000):
                        await b.click()
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
        # 3) Fill the create-account form, solve the captcha, and submit it.
        await self._create_account(page)

    async def _email_field_visible(self, page: Page) -> bool:
        for sel in ('input[data-automation-id="email"]', 'input[type="email"]'):
            try:
                e = page.locator(sel).first
                if await e.count() and await e.is_visible(timeout=600):
                    return True
            except Exception:
                continue
        return False

    async def _dbg_shot(self, page: Page, tag: str) -> None:
        """Debug screenshot of the live account/wizard step — only when WORKDAY_DEBUG_SHOTS is set,
        so it's a no-op in production. Writes to logs/workday_recon/_debug/<tag>.png."""
        if not os.getenv("WORKDAY_DEBUG_SHOTS"):
            return
        try:
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "logs", "workday_recon", "_debug")
            os.makedirs(d, exist_ok=True)
            await page.screenshot(path=os.path.join(d, f"{tag}.png"), full_page=False)
            logger.info("workday dbg shot %s url=%s", tag, page.url[:90])
        except Exception:
            pass

    async def _create_account(self, page: Page, profile_form: dict | None = None) -> None:
        """Fill the Workday create-account form (email + password twice + required Terms box),
        solve the register-step reCAPTCHA (the ONLY captcha in the flow), submit, and handle an
        emailed verification code if the tenant sends one. `profile_form` defaults to the one the
        subclass stashed on `self` in prefill, so it works whether called with or without it."""
        pf = profile_form if profile_form is not None else getattr(self, "_profile_form", {})
        email = (pf or {}).get("email") or ""
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        # wait for the create-account form to actually render (SPA) before filling a blank shell
        try:
            await page.wait_for_selector('input[data-automation-id="email"], input[type="email"]',
                                         timeout=12000, state="visible")
        except Exception:
            pass
        await self._dbg_shot(page, "06_create_account_form")
        if os.getenv("WORKDAY_DEBUG_SHOTS"):
            try:
                dump = await page.evaluate(
                    """()=>[...document.querySelectorAll('input,button')].filter(e=>e.offsetParent)"""
                    """.map(e=>({tag:e.tagName,type:e.type,aid:e.getAttribute('data-automation-id'),"""
                    """name:e.name,ph:e.placeholder,txt:(e.innerText||'').slice(0,20)}))""")
                logger.info("workday create-account controls: %s", str(dump)[:900])
            except Exception:
                pass
        # email — data-automation-id first, then a robust fallback by type/name/label proximity
        for sel in ('input[data-automation-id="email"]', 'input[type="email"]',
                    'input[name="email" i]', 'input[autocomplete="username"]',
                    'input[aria-label*="Email" i]'):
            try:
                e = page.locator(sel).first
                if await e.count() and await e.is_visible(timeout=1200):
                    await e.fill(email, timeout=4000)
                    break
            except Exception:
                continue
        # password + verify password (Workday's two inputs), with a generic fallback.
        filled_pw = 0
        for sel in ('input[data-automation-id="password"]',
                    'input[data-automation-id="verifyPassword"]',
                    'input[data-automation-id="confirmPassword"]'):
            try:
                e = page.locator(sel).first
                if await e.count() and await e.is_visible(timeout=800):
                    await e.fill(pw, timeout=4000)
                    filled_pw += 1
            except Exception:
                continue
        if filled_pw == 0:
            try:
                boxes = page.locator('input[type="password"]')
                for i in range(await boxes.count()):
                    try:
                        await boxes.nth(i).fill(pw, timeout=3000)
                    except Exception:
                        continue
            except Exception:
                pass
        # required Terms / "I agree" checkbox (its label is often just a link, so a text
        # consent-matcher misses it — tick every required non-marketing box).
        await self._tick_required_checkboxes(page)
        # The register step is the ONLY captcha in the Workday flow (reCAPTCHA v2-checkbox /
        # Enterprise, per-tenant). Solve+inject it BEFORE the Create Account click — a graceful
        # no-op when CAPTCHA_SOLVER_KEY is unset, so a dry-run without a key never breaks (it
        # just leaves the gate up and the run stops for a human).
        try:
            await captcha_solver.solve_on_page(page)
        except Exception as exc:
            logger.debug("workday: captcha solve raised: %s", exc)
        # record the moment we (attempt to) create the account, so the emailed verification-code
        # reader only considers mail that arrives AFTER this point.
        self._acct_ts = time.time()
        try:
            rc = await page.evaluate(
                "()=>({grecaptcha:!!window.grecaptcha,"
                "enterprise:!!(window.grecaptcha&&window.grecaptcha.enterprise),"
                "frames:[...document.querySelectorAll('iframe')].map(f=>f.src)"
                ".filter(s=>/recaptcha|hcaptcha|turnstile/i.test(s)).length,"
                "badge:!!document.querySelector('.grecaptcha-badge'),"
                "sitekey:(document.querySelector('[data-sitekey]')||{}).getAttribute?"
                "document.querySelector('[data-sitekey]').getAttribute('data-sitekey'):null})")
            logger.info("workday register captcha presence: %s", rc)
        except Exception:
            pass
        created = False
        for attempt in range(3):
            for sel in ('button[data-automation-id="createAccountSubmitButton"]',
                        'button[data-automation-id="click_filter"]',
                        'button:has-text("Create Account")'):
                try:
                    b = page.locator(sel).first
                    if await b.count():
                        # The Create Account button sits BELOW the fold on Workday's create-account
                        # form, so a plain .click() times out waiting for it to be actionable
                        # (elementFromPoint at its centre is null = off-screen). Scroll it into view
                        # and force-click — this is what actually submits (verified live 2026-09-01).
                        try:
                            await b.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        try:
                            await b.click(force=True, timeout=5000)
                        except Exception:
                            # last-resort JS click if even the force click can't land
                            try:
                                await page.evaluate(
                                    "()=>{const b=document.querySelector"
                                    "('button[data-automation-id=\"createAccountSubmitButton\"]');"
                                    "if(b)b.click();}")
                            except Exception:
                                pass
                        break
                except Exception:
                    continue
            # wait up to ~25s for the account to be created (the create-account email field
            # disappears / the wizard advances) — gives the NopeCHA extension time to solve an
            # invisible reCAPTCHA-Enterprise that gates the submit (no visible checkbox on this form).
            for _ in range(25):
                await page.wait_for_timeout(1000)
                if not await self._email_field_visible(page):
                    created = True
                    break
            if created:
                break
            try:
                err = await page.evaluate(
                    "()=>{const e=[...document.querySelectorAll("
                    "'[data-automation-id*=error i],[role=alert],.gwt-Label,[class*=error i]')]"
                    ".map(x=>(x.innerText||'').trim()).filter(Boolean); return e.slice(0,4).join(' | ');}")
                if err:
                    logger.info("workday create-account still up after attempt %d: %s", attempt + 1, err[:220])
            except Exception:
                pass
            await self._dbg_shot(page, f"07_create_attempt{attempt + 1}")
        logger.info("workday create-account: created=%s", created)
        await self._dbg_shot(page, "08_after_create")
        # Some tenants email a verification code before releasing the wizard.
        try:
            await self._verify_email_if_needed(page, email)
        except Exception as exc:
            logger.debug("workday: email verify raised: %s", exc)
        # Centene/Workday instead emails an ACTIVATION LINK ('Verify your candidate account') and
        # redirects to /login — fetch the link from the persona's Maildir, confirm the email, then
        # sign in with the account we just created so the application wizard is released.
        try:
            await self._activate_and_signin(page, email)
        except Exception as exc:
            logger.debug("workday: activate/signin raised: %s", exc)

    async def _verify_email_if_needed(self, page: Page, email: str) -> None:
        """If the create-account step shows an email-verification code input, read the code from
        the persona's OWN Maildir (verify_code — email-control only, never a captcha) and submit
        it. Best-effort and live-only: the mailbox + `mail` group are required, so a dry run
        without them simply leaves the step for the human / the co-pilot's emailed-code watcher."""
        code_input = None
        for sel in ('input[data-automation-id="verificationCode"]',
                    'input[data-automation-id="code"]',
                    'input[name*="verification" i]', 'input[name*="code" i]'):
            try:
                e = page.locator(sel).first
                if await e.count() and await e.is_visible(timeout=800):
                    code_input = e
                    break
            except Exception:
                continue
        if code_input is None or not email:
            return
        import asyncio

        from backend.tools import verify_code
        since = getattr(self, "_acct_ts", 0.0)
        code = None
        for _ in range(8):                       # ~40s; the code email lands in a minute or so
            code = verify_code.read_code(email, since_ts=since)
            if code:
                break
            await asyncio.sleep(5)
        if not code:
            return
        try:
            await code_input.fill(code, timeout=4000)
        except Exception:
            return
        for sel in ('button[data-automation-id="click_filter"]',
                    'button:has-text("Verify")', 'button:has-text("Submit")',
                    'button:has-text("Continue")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=800):
                    await b.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

    async def _activate_and_signin(self, page: Page, email: str) -> None:
        """After create-account, Centene/Workday redirects to /login and emails an ACTIVATION LINK
        ('Verify your candidate account'). Fetch the link from the persona's Maildir, navigate to it
        to confirm the email, then sign in with the account we just created so the wizard is released."""
        if not email:
            return
        import asyncio
        since = getattr(self, "_acct_ts", 0.0) - 60
        link = None
        for _ in range(10):                      # ~100s — the activation email lands within a minute
            link = _workday_activation_link(email, since)
            if link:
                break
            await asyncio.sleep(10)
        if link:
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3500)
                logger.info("workday: activated candidate account via email link")
            except Exception as exc:
                logger.debug("workday: activation-link nav raised: %s", exc)
        await self._dbg_shot(page, "09_after_activate")
        await self._sign_in(page, email)

    async def _sign_in(self, page: Page, email: str) -> None:
        """Sign in with the just-created account (email + self._account_pw) so the wizard is released.
        Best-effort; no-op unless a sign-in form (email field + Sign In button) is present."""
        pw = getattr(self, "_account_pw", None)
        if not pw:
            return
        # The activation link lands on /login (or /login/ok) whose inputs hydrate via XHR — WAIT for
        # the email field to render before touching it (the old instant check saw an empty shell and
        # bailed, so base.prefill then read the page as login_required and returned early).
        email_sels = ('input[data-automation-id="email"]', 'input[data-automation-id="userName"]',
                      'input[type="email"]', 'input[name="username" i]')
        try:
            await page.wait_for_selector(", ".join(email_sels), timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        filled_email = False
        for sel in email_sels:
            try:
                e = page.locator(sel).first
                if await e.count() and await e.is_visible(timeout=1000):
                    await e.fill(email, timeout=4000)
                    filled_email = True
                    break
            except Exception:
                continue
        if not filled_email:
            await self._dbg_shot(page, "10_after_signin")
            return
        for sel in ('input[data-automation-id="password"]', 'input[type="password"]'):
            try:
                e = page.locator(sel).first
                if await e.count() and await e.is_visible(timeout=1000):
                    await e.fill(pw, timeout=4000)
                    break
            except Exception:
                continue
        for sel in ('button[data-automation-id="signInSubmitButton"]',
                    'button[data-automation-id="click_filter"]',
                    'button:has-text("Sign In")'):
            try:
                b = page.locator(sel).first
                if await b.count():
                    try:
                        await b.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    await b.click(force=True, timeout=5000)
                    break
            except Exception:
                continue
        # Sign-in navigates to the redirect target (the application wizard) — give the SPA time to
        # leave /login and hydrate the first wizard step before base.prefill analyzes it.
        try:
            await page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        await self._dbg_shot(page, "10_after_signin")

    # ---- Workday-specific gap fill (label/role driven so it generalizes across tenants) ----
    async def _fill_workday_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # Country/State are button[aria-haspopup=listbox] selects the analyzer can't fill;
        # Country first (State's options are Country-dependent and load after it's set).
        try:
            await self._fill_wd_select(page, "country", ["United States", "United States of America"])
        except Exception:
            pass
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                await self._fill_wd_select(page, "state", [state])
            except Exception:
                pass
        # EEO / diversity self-ID + required legal consent. The shared helpers handle
        # react-selects / native <select> / radios / role=combobox; Workday's demographic
        # button[aria-haspopup=listbox] selects need the WD-specific decline below.
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        try:
            await self._decline_wd_demographics(page)
        except Exception as exc:
            logger.debug("workday: wd demographic decline raised: %s", exc)
        # Pre-screening / questionnaire questions the analyzer misses (WD selects + radios),
        # answered deterministically & TRUTHFULLY for the synthetic US persona.
        await self._answer_screeners(page, facts)

    async def _answer_screeners(self, page: Page, facts) -> None:
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("workday: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("workday: radio screeners raised: %s", exc)
        try:
            await self._fill_wd_text_questions(page)
        except Exception as exc:
            logger.debug("workday: text questions raised: %s", exc)
        try:
            await self._fill_wd_date_signature(page)
        except Exception as exc:
            logger.debug("workday: date signature raised: %s", exc)
        try:
            await self._handle_wotc_assessment(page)
        except Exception as exc:
            logger.debug("workday: wotc assessment raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered Workday button[aria-haspopup=listbox] selects; for each
        whose label maps to a deterministic answer, open + pick the matching option."""
        try:
            labels = await page.evaluate(_WD_SELECT_LABELS_JS)
        except Exception:
            return
        _dbg = bool(os.getenv("WORKDAY_DEBUG_SHOTS"))
        if _dbg:
            try:
                ctx = await page.evaluate(
                    "()=>{const bs=[...document.querySelectorAll('button[aria-haspopup=\"listbox\"]')]"
                    ".filter(b=>/select one/i.test((b.innerText||'').trim()));"
                    "if(!bs.length)return {n:0};const b=bs[0];"
                    "const ff=b.closest('[data-automation-id^=\"formField\"]')||b.parentElement;"
                    "const lb=b.getAttribute('aria-labelledby');"
                    "const lbt=lb?lb.split(/\\s+/).map(id=>{const e=document.getElementById(id);"
                    "return e?e.innerText.trim():'';}).filter(Boolean).join(' | '):'';"
                    "const prev=ff.previousElementSibling;"
                    "const par=ff.parentElement;"
                    "return {n:bs.length,ariaLb:lb,lbText:lbt.slice(0,120),"
                    "ariaLabel:(b.getAttribute('aria-label')||'').slice(0,120),"
                    "prevText:(prev?prev.innerText.trim():'').slice(0,120),"
                    "parFirstChild:(par&&par.firstElementChild?par.firstElementChild.innerText.trim():'').slice(0,120),"
                    "parAid:par?par.getAttribute('data-automation-id'):null,"
                    "ffAid:ff.getAttribute('data-automation-id')};}")
                logger.info("workday SCREENER-CTX: %r", ctx)
            except Exception as exc:
                logger.info("workday SCREENER-CTX raised: %s", exc)
        for f in labels:
            if f.get("answered"):
                continue
            label = (f.get("label") or "").lower()
            key = f.get("key") or ""
            if _DEMOGRAPHIC_RE.search(label):
                continue                         # handled by _decline_wd_demographics
            is_prof = bool(re.search(r"proficiency|language", label)
                           and re.search(r"english|spanish", label))
            values = self._screener_answer(label, facts)
            if is_prof and not values:
                high = True if "english" in label else bool(facts.get("bilingual"))
                values = (["Native", "Fluent", "Advanced", "Professional"] if high
                          else ["None", "No proficiency", "Basic", "Limited"])
            if _dbg:
                logger.info("workday SCREENER: label=%r -> values=%r", label[:70], values)
            if not values:
                continue
            try:
                ok = await self._fill_wd_select(page, key, values, allow_first=is_prof)
                if _dbg:
                    logger.info("workday SCREENER: picked=%s for %r", ok, label[:50])
            except Exception:
                pass
        # Fallback: a required Yes/No screener select whose long question text our label heuristics
        # couldn't isolate (e.g. Centene's government-entity conflict question, which has a big intro
        # paragraph) is left on 'Select One'. On this application-questions block EVERY residual is a
        # conflict-of-interest / relationship question — truthfully 'No' for a fresh synthetic persona
        # (the only 'Yes' one, work-authorization, is reliably label-matched above). Answer any
        # remaining Yes/No select that offers a 'No' with 'No'; NEVER touch a non-Yes/No select.
        try:
            await self._answer_residual_yesno_no(page)
        except Exception as exc:
            logger.debug("workday: residual yes/no fill raised: %s", exc)

    async def _answer_residual_yesno_no(self, page: Page) -> None:
        """Tag each still-'Select One' button-listbox select whose OPTION SET is exactly {Yes,No}
        (peeked by opening + reading the listbox), and answer 'No'. Scoped to Yes/No so a real
        multi-option or proficiency select is never blindly answered."""
        # How many unanswered selects offer a Yes/No pair? Peek by opening each in turn.
        idxs = await page.evaluate(
            "()=>{const ph=/select one|select\\.\\.\\.|choose|^\\s*$/i;"
            "const bs=[...document.querySelectorAll('button[aria-haspopup=\"listbox\"]')];"
            "return bs.map((b,i)=>({i,unans:ph.test((b.innerText||'').trim())}))"
            ".filter(x=>x.unans).map(x=>x.i);}")
        for i in idxs or []:
            try:
                # tag the i-th listbox button
                tagged = await page.evaluate(
                    "(i)=>{const bs=[...document.querySelectorAll('button[aria-haspopup=\"listbox\"]')];"
                    "const b=bs[i];if(!b)return false;"
                    "const ph=/select one|select\\.\\.\\.|choose|^\\s*$/i;"
                    "if(!ph.test((b.innerText||'').trim()))return false;"
                    "b.setAttribute('data-jfwd','1');return true;}", i)
                if not tagged:
                    continue
                # open + read the option labels to confirm it's a Yes/No select
                await page.click("button[data-jfwd='1']", timeout=3000)
                await page.wait_for_timeout(500)
                opts = await page.evaluate(
                    "()=>{const lb=document.querySelector('[role=\"listbox\"]:not([hidden]),"
                    "[data-automation-id=\"activeListContainer\"]');"
                    "if(!lb)return [];return [...lb.querySelectorAll('[data-automation-id=\"promptOption\"],"
                    "[role=\"option\"],li[role=\"option\"]')].map(o=>(o.innerText||'').trim()).filter(Boolean);}")
                low = [o.lower() for o in (opts or [])]
                is_yesno = any(o == "yes" for o in low) and any(o == "no" for o in low) and len(low) <= 4
                # close the peek-open listbox so _pick_tagged_select opens it fresh
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                except Exception:
                    pass
                if is_yesno:
                    picked = await self._pick_tagged_select(page, ["No"])
                    if os.getenv("WORKDAY_DEBUG_SHOTS"):
                        logger.info("workday SCREENER-RESIDUAL: idx=%d opts=%r picked_no=%s",
                                    i, opts, picked)
                else:
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                await page.evaluate(
                    "()=>{const b=document.querySelector('button[data-jfwd=\"1\"]');"
                    "if(b)b.removeAttribute('data-jfwd');}")
            except Exception:
                try:
                    await page.evaluate(
                        "()=>{const b=document.querySelector('button[data-jfwd=\"1\"]');"
                        "if(b)b.removeAttribute('data-jfwd');}")
                except Exception:
                    pass

    async def _fill_wd_text_questions(self, page: Page) -> None:
        """Fill free-text application-question inputs the analyzer misses. Currently the mass-hiring
        pay-expectation field ('Please provide your minimum base pay expectations for this role') —
        a required text input answered with a plausible market rate for these CSR / care roles."""
        try:
            tags = await page.evaluate(
                "()=>{const out=[];let n=0;"
                "for(const el of document.querySelectorAll("
                "'[data-automation-id^=\"formField\"] input[type=\"text\"],"
                "[data-automation-id^=\"formField\"] textarea')){"
                "if((el.value||'').trim())continue;"
                "const ff=el.closest('[data-automation-id^=\"formField\"]');"
                "const q=(((ff&&ff.innerText)||'')+' '+(el.getAttribute('aria-label')||'')).toLowerCase();"
                "let kind=null;"
                "if(/expectation|expected (pay|salary|compensation|wage|rate)|"
                "(minimum|base|desired).{0,20}(pay|salary|compensation|wage|rate)|"
                "salary requirement|pay requirement/.test(q))kind='pay';"
                "if(kind){el.setAttribute('data-jftext',String(n));out.push({n:n,kind:kind});n++;}}"
                "return out;}")
        except Exception:
            return
        vals = {"pay": "$22 per hour"}
        for t in tags or []:
            val = vals.get(t.get("kind"))
            if not val:
                continue
            try:
                e = page.locator(f'[data-jftext="{t["n"]}"]').first
                if await e.count():
                    await e.fill(val, timeout=3000)
                    if os.getenv("WORKDAY_DEBUG_SHOTS"):
                        logger.info("workday TEXTQ: filled %s = %r", t.get("kind"), val)
            except Exception:
                pass
        try:
            await page.evaluate(
                "()=>document.querySelectorAll('[data-jftext]').forEach("
                "e=>e.removeAttribute('data-jftext'))")
        except Exception:
            pass

    async def _handle_wotc_assessment(self, page: Page) -> None:
        """The Centene 'Take Assessment' step is a Work Opportunity Tax Credit (WOTC) questionnaire —
        technically optional but you must OPEN it and OPT OUT to continue. A synthetic persona always
        declines (it asks about veteran / SNAP-TANF benefit status — never claim those). Click 'Take
        Assessment', then the opt-out control (page or embedded ADP iframe)."""
        # Fire ONLY on the real WOTC step — detected by a VISIBLE 'Take Assessment' button (the
        # progress-nav lists 'Take Assessment' as an upcoming step name on EVERY page, so matching
        # body text alone would spuriously fire the handler on My Information etc.).
        try:
            is_wotc = await page.evaluate(
                "()=>{const b=[...document.querySelectorAll('button,a,[role=button]')]"
                ".find(e=>/take assessment/i.test(e.innerText||'')&&e.offsetParent!==null);"
                "return !!b || /you must complete the assessment test to continue|"
                "work opportunity tax credit/i.test((document.querySelector('main,[role=main],"
                "[data-automation-id=jobApplyPage]')||document.body).innerText||'');}")
        except Exception:
            is_wotc = False
        if not is_wotc:
            return
        _dbg = bool(os.getenv("WORKDAY_DEBUG_SHOTS"))
        # already opted out (a 'Completed'/'assessment complete' badge)?
        try:
            if await page.evaluate("()=>/assessment complete|you have completed|opted out|"
                                   "thank you for completing/i.test(document.body.innerText||'')"):
                return
        except Exception:
            pass
        # 'Take Assessment' opens the ADP questionnaire in a NEW TAB/POPUP — capture it.
        ta = None
        for sel in ('button:has-text("Take Assessment")', 'a:has-text("Take Assessment")',
                    '[data-automation-id*="assessment" i]'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=800):
                    ta = b
                    break
            except Exception:
                continue
        if ta is None:
            return
        survey = None
        try:
            async with page.context.expect_page(timeout=9000) as pinfo:
                await ta.scroll_into_view_if_needed(timeout=2000)
                await ta.click(timeout=4000)
            survey = await pinfo.value
        except Exception:
            survey = None
        if survey is None:
            # no popup — the questionnaire may have loaded in-place / an iframe
            await page.wait_for_timeout(3000)
            survey = page
        try:
            await survey.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await survey.wait_for_timeout(3500)
        if _dbg:
            try:
                await survey.screenshot(path=os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "logs", "workday_recon", "_debug", "wotc_questionnaire.png"), full_page=False)
                body = await survey.evaluate("()=>document.body.innerText.slice(0,600)")
                logger.info("workday WOTC survey text: %r", body)
            except Exception as exc:
                logger.info("workday WOTC survey shot raised: %s", exc)
        optout = ("opt out", "opt-out", "i choose not to", "elect not to answer", "decline to answer",
                  "do not wish to answer", "not to participate", "choose not to participate",
                  "no thank", "i do not wish", "prefer not to answer", "i decline", "decline")
        clicked = False
        # walk the survey popup + its frames; opt-out is usually the last (lower-right) control
        for _round in range(3):
            frames = [survey]
            try:
                frames += list(survey.frames)
            except Exception:
                pass
            for fr in frames:
                for txt in optout:
                    try:
                        b = fr.locator(
                            f'button:has-text("{txt}"), a:has-text("{txt}"), '
                            f'[role="button"]:has-text("{txt}"), input[value*="{txt}" i]').first
                        if await b.count() and await b.is_visible(timeout=400):
                            await b.scroll_into_view_if_needed(timeout=1500)
                            await b.click(timeout=3000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if clicked:
                await survey.wait_for_timeout(2500)
                # a confirm dialog may follow the opt-out
                for txt2 in ("confirm", "yes", "ok", "submit", "continue", "finish", "done"):
                    try:
                        for fr in [survey] + list(survey.frames):
                            b = fr.locator(f'button:has-text("{txt2}"), [role="button"]:has-text("{txt2}")').first
                            if await b.count() and await b.is_visible(timeout=300):
                                await b.click(timeout=2000)
                                await survey.wait_for_timeout(1200)
                                break
                    except Exception:
                        continue
                break
            await survey.wait_for_timeout(2000)
        if _dbg:
            logger.info("workday WOTC: opt-out clicked=%s (popup=%s)", clicked, survey is not page)
        # close the popup + return to the application; Workday should now show the assessment complete
        if survey is not page:
            try:
                await survey.wait_for_timeout(1500)
                await survey.close()
            except Exception:
                pass
        try:
            await page.bring_to_front()
            await page.wait_for_timeout(2500)
        except Exception:
            pass
        if _dbg:
            await self._dbg_shot(page, "wotc_after_optout")

    async def _fill_wd_date_signature(self, page: Page) -> None:
        """The Voluntary Self-ID of Disability form requires a signature Date (today). Workday CxS
        renders a 3-part MM/DD/YYYY widget (dateSectionMonth/Day/Year-input) or a single date input.
        Fill any EMPTY date widget with today's date so the disclosure page can advance."""
        import datetime
        d = datetime.date.today()
        did = False
        # 3-part spinner widget (the standard CxS date field)
        parts = (('input[data-automation-id="dateSectionMonth-input"]', f"{d.month:02d}"),
                 ('input[data-automation-id="dateSectionDay-input"]', f"{d.day:02d}"),
                 ('input[data-automation-id="dateSectionYear-input"]', str(d.year)))
        for sel, val in parts:
            try:
                loc = page.locator(sel)
                cnt = await loc.count()
                for i in range(cnt):
                    e = loc.nth(i)
                    if await e.is_visible(timeout=400) and not (await e.input_value() or "").strip():
                        await e.fill(val, timeout=2000)
                        did = True
            except Exception:
                continue
        if not did:
            # single free-text date input on the disability/self-ID form
            for sel in ('input[id$="--dateSignedOn"]', 'input[id*="isabilit" i][type="text"]',
                        '[data-automation-id="selfIdentifiedDisabilityData--dateSignedOn"] input'):
                try:
                    e = page.locator(sel).first
                    if await e.count() and await e.is_visible(timeout=400) \
                            and not (await e.input_value() or "").strip():
                        await e.fill(d.strftime("%m/%d/%Y"), timeout=2000)
                        did = True
                        break
                except Exception:
                    continue
        if did and os.getenv("WORKDAY_DEBUG_SHOTS"):
            logger.info("workday DATE-SIG: filled today's date %s", d.strftime("%m/%d/%Y"))

    async def _answer_radio_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED radio-group screener (Workday questionnaire Yes/No) with a
        truthful, backed pick from _screener_answer. Leaves an unmatched group for the human."""
        facts = facts or {}
        try:
            groups = await page.evaluate(_RADIO_GROUPS_JS)
        except Exception:
            return
        for grp in groups:
            if grp.get("answered"):
                continue
            label = (grp.get("label") or "").lower()
            if _DEMOGRAPHIC_RE.search(label):
                continue                         # never answer a demographic radio here
            cands = self._screener_answer(label, facts)
            if not cands:
                continue
            opts = grp.get("options") or []
            picked = None
            for c in cands:
                cl = c.strip().lower()
                for o in opts:
                    if self._opt_match(cl, (o.get("text") or "").strip().lower()):
                        picked = o
                        break
                if picked:
                    break
            if not picked:
                continue
            try:
                await self._click_radio(page, grp["name"], picked.get("value"))
            except Exception:
                pass

    async def _decline_wd_demographics(self, page: Page) -> None:
        """Decline every UNANSWERED Workday demographic button[aria-haspopup=listbox] select
        (gender / race / ethnicity / veteran / disability) with its explicit non-disclosure
        option — never claiming a protected characteristic. A demographic with no decline option
        is left blank (nothing safe to pick)."""
        try:
            labels = await page.evaluate(_WD_SELECT_LABELS_JS)
        except Exception:
            return
        for f in labels:
            if f.get("answered"):
                continue
            label = (f.get("label") or "").lower()
            if not _DEMOGRAPHIC_RE.search(label):
                continue
            try:
                await self._fill_wd_select(page, f.get("key") or "", list(_DECLINE_VALUES),
                                           allow_first=False)
            except Exception:
                pass

    async def _fill_wd_select(self, page: Page, label_substr: str, values,
                              allow_first: bool = False) -> bool:
        """Fill a Workday button[aria-haspopup=listbox] select whose label contains label_substr:
        click it to open the listbox, type into the popup search box (when present), and click the
        matching (or first real) option. Workday selects need this open-and-pick — a plain .fill
        types prose the widget rejects, and setting a native input jumps to the wrong option."""
        found = await page.evaluate(_WD_TAG_SELECT_JS, label_substr.lower())
        if not found:
            return False
        return await self._pick_tagged_select(page, values, allow_first)

    async def _pick_tagged_select(self, page: Page, values, allow_first: bool = False) -> bool:
        """Open the button[data-jfwd=1] listbox and pick a value. Workday listbox options do NOT
        commit on a plain .click() (verified on Country) — they commit on KEYBOARD, so type-ahead
        into the open listbox then Enter, with a click on the VISIBLE option as a fallback, and
        VERIFY the button's text actually changed off its 'Select One' placeholder."""
        ph = re.compile(r"select one|select\.\.\.|select a value|choose|^\s*$", re.I)

        async def _cur() -> str:
            try:
                return ((await page.eval_on_selector(
                    "button[data-jfwd='1']", "e=>e.innerText")) or "").strip()
            except Exception:
                return ""

        start = await _cur()

        async def _committed() -> bool:
            c = await _cur()
            return bool(c) and c != start and not ph.search(c)

        picked = False
        for val in values:
            try:
                await page.click("button[data-jfwd='1']", timeout=3000)
                await page.wait_for_timeout(450)
                # A long list (country/state) shows a search box; a short Yes/No list has none, but
                # the OPEN listbox still accepts type-ahead — type the value either way.
                sf = page.locator(
                    'input[data-automation-id="searchBox"], '
                    '[data-automation-id="activeListContainer"] input[type="text"], '
                    "input[role='combobox']").last
                typed = False
                try:
                    if await sf.count() and await sf.is_visible(timeout=500):
                        await sf.fill(val, timeout=2000)
                        typed = True
                except Exception:
                    pass
                if not typed:
                    try:
                        await page.keyboard.type(val, delay=25)
                    except Exception:
                        pass
                await page.wait_for_timeout(650)
                listbox = page.locator('[role="listbox"]:visible, '
                                       '[data-automation-id="activeListContainer"]:visible').last
                opts = listbox.locator('[data-automation-id="promptOption"], [role="option"], '
                                       'li[role="option"]')
                target = opts.filter(
                    has_text=re.compile(rf"^\s*{re.escape(val)}\s*$", re.I)).first
                if not await target.count():
                    target = opts.filter(
                        has_text=re.compile(re.escape(val.split()[0]), re.I)).first
                if not await target.count() and allow_first:
                    target = opts.filter(
                        has_not_text=re.compile("no matches|no results|searching", re.I)).first
                if await target.count():
                    try:
                        await target.scroll_into_view_if_needed(timeout=1200)
                        await target.click(timeout=2500)
                        await page.wait_for_timeout(300)
                    except Exception:
                        pass
                if await _committed():
                    picked = True
                    break
                # The click didn't commit — type-ahead has highlighted the match; Enter commits it.
                try:
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(400)
                except Exception:
                    pass
                if await _committed():
                    picked = True
                    break
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                except Exception:
                    pass
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        try:
            await page.eval_on_selector("button[data-jfwd='1']",
                                        "e=>e.removeAttribute('data-jfwd')")
        except Exception:
            pass
        return picked

    async def _fill_wd_identity(self, page: Page, profile_form: dict) -> None:
        """Workday's My Information renders STRUCTURED name/address/phone sub-fields the generic
        analyzer mis-maps (it dumps the FULL NAME into both name fields, one 'location' string into
        every address sub-field, and the full phone into the extension). Fill each from its OWN
        persona value by Workday's stable ids, OVERWRITING the analyzer's mistakes. A no-op on
        wizard steps that carry none of these fields."""
        pf = profile_form if isinstance(profile_form, dict) else {}
        full = (pf.get("full_name") or pf.get("name") or "").strip()
        first = (pf.get("first_name") or "").strip()
        last = (pf.get("last_name") or "").strip()
        if (not first or not last) and full:
            parts = full.split()
            if len(parts) >= 2:
                first = first or parts[0]
                last = last or parts[-1]
        street = (pf.get("street_address") or pf.get("address") or "").strip()
        city = (pf.get("city") or "").strip()
        zipc = (pf.get("zip_code") or pf.get("zip") or pf.get("postal_code") or "").strip()
        # The synthetic persona's ZIP is not guaranteed to belong to its STATE, and Workday validates
        # 'X is not a valid postal code for <State>' — a hard block. Override with a representative
        # in-state ZIP whenever we know the state, so postal-vs-state is always consistent.
        state = (pf.get("state") or "").strip()
        st_zip = _US_STATE_ZIP.get(state.lower())
        if st_zip:
            zipc = st_zip
        phone = re.sub(r"\D", "", (pf.get("phone") or ""))
        if len(phone) == 11 and phone.startswith("1"):
            phone = phone[1:]                        # national number; Workday adds the +1 country code

        async def _set(selectors, val):
            if not val:
                return
            for sel in selectors:
                try:
                    e = page.locator(sel).first
                    if await e.count() and await e.is_visible(timeout=800):
                        await e.fill("", timeout=2000)
                        await e.fill(val, timeout=3000)
                        return
                except Exception:
                    continue

        await _set(['input[data-automation-id="legalNameSection_firstName"]',
                    'input[id$="--legalName--firstName"]', 'input[id$="--firstName"]'], first)
        await _set(['input[data-automation-id="legalNameSection_lastName"]',
                    'input[id$="--legalName--lastName"]', 'input[id$="--lastName"]'], last)
        await _set(['input[data-automation-id="addressSection_addressLine1"]',
                    'input[id$="--addressLine1"]'], street)
        await _set(['input[data-automation-id="addressSection_city"]',
                    'input[id$="--city"]'], city)
        await _set(['input[data-automation-id="addressSection_postalCode"]',
                    'input[id$="--postalCode"]'], zipc)
        await _set(['input[data-automation-id="phone-number"]',
                    'input[id$="--phoneNumber"]'], phone)
        # The extension sub-field must NOT carry the phone number — clear it if the analyzer filled it.
        for sel in ('input[data-automation-id="phone-extension"]', 'input[id$="--extension"]'):
            try:
                e = page.locator(sel).first
                if await e.count() and (await e.input_value() or "").strip():
                    await e.fill("", timeout=1500)
            except Exception:
                continue

    async def _fill_wd_source(self, page: Page) -> None:
        """Answer the required 'How Did You Hear About Us?' multiselect prompt with a neutral
        job-board source so My Information can advance (the analyzer leaves this Workday prompt
        blank). Best-effort; a no-op when the field is absent or already answered."""
        # 'How Did You Hear About Us?' is a Workday searchable multiselect: a text input (#source--source,
        # placeholder 'Search', aria-required) that surfaces [data-automation-id=promptOption]s as you
        # type. Type a source term and click its matching option; a chip then fills promptSelectionLabel.
        async def _answered() -> bool:
            # A selected source becomes a PILL in the source field (aria-label '…press delete to clear
            # value.', id 'pill-…'), like the phone-code '+1' pill. Also accept the older chip markers.
            try:
                return await page.evaluate(
                    "()=>{const f=document.querySelector('[data-automation-id=\"formField-source\"]');"
                    "if(!f)return false;"
                    "if(f.querySelector('[id^=\"pill-\"],[aria-label*=\"press delete\"],"
                    "[data-automation-id=\"selectedItem\"],[data-automation-id=\"DELETE_charm\"],"
                    "[data-automation-id$=\"-selectedItem\"],.css-selectedItem'))return true;"
                    "const c=f.querySelector('[data-automation-id=\"promptSelectionLabel\"]');"
                    "return !!(c&&c.innerText.trim());}")
            except Exception:
                return False
        _dbg = bool(os.getenv("WORKDAY_DEBUG_SHOTS"))
        if await _answered():
            return
        inp = page.locator('#source--source, '
                           '[data-automation-id="formField-source"] input[type="text"], '
                           '[data-automation-id="formField-source"] '
                           '[data-automation-id="multiselectInputContainer"] input').first
        try:
            if not await inp.count():
                if _dbg:
                    logger.info("workday SOURCE: input not found")
                return
            await inp.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            return
        # The option is a CHECKBOX-style menuItem: <div data-automation-id="menuItem" role="option"
        # aria-label="Job Board not checked" aria-selected="false"> wrapping a promptOption label.
        # Selecting toggles it to a PILL in the source field. Open the prompt, then toggle the wanted
        # menuItem — click it, and if that doesn't take, focus + Space (the checkbox commit key).
        wants = ["Job Board", "Employee Referral", "Online Advertising", "Company Website",
                 "Careers Website", "Website", "Other"]
        try:
            # The base analyzer types a drafted 'how did you hear' string (e.g. 'Online job search')
            # into this multiselect's search input, which FILTERS the option list to nothing — clear
            # it first so every menuItem is visible again, then open the prompt.
            await inp.click(timeout=2000)
            await inp.fill("", timeout=1500)
            await inp.click(timeout=2000)
            await page.wait_for_timeout(900)
        except Exception:
            return
        for want in wants:
            if await _answered():
                break
            mi = page.locator('[data-automation-id="menuItem"][role="option"]').filter(
                has_text=re.compile(rf"^\s*{re.escape(want)}\s*$", re.I)).first
            try:
                if not await mi.count():
                    continue
                await mi.scroll_into_view_if_needed(timeout=1500)
                await mi.click(timeout=3000)
                await page.wait_for_timeout(600)
                if not await _answered():
                    try:
                        await mi.focus()
                        await page.keyboard.press("Space")
                        await page.wait_for_timeout(600)
                    except Exception:
                        pass
                if _dbg:
                    logger.info("workday SOURCE: menuItem want=%r answered=%s",
                                want, await _answered())
                if await _answered():
                    break
            except Exception as exc:
                if _dbg:
                    logger.info("workday SOURCE: menuItem want=%r raised %s", want, exc)
                continue
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        if _dbg and not await _answered():
            logger.info("workday SOURCE: NOT answered")

    async def _fill_wd_country(self, page: Page) -> None:
        """Set Country = United States on My Information. Workday CxS renders Country as a
        button[aria-haspopup=listbox] inside [data-automation-id=formField-country] whose <label>
        text is sometimes empty (so a label-substring match misses it, leaving the geo-defaulted
        country e.g. France) — tag it by the field's automation-id and open-and-pick; fall back to a
        native <select> on the tenants that use one. A no-op once it already reads United States."""
        try:
            cur = await page.evaluate(
                "()=>{const b=document.querySelector("
                "'[data-automation-id=\"formField-country\"] button[aria-haspopup=\"listbox\"],"
                "button[data-automation-id=\"countryDropdown\"],"
                "[data-automation-id=\"formField-country\"] select');"
                "return b?(b.tagName==='SELECT'?(b.options[b.selectedIndex]||{}).text:b.innerText).trim():null;}")
        except Exception:
            cur = None
        if cur and re.search(r"united states", cur, re.I):
            return
        # Native <select> path (read options, select the US label exactly).
        try:
            for sel in ('[data-automation-id="formField-country"] select',
                        'select[id$="--country"]', 'select[name*="country" i]'):
                e = page.locator(sel).first
                if await e.count():
                    labels = await e.evaluate("s=>[...s.options].map(o=>o.label||o.text)")
                    for want in ("United States of America", "United States"):
                        if any((l or "").strip().lower() == want.lower() for l in labels):
                            await e.select_option(label=want)
                            return
        except Exception:
            pass
        # Button-listbox path (the observed Centene shape): a button[aria-haspopup=listbox][name=country]
        # inside formField-country. Open it, type into the popup search box, and click the option that
        # starts with 'United States' (prefer '…of America'; NEVER a bare 'United'-prefix match that
        # would land on United Arab Emirates / United Kingdom).
        _dbg = bool(os.getenv("WORKDAY_DEBUG_SHOTS"))
        btn = page.locator('[data-automation-id="formField-country"] button[aria-haspopup="listbox"], '
                           'button[data-automation-id="countryDropdown"], '
                           'button[name="country"][aria-haspopup="listbox"]').first

        async def _cur_country() -> str:
            try:
                return (await btn.inner_text() or "").strip()
            except Exception:
                return ""

        try:
            n = await btn.count()
            if _dbg:
                logger.info("workday COUNTRY: btn.count=%s cur=%r", n, cur)
            if not n:
                return
        except Exception:
            return
        # This country listbox has NO search box and renders the WHOLE list (no virtualization), so
        # the DOM also holds other closed dropdowns' options (phone-code 'United States (+1)') — an
        # unscoped click lands on the wrong/hidden one. Open, TYPE-AHEAD to jump to 'United States',
        # click the option INSIDE THE VISIBLE listbox, and VERIFY the button text actually changed.
        for attempt in range(3):
            if re.search(r"united states", await _cur_country(), re.I):
                break
            try:
                await btn.scroll_into_view_if_needed(timeout=2000)
                await btn.click(timeout=3000)
                await page.wait_for_timeout(600)
            except Exception as exc:
                if _dbg:
                    logger.info("workday COUNTRY: open raised %s", exc)
                break
            # Workday listboxes accept type-ahead even without a visible search box.
            try:
                await page.keyboard.type("United States of America", delay=25)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            listbox = page.locator('[role="listbox"]:visible, '
                                    '[data-automation-id="activeListContainer"]:visible').last
            clicked = False
            for pat in (r"united states of america", r"^\s*united states\s*$"):
                try:
                    opt = listbox.locator('[data-automation-id="promptOption"], [role="option"], '
                                          'li[role="option"]').filter(
                        has_text=re.compile(pat, re.I)).first
                    if await opt.count():
                        await opt.scroll_into_view_if_needed(timeout=1500)
                        await opt.click(timeout=3000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # type-ahead already highlighted the match — commit it with Enter.
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
            await page.wait_for_timeout(500)
            if _dbg:
                logger.info("workday COUNTRY: attempt %d -> now=%r (clicked=%s)",
                            attempt, await _cur_country(), clicked)
            if re.search(r"united states", await _cur_country(), re.I):
                break
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass

    async def _click_radio(self, page: Page, name: str, value) -> bool:
        found = await page.evaluate(
            """([nm,val])=>{for(const r of document.querySelectorAll('input[type=radio]')){
                if(r.name===nm && r.value===val){r.setAttribute('data-jfr','1');return true;}}
              return false;}""", [name, value])
        if not found:
            return False
        ok = True
        try:
            await page.check("input[data-jfr='1']", timeout=3000, force=True)
        except Exception:
            try:
                await page.eval_on_selector(
                    "input[data-jfr='1']",
                    "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));}")
            except Exception:
                ok = False
        try:
            await page.eval_on_selector("input[data-jfr='1']", "e=>e.removeAttribute('data-jfr')")
        except Exception:
            pass
        return ok

    async def _tick_required_checkboxes(self, page: Page) -> None:
        """Tick every REQUIRED, currently-unchecked checkbox that is not a marketing opt-in — the
        create-account Terms box is required but its <label> is often just a link, so a text
        consent-matcher misses it and the step is blocked. Never ticks newsletter/marketing."""
        try:
            boxes = page.locator('input[type="checkbox"]')
            for i in range(await boxes.count()):
                cb = boxes.nth(i)
                try:
                    req = await cb.evaluate(
                        "e=>e.required||e.getAttribute('aria-required')==='true'")
                    if not req or await cb.is_checked():
                        continue
                    ctx = (await cb.evaluate(
                        "e=>{const c=e.closest('div,li,fieldset,form');return c?c.innerText:'';}")
                        or "").lower()
                    if re.search(r"newsletter|marketing|promotional|subscribe|"
                                 r"contact you about|talent community|opportunities", ctx):
                        continue
                    try:
                        await cb.check(timeout=2500)
                    except Exception:
                        await cb.evaluate(
                            "e=>{e.checked=true;"
                            "e.dispatchEvent(new Event('click',{bubbles:true}));"
                            "e.dispatchEvent(new Event('change',{bubbles:true}));}")
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("workday: checkbox tick raised: %s", exc)

    async def _tick_acknowledge(self, page: Page) -> None:
        """Tick a required certification/acknowledgement checkbox or radio (a single affirmative
        option like 'I certify' / 'I acknowledge')."""
        try:
            ids = await page.evaluate(
                """()=>{const out=[];
                  for(const el of document.querySelectorAll('input[type=checkbox],input[type=radio]')){
                    if(el.checked||!el.id)continue;
                    const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');
                    const t=((l&&l.innerText)||(el.closest('label')||{}).innerText||'').toLowerCase();
                    if(/acknowledge|i certify|i attest|i agree|i understand|i confirm/.test(t))
                      out.push(el.id);}
                  return out;}""")
        except Exception:
            return
        for eid in ids:
            try:
                await page.locator(f'[id="{eid}"]').check(force=True, timeout=2500)
            except Exception:
                try:
                    await page.evaluate(
                        """(id)=>{const e=document.getElementById(id);if(e){e.checked=true;"""
                        """e.dispatchEvent(new Event('click',{bubbles:true}));"""
                        """e.dispatchEvent(new Event('change',{bubbles:true}));}}""", eid)
                except Exception:
                    pass

    @staticmethod
    def _opt_match(cand: str, opt: str) -> bool:
        """Match a candidate answer to an option text. Short answers (yes/no/ged) need a word
        boundary so 'No' never matches 'None'; longer answers ('1-3 years') use substring."""
        if not cand or not opt:
            return False
        if cand == opt:
            return True
        if len(cand) <= 4:
            return (opt.startswith(cand + " ") or opt.startswith(cand + ",")
                    or (" " + cand + " ") in (" " + opt + " "))
        return cand in opt or opt in cand

    @staticmethod
    def _screener_answer(t: str, facts: dict):
        """Deterministic, truthful answer candidates for a Workday pre-screening / questionnaire
        question (lowercased label). Returns an ordered list of option-text candidates (strongest
        first), or None to leave it for the human. Truthful for a synthetic US persona DESIGNED to
        fit the job (located at the job's city, native English, bilingual only when the role is).

        These four tenants (Concentrix health-insurance rep, CVS member services, Centene care
        coordinator, Cigna customer service) share the CSR / member-services screener lexicon."""
        facts = facts or {}
        if re.search(r"acknowledge|i certify|i attest", t):
            return None                                   # handled by _tick_acknowledge
        if re.search(r"spanish", t):
            return (["Fluent", "Native", "Advanced", "Bilingual"] if facts.get("bilingual")
                    else ["None", "No proficiency", "Basic", "Beginner", "Limited"])
        if re.search(r"english", t):
            # A US persona is a native English speaker — lead the strongest tier.
            return ["Native", "Native or bilingual", "Fluent", "Advanced", "Professional"]
        if re.search(r"highest level of education|education (you have )?achieved|level of education", t):
            return [facts.get("education_level") or "Bachelor", "Bachelor", "High School",
                    "Associate", "GED"]
        # Customer-service / call-center / member-services experience — pick the HIGHEST believable
        # tier (the tailored résumé shows ~8 yrs), never a weak middle one that undersells +
        # contradicts it. Matched in BOTH orders: "experience ... as a CSR" AND "member services
        # experience" (Workday phrases it either way — keyword before OR after "experience").
        if re.search(r"experience.*(customer service|call center|contact center|retail|customer|member)"
                     r"|(customer service|call center|contact center|member services?|csr|help ?desk)"
                     r"[^.?]*experience", t):
            return ["5+ years", "5 or more", "More than 5", "6+ years", "5 years", "3-5 years",
                    "3+ years", "1-3 years", "Yes"]
        if re.search(r"(supervisor|leadership|management|managerial|team lead)\s*(or [a-z]+ )?experience|"
                     r"experience.*(supervisor|leadership|manage|team lead)|"
                     r"how (much|many years?).*experience|years of experience", t):
            return ["4-5 years", "5+ years", "6+ years", "3-5 years", "5 years", "More than",
                    "1-3 years", "Yes"]
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat", t):
            return ["Yes"]
        # A schedule-conflict/attendance screener → No. Scoped to the attendance/schedule/
        # availability context so a behavioral "describe a time you resolved a conflict" prompt
        # (an open-text field) is NOT mistaken for a Yes/No screener and left for the human.
        if re.search(r"(?:commitment|obligation|conflict).{0,40}"
                     r"(?:interfere|attendance|schedule|availab|work)"
                     r"|foresee (?:any )?(?:commitment|conflict|obligation)"
                     r"|interfere with (?:your )?(?:attendance|schedule|work|availab)"
                     r"|impact.*attendance", t):
            return ["No"]
        if re.search(r"private|secure|quiet|workspace|distraction|free from", t):
            return ["Yes"]
        if re.search(r"ethernet|hardwired|hard-wired|wired", t):
            return ["Yes, my home internet is hardwired", "Yes"]
        if re.search(r"download speed|\bmbps\b|high.?speed|cable or fiber|internet|connection", t):
            return ["Yes"]
        if re.search(r"documentation|diploma or ged|provide.*if needed|verify.*education|"
                     r"able to provide", t):
            return ["Yes"]
        if re.search(r"18 (years|and older)|older|authorized|eligible to work", t):
            return ["Yes"]
        if re.search(r"seasonal|interested in (the |this )?(season|temporary|position|role|opportunity)", t):
            return ["Yes"]
        if re.search(r"\bcitizen(ship)?\b|u\.?s\.? citizen", t):
            return ["Yes"]
        if re.search(r"require sponsor|need sponsor|visa sponsor", t):
            return ["No"]
        if re.search(r"able to meet this requirement|do you meet this requirement|"
                     r"meet (this|the) requirement|able to work|\bshift\b|overtime|"
                     r"willing to (work|attend|commit|travel|obtain)|onsite|on-site|"
                     r"in.?office|in person|first week|training|"
                     r"obtain a[n]? .*(clearance|public trust)|public trust|"
                     r"background (check|investigation)", t):
            return ["Yes"]
        # Conflict-of-interest / prior-relationship screeners — No for a FRESH synthetic persona
        # (truthful by design: no non-compete, no relatives at the employer, no board relationship,
        # no prior employment, not in an OFAC-sanctioned territory). Deterministic + backed.
        if re.search(r"non.?compete|restrictive covenant|"
                     r"agreement.{0,40}(interfere|compet|conflict)|"
                     r"(relative|family member|immediate family).{0,40}(employ|work)|"
                     r"currently employed by (the|this|our|us|centene)|"
                     r"member of .*board of directors|related to .*board of directors|"
                     r"related to (a |an )?(member|employee|officer|director)|"
                     r"working relationship with (a |an )?.*(federal|state|local|government|"
                     r"defense|veterans|health program)|"
                     r"conflict of interest|previously (been )?(employed|worked)|"
                     r"(worked|employed) (for|with|at|by) (us|the company|centene|this organization)|"
                     r"debarred|excluded from|\bofac\b|sanction(ed|s)?|"
                     r"cuba|iran|north korea|syria|crimea", t):
            return ["No"]
        return None

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty visible fields on the current step, so the report's
        `unfilled` reflects the Workday gap fill and the co-pilot's submit gate is honest.
        (Workday renders a real <input>/<select> under each custom widget, so a standard DOM
        scan still sees the underlying required state; button-listbox selects that stayed on
        their 'Select One' placeholder are picked up by _WD_UNANSWERED_SELECTS_JS and appended.)"""
        out = []
        try:
            out = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||!!el.closest('[aria-required="true"]');
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
            out = []
        try:
            for lbl in await page.evaluate(_WD_UNANSWERED_SELECTS_JS):
                if lbl and lbl not in out:
                    out.append(lbl)
        except Exception:
            pass
        return out

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Close a cookie/consent banner (OneTrust) that floats over the action bar and can
        intercept the Apply / Continue / Submit clicks."""
        for name in ("Reject Optional Cookies", "Reject All", "Accept All Cookies",
                     "Accept Cookies", "Accept All", "I Agree", "OK"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    # ---- wizard walker (mirrors OracleORCStrategy._advance_wizard) ----
    async def _step_signature(self, page: Page) -> str:
        """A fingerprint of the current wizard step, to tell whether a Continue click advanced.
        On this Workday tenant the URL and the <h2> heading are the CONSTANT job title across every
        step, so they can't distinguish steps — fingerprint the CONTENT instead: the active progress
        step's index/text, the number of form-field groups, and the first field labels (My Information
        has Country/Name/Address; My Experience has a résumé dropzone with ~0; Questions has screeners)."""
        try:
            return await page.evaluate(
                "()=>{const steps=[...document.querySelectorAll("
                "'[data-automation-id=progressBar] [role=listitem],[data-automation-id=progressBar] li,"
                "[data-automation-id^=progressBar] button,[data-automation-id=progressBar] [aria-current]')];"
                "let idx=-1;steps.forEach((s,i)=>{if(s.getAttribute('aria-current')"
                "||/(^|\\s)(current|active|is-current)(\\s|$)/i.test(s.className))idx=i;});"
                "const a=document.querySelector('[aria-current=\"step\"],[aria-current=\"true\"],"
                "[data-automation-id=progressBar] [aria-current]');"
                "const ffs=[...document.querySelectorAll('[data-automation-id^=\"formField\"]')];"
                "const labs=ffs.slice(0,5).map(f=>{const l=f.querySelector('label');"
                "return l?l.innerText.trim().slice(0,18):'';}).join(',');"
                "return 'i'+idx+'/'+(a?a.innerText.trim().slice(0,20):'')+'|n'+ffs.length+'|'+labs;}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind) for the step's primary button: kind='submit' on the final
        (Review) step, 'advance' on Save/Continue/Next, else None. Workday's footer button is
        canonical, so try it first; its LABEL is what distinguishes advance from submit."""
        try:
            for sel in _FOOTER_BTNS:
                b = await page.query_selector(sel)
                if not b or not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _SUBMIT_RE.search(txt) and not _ADVANCE_RE.search(txt):
                    return b, "submit"
                if _ADVANCE_RE.search(txt) or txt:
                    return b, "advance"
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
            logger.debug("workday: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        """Fill a wizard step: attach the résumé (My Experience), decline demographics, tick
        required consent, fill any ordinary matched fields, and answer the step's screeners."""
        await self._dismiss_cookie_banner(page)
        # Workday renders each wizard step's fields via XHR AFTER navigation; analyzing too early
        # maps 0 fields and leaves required text inputs blank. Wait for the step's form to hydrate.
        try:
            await page.wait_for_selector(
                'input[data-automation-id], button[aria-haspopup="listbox"], '
                '[data-automation-id^="formField"], textarea[data-automation-id]',
                timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        # DEBUG: dump the current validation errors so the real blocker at each Continue is visible.
        if os.getenv("WORKDAY_DEBUG_SHOTS"):
            try:
                errs = await page.evaluate(
                    "()=>{const e=[...document.querySelectorAll("
                    "'[data-automation-id=\"errorMessage\"],[data-automation-id=\"validationMessage\"],"
                    "[data-automation-id=\"errorLabel\"],.css-error,[role=\"alert\"]')]"
                    ".map(x=>x.innerText.trim()).filter(Boolean);"
                    "return [...new Set(e)].slice(0,10);}")
                logger.info("workday ERRORS: %r", errs)
            except Exception as exc:
                logger.debug("workday: error dump raised: %s", exc)
        # Country/State live on My Information — the analyzer can't fill them, and Workday geo-defaults
        # Country to the datacenter's country (e.g. France). SET Country=United States on every step-1
        # pass (a no-op on steps without one); _fill_wd_country covers the button-listbox AND native
        # <select> shapes and tags by the field's automation-id (its <label> can be empty).
        try:
            await self._fill_wd_country(page)
        except Exception:
            pass
        st = (profile_form.get("state") or "").strip() if isinstance(profile_form, dict) else ""
        if st:
            try:
                await self._fill_wd_select(page, "state", [st])
            except Exception:
                pass
        # My Experience carries the résumé upload; attach_resume targets a résumé-like file input
        # and skips photo/avatar/autofill inputs, so it's a no-op on steps without one (and on
        # subclasses that didn't stash a résumé path).
        try:
            await self.attach_resume(page, getattr(self, "_resume_path", "") or "")
        except Exception as exc:
            logger.debug("workday: step resume attach raised: %s", exc)
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        try:
            await self._decline_wd_demographics(page)
        except Exception:
            pass
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("workday: step fill raised: %s", exc)
        # OVERWRITE the analyzer's structured-field mistakes (full name in both name fields, one
        # location string across every address sub-field, phone in the extension) with per-field
        # persona values, and answer the required 'How Did You Hear About Us?' source prompt.
        try:
            await self._fill_wd_identity(page, profile_form)
        except Exception as exc:
            logger.debug("workday: identity fill raised: %s", exc)
        # 'How Did You Hear About Us?' — try the button-listbox shape first (label match), then the
        # multiselect-prompt shape (_fill_wd_source). One of the two matches on any Workday tenant.
        try:
            await self._fill_wd_select(
                page, "how did you hear",
                ["Job Board", "Indeed", "LinkedIn", "Company Website", "Online Job Board",
                 "Search Engine", "Other"], allow_first=True)
        except Exception:
            pass
        try:
            await self._fill_wd_source(page)
        except Exception as exc:
            logger.debug("workday: source fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("workday: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step wizard: click Continue while it advances (filling each new step),
        and STOP at the final Submit — recording its selector in the report WITHOUT clicking it.
        If a Continue click does NOT advance (validation blocked it because a required field is
        still empty), stop and leave the gaps in `unfilled` for the human / next iteration."""
        for _wstep in range(9):
            await self._dismiss_cookie_banner(page)
            if os.getenv("WORKDAY_DEBUG_SHOTS"):
                try:
                    hd = await page.evaluate(
                        "()=>{const p=document.querySelector("
                        "'[data-automation-id=progressBar] [aria-current],[aria-current=\"step\"]');"
                        "const h=document.querySelector('h2,[data-automation-id=jobApplyPageTitle]');"
                        "return (p?p.innerText.trim():'?')+' | '+(h?h.innerText.trim().slice(0,40):'?');}")
                    logger.info("workday WIZARD step %d: %s", _wstep, hd)
                except Exception:
                    pass
            # Fill the CURRENT step FIRST (on the FIRST pass this is My Information, reached after
            # account-create+sign-in) — Workday renders each step's fields via XHR after nav, so
            # filling here (with a hydration wait inside _fill_current_step) beats the base pipeline
            # which analyzed the still-loading step-1 form and mapped 0 fields. Guarded so one step's
            # DOM drift can't abort the whole walk.
            try:
                await self._fill_current_step(page, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("workday: fill_current_step raised on wizard step %d: %s", _wstep, exc)
            # Find the step's primary button, retrying after a short hydration wait (a just-navigated
            # step — e.g. My Experience — can render its footer button a beat after the fields).
            btn, kind = await self._primary_button(page)
            if btn is None:
                await page.wait_for_timeout(2000)
                btn, kind = await self._primary_button(page)
            if os.getenv("WORKDAY_DEBUG_SHOTS"):
                logger.info("workday WIZARD step %d primary_button kind=%r", _wstep, kind)
                await self._dbg_shot(page, f"wiz_{_wstep}")
            if btn is None:
                break
            if kind == "submit":
                # The final (Review) step is reached — the current-step fill above already ran, so
                # record the true final-submit button (never a Continue). We do NOT click it; the
                # co-pilot's gated auto-submit / a human presses it. The register captcha is already
                # handled; the final CxS Submit carries none, but solve_on_page is a safe no-op.
                try:
                    await captcha_solver.solve_on_page(page)
                except Exception:
                    pass
                report["submit_selector"] = (
                    "button[data-automation-id='pageFooterNextButton'], "
                    "button[data-automation-id='bottom-navigation-next-button'], "
                    "button:has-text('Submit')")
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(page)
                return
            sig = await self._step_signature(page)
            if os.getenv("WORKDAY_DEBUG_SHOTS"):
                try:
                    st = await page.evaluate(
                        "()=>{const sels=[...document.querySelectorAll('button[aria-haspopup=\"listbox\"]')]"
                        ".map(b=>{const ff=b.closest('[data-automation-id^=\"formField\"]');"
                        "const l=ff&&ff.querySelector('label');"
                        "return (ff&&ff.getAttribute('data-automation-id')||'?')+'/'+((l&&l.innerText)||b.getAttribute('name')||'?').trim().slice(0,22)+'='+(b.innerText||'').trim().slice(0,26);});"
                        "const emptyReq=[...document.querySelectorAll('[data-automation-id^=\"formField\"]')]"
                        ".filter(f=>{const lab=f.querySelector('label');const req=lab&&/\\*/.test(lab.innerText);"
                        "if(!req)return false;const inp=f.querySelector('input:not([type=hidden]),textarea');"
                        "const btn=f.querySelector('button[aria-haspopup=\"listbox\"]');"
                        "const ms=f.querySelector('[id^=\"pill-\"]');"
                        "if(ms)return false;"
                        "if(btn)return /select one|^$/i.test((btn.innerText||'').trim());"
                        "if(inp)return !inp.value;return false;})"
                        ".map(f=>{const l=f.querySelector('label');return (l&&l.innerText||'?').trim().slice(0,30);});"
                        "return {selects:sels.slice(0,12),emptyRequired:emptyReq.slice(0,10)};}")
                    logger.info("workday PRE-CONTINUE selects/empty: %r", st)
                except Exception:
                    pass
            try:
                await btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                await btn.click(force=True, timeout=5000)
                await page.wait_for_timeout(2500)
            except Exception:
                break
            if os.getenv("WORKDAY_DEBUG_SHOTS"):
                try:
                    st2 = await page.evaluate(
                        "()=>{const h=document.querySelector('h1,h2,[data-automation-id=\"jobApplyPageTitle\"]');"
                        "const ph=document.querySelector('[id$=\"--phoneNumber\"]');"
                        "const errs=[...document.querySelectorAll('*')].filter(e=>e.childElementCount===0"
                        "&&/is required|valid format|must have|error/i.test(e.innerText||'')"
                        "&&(e.innerText||'').length<80).map(e=>e.innerText.trim());"
                        "return {heading:h?h.innerText.trim().slice(0,40):null,"
                        "phone:ph?ph.value:null,errs:[...new Set(errs)].slice(0,8)};}")
                    logger.info("workday POST-CONTINUE: %r", st2)
                    await self._dbg_shot(page, "11_post_continue")
                except Exception:
                    pass
            if await self._step_signature(page) == sig:
                # Did not advance -> a required field on this step is still empty (or an inline
                # validation error). Re-fill once (the error box now pinpoints what's missing) and
                # retry the advance; if it STILL won't move, stop and leave the gaps for the human.
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                btn2, _ = await self._primary_button(page)
                if btn2 is not None:
                    try:
                        await btn2.scroll_into_view_if_needed(timeout=3000)
                        await btn2.click(force=True, timeout=5000)
                        await page.wait_for_timeout(2500)
                    except Exception:
                        pass
                if await self._step_signature(page) == sig:
                    report["wizard_blocked_step"] = sig
                    report["unfilled"] = await self._rescan_required(page)
                    return


class WorkdayMassHiringStrategy(WorkdayStrategy):
    """Mass-Hiring auto-apply for the four validated Workday CxS tenants (Concentrix / CVS Health
    / Centene / Cigna). OFF by default (byte-identical to the shared pipeline — fill reachable,
    stop at the account gate); under WORKDAY_ADVANCE it creates the guest account (register
    reCAPTCHA solved via captcha_solver), fills the wizard, and records the final Submit WITHOUT
    clicking it. Kept SEPARATE from the stock WorkdayStrategy so only these vetted tenants route
    to the account-create path — every other Workday URL falls through to WorkdayStrategy — and
    so the stock class keeps NO prefill override (phenom.PhenomWorkdayStrategy relies on its
    super().prefill resolving to the shared base pipeline)."""
    name = "workday_masshiring"
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(_MASSHIRING_HOST_RE.search(url or ""))

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # Stash what the account-creation helpers need (they only receive `page`).
        self._profile_form = profile_form or {}
        self._facts = facts or {}
        self._resume_path = resume_path
        # DEFAULT (advance OFF): behave EXACTLY like the shared pipeline — a plain fill that stops
        # at the account gate as login_required. super() resolves to WorkdayStrategy (no prefill
        # override) → base.ApplyStrategy.prefill, which runs our (stock) open_form's Apply clicks.
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
            await self.open_form(page)            # stock Workday "Apply" / "Apply Manually"
        except Exception as exc:
            logger.debug("workday_masshiring: open_form raised: %s", exc)
        try:
            await self._start_and_create_account(page)
        except Exception as exc:
            logger.debug("workday_masshiring: account creation raised: %s", exc)
        # The shared pipeline fills whatever is now reachable (base.prefill re-runs open_form,
        # which no-ops once the Apply button is gone). It returns login_required if the account
        # gate is still up (e.g. no captcha key) — the honest "needs the captcha key" signal.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["strategy"] = self.name
        report["account_password"] = getattr(self, "_account_pw", "")
        if report.get("mode") == "resume_parser_only":
            return report
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            report["note"] = ("workday_masshiring: account gate not cleared "
                              "(needs CAPTCHA_SOLVER_KEY + a US residential IP)")
            return report
        try:
            await self._fill_workday_gaps(page, profile_form, facts)
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("workday_masshiring: gap fill raised: %s", exc)
        try:
            await self._advance_wizard(page, report, profile_form, cover_letter, facts)
        except Exception as exc:
            logger.debug("workday_masshiring: wizard advance raised: %s", exc)
        return report


# ---- shared page-side extractors (module-level so they read cleanly) -----------
# A Workday select is a <button aria-haspopup="listbox"> inside a
# <div data-automation-id="formField-…"> that also holds the field <label>. Its current text is
# the selection ("Select One" while unanswered). Enumerate {label, key, answered} for each.
# Extract a Workday select's QUESTION text. Application-Questions selects have NO <label> (the
# question is the formField's own text, or an aria-labelledby element) — so fall back through
# <label> → aria-labelledby → the formField's innerText minus the button's current value.
# Inlined as a nested function inside each evaluate()'d body (Playwright wants ONE expression).
_WD_LABEL_JS_FN = r"""function _wdLabel(b){
    const clean=s=>(s||'').replace(/\s*\*\s*$/,'')
      .replace(/^\s*required\s*/i,'').replace(/\s*required\s*$/i,'')
      .replace(/\s+/g,' ').trim();
    const ff=b.closest('[data-automation-id^="formField"]')||b.parentElement;
    let t='';
    const l=ff&&ff.querySelector('label'); if(l) t=clean(l.innerText);
    if(t.length<12){const lb=b.getAttribute('aria-labelledby');
      if(lb){const s=clean(lb.split(/\s+/).map(id=>{const e=document.getElementById(id);
        return e?e.innerText:'';}).filter(Boolean).join(' '));
        if(s.length>t.length) t=s;}}
    if(t.length<12){const al=clean(b.getAttribute('aria-label')||''); if(al.length>t.length&&!/^select one/i.test(al)) t=al;}
    if(t.length>=12 && !/^select one/i.test(t)) return t;
    // The Application-Questions select renders the question INSIDE the formField but NOT in a <label>
    // (a div/legend/text) followed by the 'Select One' button — so ff.innerText minus the BUTTON's
    // own text is the question.
    if(ff){const ft=clean((ff.innerText||'').split(b.innerText||'').join(' '));
      if(ft.length>=12 && ft.length<=700 && !/^select one/i.test(ft)) return ft;}
    // A long question (e.g. the Centene government-entity screener) renders as the formField's
    // immediately-PRECEDING sibling(s) — the text just before the select. Collect back a few.
    if(ff){let p=ff.previousElementSibling,acc='';
      for(let i=0;i<3&&p;i++){const s=clean(p.innerText);
        if(s&&!/^select one/i.test(s)){acc=s+(acc?' '+acc:'');}
        if(acc.length>=12)break; p=p.previousElementSibling;}
      if(acc.length>=12 && acc.length<=700) return acc;}
    // Else the question sits in an ancestor container. Climb until a parent whose text (minus the
    // select's own 'Select One') reads like a question (12..700 chars).
    const own=clean((ff||b).innerText);
    let node=ff||b;
    for(let i=0;i<6 && node && node.parentElement;i++){
      node=node.parentElement;
      const full=clean((node.innerText||'').split(own).join(' '));
      if(full.length>=12 && full.length<=700) return full;
    }
    return t;}"""

_WD_SELECT_LABELS_JS = (r"""()=>{""" + _WD_LABEL_JS_FN + r"""
  const out=[];const seen=new Set();
  const ph=/select one|select\.\.\.|select a value|^\s*search\s*$|choose/i;
  for(const b of document.querySelectorAll('button[aria-haspopup="listbox"]')){
    let t=_wdLabel(b);
    if(t.length<3) continue;
    const cur=(b.innerText||'').trim();
    const answered=!!cur && !ph.test(cur);
    const key=t.slice(0,110);
    if(seen.has(key)) continue; seen.add(key);
    out.push({label:t, key, answered});
  } return out;}""")

# Tag the FIRST unanswered Workday select whose label contains label_substr with data-jfwd=1.
_WD_TAG_SELECT_JS = (r"""(lbl)=>{""" + _WD_LABEL_JS_FN + r"""
  const n=s=>(s||'').toLowerCase();
  const ph=/select one|select\.\.\.|select a value|^\s*search\s*$|choose/i;
  for(const b of document.querySelectorAll('button[aria-haspopup="listbox"]')){
    if(!n(_wdLabel(b)).includes(lbl)) continue;
    const cur=(b.innerText||'').trim();
    if(cur && !ph.test(cur)) continue;          // already answered — skip
    b.setAttribute('data-jfwd','1'); return true;} return false;}""")

# Labels of REQUIRED Workday selects still on their placeholder (unanswered) — appended to the
# required-empty scan so a still-blank required select doesn't read as "form complete".
_WD_UNANSWERED_SELECTS_JS = r"""()=>{const out=[];
  const ph=/select one|select\.\.\.|select a value|^\s*search\s*$|choose/i;
  for(const b of document.querySelectorAll('button[aria-haspopup="listbox"]')){
    const ff=b.closest('[data-automation-id^="formField"]')||b.parentElement;
    const req=b.getAttribute('aria-required')==='true'
      ||(ff&&ff.getAttribute('aria-required')==='true')
      ||!!(ff&&ff.querySelector('label abbr, label .css-required'));
    if(!req) continue;
    const cur=(b.innerText||'').trim();
    if(cur && !ph.test(cur)) continue;
    let t='';const l=ff&&ff.querySelector('label');if(l)t=l.innerText;
    t=(t||b.getAttribute('aria-label')||'field').replace(/\s*\*\s*$/,'').replace(/\s+/g,' ').trim().slice(0,80);
    out.push(t);} return out;}"""

# Enumerate radio groups with {name, label(question), options[], answered} — the question prompt
# is found by climbing to the smallest ancestor whose text exceeds the option labels.
_RADIO_GROUPS_JS = r"""()=>{const byName={};
  for(const r of document.querySelectorAll('input[type=radio]')){
    const nm=r.name||''; if(!nm) continue; (byName[nm]=byName[nm]||[]).push(r);}
  const lab=r=>{const l=r.id?document.querySelector('label[for="'+
        (window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;
    return ((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'').trim();};
  const out=[];
  for(const nm in byName){const rs=byName[nm];
    const opts=rs.map(r=>({value:r.value,text:lab(r).replace(/\s+/g,' '),checked:r.checked}));
    let box=rs[0].parentElement;
    while(box&&!rs.every(r=>box.contains(r))) box=box.parentElement;
    const optLen=opts.map(o=>o.text).join(' ').replace(/\s+/g,'').length;
    let g=0;
    while(box&&box.parentElement&&g<4){
      if((box.innerText||'').replace(/\s+/g,'').length>optLen+10) break;
      box=box.parentElement; g++;}
    let qt=box?(box.innerText||''):'';
    for(const o of opts) if(o.text) qt=qt.split(o.text).join(' ');
    qt=qt.replace(/\s+/g,' ').trim();
    out.push({name:nm,label:qt,answered:rs.some(r=>r.checked),
      options:opts.map(o=>({value:o.value,text:o.text}))});}
  return out;}"""
