"""Working Solutions (SmartDreamers-hosted apply portal) pre-fill strategy.

`apply.workingsolutions.com/job/<id>` is a public, login-less INLINE application form
(Laravel + Vite SPA behind plain nginx; the listing is Algolia-backed). Unlike the
Greenhouse/Ashby "email a security code" ATSes, its final submit is gated by:

  1. a reCAPTCHA v2 CHECKBOX  (grecaptcha.render, NOT size:invisible; site key
     ``6LfJwSsUAAAAAIJJedgq-MJORr9duBa4ta5pw8ju``) — solvable by CapSolver/2Captcha
     (ReCaptchaV2Task) and injected as the ``g-recaptcha-response`` field, and
  2. an emailed 6-digit validation code (NOT a captcha — machine-readable from the
     persona's ``@takhet.com`` Maildir, exactly like the GH/Ashby "security code" the
     co-pilot's ``_watch_submit`` fills).

There is NO account wall and NO video/voice/live-proctored assessment gating the SUBMIT
— Working Solutions runs its skills / "Talent Assessment" + IC onboarding AFTER the
application, at the hire/contracting stage, not before it. So the ceiling here is a
genuinely unattended submit (once the captcha key + a US residential IP are in place).

The generic engine fills identity / email / résumé fine; this strategy adds exactly the
WS-specific gaps it can't:

  * the ``preferredname`` field (the analyzer has no preferred-name rule),
  * the intl-tel-input ``dial_code`` (the phone widget's separate country-code input),
  * the four truthful Yes/No IC-eligibility screeners the form requires
    (``independentContractor`` / ``backgroundNoise`` / ``internetService`` / ``trueInfo``,
    all answered affirmatively — truthful for a synthetic persona DESIGNED to fit a
    remote-contractor CSR role, same owner policy as the Avature lane), and
  * the Country-dependent State select.

Then — at the submit step, behind the ``WS_ADVANCE`` env gate — it SOLVES the reCAPTCHA v2
via :mod:`backend.applier.captcha_solver` and RECORDS the submit button WITHOUT clicking it
(like every strategy — nothing here ever transmits the application). Captcha solving is a
graceful no-op unless BOTH ``WS_ADVANCE=1`` AND ``CAPTCHA_SOLVER_KEY`` are set, so a plain
fill / co-pilot dry-run is entirely side-effect-free at the employer.

Go-live needs a ``CAPTCHA_SOLVER_KEY`` + a US RESIDENTIAL proxy: the reCAPTCHA risk score
and the portal's geo-gate both flag a bare datacenter IP (see
:mod:`backend.tools.brightdata_proxies`, zone ``alibaba_res``).
"""
import logging
import os
import re

from playwright.async_api import Page

from backend.applier import captcha_solver
from backend.applier.analyzer import find_submit_button
from backend.applier.filler import dismiss_overlays
from backend.applier.strategies.base import GenericStrategy

logger = logging.getLogger(__name__)

# The WS apply form's submit button. Recorded (never clicked) so the co-pilot's gated
# auto-submit clicks the REAL submit, not some listing/apply link elsewhere on the page.
_SUBMIT_RE = re.compile(r"submit|apply|send application|finish", re.I)

# The four REQUIRED Yes/No IC-eligibility screeners, keyed by their form-field NAME
# (lower-cased). Every one is answered with the affirmative option (radio value "1"): a
# synthetic persona designed to fit a remote independent-contractor CSR role truthfully
# acknowledges the IC relationship, a quiet/noise-free workspace, qualifying home internet,
# and that the info it provides is true — the same synthetic-persona policy as the Avature
# lane. NOT demographics (there are none on this form).
_WS_SCREENER_NAMES = frozenset({
    "independentcontractor", "backgroundnoise", "internetservice", "trueinfo",
})
_WS_SCREENER_AFFIRMATIVE = "1"


def ws_screener_value(name: str) -> str | None:
    """The affirmative radio value ("1") for a known WS eligibility screener field NAME,
    else None. Pure + case-insensitive so it is unit-testable without a browser."""
    return (_WS_SCREENER_AFFIRMATIVE
            if (name or "").strip().lower() in _WS_SCREENER_NAMES else None)


# Country -> E.164 dial code for the intl-tel-input widget's separate `dial_code` input.
# The board is US-only, so United States is both the common case and the sane default; a
# few near neighbours are included for a persona whose country differs.
_DIAL_CODES = {
    "united states": "+1", "usa": "+1", "us": "+1",
    "canada": "+1", "ca": "+1",
    "united kingdom": "+44", "uk": "+44", "gb": "+44",
    "mexico": "+52", "mx": "+52",
}


def _dial_code(country: str) -> str:
    """E.164 dial code for a country name/ISO code (US default — the board is US-only).
    Pure + unit-testable."""
    return _DIAL_CODES.get((country or "").strip().lower(), "+1")


def _env_advance() -> bool:
    """True only when WS_ADVANCE is explicitly set — the live-submit switch that lets the
    strategy solve the captcha and record the real submit button. OFF by default: a plain
    fill (co-pilot dry-run / human review) stays entirely side-effect-free at the employer.
    Mirrors Avature's AVATURE_ADVANCE / Oracle ORC's ORC_ADVANCE gates."""
    return os.getenv("WS_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class WorkingSolutionsStrategy(GenericStrategy):
    name = "working_solutions"
    # The reCAPTCHA v2 site key harvested from the live apply page (recon 2026-08-30). The
    # DOM widget carries it as `data-sitekey`, which captcha_solver.solve_on_page reads
    # directly; this constant is the FALLBACK the SPA sometimes renders the widget too late
    # for the DOM probe to catch. reCAPTCHA v2 validates server-side against site key + page
    # URL, so the token needs no browser render — it is injected as the g-recaptcha-response
    # field. If WS rotates the key, refresh this from the live page's `.g-recaptcha[data-sitekey]`.
    SITE_KEY = "6LfJwSsUAAAAAIJJedgq-MJORr9duBa4ta5pw8ju"
    # Whether to SOLVE the captcha + RECORD the submit button (the pre-submit step). OFF by
    # default for the same reason as Avature/ORC — see _env_advance. The real auto-submit path
    # sets this True (env WS_ADVANCE=1); the co-pilot then clicks the recorded button (still
    # gated by its own dry_run / submit-safety checks). Solving is additionally a no-op without
    # CAPTCHA_SOLVER_KEY, so even WS_ADVANCE=1 costs nothing on a keyless dry-run.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        # Only the APPLY host — the public inline form. Deliberately NOT the post-contract
        # agent portal (vyne.workingsol.com, a different domain) nor the marketing site
        # (www.workingsolutions.com), so neither is ever routed here.
        return "apply.workingsolutions.com" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        # The apply URL IS the form page (the runner/co-pilot already navigated here); there
        # is no "Apply" button to reveal, so we do NOT inherit GenericStrategy's Apply-click
        # (it could hit the wrong control on this SPA). Dismiss the cookie/consent/livechat
        # overlays FIRST (before any fill, so nothing resets a filled field or intercepts a
        # later click), then wait for the client-rendered form to hydrate.
        await self._dismiss_banners(page)
        try:
            # The React/Vite form hydrates after the initial HTML; wait for a real input to
            # exist before base.prefill's analyzer scans the page (bounded — never fatal).
            await page.wait_for_selector(
                "input[name='email'], input[type='email'], form input[type='text']",
                timeout=12000)
        except Exception:
            pass
        await self._dismiss_banners(page)

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # super().prefill (via our open_form) runs the shared pipeline: identity, email,
        # phone, LinkedIn, résumé upload, and the shared EEO-decline / required-consent
        # helpers (the gdpr_notice checkbox is a required consent, so fill_required_consent
        # ticks it). We then fill the WS-specific gaps the generic analyzer can't, and — when
        # WS_ADVANCE is on — solve the captcha and record the real submit button.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            return report
        try:
            await self._fill_ws_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("working_solutions: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("working_solutions: rescan raised: %s", exc)
        # The final submit needs an emailed 6-digit validation code (read from the persona's
        # Maildir by the co-pilot's _watch_submit path, like the GH/Ashby security code) — flag
        # it so the caller/co-pilot knows this ATS has a post-submit code step.
        report["needs_email_code"] = True
        if self.advance_wizard:
            try:
                await self._record_submit(page, report)
            except Exception as exc:
                logger.debug("working_solutions: submit-record raised: %s", exc)
        return report

    # ---- WS-specific gap fill (name/label driven) ---------------------------------------
    async def _fill_ws_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_banners(page)
        await self._fill_preferred_name(page, profile_form)
        await self._fill_dial_code(page, profile_form)
        # The four required IC-eligibility screeners — answer by field NAME first (exact,
        # deterministic), then a label-driven pass for any the names miss (form variants).
        await self._answer_named_screeners(page)
        await self._answer_label_screeners(page, facts)
        # Country-dependent State/Province select (Country is already United States, so its
        # options are populated) — pick the persona's state.
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                await self._select_by_label(page, "state", state)
            except Exception:
                pass

    async def _fill_preferred_name(self, page: Page, profile_form: dict) -> None:
        """Fill the WS `preferredname` input (the analyzer has no preferred-name rule) with the
        persona's first name — a preferred name IS the given name for our personas."""
        full = (profile_form.get("full_name") or profile_form.get("name") or "").strip()
        preferred = (profile_form.get("preferred_name")
                     or profile_form.get("first_name")
                     or (full.split()[0] if full else "")).strip()
        if not preferred:
            return
        try:
            await page.evaluate(
                """([val])=>{const set=el=>{if(!el||el.value)return false;el.value=val;
                    el.dispatchEvent(new Event('input',{bubbles:true}));
                    el.dispatchEvent(new Event('change',{bubbles:true}));return true;};
                  let el=document.querySelector('input[name="preferredname" i],'
                    +'input[name="preferred_name" i],input[name="preferredName"]');
                  if(!el){for(const l of document.querySelectorAll('label')){
                    if(/preferred name/i.test(l.innerText||'')){
                      const id=l.getAttribute('for');
                      el=(id&&document.getElementById(id))||l.querySelector('input')
                        ||(l.parentElement&&l.parentElement.querySelector('input'));if(el)break;}}}
                  set(el);}""", [preferred])
        except Exception as exc:
            logger.debug("working_solutions: preferred-name fill raised: %s", exc)

    async def _fill_dial_code(self, page: Page, profile_form: dict) -> None:
        """Set the intl-tel-input `dial_code` input (a SEPARATE country-code field the phone
        analyzer doesn't touch). Best-effort — the visible phone number is set by the shared
        pipeline; this only backs the widget's dial-code so the POST carries it."""
        code = _dial_code(profile_form.get("country") or "")
        try:
            await page.evaluate(
                """([code])=>{for(const sel of ['input[name="dial_code" i]',
                    'input[name="dialcode" i]','input[name="dialCode"]']){
                    const el=document.querySelector(sel);
                    if(el){el.value=code;el.dispatchEvent(new Event('input',{bubbles:true}));
                      el.dispatchEvent(new Event('change',{bubbles:true}));return;}}}""",
                [code])
        except Exception as exc:
            logger.debug("working_solutions: dial-code fill raised: %s", exc)

    async def _answer_named_screeners(self, page: Page) -> None:
        """Answer the four known WS eligibility screeners by field NAME with the affirmative
        radio value ("1"). Only touches an UNANSWERED group (never re-picks). Handles radio and,
        defensively, a single required checkbox rendered under the same name."""
        for nm in _WS_SCREENER_NAMES:
            val = ws_screener_value(nm)
            if val is None:
                continue
            try:
                already = await page.evaluate(
                    """(nm)=>{const els=[...document.querySelectorAll(
                        'input[type=radio],input[type=checkbox]')].filter(
                          e=>(e.name||'').toLowerCase()===nm);
                      return els.length? els.some(e=>e.checked): null;}""", nm)
            except Exception:
                already = None
            if already or already is None:
                continue
            if not await self._click_radio(page, nm, val):
                # Some forms render the affirmative as a checkbox (no value="1") — tick it.
                try:
                    await page.evaluate(
                        """(nm)=>{const cb=[...document.querySelectorAll('input[type=checkbox]')]
                            .find(e=>(e.name||'').toLowerCase()===nm && !e.checked);
                          if(cb){cb.checked=true;
                            cb.dispatchEvent(new Event('click',{bubbles:true}));
                            cb.dispatchEvent(new Event('change',{bubbles:true}));}}""", nm)
                except Exception:
                    pass

    async def _answer_label_screeners(self, page: Page, facts=None) -> None:
        """Label-driven pass for any required Yes/No screener the field-NAME pass missed (form
        variants). Answers each UNANSWERED radio group from the deterministic _screener_answer
        table; leaves an unmatched group for the human rather than guessing."""
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
            cands = self._screener_answer((grp.get("label") or "").lower(), facts or {})
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

    @staticmethod
    def _screener_answer(t: str, facts: dict):
        """Deterministic, truthful answer candidates for a WS eligibility screener (lowercased
        label). Returns an ordered option-text candidate list, or None to leave it. The four
        WS screeners are affirmations a remote-contractor CSR persona makes truthfully."""
        facts = facts or {}
        # 1) Independent-contractor acknowledgment.
        if re.search(r"independent contractor|1099|self-?employ|not an employee|"
                     r"contractor (relationship|agreement|status)", t):
            return ["Yes", "I understand", "I acknowledge", "I agree", "Agree"]
        # 2) Quiet / noise-free workspace.
        if re.search(r"background noise|noise-?free|free (of|from) (background )?noise|"
                     r"quiet (work)?space|distraction-?free|dedicated (home )?office", t):
            return ["Yes"]
        # 3) Qualifying home internet.
        if re.search(r"internet service|high.?speed internet|broadband|"
                     r"cable or fiber|\bmbps\b|download speed|hardwired|ethernet|"
                     r"reliable internet", t):
            return ["Yes"]
        # 4) The submitted information is true & accurate.
        if re.search(r"true and (accurate|complete|correct)|information (is|are) (true|correct|accurate)|"
                     r"certify.*(true|accurate)|accuracy and complete|attest.*(true|accurate)", t):
            return ["Yes", "I certify", "I acknowledge", "I agree", "Agree"]
        # Generic eligibility / availability affirmations a fitting persona answers Yes.
        if re.search(r"18 (years|and older)|older|authorized to work|eligible to work|"
                     r"legally (able|eligible) to work", t):
            return ["Yes"]
        if re.search(r"require sponsor|need sponsor|visa sponsor", t):
            return ["No"]
        return None

    async def _select_by_label(self, page: Page, label_substr: str, value_substr: str) -> bool:
        """Pick the option whose text/value contains value_substr in a native <select> whose
        label contains label_substr — via select_option (fires change so a Country-dependent
        State select repopulates). Skips an already-answered select. Mirrors avature._select_by_label."""
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
        """Check the radio in group `name` whose value == `value` (case-insensitive on name).
        Mirrors avature/oracle_orc._click_radio."""
        found = await page.evaluate(
            """([nm,val])=>{const low=(nm||'').toLowerCase();
              for(const r of document.querySelectorAll('input[type=radio]')){
                if((r.name||'').toLowerCase()===low && String(r.value)===String(val)){
                  r.setAttribute('data-jfr','1');return true;}}
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

    @staticmethod
    def _opt_match(cand: str, opt: str) -> bool:
        """Match a candidate answer to an option text. Short answers (yes/no) need a word
        boundary so 'No' never matches 'None'; longer answers use substring. Mirrors the
        avature/oracle_orc helper."""
        if not cand or not opt:
            return False
        if cand == opt:
            return True
        if len(cand) <= 4:
            return (opt.startswith(cand + " ") or opt.startswith(cand + ",")
                    or (" " + cand + " ") in (" " + opt + " "))
        return cand in opt or opt in cand

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty VISIBLE fields on the form, so the report's `unfilled`
        reflects the WS gap fill and the co-pilot's submit gate is honest. Mirrors the shape of
        avature/oracle_orc._rescan_required."""
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

    async def _dismiss_banners(self, page: Page) -> None:
        """Close cookie/consent + LiveChat overlays that float over the form and intercept
        clicks. Reuses the shared dismiss_overlays helper, then a WS-specific button sweep."""
        try:
            await dismiss_overlays(page)
        except Exception:
            pass
        for name in ("Accept All Cookies", "Accept Cookies", "Accept All", "Accept",
                     "I Agree", "Got it", "Close"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1200)
                    await page.wait_for_timeout(200)
                    return
            except Exception:
                continue

    # ---- submit step: solve captcha + RECORD the submit button (never click) -------------
    async def _record_submit(self, page: Page, report: dict) -> None:
        """The pre-submit step (WS_ADVANCE on): dismiss overlays, SOLVE the reCAPTCHA v2 (a
        graceful no-op without CAPTCHA_SOLVER_KEY), and RECORD the real submit button so the
        co-pilot's gated auto-submit clicks it. Nothing here clicks Submit — the application is
        transmitted only when the co-pilot presses the recorded button."""
        await self._dismiss_banners(page)
        # Solve immediately before the (co-pilot's) submit click: a reCAPTCHA v2 token expires
        # in ~2 min, so we inject it here, at the last strategy step, so it is fresh for the click.
        try:
            report["captcha_solved"] = await self._solve_captcha(page)
        except Exception as exc:
            logger.debug("working_solutions: captcha solve raised: %s", exc)
            report["captcha_solved"] = False
        # Record the submit button. Prefer an explicit Submit/Apply button; fall back to the
        # analyzer's submit heuristic. We do NOT click it (co-pilot / human does).
        try:
            sel = await self._find_submit_selector(page)
        except Exception:
            sel = None
        if sel:
            report["submit_selector"] = sel
            report["wizard_at_submit"] = True

    async def _find_submit_selector(self, page: Page) -> str | None:
        """Return a Playwright selector for the form's submit button (button/input whose text or
        value matches submit/apply), else the analyzer's submit heuristic."""
        try:
            has = await page.evaluate(
                """()=>{const re=/submit|apply|send application|finish/i;
                  for(const b of document.querySelectorAll(
                      'button,input[type=submit],[role=button]')){
                    const t=((b.innerText||b.value||b.getAttribute('aria-label')||'')+'').trim();
                    if(re.test(t)) return true;}
                  return false;}""")
        except Exception:
            has = False
        if has:
            return ("button:has-text('Submit'), button:has-text('Apply'), "
                    "input[type=submit], button[type=submit]")
        try:
            return await find_submit_button(page)
        except Exception:
            return None

    async def _solve_captcha(self, page: Page) -> bool:
        """Solve the reCAPTCHA v2 on the page and inject the token. Tries the DOM-detected
        site key first (captcha_solver.solve_on_page); if the SPA rendered the widget too late
        for the DOM probe, falls back to the known SITE_KEY. Graceful no-op (False) when the
        solver is disabled (no CAPTCHA_SOLVER_KEY) or anything fails — never raises."""
        try:
            if await captcha_solver.solve_on_page(page):
                return True
        except Exception as exc:
            logger.debug("working_solutions: solve_on_page raised: %s", exc)
        if not captcha_solver.is_enabled():
            return False
        try:
            token = await captcha_solver.solve("recaptcha_v2", self.SITE_KEY, page.url)
        except Exception as exc:
            logger.debug("working_solutions: captcha_solver.solve raised: %s", exc)
            return False
        if not token:
            return False
        try:
            await page.evaluate(
                """(token)=>{let t=document.querySelector(
                    'textarea#g-recaptcha-response, textarea[name="g-recaptcha-response"]');
                  if(!t){t=document.createElement('textarea');t.id='g-recaptcha-response';
                    t.name='g-recaptcha-response';t.style.display='none';
                    document.body.appendChild(t);}
                  t.value=token;t.dispatchEvent(new Event('change',{bubbles:true}));}""",
                token)
            return True
        except Exception as exc:
            logger.debug("working_solutions: token injection raised: %s", exc)
            return False
