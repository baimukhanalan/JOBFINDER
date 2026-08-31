"""Amazon corporate ATS pre-fill strategy (account.amazon.jobs / passport.amazon.jobs).

Unlike the login-less Greenhouse/Ashby forms — and unlike the login-less-but-multi-step
Avature/Oracle-ORC wizards — Amazon gates the application behind a real ACCOUNT wall
(Amazon Passport, SAML/OIDC) BEFORE the form is reachable:

    account.amazon.jobs/jobs/<id>/apply  --(SAML)-->  passport.amazon.jobs
      register (email + password) — AWS WAF CAPTCHA on /api/createAccountWithEmail
      -> emailed OTP (/api/sendVerificationCode -> /api/confirmVerificationCode)
      -> authenticated apply form  (account.amazon.jobs/applicant/jobs/<id>/apply)
      -> contact info / résumé / eligibility screeners / EEO self-ID / Review / Submit

Two things the generic engine can't do here: (a) the AWS WAF CAPTCHA (Amazon-proprietary,
NOT reCAPTCHA/hCaptcha — solved via captcha_solver.solve_aws_waf), and (b) the account
bootstrap + email-OTP step that stands between the apply URL and the actual form.

Everything about the LIVE path (creating the account, transmitting PII, clicking the final
Submit) is GATED behind env AMAZON_ADVANCE (mirrors AVATURE_ADVANCE / ORC_ADVANCE). With the
gate OFF — the default — a plain fill / co-pilot dry-run only ever lands on the Passport wall,
which the analyzer reports as `login_required`; nothing is created and nothing is transmitted.
Even with the gate ON, the account is created only when the register button is pressed and the
application only when the FINAL Submit is pressed (recorded, never auto-clicked here) — so the
strategy always fills-and-stops like every other one.

Go-live needs BOTH a CapSolver key (CAPTCHA_SOLVER_KEY, for the AWS WAF challenge) AND a US
residential proxy (the WAF token is IP-bound and datacenter IPs are risk-flagged) — see the
Mass Hiring auto-apply notes in CLAUDE.md.
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

# Corporate-apply wizard buttons are plain <button>s (Amazon's "Cloudscape"/polaris UI):
# advance on Continue/Next, STOP (record the selector) on Submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review|proceed)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
_WIZARD_BTN = "button, a[role='button'], [data-testid*='submit' i]"

# Amazon Passport PASSWORD_RULES (scraped from the live shell): upper + lower + digit +
# special + length 8..255 + no leading/trailing whitespace.
_PASSWORD_SPECIALS = "!~%@*><_#^$?|;:&+=(){}[]-`.,/"


def _gen_password() -> str:
    """A strong password satisfying Amazon Passport's complexity (upper+lower+digit+special,
    length 8-255, no surrounding whitespace)."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Jf{body}9!"


def _env_advance() -> bool:
    """True only when AMAZON_ADVANCE is explicitly set — the live switch that lets the strategy
    bootstrap the account (create it, verify the email OTP) and walk the apply wizard past the
    Passport wall. OFF by default so a plain fill / dry-run never creates an account or transmits
    PII. Mirrors Avature's AVATURE_ADVANCE / Oracle's ORC_ADVANCE gates."""
    return os.getenv("AMAZON_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class AmazonStrategy(ApplyStrategy):
    name = "amazon"
    # Whether to WALK past the Passport account wall (create the account, verify the emailed OTP,
    # then fill + advance the apply wizard). OFF by default — see _env_advance. The real
    # auto-submit lane sets this True (env AMAZON_ADVANCE=1), the same way the rest of the engine
    # gates its live actions; it ALSO needs a CapSolver key + a residential proxy to actually get
    # through (both are graceful no-ops otherwise, leaving the run on the login wall).
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        # The Mass Hiring apply URL is account.amazon.jobs/jobs/<id>/apply; it SAML-redirects to
        # passport.amazon.jobs. Match both hosts (and any *.amazon.jobs apply surface) so the
        # strategy owns the whole flow. Keep non-amazon.jobs hosts OUT.
        return ("account.amazon.jobs" in u or "passport.amazon.jobs" in u
                or ("amazon.jobs" in u and ("/apply" in u or "/jobs/" in u)))

    # ---- lifecycle ----------------------------------------------------------
    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # Stash context so open_form (called INSIDE super().prefill) can bootstrap the account
        # with the persona's email/name (base.open_form takes only `page`).
        self._pf = profile_form or {}
        self._facts = facts or {}
        self._profile_id = profile_id
        self._resume_path = resume_path
        # super().prefill runs our open_form (account bootstrap when gated on), then the shared
        # pipeline on the reached page (identity, email, eligibility, résumé upload). If we never
        # got past the Passport wall, analyze_page reports login_required and super() returns the
        # stopped report unchanged.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["account_password"] = getattr(self, "_account_pw", "")
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            # Still on the Passport account wall (the normal state for a dry-run without a captcha
            # key + residential proxy). Flag it so the caller/dashboard shows a needs-account state
            # rather than a phantom "form complete".
            report["needs_account"] = True
            return report
        try:
            await self._fill_amazon_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("amazon: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("amazon: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("amazon: wizard advance raised: %s", exc)
        return report

    async def open_form(self, page: Page) -> None:
        # The runner/co-pilot already navigated to the apply URL, which SAML-redirects to the
        # Passport register/login SPA. Dismiss the cookie banner FIRST (before any fill, so it
        # never resets a filled field or intercepts a click).
        await self._dismiss_cookie_banner(page)
        if not (self.advance_wizard and captcha_solver.is_enabled()):
            # No live account creation on a dry-run (or without a captcha key): leave the run on
            # the Passport wall — analyze_page will report login_required and stop cleanly.
            return
        try:
            await self._bootstrap_account(page)
        except Exception as exc:
            logger.debug("amazon: account bootstrap raised: %s", exc)
        await self._dismiss_cookie_banner(page)

    # ---- account bootstrap (LIVE, gated) ------------------------------------
    async def _on_passport(self, page: Page) -> bool:
        """True when we're on the Passport account wall (register/login), not the apply form."""
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "passport.amazon.jobs" in u:
            return True
        try:
            txt = ((await page.inner_text("body"))[:4000] or "").lower()
        except Exception:
            txt = ""
        return bool(re.search(r"create account|create a password|create your account|sign in", txt))

    async def _bootstrap_account(self, page: Page) -> None:
        """Create the Passport account for the persona and verify the emailed OTP, so the apply
        form becomes reachable. Best-effort and side-effect-free until the register button is
        pressed; ONLY runs when advance_wizard AND a captcha key are present (gated in open_form).

        The AWS WAF CAPTCHA guards /api/createAccountWithEmail (header X-Waf-Captcha-Token) — we
        solve it via captcha_solver.solve_aws_waf, which injects the aws-waf-token cookie; the
        Passport SDK then mints its own header token for the request. The OTP is read from the
        persona's own @takhet.com Maildir (verify_code.read_code)."""
        if not await self._on_passport(page):
            return
        email = self._account_email()
        if not email:
            return
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        start_ts = time.time()

        # 1) Solve the AWS WAF challenge BEFORE filling the register form (its token is short-lived
        # and IP-bound — solve it on the same session, right before the register request).
        try:
            await captcha_solver.solve_aws_waf(page)
        except Exception:
            pass

        # 2) Fill the register fields (resilient locators — the Passport SPA labels its inputs by
        # type/placeholder/autocomplete, not a stable data-testid we can rely on).
        name = self._persona_name()
        await self._fill_first(page, [
            'input[type="email"]', 'input[name="email" i]', 'input[autocomplete="email"]',
            'input[placeholder*="email" i]'], email)
        if name:
            await self._fill_first(page, [
                'input[name="name" i]', 'input[autocomplete="name"]',
                'input[placeholder*="full name" i]', 'input[placeholder*="name" i]'], name)
        # both the password and the confirm-password inputs
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

        # 3) Re-solve immediately before the register click (token may have expired), then click
        # Create account / Continue.
        try:
            await captcha_solver.solve_aws_waf(page)
        except Exception:
            pass
        await self._click_first(page, [
            'button:has-text("Create account")', 'button:has-text("Create Account")',
            'button:has-text("Continue")', 'button:has-text("Sign up")',
            'button[type="submit"]'])
        await page.wait_for_timeout(3000)

        # 4) Email OTP: Amazon mails a verification code to the persona box — read it and enter it.
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

        # 5) The apply form loads on account.amazon.jobs/applicant/... — let it settle. A late AWS
        # WAF re-challenge can appear here too; solve it best-effort.
        try:
            await captcha_solver.solve_aws_waf(page)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    def _account_email(self) -> str:
        pf = getattr(self, "_pf", {}) or {}
        return (pf.get("email") or pf.get("application_email") or "").strip()

    def _persona_name(self) -> str:
        pf = getattr(self, "_pf", {}) or {}
        return (pf.get("full_name") or pf.get("name") or "").strip()

    async def _await_email_code(self, email: str, since_ts: float,
                                attempts: int = 10, interval: float = 6.0) -> str:
        """Poll the persona's Maildir for Amazon's emailed verification code (reuses the same
        reader the co-pilot uses for GH/Ashby security codes). Returns '' if none arrives."""
        try:
            from backend.tools.verify_code import read_code
        except Exception:
            return ""
        for _ in range(max(1, attempts)):
            try:
                code = read_code(email, since_ts)
            except Exception:
                code = None
            if code:
                return code
            try:
                await self._page_sleep(interval)
            except Exception:
                pass
        return ""

    @staticmethod
    async def _page_sleep(seconds: float) -> None:
        import asyncio
        await asyncio.sleep(seconds)

    async def _fill_first(self, page: Page, selectors, value: str) -> bool:
        """Fill the first visible, empty input matching any selector."""
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

    # ---- Amazon-specific gap fill (authenticated apply form) ----------------
    async def _fill_amazon_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # EEO / voluntary self-ID + required legal consent — decline every demographic (never
        # claiming a protected characteristic) and tick required consent, via the shared helpers.
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        # Country-dependent State/Province select (populated after Country=United States).
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                if not await self._select_by_label(page, "state", state):
                    await self._select_by_label(page, "country", "United States")
                    await page.wait_for_timeout(1000)
                    await self._select_by_label(page, "state", state)
            except Exception:
                pass
        # Deterministic pre-screening / eligibility questions the analyzer misses (native selects
        # + radio groups), answered TRUTHFULLY for a synthetic US persona designed to fit the role.
        await self._answer_screeners(page, facts)

    async def _answer_screeners(self, page: Page, facts) -> None:
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("amazon: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("amazon: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered native <select> screeners; for each whose label maps to
        a deterministic answer, pick the matching option."""
        try:
            fields = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  const ph=t=>!t||/select an option|select a |please select|choose/i.test(t);
                  for(const el of document.querySelectorAll('select:not([multiple])')){
                    const l=el.id?document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'):null;
                    let t=((l&&l.innerText)||el.getAttribute('aria-label')||'');
                    if(!t){const b=el.closest('div,fieldset');t=b?(b.innerText||''):'';}
                    t=t.replace(/\\s+/g,' ').trim(); if(t.length<4) continue;
                    const cur=el.options[el.selectedIndex];
                    const answered=!!el.value && cur && !ph(cur.text);
                    const key=t.slice(0,110); if(seen.has(key)) continue; seen.add(key);
                    out.push({label:t, key, answered});
                  } return out;}""")
        except Exception:
            return
        for f in fields:
            if f.get("answered"):
                continue
            label = (f.get("label") or "").lower()
            key = f.get("key") or ""
            is_prof = bool(re.search(r"proficiency|language", label)
                           and re.search(r"english|spanish", label))
            values = self._screener_answer(label, facts)
            if is_prof and not values:
                high = True if "english" in label else bool(facts.get("bilingual"))
                values = (["Native", "Fluent", "Advanced", "Professional"] if high
                          else ["None", "No proficiency", "Basic", "Limited"])
            if not values:
                continue
            for v in values:
                if await self._select_by_label(page, key, v):
                    break

    async def _answer_radio_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED radio-group screener with a truthful, backed pick from
        _screener_answer. Leaves an unmatched group for the human rather than guessing."""
        facts = facts or {}
        try:
            groups = await page.evaluate(
                """()=>{const byName={};
                  for(const r of document.querySelectorAll('input[type=radio]')){
                    const nm=r.name||''; if(!nm) continue; (byName[nm]=byName[nm]||[]).push(r);}
                  const lab=r=>{const l=r.id?document.querySelector('label[for="'+
                        (window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;
                    return ((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'').trim();};
                  const out=[];
                  for(const nm in byName){const rs=byName[nm];
                    const opts=rs.map(r=>({value:r.value,text:lab(r).replace(/\\s+/g,' '),checked:r.checked}));
                    let box=rs[0].parentElement;
                    while(box&&!rs.every(r=>box.contains(r))) box=box.parentElement;
                    const optLen=opts.map(o=>o.text).join(' ').replace(/\\s+/g,'').length;
                    let g=0;
                    while(box&&box.parentElement&&g<4){
                      if((box.innerText||'').replace(/\\s+/g,'').length>optLen+10) break;
                      box=box.parentElement; g++;}
                    let qt=box?(box.innerText||''):'';
                    for(const o of opts) if(o.text) qt=qt.split(o.text).join(' ');
                    qt=qt.replace(/\\s+/g,' ').trim();
                    out.push({name:nm,label:qt,answered:rs.some(r=>r.checked),
                      options:opts.map(o=>({value:o.value,text:o.text}))});}
                  return out;}""")
        except Exception:
            return
        for grp in groups:
            if grp.get("answered"):
                continue
            cands = self._screener_answer((grp.get("label") or "").lower(), facts)
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

    # ---- widget helpers (native controls) -----------------------------------
    async def _select_by_label(self, page: Page, label_substr: str, value_substr: str) -> bool:
        """Pick the option whose text/value contains value_substr in a native <select> whose label
        contains label_substr — via select_option (fires change, so a Country-dependent State
        select repopulates). Skips a select that is ALREADY answered so two similar labels don't
        both bind to the first one."""
        info = await page.evaluate(
            """([lbl,val])=>{const n=s=>(s||'').toLowerCase();
              const placeholder=t=>!t||/select an option|select a |please select|choose/.test(n(t));
              for(const l of document.querySelectorAll('label')){
                if(!n(l.innerText).includes(lbl)) continue;
                let el=l.getAttribute('for')?document.getElementById(l.getAttribute('for')):null;
                if(!el||el.tagName!=='SELECT') el=(l.parentElement||document).querySelector('select');
                if(!el||el.tagName!=='SELECT') continue;
                if(el.value && !placeholder(el.options[el.selectedIndex]&&el.options[el.selectedIndex].text)) continue;
                const o=[...el.options].find(o=>o.value && (n(o.text).includes(val)||n(o.value).includes(val)));
                if(!o) continue;
                el.setAttribute('data-jf','1'); return {value:o.value};
              } return null;}""", [label_substr.lower(), value_substr.lower()])
        if not info:
            return False
        try:
            await page.select_option("select[data-jf='1']", value=info["value"])
            ok = True
        except Exception:
            ok = False
        try:
            await page.eval_on_selector("select[data-jf='1']", "e=>e.removeAttribute('data-jf')")
        except Exception:
            pass
        await page.wait_for_timeout(200)
        return ok

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
        """Tick every REQUIRED, currently-unchecked checkbox that is not a marketing opt-in
        (Passport's Terms/consent box), so the register step's Continue is not blocked."""
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
            logger.debug("amazon: checkbox tick raised: %s", exc)

    async def _tick_acknowledge(self, page: Page) -> None:
        """Tick a required certification/acknowledgement checkbox or radio (single affirmative
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
        """Deterministic, truthful answer candidates for an Amazon CS-role screener (lowercased
        label). Ordered strongest-first; returns None to leave a question for the human. Truthful
        for a synthetic US persona DESIGNED to fit the job (located at the job's place, native
        English, bilingual only when the role is)."""
        facts = facts or {}
        if re.search(r"acknowledge|i certify|i attest", t):
            return None                                   # handled by _tick_acknowledge
        # A bilingual-role Yes/No screener — the synth persona is designed to fit it (owner policy).
        # Must precede the english/spanish proficiency patterns (which return a scale, not Yes/No).
        if re.search(r"able to (speak|read|write|translate|converse)|"
                     r"fluent in .+ and english|bilingual in|proficient in .+ and english", t):
            return ["Yes"]
        if re.search(r"spanish", t):
            return (["Fluent", "Native", "Advanced", "Bilingual"] if facts.get("bilingual")
                    else ["None", "No proficiency", "Basic", "Beginner", "Limited"])
        if re.search(r"english", t):
            return ["Native", "Native or bilingual", "Fluent", "Advanced", "Professional"]
        if re.search(r"highest level of education|education (you have )?achieved|level of education", t):
            return [facts.get("education_level") or "Bachelor", "Bachelor", "High School",
                    "Associate", "GED"]
        # CS / technical-support experience — match either word order ("experience with customer
        # service" AND "years of technical support experience"). Amazon's Ring roles are
        # "Technical Customer Support", so the tech-support phrasing must resolve too.
        _cs = (r"customer service|call center|contact center|technical (?:customer )?support|"
               r"customer support|help ?desk|retail|customer")
        if re.search(rf"experience.*(?:{_cs})|(?:{_cs}).*experience", t):
            return ["5+ years", "5 or more", "More than 5", "6+ years", "5 years", "3-5 years",
                    "3+ years", "1-3 years", "Yes"]
        if re.search(r"(supervisor|leadership|management|managerial|team lead)\s*(or [a-z]+ )?experience|"
                     r"experience.*(supervisor|leadership|manage|team lead)|"
                     r"how (much|many years?).*experience|years of experience|total years", t):
            return ["5+ years", "5-10 years", "6-10 years", "4-5 years", "3-5 years", "5 years",
                    "More than", "1-3 years", "Yes"]
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat", t):
            return ["Yes"]
        # A schedule-conflict/attendance screener → No. Scoped so a behavioral "describe a time you
        # resolved a conflict" open-text prompt is NOT mistaken for a Yes/No screener.
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
                     r"background (check|investigation)", t):
            return ["Yes"]
        return None

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty visible fields on the current step, so the report's
        `unfilled` reflects the gap fill and the co-pilot's submit gate is honest."""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;
                    const req=el.required||el.getAttribute('aria-required')==='true';
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

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Close a cookie/consent banner that floats over the action bar and can intercept the
        register / Continue / Submit clicks."""
        for name in ("Reject Optional Cookies", "Reject All", "Accept All Cookies",
                     "Accept Cookies", "Accept All", "Accept the use of cookies", "I Agree"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    # ---- wizard walker (mirrors Avature/Oracle-ORC) -------------------------
    async def _step_signature(self, page: Page) -> str:
        """A cheap fingerprint of the current step, to tell whether a Continue click advanced
        (the corporate form re-renders in place)."""
        try:
            return await page.evaluate(
                "()=>{const a=document.querySelector('[aria-current=\"step\"],[aria-current=\"true\"],"
                ".progress-current,[class*=active][class*=step]');"
                "const h=document.querySelector('h1,h2,legend,[class*=step-title],[class*=section-title]');"
                "return (a?a.innerText.trim().slice(0,40):'')+'|'+(h?h.innerText.trim().slice(0,40):'');}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind) for the step's primary button: 'submit' on the final (Review)
        step, 'advance' on Continue/Next, else None."""
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
            logger.debug("amazon: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        """Fill an EEO / voluntary / review step: decline demographics, tick required consent,
        fill any ordinary matched fields, and answer the step's screeners."""
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
            logger.debug("amazon: step fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("amazon: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step apply wizard: click Continue while it advances (filling each new
        step), and STOP at the final Submit — recording its selector in the report WITHOUT
        clicking it. Solve any AWS WAF / reCAPTCHA that appears at the submit step first (both are
        graceful no-ops when their solver key is unset, so a dry-run is unaffected). If a Continue
        click does NOT advance (validation blocked it), stop and leave the gaps in `unfilled`."""
        for _ in range(7):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                # Final (Review) step — fill anything still on it, solve a submit-step captcha,
                # then record the true final-submit button (never a Continue). We do NOT click it.
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                try:
                    await captcha_solver.solve_aws_waf(page)
                except Exception:
                    pass
                try:
                    await captcha_solver.solve_on_page(page)
                except Exception:
                    pass
                report["submit_selector"] = (
                    "button:has-text('Submit application'), button:has-text('Submit'), "
                    "button[data-testid*='submit' i], [data-testid*='submit' i] button")
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
