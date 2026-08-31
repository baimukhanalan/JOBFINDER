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

    async def _create_account(self, page: Page, profile_form: dict | None = None) -> None:
        """Fill the Workday create-account form (email + password twice + required Terms box),
        solve the register-step reCAPTCHA (the ONLY captcha in the flow), submit, and handle an
        emailed verification code if the tenant sends one. `profile_form` defaults to the one the
        subclass stashed on `self` in prefill, so it works whether called with or without it."""
        pf = profile_form if profile_form is not None else getattr(self, "_profile_form", {})
        email = (pf or {}).get("email") or ""
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        # email
        for sel in ('input[data-automation-id="email"]', 'input[type="email"]'):
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
        for sel in ('button[data-automation-id="createAccountSubmitButton"]',
                    'button[data-automation-id="click_filter"]',
                    'button:has-text("Create Account")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=1000):
                    await b.click()
                    await page.wait_for_timeout(3500)
                    break
            except Exception:
                continue
        # Some tenants email a verification code before releasing the wizard.
        try:
            await self._verify_email_if_needed(page, email)
        except Exception as exc:
            logger.debug("workday: email verify raised: %s", exc)

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

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered Workday button[aria-haspopup=listbox] selects; for each
        whose label maps to a deterministic answer, open + pick the matching option."""
        try:
            labels = await page.evaluate(_WD_SELECT_LABELS_JS)
        except Exception:
            return
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
            if not values:
                continue
            try:
                await self._fill_wd_select(page, key, values, allow_first=is_prof)
            except Exception:
                pass

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
        picked = False
        for val in values:
            try:
                await page.click("button[data-jfwd='1']", timeout=3000)
                await page.wait_for_timeout(450)
                # Workday puts a search box in the popup for long lists (country/state); typing
                # narrows it. Skip typing if there's no box (short Yes/No lists list all options).
                sf = page.locator(
                    'input[data-automation-id="searchBox"], '
                    '[data-automation-id="activeListContainer"] input[type="text"], '
                    "input[role='combobox']").last
                try:
                    if await sf.count() and await sf.is_visible(timeout=600):
                        await sf.fill(val, timeout=2000)
                        await page.wait_for_timeout(700)
                except Exception:
                    pass
                opts = page.locator(
                    '[data-automation-id="promptOption"], '
                    '[data-automation-id="activeListContainer"] [role="option"], '
                    'ul[role="listbox"] li[role="option"], [role="option"]')
                target = opts.filter(
                    has_text=re.compile(re.escape(val.split()[0]), re.I)).first
                if not await target.count() and allow_first:
                    target = opts.filter(
                        has_not_text=re.compile("no matches|no results|searching", re.I)).first
                if await target.count():
                    await target.click(timeout=3000)
                    picked = True
                    await page.wait_for_timeout(250)
                    break                        # one value applied per select
                else:
                    await page.keyboard.press("Escape")
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
        """A cheap fingerprint of the current wizard step, to tell whether a Continue click
        actually advanced. Workday CxS changes the URL path per step (e.g. .../apply/myInformation
        → .../apply/applicationQuestions), so the pathname alone is a strong signal; the active
        progress step + heading disambiguate the rest."""
        try:
            return await page.evaluate(
                "()=>{const a=document.querySelector("
                "'[data-automation-id=progressBar] [aria-current],"
                "[aria-current=\"step\"],[aria-current=\"true\"]');"
                "const h=document.querySelector("
                "'[data-automation-id=jobApplyPageTitle],h1,h2,legend');"
                "return location.pathname.slice(-48)+'|'+(a?a.innerText.trim().slice(0,32):'')"
                "+'|'+(h?h.innerText.trim().slice(0,32):'');}")
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
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("workday: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step wizard: click Continue while it advances (filling each new step),
        and STOP at the final Submit — recording its selector in the report WITHOUT clicking it.
        If a Continue click does NOT advance (validation blocked it because a required field is
        still empty), stop and leave the gaps in `unfilled` for the human / next iteration."""
        for _ in range(8):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                # The final (Review) step is reached — fill anything still on it, then record the
                # true final-submit button (never a Continue). We do NOT click it; the co-pilot's
                # gated auto-submit / a human presses it. The register captcha is already handled;
                # the final CxS Submit carries none, but solve_on_page is a safe no-op regardless.
                await self._fill_current_step(page, profile_form, cover_letter, facts)
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
            try:
                await btn.click()
                await page.wait_for_timeout(2500)
            except Exception:
                break
            if await self._step_signature(page) == sig:
                # Did not advance -> a required field on this step is still empty (or an inline
                # validation error). Stop; the human / next iteration finishes it (the dry-run
                # screenshot shows what's left).
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)


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
_WD_SELECT_LABELS_JS = r"""()=>{const out=[];const seen=new Set();
  const ph=/select one|select\.\.\.|select a value|^\s*search\s*$|choose/i;
  for(const b of document.querySelectorAll('button[aria-haspopup="listbox"]')){
    const ff=b.closest('[data-automation-id^="formField"]')||b.parentElement;
    let t='';
    const l=ff&&ff.querySelector('label');
    if(l) t=l.innerText;
    if(!t) t=b.getAttribute('aria-label')||'';
    t=(t||'').replace(/\s*\*\s*$/,'').replace(/\s+/g,' ').trim();
    if(t.length<3) continue;
    const cur=(b.innerText||'').trim();
    const answered=!!cur && !ph.test(cur);
    const key=t.slice(0,110);
    if(seen.has(key)) continue; seen.add(key);
    out.push({label:t, key, answered});
  } return out;}"""

# Tag the FIRST unanswered Workday select whose label contains label_substr with data-jfwd=1.
_WD_TAG_SELECT_JS = r"""(lbl)=>{const n=s=>(s||'').toLowerCase();
  const ph=/select one|select\.\.\.|select a value|^\s*search\s*$|choose/i;
  for(const b of document.querySelectorAll('button[aria-haspopup="listbox"]')){
    const ff=b.closest('[data-automation-id^="formField"]')||b.parentElement;
    let t='';const l=ff&&ff.querySelector('label');if(l)t=l.innerText;
    if(!t)t=b.getAttribute('aria-label')||'';
    if(!n(t).includes(lbl)) continue;
    const cur=(b.innerText||'').trim();
    if(cur && !ph.test(cur)) continue;          // already answered — skip
    b.setAttribute('data-jfwd','1'); return true;} return false;}"""

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
