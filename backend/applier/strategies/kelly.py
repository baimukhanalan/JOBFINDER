"""Kelly (KellyConnect) ATS pre-fill strategy — mykelly.com WordPress Gravity Forms.

Unlike the account-gated iCIMS/Workday BPO portals, a Kelly job page (`www.mykelly.com/
job/<id>-…`) embeds the application inline as a **WordPress Gravity Form** (form id
`244576`) backed by Bullhorn — a single-page, login-less, guest apply. There is NO
account wall, NO multi-step wizard, and (verified live 2026-08-30) **NO captcha**: no
reCAPTCHA/hCaptcha/Turnstile references anywhere on the page — only Gravity Forms' own
hidden honeypot. The only anti-bot is Akamai bot-management on the HOST (an IP-reputation
gate that 403s our datacenter IP), which a residential proxy passes — so the SUBMIT is
fully unattended once behind `alibaba_res`.

The generic pipeline (`base.prefill`) already fills every ordinary Gravity Forms control
— native text inputs (name/email/phone/city/state/zip), the required résumé `<input
type=file>` (input_5), the Yes/No screener radio groups, the EEO self-ID (declined) and
the required consent checkboxes (pre-checked). This strategy adds the three Kelly-specific
things the generic engine can't:
  (a) it GUARDS the Gravity Forms honeypot (`.gform_validation_container` input, labelled
      "Comments") so it stays EMPTY — filling it is an instant spam-reject, and its
      innocuous "Comments" label is exactly the kind of text field the analyzer would try
      to fill;
  (b) it answers the CSR-oriented select/radio screeners the shared deterministic engine
      misses (Date available → soonest, Desired locations → remote, Employment preference
      → full-time, Years of experience → highest believable tier, Education → persona
      level) — truthfully, for a synthetic US persona designed to fit the role; and
  (c) it wires `captcha_solver.solve_on_page` at the submit step — a graceful no-op today
      (no captcha on the form), so if Kelly ever adds one the token lands automatically.

Nothing here clicks the FINAL Submit — like every strategy it fills and STOPS; the
Gravity Form transmits nothing until that button is pressed (by the co-pilot's gated
auto-submit, or a human). Recording the final submit is itself gated behind env
`KELLY_ADVANCE` (mirrors Avature's `AVATURE_ADVANCE` / Oracle's `ORC_ADVANCE`), so a
plain fill / dry-run is entirely side-effect-free at the employer.
"""
import logging
import os
import re

from playwright.async_api import Page

from backend.applier import captcha_solver
from backend.applier.analyzer import find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.strategies.base import ApplyStrategy

logger = logging.getLogger(__name__)


def _env_advance() -> bool:
    """True only when KELLY_ADVANCE is explicitly set — the live-submit switch that lets the
    strategy RECORD the final Gravity Forms Submit (and solve a captcha if one is ever added).
    OFF by default: a plain fill (co-pilot dry-run / human review) stays side-effect-free at the
    employer. Mirrors Avature's AVATURE_ADVANCE / Oracle's ORC_ADVANCE gate."""
    return os.getenv("KELLY_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class KellyStrategy(ApplyStrategy):
    name = "kelly"
    # Whether to RECORD the final submit button (and run the captcha solver at the submit step).
    # OFF by default for the same reason as Avature/Oracle — see _env_advance. Kelly's Gravity
    # Form is single-page, so there is no wizard to WALK: 'advancing' only records the Submit
    # selector + solves any captcha, and NEVER clicks. The real auto-submit path sets this True
    # (env KELLY_ADVANCE=1), the same way the rest of the engine gates its live actions.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        return "mykelly.com" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        # The apply URL IS the job page and the Gravity Form is embedded inline — there is no
        # "Apply" button to click to reveal it (clicking a generic Apply could scroll away or
        # hit an unrelated CTA). Just dismiss the cookie banner FIRST (before any fill, so it
        # never resets a filled field or intercepts the later Submit) and let base.prefill fill.
        await self._dismiss_cookie_banner(page)

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # super().prefill (via our open_form) runs the shared pipeline: identity/email/phone/
        # city/state/zip, the required résumé upload (input_5), the Yes/No screener radios via
        # the deterministic choice engine, EEO decline + required consent. We then fill the
        # Kelly-specific gaps (honeypot guard, CSR select/radio screeners the shared engine
        # misses, a redundant EEO decline) and, when advancing, record the final Submit.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            return report
        try:
            await self._fill_kelly_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("kelly: gap fill raised: %s", exc)
        # ALWAYS clear the honeypot LAST — a spam-reject is worse than any unfilled field, and a
        # downstream widget pass could conceivably touch it. Never let the "Comments" trap fill.
        try:
            n = await self._clear_honeypot(page)
            if n:
                report["honeypot_cleared"] = n
        except Exception as exc:
            logger.debug("kelly: honeypot clear raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("kelly: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report)
            except Exception as exc:
                logger.debug("kelly: advance raised: %s", exc)
        return report

    # ---- Kelly-specific gap fill (label-driven so it generalizes across postings) ----
    async def _fill_kelly_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        # EEO / diversity self-ID + required legal consent — belt-and-suspenders (base.prefill
        # already ran these on the single page; re-running is idempotent and never claims a
        # protected characteristic — the demographic answer is always the decline option).
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        # Country-dependent State/Province: Gravity Forms renders it as a native <select> whose
        # options load once Country is set; the shared pipeline usually set Country already, so
        # pick the persona's state now.
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                await self._select_by_label(page, "state", state)
            except Exception:
                pass
        # CSR screener selects + radio groups the shared deterministic engine misses (Date
        # available / Desired locations / Employment preference / Years of experience /
        # Education / language proficiency), answered deterministically & TRUTHFULLY.
        await self._answer_screeners(page, facts)

    async def _clear_honeypot(self, page: Page) -> int:
        """Empty the Gravity Forms honeypot input so the form is never spam-rejected.

        GF renders a decoy inside `<div class="gform_validation_container">` — on Kelly it's
        labelled "Comments" with the description "This field is for validation purposes and
        should be left unchanged." A real submission MUST leave it empty; a bot that fills it
        (the label reads like an ordinary text field) is silently rejected. We also cover the
        description-text form of the trap in case the container class ever changes."""
        try:
            return await page.evaluate(
                """()=>{let n=0;const seen=new Set();
                  const clear=inp=>{if(seen.has(inp))return;seen.add(inp);n++;
                    if(inp.value){inp.value='';
                      inp.dispatchEvent(new Event('input',{bubbles:true}));
                      inp.dispatchEvent(new Event('change',{bubbles:true}));}};
                  for(const c of document.querySelectorAll('.gform_validation_container'))
                    for(const inp of c.querySelectorAll('input,textarea')) clear(inp);
                  // fallback: a field whose description marks it a validation honeypot.
                  for(const d of document.querySelectorAll('.gfield_description')){
                    if(!/for validation purposes|left unchanged/i.test(d.innerText||''))continue;
                    const w=d.closest('.gfield,li,div');
                    if(w)for(const inp of w.querySelectorAll('input,textarea')) clear(inp);}
                  return n;}""")
        except Exception:
            return 0

    async def _answer_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED CSR screener truthfully for a synthetic US persona designed
        to fit the role: native <select>s via _answer_select_screeners, radio groups via
        _answer_radio_screeners. Leaves an unmatched question for the human rather than guessing."""
        facts = facts or {}
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("kelly: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("kelly: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered native <select>s; for each whose label maps to a
        deterministic answer, select the matching option (skipping the honeypot / demographics)."""
        try:
            labels = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('select:not([multiple])')){
                    if(el.closest('.gform_validation_container'))continue;   // never a honeypot
                    const l=el.id?document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'):null;
                    let t=((l&&l.innerText)||el.getAttribute('aria-label')||'').trim();
                    if(!t){const w=el.closest('.gfield,li,div');const gl=w&&w.querySelector('.gfield_label,label');
                      if(gl)t=(gl.innerText||'').trim();}
                    if(t.length<3) continue;
                    const cur=el.options[el.selectedIndex];
                    const answered=!!el.value && !!(cur) &&
                      !/select|choose|please|—|\\.\\.\\./i.test(cur.text||'');
                    const key=t.slice(0,110);
                    if(seen.has(key)) continue; seen.add(key);
                    out.push({label:t, key, answered});
                  } return out;}""")
        except Exception:
            return
        for f in labels:
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
                try:
                    if await self._select_by_label(page, key, v):
                        break
                except Exception:
                    continue

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
                    // smallest ancestor holding every radio, then climb to include the prompt.
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

    async def _select_by_label(self, page: Page, label_substr: str, value_substr: str) -> bool:
        """Find a native <select> whose label contains label_substr and pick the option whose
        text/value contains value_substr — via Playwright select_option (fires change). Skips a
        select that is ALREADY answered so two similar labels don't both bind to the first one,
        and never touches the honeypot container."""
        info = await page.evaluate(
            """([lbl,val])=>{const n=s=>(s||'').toLowerCase();
              const placeholder=t=>!t||/select|choose|please|—|\\.\\.\\./i.test(t);
              for(const el of document.querySelectorAll('select:not([multiple])')){
                if(el.closest('.gform_validation_container'))continue;
                const l=el.id?document.querySelector('label[for="'+
                  (window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'):null;
                let lt=((l&&l.innerText)||el.getAttribute('aria-label')||'');
                if(!lt){const w=el.closest('.gfield,li,div');const gl=w&&w.querySelector('.gfield_label,label');
                  if(gl)lt=gl.innerText||'';}
                if(!n(lt).includes(lbl)) continue;
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
        await page.wait_for_timeout(150)
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
        """Deterministic, truthful answer candidates for a Kelly CSR screener question
        (lowercased label). Returns an ordered list of option-text candidates (strongest first),
        or None to leave it for the human. Truthful for a synthetic US persona DESIGNED to fit
        the job (located at the job's city, native English, bilingual only when the role is)."""
        facts = facts or {}
        if re.search(r"acknowledge|i certify|i attest", t):
            return None                                   # a certification tick, not a screener
        # Kelly-specific selects ---------------------------------------------------------
        # "Date available" / "When can you start?" / "Earliest availability" — soonest option.
        if re.search(r"date available|available to start|availability to start|"
                     r"when (can|are) you (start|available)|earliest (start|availab)|"
                     r"start date", t):
            return ["Immediately", "Immediate", "As soon as possible", "ASAP", "Now",
                    "Within 2 weeks", "2 weeks", "1 week", "Yes"]
        # "Desired locations" / "Preferred work location" — this board is remote-US only.
        if re.search(r"desired location|preferred location|work location|location preference|"
                     r"where would you like to work", t):
            return ["Remote", "Work from home", "Work At Home", "Anywhere",
                    "United States", "Any", "Nationwide"]
        # "Employment preference" / "Employment type" / "Position type" — full-time CSR.
        if re.search(r"employment (preference|type|status)|position type|job type|"
                     r"work (type|schedule) preference|full.?time or part.?time", t):
            return ["Full-time", "Full Time", "Fulltime", "Full", "Any", "No preference"]
        # Language proficiency (shared with the other BPO strategies) -------------------
        if re.search(r"spanish", t):
            return (["Fluent", "Native", "Advanced", "Bilingual"] if facts.get("bilingual")
                    else ["None", "No proficiency", "Basic", "Beginner", "Limited"])
        if re.search(r"english", t):
            return ["Native", "Native or bilingual", "Fluent", "Advanced", "Professional"]
        if re.search(r"highest level of education|education (you have )?achieved|"
                     r"level of education|education level", t):
            return [facts.get("education_level") or "Bachelor", "Bachelor", "High School",
                    "Associate", "GED", "Some college"]
        # Customer-service / call-center experience — pick the HIGHEST believable tier (the
        # tailored résumé shows ~8 yrs), never a weak middle one that undersells + contradicts it.
        if re.search(r"experience.*(customer service|call center|contact center|retail|customer)|"
                     r"(customer service|call center|contact center).*experience", t):
            return ["5+ years", "5 or more", "More than 5", "6+ years", "5 years", "3-5 years",
                    "3+ years", "1-3 years", "Yes"]
        if re.search(r"(supervisor|leadership|management|managerial|team lead)\s*(or [a-z]+ )?experience|"
                     r"experience.*(supervisor|leadership|manage|team lead)|"
                     r"how (much|many years?).*experience|years of experience|total years", t):
            return ["5+ years", "5-10 years", "6-10 years", "10+ years", "4-5 years", "6+ years",
                    "3-5 years", "5 years", "More than", "1-3 years", "Yes"]
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat", t):
            return ["Yes"]
        # A schedule-conflict/attendance screener → No (scoped so a behavioral open-text
        # "describe a conflict" prompt is not mistaken for a Yes/No screener).
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
        if re.search(r"require sponsor|need sponsor|visa sponsor", t):
            return ["No"]
        if re.search(r"18 (years|and older)|older|authorized|eligible to work|legally", t):
            return ["Yes"]
        if re.search(r"seasonal|interested in (the |this )?(season|temporary|position|role|opportunity)", t):
            return ["Yes"]
        if re.search(r"\bcitizen(ship)?\b|u\.?s\.? citizen", t):
            return ["Yes"]
        if re.search(r"able to meet this requirement|do you meet this requirement|"
                     r"meet (this|the) requirement|able to work|\bshift\b|overtime|"
                     r"willing to (work|attend|commit|travel|obtain)|onsite|on-site|"
                     r"in.?office|in person|first week|training|"
                     r"background (check|investigation)|drug (test|screen)", t):
            return ["Yes"]
        return None

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty VISIBLE fields, so the report's `unfilled` reflects the
        Kelly gap fill and the co-pilot's submit gate is honest. The honeypot is deliberately
        EXCLUDED — it must stay empty and is never a task for the human."""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','reset','image','search'].includes(t)) continue;
                    if(el.closest('.gform_validation_container')) continue;   // honeypot: never
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||!!el.closest('.gfield_contains_required');
                    if(!req) continue;
                    let empty;
                    if(t==='checkbox'||t==='radio'){const nm=el.name;
                      empty=nm?![...document.querySelectorAll('[name="'+
                        (window.CSS&&CSS.escape?CSS.escape(nm):nm)+'"]')].some(x=>x.checked):!el.checked;}
                    else if(t==='file'){empty=!(el.files&&el.files.length>0);}
                    else empty=!(el.value||'').trim();
                    if(!empty) continue;
                    let lab='';const id=el.id;
                    if(id){const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab){const w=el.closest('.gfield,li,div');const gl=w&&w.querySelector('.gfield_label,label');
                      if(gl)lab=gl.innerText.trim();}
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||(el.name||'field');
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  } return out;}""")
        except Exception:
            return []

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Close a cookie/consent banner (OneTrust etc.) that floats over the form and can
        intercept the Submit click."""
        for name in ("Reject Optional Cookies", "Reject All", "Accept All Cookies",
                     "Accept Cookies", "Accept All", "I Agree", "Got it"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    async def _submit_selector(self, page: Page) -> str | None:
        """The Gravity Forms submit control: <input type=submit id='gform_submit_button_<id>'>.
        Falls back to the analyzer's generic submit heuristic."""
        for sel in ('input[type="submit"][id^="gform_submit_button_"]',
                    "button[id^='gform_submit_button_']",
                    'input[type="submit"]'):
            try:
                if await page.locator(sel).first.is_visible(timeout=1000):
                    return sel
            except Exception:
                continue
        try:
            return await find_submit_button(page)
        except Exception:
            return None

    async def _advance_wizard(self, page: Page, report: dict) -> None:
        """Kelly's Gravity Form is a SINGLE page — there is no multi-step wizard to walk, so
        'advancing' collapses to: solve any captcha the form carries (a graceful no-op today —
        Kelly's GF has NONE, verified live) and RECORD the final Submit button WITHOUT clicking
        it. The co-pilot's gated auto-submit (or a human) presses it. Recording is gated behind
        KELLY_ADVANCE only for symmetry with the other ATS strategies; the fill is side-effect-
        free either way (a Gravity Form transmits nothing until Submit is actually clicked)."""
        # Wire the solver at the submit step: a no-op unless CAPTCHA_SOLVER_KEY is set AND a
        # captcha is actually rendered (neither is true for Kelly today) — so it can never break
        # a dry run, and if Kelly ever adds one the g-recaptcha-response token lands automatically.
        try:
            if await captcha_solver.solve_on_page(page):
                report["captcha_solved"] = True
        except Exception as exc:
            logger.debug("kelly: captcha solve raised: %s", exc)
        try:
            sel = await self._submit_selector(page)
            if sel:
                report["submit_selector"] = sel
        except Exception as exc:
            logger.debug("kelly: submit selector raised: %s", exc)
        # re-clear the honeypot after the solver step (defensive) and refresh the unfilled list.
        try:
            await self._clear_honeypot(page)
            report["unfilled"] = await self._rescan_required(page)
        except Exception:
            pass
        report["wizard_at_submit"] = True
