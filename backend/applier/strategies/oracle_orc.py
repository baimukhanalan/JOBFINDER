"""Oracle Recruiting Cloud (ORC) / Candidate Experience (CX) pre-fill strategy.

Alorica and other high-volume BPOs host their careers on Oracle's SaaS Candidate
Experience site (e.g. `fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/
CandidateExperience/en/sites/CX_1/job/<id>`). Like Greenhouse/Ashby it is a
LOGIN-LESS guest apply, but the flow is a multi-step wizard:

    job page → click Apply (startApplication) → Personal info → optional
    pre-screening Yes/No → Diversity/EEO → Review → Submit

The only anti-bot at the end is (a) an emailed PIN (machine-readable from the
persona's Maildir, exactly like the GH/Ashby "security code" — the co-pilot's
`_watch_submit` fills it) and (b) an INVISIBLE reCAPTCHA v3 the page JS executes
itself — there is NO interactive captcha, no account wall, no video/voice
assessment. So the ceiling here is a full auto-submit, making Oracle ORC the 2nd
fully-autonomous ATS on the Mass Hiring board after Maximus/Avature.

The one thing the generic engine can't do is Oracle's JET custom elements
(`oj-input-text`, `oj-select-single`, `oj-radioset`/`oj-checkboxset`,
`oj-file-picker`): the analyzer doesn't recognize them, and a JET select needs a
click→type→pick type-ahead, never a plain `.fill`. This strategy adds exactly that
component-aware fill layer plus a wizard-walker, and reuses the shared pipeline
(`base.prefill`) for every ordinary input.

Nothing here clicks the FINAL Submit — like every strategy it fills and STOPS; the
application is transmitted only when that final button is pressed (by the co-pilot's
gated auto-submit, or a human). Walking the wizard past step 1 is itself gated behind
env `ORC_ADVANCE` (mirrors Avature's `AVATURE_ADVANCE`), so a plain fill / dry-run is
entirely side-effect-free at the employer.
"""
import logging
import os
import re

from playwright.async_api import Page

from backend.applier.analyzer import analyze_page, find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.filler import fill_form
from backend.applier.strategies.base import GenericStrategy

logger = logging.getLogger(__name__)

# A wizard "advance" button (Oracle CX renders it as an <oj-button> with text
# "Continue"/"Next"; the final Review step's button reads Submit). We advance on
# continue/next and STOP (record the selector) on submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
# Oracle CX buttons are <oj-button> custom elements (with an inner <button>), plain
# <button>s, and occasionally role=button links.
_WIZARD_BTN = "oj-button, button, a[role='button']"


def _env_advance() -> bool:
    """True only when ORC_ADVANCE is explicitly set — the live-submit switch that lets the
    strategy walk the wizard past step 1 (which transmits PII, and the final Submit sends the
    application). OFF by default: a plain fill (co-pilot dry-run / human review) stays entirely
    side-effect-free at the employer. Mirrors Avature's AVATURE_ADVANCE gate."""
    return os.getenv("ORC_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class OracleORCStrategy(GenericStrategy):
    name = "oracle_orc"
    # Whether to WALK the wizard past step 1 (Continue → EEO → Review → the final Submit
    # button). OFF by default for the same reason as Avature — see _env_advance. The real
    # auto-submit path sets this True (env ORC_ADVANCE=1), the same way the rest of the engine
    # gates its live actions.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        if "oraclecloud.com" not in u:
            return False
        # The CX apply surface is /hcmUI/CandidateExperience/…; be tolerant and also accept a
        # bare /sites/<CX>/job/<id> shape (some tenants shorten the path). This keeps other
        # oraclecloud.com hosts (object storage, APEX, docs) OUT.
        return ("/hcmui/candidateexperience/" in u
                or ("/sites/" in u and "/job/" in u))

    async def open_form(self, page: Page) -> None:
        # The apply URL IS the job page; the runner / co-pilot already navigated here, so we
        # never re-goto — we just START the guest application. Dismiss the cookie banner FIRST
        # (before any fill, so it never resets a filled field or intercepts the Apply click).
        await self._dismiss_cookie_banner(page)
        try:
            await self._click_apply(page)
        except Exception as exc:
            logger.debug("oracle_orc: open_form apply click raised: %s", exc)
        # A late-appearing cookie/consent overlay on the first wizard step.
        await self._dismiss_cookie_banner(page)

    async def _click_apply(self, page: Page) -> None:
        """Click the job page's Apply button to start the guest flow, then pick the MANUAL
        (email) option if Oracle shows a 'How would you like to apply?' chooser. Best-effort:
        many CX sites go straight to the form on Apply, so a missing chooser is normal."""
        for sel in ('button:has-text("Apply Now")', 'a:has-text("Apply Now")',
                    'button:has-text("Apply")', 'a:has-text("Apply")',
                    'button[title*="Apply" i]', '[data-bind*="applyNow" i]',
                    'oj-button:has-text("Apply")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
        # Oracle sometimes offers "Apply Manually" / "Use my email" vs LinkedIn/Indeed — take
        # the manual/email path (guest, no third-party account). Deliberately NO "Continue"
        # here so we never accidentally advance the wizard past step 1.
        for sel in ('button:has-text("Apply Manually")', 'a:has-text("Apply Manually")',
                    'button:has-text("Fill out application")',
                    'button:has-text("Use my Email")', 'button:has-text("Manually")',
                    'oj-button:has-text("Apply Manually")'):
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
        # super().prefill (via our open_form) fills the shared pipeline on step 1 (identity,
        # email, eligibility, résumé upload to the oj-file-picker's hidden <input type=file>).
        # We then fill the ORC-specific gaps the generic analyzer can't (JET selects/radiosets
        # screeners, EEO decline, required consent), then walk the wizard.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            return report
        try:
            await self._fill_orc_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("oracle_orc: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("oracle_orc: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("oracle_orc: wizard advance raised: %s", exc)
        return report

    # ---- ORC-specific gap fill (label/role driven so it generalizes across CX tenants) ----
    async def _fill_orc_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # EEO / diversity self-ID + required legal consent — Oracle renders these as JET
        # radiosets / checkboxsets / selects; the shared dropdowns helpers decline every
        # demographic (never claiming a protected characteristic) and tick required consent.
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        # Country-dependent State/Province is a JET select whose options load after Country is
        # set; the shared pipeline usually set Country already, so pick the persona's state now.
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                await self._fill_oj_select(page, "state", [state])
            except Exception:
                pass
        # Pre-screening Yes/No + experience/education/language questions the analyzer misses
        # (they're JET selects / radiosets), answered deterministically & TRUTHFULLY.
        await self._answer_screeners(page, facts)

    async def _answer_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED pre-screening question truthfully for a synthetic US persona
        located at the job's city: JET selects via _answer_select_screeners, JET radiosets via
        _answer_radio_screeners. Leaves an unmatched question for the human rather than guessing."""
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("oracle_orc: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("oracle_orc: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered oj-select-single widgets; for each whose label maps to
        a deterministic answer, type+pick the matching option (JET type-ahead)."""
        try:
            labels = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const w of document.querySelectorAll('oj-select-single')){
                    const l=w.querySelector('label')||
                      (w.getAttribute('aria-label')?{innerText:w.getAttribute('aria-label')}:null)||
                      (w.previousElementSibling&&w.previousElementSibling.tagName==='LABEL'
                        ?w.previousElementSibling:null);
                    const t=((l&&l.innerText)||w.getAttribute('label-hint')||'').trim();
                    if(t.length<4) continue;
                    // already answered? JET renders the selection text inside the widget.
                    const sel=(w.innerText||'').replace(t,'').trim();
                    const answered=!!sel && !/select a value|select\\.\\.\\.|choose/i.test(sel);
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
                # language-proficiency scale: HIGH for English, and for Spanish only when the
                # persona is bilingual; a low tier for Spanish otherwise.
                high = True if "english" in label else bool(facts.get("bilingual"))
                values = (["Native", "Fluent", "Advanced", "Professional"] if high
                          else ["None", "No proficiency", "Basic", "Limited"])
            if not values:
                continue
            try:
                await self._fill_oj_select(page, key, values, allow_first=is_prof)
            except Exception:
                pass

    async def _answer_radio_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED oj-radioset (or bare radio group) with a truthful, backed
        pick from _screener_answer. Leaves an unmatched group for the human."""
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

    async def _fill_oj_select(self, page: Page, label_substr: str, values,
                              allow_first: bool = False) -> bool:
        """Fill an Oracle JET oj-select-single whose label contains label_substr: click it to
        open the dropdown, type each value into the filter, and click the matching (or first)
        result. JET selects need this type-ahead — a plain .fill types prose the widget rejects,
        and setting the native <input> would jump to the wrong option."""
        found = await page.evaluate(
            """(lbl)=>{const n=s=>(s||'').toLowerCase();
              for(const w of document.querySelectorAll('oj-select-single')){
                const l=w.querySelector('label');
                const t=((l&&l.innerText)||w.getAttribute('aria-label')||w.getAttribute('label-hint')||'');
                if(!n(t).includes(lbl)) continue;
                w.setAttribute('data-jfojs','1'); return true;} return false;}""",
            label_substr.lower())
        if not found:
            return False
        picked = False
        for val in values:
            try:
                await page.click("oj-select-single[data-jfojs='1']", timeout=3000)
                await page.wait_for_timeout(400)
                # The open dropdown's filter/search input (JET renders it in a popup).
                sf = page.locator(
                    ".oj-listbox-drop input, .oj-listbox-filter input, "
                    "input[role='combobox'], oj-select-single[data-jfojs='1'] input").last
                try:
                    await sf.fill(val, timeout=2500)
                except Exception:
                    await sf.type(val, delay=40)
                await page.wait_for_timeout(900)   # option filter/AJAX
                opts = page.locator(
                    ".oj-listbox-result, .oj-listbox-results li, [role='option']")
                target = opts.filter(
                    has_text=re.compile(re.escape(val.split()[0]), re.I)).first
                if not await target.count() and allow_first:
                    target = opts.filter(
                        has_not_text=re.compile("no matches|no results|searching", re.I)).first
                if await target.count():
                    await target.click(timeout=3000)
                    picked = True
                    await page.wait_for_timeout(250)
                    break   # one value applied per select
                else:
                    await page.keyboard.press("Escape")
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        try:
            await page.eval_on_selector("oj-select-single[data-jfojs='1']",
                                        "e=>e.removeAttribute('data-jfojs')")
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
        """Deterministic, truthful answer candidates for an ORC pre-screening question
        (lowercased label). Returns an ordered list of option-text candidates (strongest first),
        or None to leave it for the human. Truthful for a synthetic US persona DESIGNED to fit
        the job (located at the job's city, native English, bilingual only when the role is)."""
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
        # Customer-service / call-center experience — pick the HIGHEST believable tier (the
        # tailored résumé shows ~8 yrs), never a weak middle one that undersells + contradicts it.
        if re.search(r"experience.*(customer service|call center|contact center|retail|customer)", t):
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
        `unfilled` reflects the ORC gap fill and the co-pilot's submit gate is honest.
        (JET renders a real <input>/<select> under each oj-* element, so a standard DOM scan
        still sees the underlying required state.)"""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;   // skip JET's hidden shadow inputs
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||!!el.closest('[aria-required="true"],.oj-complete.oj-required');
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
        """Close a cookie/consent banner (OneTrust/Oracle) that floats over the action bar and
        can intercept the Apply / Continue / Submit clicks."""
        for name in ("Reject Optional Cookies", "Reject All", "Accept All Cookies",
                     "Accept Cookies", "Accept All", "I Agree"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    # ---- wizard walker (mirrors AvatureStrategy._advance_wizard) ----
    async def _step_signature(self, page: Page) -> str:
        """A cheap fingerprint of the current wizard step, to tell whether a Continue click
        actually advanced (Oracle CX re-renders the section in place, often same URL)."""
        try:
            return await page.evaluate(
                "()=>{const a=document.querySelector('[aria-current=\"step\"],[aria-current=\"true\"],"
                ".oj-optlayout-current,.progress-current');"
                "const h=document.querySelector('h1,h2,legend,.oj-flex .oj-label, .section-title');"
                "return (a?a.innerText.trim().slice(0,40):'')+'|'+(h?h.innerText.trim().slice(0,40):'');}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind) for the step's primary button: kind='submit' on the final
        (Review) step, 'advance' on Continue/Next, else None."""
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
            logger.debug("oracle_orc: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        """Fill an EEO / voluntary / review step: decline demographics, tick required consent,
        fill any ordinary matched fields, and answer the step's JET screeners."""
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
            logger.debug("oracle_orc: step fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("oracle_orc: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step wizard: click Continue while it advances (filling each new step),
        and STOP at the final Submit — recording its selector in the report WITHOUT clicking it.
        If a Continue click does NOT advance (validation blocked it because a required field is
        still empty), stop and leave the gaps in `unfilled` for the human / next iteration."""
        for _ in range(6):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                # The final (Review) step is reached — fill anything still on it, then record
                # the true final-submit button (never a Continue). We do NOT click it.
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                report["submit_selector"] = (
                    "oj-button:has-text('Submit') button, button:has-text('Submit'), "
                    "button[title*='Submit' i], oj-button[id*='submit' i] button")
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
                # Did not advance -> a required field on this step is still empty. Stop; the
                # human / next iteration finishes it (the dry-run screenshot shows what's left).
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)
