"""SmartRecruiters ATS pre-fill strategy (e.g. Sutherland on jobs.smartrecruiters.com).

SmartRecruiters is a LOGIN-LESS guest apply, like Greenhouse/Ashby — no account wall,
no video/voice assessment gating the SUBMIT (Sutherland's SHL-style / voice screens are
post-submit, email-invited at the HIRE stage). The public posting lives at
`jobs.smartrecruiters.com/<Company>/<postingId>`; the real application form is the
Angular "oneclick" SPA at

    jobs.smartrecruiters.com/oneclick-ui/company/<companyUuid>/publication/<postingId>?dcr_ci=<Company>

which the posting page embeds as the global `ONECLICKDATA` (companyUuid=`puuid`,
postingId=`pid`, company identifier=`cident`, plus a `%s`-templated `url`). So this
strategy's `open_form` reads that global and navigates straight to the oneclick form
(falling back to clicking the "I'm interested" / Apply button), then the shared pipeline
(`base.prefill`) fills identity / email / phone / résumé and the deterministic choice
engine answers the native screeners. This layer adds the SmartRecruiters-specific gaps the
generic analyzer can't: the Google-Places LOCATION typeahead, any custom radio/select
screeners, the required data-processing CONSENT, and the voluntary EEO decline — then walks
to the single "Apply" / "Submit application" button.

Anti-bot: the oneclick SPA sits behind **DataDome** (a GeeTest slider via
geo/ct.captcha-delivery.com). From a datacenter IP that shows the "verify you are human"
interstitial, so `analyzer.detect_page_type` returns `captcha` and the fill stops cleanly,
side-effect-free — the run needs a **residential** egress (Bright Data `alibaba_res`), where
DataDome passes silently and the form renders. The SUBMIT itself carries no reCAPTCHA per
recon, but `captcha_solver.solve_on_page` is wired at the submit step anyway (a graceful
no-op without CAPTCHA_SOLVER_KEY) so a reCAPTCHA/hCaptcha/Turnstile that ever appears there
is handled. DataDome is NOT solved by that path — the residential IP is what clears it.

Nothing here clicks the FINAL Submit — like every strategy it fills and STOPS; the
application is transmitted only when that final button is pressed (by the co-pilot's gated
auto-submit, or a human). Walking past the first screen is itself gated behind env
`SMARTRECRUITERS_ADVANCE` (mirrors Avature's `AVATURE_ADVANCE` / Oracle's `ORC_ADVANCE`), so
a plain fill / dry-run is entirely side-effect-free at the employer.
"""
import logging
import os
import re

from playwright.async_api import Page

from backend.applier import captcha_solver
from backend.applier.analyzer import analyze_page, find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.filler import fill_form
from backend.applier.strategies.base import GenericStrategy

logger = logging.getLogger(__name__)

# A "advance" button on a multi-screen SmartRecruiters config (most oneclick forms are a
# SINGLE page, but a long screening section can render a Continue/Next). We advance on
# continue/next and STOP (record the selector) on the final Apply/Submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit application|send application|submit|finish|^\s*apply\s*$", re.I)
# The oneclick submit control is a plain <button> (usually text "Apply" or "Submit
# application"); some tenants add a data-test hook. Recorded (never clicked) at the end.
_SUBMIT_SELECTOR = (
    "button[type='submit'], button:has-text('Submit application'), "
    "button:has-text('Send Application'), button:has-text('Apply'), "
    "[data-test*='apply' i][role='button'], button[data-test*='apply' i]")

# Default oneclick apply-form URL template (used when ONECLICKDATA has no explicit `url`).
_ONECLICK_TMPL = ("https://jobs.smartrecruiters.com/oneclick-ui/company/{uuid}"
                  "/publication/{pid}?dcr_ci={cident}")


def _env_advance() -> bool:
    """True only when SMARTRECRUITERS_ADVANCE is explicitly set — the live-submit switch that
    lets the strategy fill past the first screen (which transmits PII, and the final Submit
    sends the application). OFF by default: a plain fill (co-pilot dry-run / human review)
    stays entirely side-effect-free at the employer. Mirrors AVATURE_ADVANCE / ORC_ADVANCE."""
    return os.getenv("SMARTRECRUITERS_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _oneclick_url(data) -> str | None:
    """Build the oneclick apply-form URL from the posting page's ONECLICKDATA global.

    PURE helper (unit-tested, no network). `data` is the parsed ONECLICKDATA dict; needs a
    company UUID (`puuid`) + publication id (`pid`) + company identifier (`cident`). Honours
    an explicit `%s`-templated `url` (companyUuid, publicationId, cident order) when present,
    else falls back to the default template. Returns None on missing/garbage input."""
    if not isinstance(data, dict):
        return None
    uuid = str(data.get("puuid") or "").strip()
    pid = str(data.get("pid") or "").strip()
    cident = str(data.get("cident") or "").strip()
    if not uuid or not pid:
        return None
    tmpl = str(data.get("url") or "").strip()
    if tmpl.count("%s") == 3:
        return tmpl % (uuid, pid, cident)
    return _ONECLICK_TMPL.format(uuid=uuid, pid=pid, cident=cident or "")


class SmartRecruitersStrategy(GenericStrategy):
    name = "smartrecruiters"
    # Whether to fill past the first screen and walk any Continue → the final Submit button.
    # OFF by default for the same reason as Avature/Oracle — see _env_advance. The real
    # auto-submit path sets this True (env SMARTRECRUITERS_ADVANCE=1), the same way the rest
    # of the engine gates its live actions; the co-pilot's dry_run still gates the click.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        # The discovery connector uses api.smartrecruiters.com — that host is never an apply
        # page, so keep it OUT so a stray API URL doesn't route here.
        if "api.smartrecruiters.com" in u:
            return False
        return "smartrecruiters.com" in u

    async def open_form(self, page: Page) -> None:
        # The co-pilot / runner already navigated to the apply_url (the posting page). If that
        # is already the oneclick form, stay; otherwise read the posting page's ONECLICKDATA
        # global and navigate straight to the real apply form (companyUuid + publicationId).
        url = page.url or ""
        if "/oneclick-ui/" in url.lower():
            await self._dismiss_cookie_banner(page)
            return
        data = None
        try:
            data = await page.evaluate(
                "() => { try { return window.ONECLICKDATA || null; } catch (e) { return null; } }")
        except Exception as exc:
            logger.debug("smartrecruiters: ONECLICKDATA read raised: %s", exc)
        target = _oneclick_url(data)
        if target:
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
            except Exception as exc:
                logger.debug("smartrecruiters: oneclick nav failed: %s", exc)
        else:
            # No embedded data (posting-page markup changed / already an SR embed) — fall back
            # to the on-page Apply button that opens the oneclick form.
            await self._click_apply(page)
        # Dismiss the cookie/consent banner NOW, on the fresh form, before any field is filled
        # (so it never intercepts a screener/consent click or the final Submit).
        await self._dismiss_cookie_banner(page)

    async def _click_apply(self, page: Page) -> None:
        """Open the oneclick form via the posting page's apply control (fallback path)."""
        for sel in ('a:has-text("I\'m interested")', 'button:has-text("I\'m interested")',
                    'a.apply-with-smartr-button', 'button:has-text("Apply Now")',
                    'a:has-text("Apply Now")', 'button:has-text("Apply")',
                    'a:has-text("Apply")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    return
            except Exception:
                continue

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # super().prefill (via our open_form) fills the shared pipeline on the oneclick form
        # (identity, email, phone, résumé upload, native screeners via deterministic_choices,
        # EEO decline, required consent). We then fill the SmartRecruiters-specific gaps the
        # generic analyzer can't (Google-Places location, custom radio/select screeners), then
        # walk to the final Submit.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        # On a datacenter IP the oneclick SPA is a DataDome "verify you are human" interstitial
        # → page_type=captcha; leave cleanly (a residential egress is required to reach the form).
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            return report
        try:
            await self._fill_sr_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("smartrecruiters: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("smartrecruiters: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("smartrecruiters: wizard advance raised: %s", exc)
        return report

    # ---- SmartRecruiters-specific gap fill (label-driven, generalizes across tenants) ----
    async def _fill_sr_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # EEO / voluntary self-ID + required legal consent — base.prefill already ran these,
        # but re-run (idempotent) in case the analyzer's pass missed a late-rendered field.
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        # The Google-Places LOCATION typeahead (SmartRecruiters' `location` input) is a
        # combobox the analyzer skips — type the persona's city and pick the first suggestion.
        try:
            await self._fill_location(page, profile_form)
        except Exception as exc:
            logger.debug("smartrecruiters: location fill raised: %s", exc)
        # Pre-screening Yes/No + experience/education/language questions the analyzer misses
        # (custom radio groups / non-native selects), answered deterministically & TRUTHFULLY.
        await self._answer_screeners(page, facts)

    async def _fill_location(self, page: Page, profile_form: dict) -> bool:
        """Fill the SmartRecruiters location typeahead (Google Places): type the persona's
        city and click the first suggestion. Best-effort — an optional/absent field is fine."""
        city = (profile_form.get("city")
                or (profile_form.get("location") or "").split(",")[0]).strip()
        if not city:
            return False
        # Find an empty location-ish text/search input by label/placeholder/name.
        found = await page.evaluate(
            """()=>{const n=s=>(s||'').toLowerCase();
              for(const el of document.querySelectorAll(
                  'input[type=text],input[type=search],input:not([type]),input[role=combobox]')){
                if((el.value||'').trim())continue;
                const id=el.id;
                const l=id?document.querySelector('label[for="'+
                  (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]'):null;
                let lt=((l&&l.innerText)||el.getAttribute('aria-label')||
                        el.getAttribute('placeholder')||el.name||'');
                lt=n(lt);
                if(/location|city|where.*based|current.*location|town/.test(lt)){
                  el.setAttribute('data-jfloc','1'); return true;}}
              return false;}""")
        if not found:
            return False
        picked = False
        try:
            loc = page.locator("input[data-jfloc='1']")
            await loc.click(timeout=2500)
            await loc.fill(city, timeout=2500)
            await page.wait_for_timeout(1400)   # Places suggestion load
            # Google Places renders `.pac-item`; SR also uses a listbox with [role=option].
            for opt_sel in (".pac-item", "[role='option']", ".oneclick-autocomplete li",
                            "ul[role='listbox'] li"):
                try:
                    opt = page.locator(opt_sel).first
                    if await opt.count() and await opt.is_visible(timeout=800):
                        await opt.click(timeout=2000)
                        picked = True
                        break
                except Exception:
                    continue
            if not picked:
                # No suggestion surfaced — commit the typed value via keyboard as a fallback.
                await loc.press("ArrowDown")
                await loc.press("Enter")
        except Exception:
            pass
        try:
            await page.eval_on_selector("input[data-jfloc='1']",
                                        "e=>e.removeAttribute('data-jfloc')")
        except Exception:
            pass
        return picked

    async def _answer_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED screener truthfully for a synthetic US persona located at
        the job's city: native <select>s via _select_by_label, radio groups via
        _answer_radio_screeners. Leaves an unmatched question for the human, never guesses."""
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("smartrecruiters: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("smartrecruiters: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered native <select> screeners; for each whose label maps
        to a deterministic answer, pick the matching option (fires change)."""
        try:
            labels = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  const ph=t=>!t||/select an option|select a |please select|choose/i.test(t);
                  for(const el of document.querySelectorAll('select:not([multiple])')){
                    const id=el.id;
                    let l=id?document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]'):null;
                    let t=((l&&l.innerText)||'');
                    if(!t){const b=el.closest('div,fieldset');t=b?(b.innerText||''):'';}
                    t=t.replace(/\\s+/g,' ').trim();
                    if(t.length<4)continue;
                    const cur=el.options[el.selectedIndex];
                    const answered=!!el.value && !(cur&&ph(cur.text));
                    const key=t.slice(0,110);
                    if(seen.has(key))continue;seen.add(key);
                    out.push({label:t,key,answered});}
                  return out;}""")
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

    async def _select_by_label(self, page: Page, label_substr: str, value_substr: str) -> bool:
        """Find a native <select> whose label contains label_substr and pick the option whose
        text/value contains value_substr — via Playwright select_option (fires change)."""
        info = await page.evaluate(
            """([lbl,val])=>{const n=s=>(s||'').toLowerCase();
              const ph=t=>!t||/select an option|select a |please select/.test(n(t));
              for(const el of document.querySelectorAll('select:not([multiple])')){
                const id=el.id;
                let l=id?document.querySelector('label[for="'+
                  (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]'):null;
                let lt=((l&&l.innerText)||'');
                if(!lt){const b=el.closest('div,fieldset');lt=b?(b.innerText||''):'';}
                if(!n(lt).includes(lbl))continue;
                // skip a select already answered (two similar labels don't both bind the first).
                if(el.value && !ph(el.options[el.selectedIndex]&&el.options[el.selectedIndex].text))continue;
                const o=[...el.options].find(o=>o.value &&
                  (n(o.text).includes(val)||n(o.value).includes(val)));
                if(!o)continue;
                el.setAttribute('data-jf','1');return {value:o.value};}
              return null;}""", [label_substr.lower(), value_substr.lower()])
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
        """Deterministic, truthful answer candidates for a SmartRecruiters screening question
        (lowercased label). Returns an ordered list of option-text candidates (strongest first),
        or None to leave it for the human. Truthful for a synthetic US persona DESIGNED to fit
        the job (located at the job's city, native English, bilingual only when the role is).
        Kept in sync with oracle_orc / avature's tables."""
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
        # Customer-service / sales / call-center experience — pick the HIGHEST believable tier
        # (the tailored résumé shows ~8 yrs), never a weak middle one that undersells +
        # contradicts it. Order-independent: Sutherland's sales roles phrase it both ways
        # ("experience in customer service" AND "years of sales experience").
        _cs = r"customer service|call center|contact center|retail|sales|customer"
        if re.search(rf"experience.*(?:{_cs})|(?:{_cs}).*experience", t):
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
        """Labels of required-but-empty VISIBLE fields on the current screen, so the report's
        `unfilled` reflects the SmartRecruiters gap fill and the co-pilot's submit gate is honest."""
        try:
            return await page.evaluate(
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
            return []

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Close a cookie/consent banner (OneTrust/SmartRecruiters) that floats over the action
        bar and can intercept the screener / consent / Submit clicks."""
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

    # ---- wizard walker (mirrors OracleORCStrategy._advance_wizard) ----
    async def _step_signature(self, page: Page) -> str:
        """A cheap fingerprint of the current screen, to tell whether a Continue click actually
        advanced (SmartRecruiters re-renders in place, same URL on a multi-screen config)."""
        try:
            return await page.evaluate(
                "()=>{const h=document.querySelector('h1,h2,legend,.section-title,"
                ".form-section-title');"
                "return (h?h.innerText.trim().slice(0,40):'')+'|'+"
                "document.querySelectorAll('input,select,textarea').length;}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind) for the screen's primary button: kind='submit' on the final
        (Apply/Submit) screen, 'advance' on Continue/Next, else None."""
        try:
            for b in await page.query_selector_all("button, a[role='button']"):
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _ADVANCE_RE.search(txt):
                    return b, "advance"
                if _SUBMIT_RE.search(txt):
                    return b, "submit"
            sel = await find_submit_button(page)
            if sel:
                b = await page.query_selector(sel)
                if b:
                    txt = ((await b.inner_text()) or "").strip()
                    return b, ("advance" if _ADVANCE_RE.search(txt) else "submit")
        except Exception as exc:
            logger.debug("smartrecruiters: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        """Fill an EEO / voluntary / screening screen: decline demographics, tick required
        consent, fill any ordinary matched fields, and answer the screen's screeners."""
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
            logger.debug("smartrecruiters: step fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("smartrecruiters: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk any multi-screen config (most SmartRecruiters forms are a SINGLE screen):
        click Continue while it advances (filling each new screen), and STOP at the final
        Submit — recording its selector WITHOUT clicking it. Right before recording, wire the
        captcha solver (graceful no-op without a key; DataDome is NOT solved here — a
        residential IP is what clears it). If a Continue does NOT advance (validation blocked
        it), stop and leave the gaps in `unfilled`."""
        for _ in range(6):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                # No Continue and no submit button surfaced yet — record the canonical SR
                # submit selector so the co-pilot gate has a target, and stop.
                await captcha_solver.solve_on_page(page)
                report["submit_selector"] = _SUBMIT_SELECTOR
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(page)
                return
            if kind == "submit":
                # The final screen is reached — fill anything still on it, solve a submit-step
                # captcha if one is present (no-op otherwise), then record the true final-submit
                # button (never a Continue). We do NOT click it.
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                await captcha_solver.solve_on_page(page)
                report["submit_selector"] = _SUBMIT_SELECTOR
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
                # Did not advance -> a required field on this screen is still empty. Stop; the
                # human / next iteration finishes it (the dry-run screenshot shows what's left).
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)
