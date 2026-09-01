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
        await self._dismiss_chatbot(page)   # the Phenom chatbot overlay intercepts Apply/Submit clicks
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
            await self._dismiss_chatbot(page)

    async def _conduent_finish(self, page: Page, state: str = "") -> dict:
        """Walk Conduent's post-form multi-step submit: initial Submit/Next → DRP (Dispute Resolution
        Plan) acknowledgment checkbox + Submit → DRP-Applicant Agreement checkbox + Submit → any
        remaining step, until the on-page 'application received / thank you' confirmation. Re-fills the
        State/Province native <select> each step (its options load Country-dependently by AJAX, so the
        first fill can miss) and ticks required legal agreement/consent checkboxes (never marketing
        opt-ins). Returns {confirmed_onpage, blocked, steps}."""
        out = {"confirmed_onpage": False, "blocked": None, "steps": 0}
        _CONF = re.compile(r"thank you for (applying|your (interest|application))|application (has been )?"
                           r"(received|submitted|completed)|successfully submitted|we[' ]?ve received your "
                           r"application|your application (is complete|has been submitted)|"
                           # Conduent's post-submit thank-you page (verified live: reached + a real
                           # 'Thank You for Applying at Conduent' ack landed) — the app IS submitted here,
                           # the 'Required Assessment' is the post-application human skills test.
                           r"you.?re almost done|required (skills )?assessment|to complete your application "
                           r"(for|,)|complete the required (skills )?assessment|please click the link above",
                           re.I)
        for step in range(14):   # Conduent's wizard is long: personal → DRP → arbitration → demo select
            # → screeners ×2 → disability radio → veteran radio → … ; 8 was too few to reach the end.
            out["steps"] = step + 1
            await self._dismiss_chatbot(page)
            await self._dismiss_feedback_popup(page)   # survey overlay intercepts the DRP checkbox/Submit
            if state:                       # the State select is Country-dependent AJAX — re-fill each step
                try:
                    await self._fill_native_state(page, state)
                except Exception:
                    pass
            try:                            # phone must be digits + '+ # -' only (else step 1 won't submit)
                await self._sanitize_phone(page)
            except Exception:
                pass
            try:                            # DEBUG: what do the country/state selects actually read now?
                diag = await page.evaluate(
                    """()=>{const g=s=>s?{val:s.value,txt:((s.options[s.selectedIndex]||{}).text||'').slice(0,24),n:s.options.length}:null;
                      return {country:g(document.querySelector('#country')||document.querySelector('[name=\"rcrs-country\"]')),
                              state:g(document.querySelector('#state')||document.querySelector('[name=\"rcrs-region\"]'))};}""")
                print(f"[conduent step {step+1} selects: {diag}]", flush=True)
            except Exception:
                pass
            # scroll to the bottom so a lazily-rendered acknowledgment checkbox (the DRP box appears
            # AFTER the long plan text) is in the DOM before we tick it
            try:
                await page.evaluate("()=>window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(600)
            except Exception:
                pass
            # real Playwright .check() on the visible agreement checkbox(es) — a JS checked=true can be
            # ignored by a validation that listens for a real input event / custom widget
            try:
                cbs = page.locator('input[type="checkbox"]:visible')
                for i in range(min(await cbs.count(), 8)):
                    cb = cbs.nth(i)
                    try:
                        if await cb.is_checked():
                            continue
                        lab = (await cb.evaluate(
                            "c=>((c.labels&&c.labels[0]&&c.labels[0].innerText)||"
                            "(c.closest('label')?c.closest('label').innerText:'')||"
                            "((c.closest('div,li,td,fieldset,section')||{}).innerText||'')).slice(0,240)"))
                        low = (lab or "").lower()
                        if re.search(r"newsletter|marketing|opt.?in|subscribe|contact you about|text message|sms|whatsapp", low):
                            continue
                        if bool(await cb.evaluate("c=>c.required")) or re.search(
                                r"agree|acknowledge|consent|understand|dispute resolution|\bdrp\b|certify|terms|i have read|applicant agreement", low):
                            await cb.check(timeout=3000, force=True)
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                await page.evaluate(
                    """()=>{const skip=/newsletter|marketing|opt.?in|subscribe|contact you about|text message|sms/i;
                      for(const c of document.querySelectorAll('input[type=checkbox]')){
                        if(c.checked||c.disabled) continue;
                        const l=(c.labels&&c.labels[0]&&c.labels[0].innerText)||(c.closest('label')?c.closest('label').innerText:'');
                        const near=(c.closest('div,li,td,fieldset,section')||{}).innerText||'';
                        const t=(l||near||'').slice(0,240);
                        if(skip.test(t)) continue;
                        if(c.required||/agree|acknowledge|consent|understand|dispute resolution|\\bdrp\\b|certify|terms|i have read|applicant agreement/i.test(t)){
                          c.checked=true; c.dispatchEvent(new Event('input',{bubbles:true})); c.dispatchEvent(new Event('change',{bubbles:true}));}}}""")
            except Exception:
                pass
            try:
                body = (await page.inner_text("body"))[:6000]
            except Exception:
                body = ""
            if _CONF.search(body):
                out["confirmed_onpage"] = True
                print(f"[CONDUENT CONFIRMED on-page after {out['steps']} step(s)]", flush=True)
                try:
                    await page.screenshot(path=f"/tmp/phenom_confirmed_{int(time.time())}.png")
                except Exception:
                    pass
                return out
            # Decline any EEO/demographic <select> this step surfaced (gender/race/veteran/disability),
            # via real select_options (Opt Out / Prefer-not) so a required demographic doesn't block.
            try:
                await self._decline_native_demographics(page)
            except Exception:
                pass
            try:                            # CC-305 disability + other demographic RADIO groups → decline
                await self._decline_radio_demographics(page)
            except Exception:
                pass
            # Answer any eligibility Yes/No screener <select> (authorized to work → Yes, sponsorship → No)
            # deterministically + truthfully, so a required screener doesn't block the submit.
            try:
                await self._answer_native_screeners(page)
            except Exception:
                pass
            # Tick required agreement/DRP checkboxes via a REAL label click (the JS block above misses a
            # custom-styled checkbox whose <input> is hidden, and Conduent ignores a JS checked=).
            try:
                await self._tick_required_checkboxes(page)
            except Exception:
                pass
            # Re-commit Country + State via real select_options IMMEDIATELY before the submit click —
            # the checkbox ticking / scroll above can drop Conduent's validation flags, and its
            # validation accepts ONLY a real select_option (not a JS set), so commit both last, with
            # no intervening DOM ops, so the submit passes. (_fill_native_state select_options country,
            # waits for the dependent state AJAX, then select_options state.)
            if state:
                try:
                    await self._fill_native_state(page, state)
                except Exception:
                    pass
            clicked = False
            for sel in ('button:has-text("Submit")', 'button[type="submit"]', 'button:has-text("I Agree")',
                        'button:has-text("Agree")', 'button:has-text("Accept")', 'button:has-text("Continue")',
                        'button:has-text("Next")', 'input[type="submit"]', 'input[type="button"][value*="Next" i]',
                        'a[role="button"]:has-text("Submit")', 'a[role="button"]:has-text("Next")'):
                try:
                    b = page.locator(sel).first
                    if await b.count() and await b.is_visible(timeout=800):
                        await b.click(timeout=6000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                out["blocked"] = "no submit/continue button"
                print(f"[conduent: no submit button at step {out['steps']}]", flush=True)
                return out
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=12000)
            except Exception:
                pass
            await page.wait_for_timeout(2800)
            try:
                low = (await page.inner_text("body"))[:4000].lower()
                m = (re.search(r"please (select|enter|complete|provide|choose|check|agree)[^.]{0,55}", low)
                     or re.search(r"this (is a|field is) required[^.]{0,30}", low)
                     or re.search(r"you must (agree|acknowledge|check|accept)[^.]{0,45}", low)
                     or re.search(r"(is|are) required[^.]{0,30}", low))
                if m:
                    out["blocked"] = m.group(0)[:90]
                    print(f"[conduent step {out['steps']} validation: {out['blocked']}]", flush=True)
                    try:                    # DIAGNOSTIC: enumerate the required-but-empty fields left
                        req = await page.evaluate(r"""()=>{const out=[];
                          for(const el of document.querySelectorAll('input,select,textarea')){
                            const t=(el.type||'').toLowerCase();
                            if(['hidden','submit','button','reset','image'].includes(t)) continue;
                            const r=el.getBoundingClientRect(); if(!(r.width||r.height)) continue;
                            const req=el.required||el.getAttribute('aria-required')==='true';
                            let empty;
                            if(t==='checkbox'||t==='radio'){const nm=el.name;
                              empty=nm?![...document.querySelectorAll('[name="'+(window.CSS&&CSS.escape?CSS.escape(nm):nm)+'"]')].some(x=>x.checked):!el.checked;}
                            else if(el.tagName==='SELECT'){const c=el.options[el.selectedIndex];empty=!el.value||/please select|^select$|choose|^-$/i.test(((c&&c.text)||'').trim());}
                            else empty=!(el.value||'').trim();
                            if(!req||!empty) continue;
                            const lbl=el.id?((document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]')||{}).innerText||''):'';
                            const near=(el.closest('div,li,fieldset,section,td')||{}).innerText||'';
                            out.push({tag:el.tagName.toLowerCase(),type:t,id:el.id,name:el.name,
                              q:(lbl||near||'').replace(/\s+/g,' ').trim().slice(0,70),
                              opts:el.tagName==='SELECT'?[...el.options].slice(0,6).map(o=>o.text.trim()):undefined});
                          } return out.slice(0,12);}""")
                        if req:
                            print(f"[conduent step {out['steps']} required-empty ({len(req)}): {req}]", flush=True)
                    except Exception:
                        pass
            except Exception:
                pass
        return out

    async def _sanitize_phone(self, page: Page) -> None:
        """Conduent's phone field rejects anything but digits and + # - ('Only digits and special
        characters (+ # -) are allowed'), but the synthetic persona's phone is formatted like
        '+1 (614) 555-0184' (parentheses + spaces) → the personal-info submit is silently blocked and
        the wizard never advances (verified live: the sole difference between an advancing run and a
        stuck one was a parenthesised phone). Strip the disallowed characters and re-fill via a real
        Playwright .fill(). Also UNTICK the marketing WhatsApp/SMS opt-in (a synthetic persona must not
        opt into marketing) — never the required privacy consent."""
        try:
            loc = page.locator('#phoneNumber, [name="phoneNumber"], input[id*="phone" i]').first
            if await loc.count():
                cur = await loc.input_value()
                clean = re.sub(r"[^0-9+#-]", "", cur or "")
                if clean and clean != (cur or ""):
                    await loc.fill(clean, timeout=3000)
                    print(f"[conduent phone sanitized: {cur!r} -> {clean!r}]", flush=True)
        except Exception:
            pass
        try:                                  # untick marketing opt-in (whatsAppOptIn / SMS / marketing)
            await page.evaluate(
                """()=>{const M=/whatsapp|marketing|opt.?in|newsletter|contact you (with|about)|text message|\\bsms\\b/i;
                  for(const c of document.querySelectorAll('input[type=checkbox]')){
                    if(!c.checked) continue;
                    const lf=c.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(c.id):c.id)+'"]'):null;
                    const t=((lf&&lf.innerText)||(c.closest('label,div,li')||{}).innerText||'')+' '+(c.id||'');
                    if(M.test(t)){c.checked=false;c.dispatchEvent(new Event('input',{bubbles:true}));c.dispatchEvent(new Event('change',{bubbles:true}));}}}""")
        except Exception:
            pass

    async def _tick_required_checkboxes(self, page: Page) -> int:
        """Tick Conduent's required agreement / DRP acknowledgment checkboxes with a REAL interaction —
        preferring a click on the <label for=id> (Conduent renders a custom-styled checkbox whose real
        <input> is visually hidden, so `input:visible` never finds it and a JS `checked=true` is ignored
        by validation, exactly like the Country/State selects). Falls back to force-check, then a JS
        event dispatch. Skips marketing opt-ins; never ticks anything but required/agreement boxes.
        Verified need live: the DRP box id='businessServicesPlan' ('...OPPORTUNITY TO PRINT, READ AND
        REVIEW THE CONDUENT BUSINESS...') blocked the submit with 'you must agree'."""
        try:
            try:                              # the DRP box renders BELOW the long plan text — scroll it in
                await page.evaluate("()=>window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(500)
            except Exception:
                pass
            targets = await page.evaluate(r"""()=>{
              const SKIP=/newsletter|marketing|opt.?in|subscribe|contact you about|text message|sms|whatsapp/i;
              const WANT=/agree|acknowledge|consent|understand|dispute resolution|\bdrp\b|certify|terms|i have (had|read)|applicant agreement|opportunity to (print|read|review)|read and review/i;
              const out=[]; let i=0;
              for(const c of document.querySelectorAll('input[type=checkbox]')){
                i++;
                if(c.checked||c.disabled) continue;
                const lf=c.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(c.id):c.id)+'"]'):null;
                const lbl=(lf&&lf.innerText)||'';
                const near=(c.closest('label,div,li,td,fieldset,section')||{}).innerText||'';
                const txt=(lbl+' '+near).slice(0,240);
                if(SKIP.test(txt)) continue;
                const req=c.required||c.getAttribute('aria-required')==='true';
                if(!(req||WANT.test(txt))) continue;
                c.setAttribute('data-phchk','c'+i);
                out.push({id:c.id||'', chk:'[data-phchk="c'+i+'"]', hasLabel:!!lf});
              }
              return out;}""")
            n = 0
            for t in (targets or []):
                # RETRY: the survey popup re-appears and intercepts the label click, so the DRP box
                # intermittently never checks (verified: some runs stuck 'this is a required field' for
                # a dozen steps). Dismiss the popup, then try label-click / force-check / input-click,
                # verifying is_checked, up to 3×.
                checked = False
                for attempt in range(3):
                    try:
                        await self._dismiss_feedback_popup(page)
                    except Exception:
                        pass
                    if t.get("hasLabel") and t.get("id"):
                        try:
                            lab = page.locator(f'label[for="{t["id"]}"]').first
                            if await lab.count():
                                await lab.click(timeout=2500, force=True)
                        except Exception:
                            pass
                    try:
                        if await page.locator(t["chk"]).first.is_checked():
                            checked = True
                            break
                    except Exception:
                        pass
                    try:                       # force-check the input directly (custom-styled → force)
                        await page.locator(t["chk"]).first.check(timeout=2000, force=True)
                        if await page.locator(t["chk"]).first.is_checked():
                            checked = True
                            break
                    except Exception:
                        pass
                    await page.wait_for_timeout(500)
                try:
                    if not checked and not await page.locator(t["chk"]).first.is_checked():
                        await page.locator(t["chk"]).first.evaluate(
                            "c=>{c.checked=true;c.dispatchEvent(new Event('input',{bubbles:true}));"
                            "c.dispatchEvent(new Event('change',{bubbles:true}));c.dispatchEvent(new Event('click',{bubbles:true}));}")
                    n += 1
                except Exception:
                    pass
            if targets:
                print(f"[conduent ticked {n}/{len(targets)} agreement checkbox(es): {[t.get('id') for t in targets][:6]}]", flush=True)
            else:
                try:                          # DIAGNOSTIC: why did we match no checkbox to tick?
                    allcb = await page.evaluate(r"""()=>{const out=[];
                      for(const c of document.querySelectorAll('input[type=checkbox]')){
                        const lf=c.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(c.id):c.id)+'"]'):null;
                        const near=(c.closest('label,div,li,td,fieldset,section')||{}).innerText||'';
                        out.push({id:c.id||'',checked:c.checked,disabled:c.disabled,req:c.required||c.getAttribute('aria-required')==='true',
                          lab:((lf&&lf.innerText)||near||'').replace(/\s+/g,' ').trim().slice(0,50)});
                      } return out.slice(0,10);}""")
                    print(f"[conduent checkbox-scan found {len(allcb)}: {allcb}]", flush=True)
                except Exception:
                    pass
            return n
        except Exception:
            return 0

    async def _decline_native_demographics(self, page: Page) -> int:
        """Decline every EEO/demographic NATIVE <select> on the current Conduent step (gender / race /
        ethnicity / veteran / disability / orientation) by choosing its non-disclosure option — Opt Out /
        Not Specified / Prefer not to answer — via a REAL Playwright select_option (Conduent's validation
        accepts ONLY a real select, exactly like Country/State; a JS set leaves it 'required'). NEVER
        picks a real characteristic; a demographic select with no decline option is left blank. Returns
        the count declined. The Conduent apply's later wizard steps surface EEO selects the shared
        pipeline's earlier demographic pass never saw (verified live: step-5 gender select
        male/female/not specified/opt out blocked the submit)."""
        try:
            targets = await page.evaluate(r"""()=>{
              const DEMO=/gender|\bsex\b|\brace\b|racial|ethnic|hispanic|latino|veteran|disab|orientation|gender identity|pronoun|self.?ident|diversity/i;
              const DECL=/opt.?out|not specified|prefer not|decline|do not wish|does not wish|choose not|don'?t wish|not to (answer|disclose|identify)|undisclosed/i;
              const out=[]; let i=0;
              for(const s of document.querySelectorAll('select')){
                i++;
                const r=s.getBoundingClientRect(); if(!(r.width||r.height)) continue;
                const lbl=s.id?((document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(s.id):s.id)+'"]')||{}).innerText||''):'';
                const meta=lbl+' '+(s.name||'')+' '+(s.getAttribute('aria-label')||'');
                const optsText=[...s.options].map(o=>o.text).join(' ');
                if(!(DEMO.test(meta)||DEMO.test(optsText))) continue;
                const cur=(s.options[s.selectedIndex]||{}).text||'';
                if(s.value && !/please select|^select$|choose|^-$|^\s*$/i.test(cur.trim())) continue; // already set
                const d=[...s.options].find(o=>DECL.test(o.text));
                if(!d) continue;
                s.setAttribute('data-phdemo','d'+i);
                out.push({sel:'[data-phdemo="d'+i+'"]', value:d.value, text:d.text.slice(0,24)});
              }
              return out;}""")
            n = 0
            for t in (targets or []):
                try:
                    await page.select_option(t["sel"], value=t["value"], timeout=3000)
                    n += 1
                except Exception:
                    pass
            if n:
                print(f"[conduent declined {n} demographic select(s): {[t['text'] for t in targets][:6]}]", flush=True)
            return n
        except Exception:
            return 0

    async def _decline_radio_demographics(self, page: Page) -> int:
        """Decline demographic RADIO groups — the CC-305 Voluntary Self-Identification of Disability
        ('please check one of the boxes below'), veteran status, gender — by real-clicking the
        'I do not want to answer / prefer not' radio (Conduent's late wizard renders CC-305 as a radio
        group the <select> decline never touched; verified live it was the last blocker). Never selects
        a real characteristic; a group with no decline option is left blank."""
        try:
            targets = await page.evaluate(r"""()=>{
              const DEMO=/disab|veteran|gender|\bsex\b|\brace\b|racial|ethnic|hispanic|latino|orientation|self.?ident|cc-?305/i;
              const DECL=/do not (want|wish) to (answer|self.?identif|disclose)|don'?t (want|wish) to answer|prefer not to (answer|say|disclose)|decline to (self.?identif|answer|state)|i (don'?t|do not) wish|not (wish|want) to answer|choose not to (answer|identif)/i;
              const groups={};
              for(const r of document.querySelectorAll('input[type=radio]')){
                const k=r.name||('_anon_'+(r.id||''));
                (groups[k]=groups[k]||[]).push(r);
              }
              const out=[]; let i=0;
              for(const name in groups){
                const rs=groups[name];
                if(rs.some(r=>r.checked)) continue;
                const cont=rs[0].closest('fieldset,section,div,li,table')||document.body;
                const optText=rs.map(r=>{const lf=r.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;return (lf&&lf.innerText)||(r.closest('label')||{}).innerText||'';}).join(' | ');
                const q=((cont.innerText||'').slice(0,300))+' '+optText;
                if(!DEMO.test(q)) continue;
                const VET=/veteran|vevraa/i.test(q);
                const optLab=r=>{const lf=r.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;return (lf&&lf.innerText)||(r.closest('label')||{}).innerText||'';};
                let dec=null,dlab='';
                for(const r of rs){const t=optLab(r); if(DECL.test(t)){dec=r;dlab=t;break;}}
                // Veteran status has no 'prefer not' — the truthful synthetic answer is the NEGATIVE
                // ('I am NOT a protected veteran'); a synthetic persona has no military service (a
                // persona-DESIGN attribute, like avature's _decline_demographics). Never the positive.
                if(!dec && VET){for(const r of rs){const t=optLab(r);
                  if(/not (a )?(protected )?veteran|i am not (a |an )?(protected )?veteran|do not (consider|identify).{0,25}veteran|no,? ?i am not/i.test(t)
                     && !/i am a|identify as a (protected )?veteran|yes,? ?i am/i.test(t)){dec=r;dlab=t;break;}}}
                if(!dec) continue;
                i++; dec.setAttribute('data-phradio','r'+i);
                out.push({sel:'[data-phradio="r'+i+'"]', id:dec.id||'', lab:(dlab||'').replace(/\s+/g,' ').trim().slice(0,40)});
              }
              return out;}""")
            n = 0
            for t in (targets or []):
                done = False
                if t.get("id"):
                    try:
                        lab = page.locator(f'label[for="{t["id"]}"]').first
                        if await lab.count() and await lab.is_visible(timeout=500):
                            await lab.click(timeout=2000)
                            done = True
                    except Exception:
                        pass
                if not done:
                    try:
                        await page.locator(t["sel"]).first.check(timeout=2000, force=True)
                        done = True
                    except Exception:
                        pass
                try:
                    if not await page.locator(t["sel"]).first.is_checked():
                        await page.locator(t["sel"]).first.evaluate(
                            "r=>{r.checked=true;r.dispatchEvent(new Event('input',{bubbles:true}));"
                            "r.dispatchEvent(new Event('change',{bubbles:true}));r.dispatchEvent(new Event('click',{bubbles:true}));}")
                    n += 1
                except Exception:
                    pass
            if targets:
                print(f"[conduent declined {n}/{len(targets)} radio demographic(s): {[t.get('lab') for t in targets][:4]}]", flush=True)
            return n
        except Exception:
            return 0

    async def _answer_native_screeners(self, page: Page) -> int:
        """Answer Conduent's late-wizard eligibility Yes/No screener <select>s deterministically and
        TRUTHFULLY for a US synthetic persona, via real select_option (Conduent accepts only a real
        select). Touches ONLY an unanswered Yes/No select whose question clearly matches a known
        screener — authorized/eligible to work / right to work / 18+ / can provide proof → Yes;
        require sponsorship / worked for Conduent before / convicted → No — never guessing an ambiguous
        one. The 'authorized … WITHOUT sponsorship' polarity resolves to Yes first (verified live:
        step-7 'are you legally authorized to work' blocked the submit)."""
        try:
            targets = await page.evaluate(r"""()=>{
              const NO=/require (a )?(visa )?sponsorship|need (visa )?sponsorship|visa sponsorship (now|to|is)|sponsorship (now or|to work|in the future)|worked (for|at|with) conduent|previously (been )?employed (by|at) conduent|former (conduent )?employee|currently (employed|work) (for|at) conduent|convicted|felon|agreement with a (current|former|previous|prior)|non.?compete|restrictive covenant|conflict of interest|impede|interfere with your ability|employed by a foreign|foreign (government|owned|entity|company|state|interest)/i;
              const YES=/legally authoriz|authoriz(ed|ation) to work|authorised to work|eligible to work|right to work|lawfully (work|authoriz)|at least 18|18 years|age of 18|provide proof of.{0,20}(eligib|authoriz)|eligible for employment|able to provide (documentation|proof)|able to meet (the )?requirement|meet the requirements listed|confirm your (home )?internet|internet speed|reliable (high.?speed )?internet|(have|access to).{0,20}internet/i;
              const WITHOUT=/authoriz[^.]*without[^.]*sponsor|without (requiring|needing|the need) .{0,20}sponsor|work .{0,20}without sponsor/i;
              const out=[]; let i=0;
              for(const s of document.querySelectorAll('select')){
                i++; const r=s.getBoundingClientRect(); if(!(r.width||r.height)) continue;
                const lbl=s.id?((document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(s.id):s.id)+'"]')||{}).innerText||''):'';
                const near=(s.closest('div,li,fieldset,section,td')||{}).innerText||'';
                const q=(lbl+' '+near+' '+(s.name||'')+' '+(s.getAttribute('aria-label')||'')).slice(0,300);
                const cur=(s.options[s.selectedIndex]||{}).text||'';
                if(s.value && !/please select|^select$|choose|^-$|^\s*$/i.test(cur.trim())) continue;
                const opts=[...s.options];
                const yes=opts.find(o=>/^\s*yes\s*$/i.test(o.text));
                const no=opts.find(o=>/^\s*no\s*$/i.test(o.text));
                if(!yes||!no) continue;
                let pick=null;
                if(WITHOUT.test(q)) pick=yes;
                else if(NO.test(q)) pick=no;
                else if(YES.test(q)) pick=yes;
                if(!pick) continue;
                s.setAttribute('data-phscr','s'+i);
                out.push({sel:'[data-phscr="s'+i+'"]', value:pick.value, text:pick.text.trim(), q:q.replace(/\s+/g,' ').slice(0,60)});
              }
              return out;}""")
            n = 0
            for t in (targets or []):
                try:
                    await page.select_option(t["sel"], value=t["value"], timeout=3000)
                    n += 1
                except Exception:
                    pass
            if n:
                print(f"[conduent answered {n} screener(s): {[(t['q'], t['text']) for t in targets][:6]}]", flush=True)
            # Fill required free-TEXT screener questions the persona can answer neutrally + truthfully:
            # 'What compensation are you seeking?' → a market-rate answer (a required text field, not a
            # select — verified live it blocked the final step). Only an unanswered, clearly-matched one.
            try:
                texts = await page.evaluate(r"""()=>{
                  const COMP=/compensation|desired (salary|pay)|salary (expectation|requirement|desired)|pay you (are|.?re) seeking|expected (salary|pay|compensation)|what.{0,15}(salary|compensation|pay)/i;
                  const out=[]; let i=0;
                  for(const el of document.querySelectorAll('input[type=text],input:not([type]),textarea')){
                    i++; const r=el.getBoundingClientRect(); if(!(r.width||r.height)) continue;
                    if((el.value||'').trim()) continue;
                    const lbl=el.id?((document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]')||{}).innerText||''):'';
                    const near=(el.closest('div,li,fieldset,section,td')||{}).innerText||'';
                    const q=(lbl+' '+near+' '+(el.name||'')+' '+(el.id||'')).slice(0,200);
                    if(!COMP.test(q)) continue;
                    el.setAttribute('data-phtxt','t'+i);
                    out.push({sel:'[data-phtxt="t'+i+'"]', kind:'comp'});
                  }
                  return out;}""")
                for t in (texts or []):
                    val = "Market rate" if t.get("kind") == "comp" else ""
                    if val:
                        try:
                            await page.locator(t["sel"]).first.fill(val, timeout=3000)
                            n += 1
                        except Exception:
                            pass
                if texts:
                    print(f"[conduent filled {len(texts)} text screener(s)]", flush=True)
            except Exception:
                pass
            return n
        except Exception:
            return 0

    async def _fill_native_state(self, page: Page, state: str,
                                 country: str = "United States") -> bool:
        """Set the Conduent apply form's NATIVE Country + State/Province <select>s. The state options
        are Country-dependent AJAX that load only when the Country select fires a real change, so we
        ALWAYS (re)set Country by EXACT option text with input/change events — a native <select> always
        has a truthy `.value` (option 0 'Please select a country' has a non-empty value), so the old
        `if(!c.value)` guard skipped setting it and the states never loaded → submit failed
        'please select a country'. Then POLL until the state options populate and match the persona's
        state by full name. Idempotent (skips a select already on the target). Verified live: setting
        Country=United States loads 63 state options in ~0.8s, then State=Kentucky sticks."""
        # 1) Country — set via a REAL Playwright select_option (native interaction). A JS `.value=`
        #    + dispatched change DOES load the state AJAX, but Conduent's own submit validation still
        #    flags 'please select a country' (verified live: country reads 'United States', states
        #    loaded, yet submit blocks) — select_option clears it. Done every call while #country
        #    exists (it's gone past step 1); it reloads the states, which we re-set below. The analyzer
        #    may have JS-set the value already, so we force the real selection regardless of current text.
        try:
            loc = page.locator('#country, [name="rcrs-country"]').first
            if await loc.count():
                try:
                    await loc.select_option(label=country, timeout=4000)
                except Exception:
                    val = await loc.evaluate(
                        """(s,w)=>{const o=[...s.options].find(x=>x.text.trim().toLowerCase()===w.toLowerCase())
                             ||[...s.options].find(x=>/^united states$/i.test(x.text.trim()))
                             ||[...s.options].find(x=>/united states/i.test(x.text)&&!/minor|outlying/i.test(x.text));
                           return o?o.value:'';}""", country)
                    if val:
                        await loc.select_option(value=val, timeout=4000)
        except Exception:
            pass
        if not state:
            return False
        # 2) State — the Country-dependent options load by AJAX. Like Country, Conduent's validation
        #    accepts only a REAL select_option (a JS `.value=`+change loaded the options and showed
        #    'Kentucky' selected, yet submit still said 'please select state'), so POLL until the
        #    options populate, then commit via Playwright select_option (exact label, value fallback).
        sloc = page.locator('#state, [name="rcrs-region"]').first
        for _ in range(18):
            try:
                if not await sloc.count():
                    await page.wait_for_timeout(800)
                    continue
                info = await sloc.evaluate(
                    """(s)=>{const real=[...s.options].filter(o=>o.value && !/please select|select a|choose|^-$/i.test(o.text.trim()));
                       return {n:real.length};}""")
                if (info or {}).get("n", 0) < 2:
                    await page.wait_for_timeout(800)
                    continue
                try:
                    await sloc.select_option(label=state, timeout=3000)
                    return True
                except Exception:
                    val = await sloc.evaluate(
                        """(s,want)=>{const n=x=>(x||'').toLowerCase().trim();
                           const o=[...s.options].find(o=>n(o.text)===n(want)||n(o.value)===n(want))
                             ||[...s.options].find(o=>n(o.text).includes(n(want)));
                           return o?o.value:'';}""", state)
                    if val:
                        await sloc.select_option(value=val, timeout=3000)
                        return True
                    print(f"[phenom state '{state}' no-match]", flush=True)
                    return False
            except Exception:
                pass
            await page.wait_for_timeout(800)
        return False

    async def _dismiss_chatbot(self, page: Page) -> None:
        """The Phenom chatbot popup (#phenomChatbotWrapper, auto-opens 'window-open') overlays the page
        and INTERCEPTS pointer events, so Playwright's Apply/Submit clicks retry forever. Inject a
        persistent style hiding it (+ any Phenom chat overlay) so clicks reach the form. Re-injected per
        navigation since a style tag is per-document."""
        try:
            await page.add_style_tag(content=(
                "#phenomChatbotWrapper,.phenom-chatbot-wrapper,[id^='phenomChatbot'],"
                "[class*='chatbot-wrapper'],[data-ph-at-id*='chatbot']"
                "{display:none!important;pointer-events:none!important;visibility:hidden!important;}"))
        except Exception:
            pass
        try:
            await page.evaluate(
                "()=>{for(const s of ['#phenomChatbotWrapper','.phenom-chatbot-wrapper']){"
                "const e=document.querySelector(s); if(e){e.style.display='none';e.style.pointerEvents='none';}}}")
        except Exception:
            pass

    async def _dismiss_feedback_popup(self, page: Page) -> None:
        """Dismiss the Conduent site survey overlay ('How would you rate your experience?', a
        Qualtrics/Medallia widget) that floats over the apply form's bottom-left and INTERCEPTS the
        DRP acknowledgment checkbox / Submit clicks (verified live: the final step blocked on the DRP
        'This is a required field' while this popup covered the checkbox). Hide its overlay container +
        click any close control. Targeted by the widget's own text so no form control is hidden."""
        try:
            await page.evaluate(
                """()=>{
                  const vw=innerWidth, vh=innerHeight;
                  const hit=e=>/how would you rate your experience|rate your experience|send feedback/i.test((e.innerText||''))&&(e.innerText||'').length<400;
                  for(const e of document.querySelectorAll('div,section,aside')){
                    if(!hit(e)) continue;
                    // walk up AT MOST 3 levels to a fixed/absolute OVERLAY; hide it ONLY if it is small
                    // (a floating widget, < 60% of the viewport) so we never hide the form/page container.
                    let c=e, overlay=null;
                    for(let i=0;i<3 && c;i++){const st=getComputedStyle(c);
                      if(st.position==='fixed'||st.position==='absolute'){overlay=c;break;} c=c.parentElement;}
                    const t=overlay||e;
                    const r=t.getBoundingClientRect();
                    if(r.width>vw*0.6 && r.height>vh*0.6) continue;   // too big — not the widget, leave it
                    try{t.style.display='none';t.style.pointerEvents='none';t.style.visibility='hidden';}catch(_){}
                  }
                  // known survey-widget containers (Qualtrics QSI / Medallia) — safe, name-scoped
                  for(const s of ['[id^="QSI" i]','[class*="QSIFeedbackButton" i]','[class*="medallia" i]']){
                    for(const e of document.querySelectorAll(s)){try{e.style.display='none';e.style.pointerEvents='none';}catch(_){}}
                  }
                }""")
        except Exception:
            pass
        # click an explicit close (×) if the widget exposes one as a real control
        for sel in ('button[aria-label="Close" i]', 'a[aria-label="Close" i]',
                    '[title="Close" i]', 'button.close'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=400):
                    await b.click(timeout=1200)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                continue

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
        # Conduent's Phenom apply form renders State/Province as a NATIVE <select> (not an Oracle JET
        # oj-select), so the parent's JET state-filler leaves it "Please select" → the submit fails
        # "Please select State". Set the native select to the persona's state directly.
        try:
            await self._fill_native_state(page, (profile_form or {}).get("state") or "")
        except Exception as exc:
            logger.debug("phenom: native state fill raised: %s", exc)
        # Conduent's apply is a MULTI-STEP submit (identity form → DRP acknowledgment → DRP-Applicant
        # Agreement → …). The shared driver clicks Submit only ONCE, so walk the remaining steps here
        # when the submit gate is on. Gated by PHENOM_ADVANCE (the driver's gate) or ORC_ADVANCE.
        if os.getenv("PHENOM_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on") \
                or os.getenv("ORC_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on"):
            try:
                fin = await self._conduent_finish(page, (profile_form or {}).get("state") or "")
                report["conduent_finish"] = fin
                if fin.get("confirmed_onpage"):
                    report["confirmed_onpage"] = True
            except Exception as exc:
                logger.debug("phenom: conduent finish raised: %s", exc)
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
