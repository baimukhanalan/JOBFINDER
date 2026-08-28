"""Avature ATS pre-fill strategy (e.g. maximus.avature.net).

Unlike the login-less Greenhouse/Ashby forms, an Avature career portal creates the
candidate ACCOUNT inline with the application: the `/careers/Register?folderId=<jobId>`
page is a single multi-step Wizard whose FIRST step already carries the account
(email + password + confirm) together with the identity / eligibility / address /
résumé / work-history fields, and later steps carry voluntary EEO disclosures and a
final Review → Submit. There is NO captcha and (for Maximus) no assessment gating the
submit — the only thing the generic engine can't do is (a) fill the two password
inputs (the analyzer has no password rule and deliberately skips them) and (b) walk the
multi-step wizard. This strategy adds exactly those two things and reuses the shared
fill pipeline (`base.prefill`) for every ordinary field.

Nothing here clicks the FINAL Submit — like every strategy it fills and stops; the
account is only actually created when that final button is pressed (by the co-pilot's
gated auto-submit, or a human), so a dry-run is fully side-effect-free at the employer.
"""
import logging
import os
import re
import secrets

from playwright.async_api import Page

from backend.applier.analyzer import analyze_page, find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.filler import fill_form
from backend.applier.strategies.base import ApplyStrategy

logger = logging.getLogger(__name__)

# A wizard "advance" button (Avature renders the step's primary button as
# `nextButton WizardButtonPrimary`, text "Continue"; the final step's button reads
# Submit/Finish). We advance on continue/next and STOP (record the selector) on submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
_WIZARD_BTN = ".WizardButtonPrimary, .nextButton, button[id$='-next']"


def _gen_password() -> str:
    """A strong password that satisfies typical ATS complexity (upper+lower+digit+symbol)."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Jf{body}9!"


def _env_advance() -> bool:
    """True only when AVATURE_ADVANCE is explicitly set — the live-submit switch that lets the
    strategy walk the wizard past step 1 (which transmits PII + creates the account on submit)."""
    return os.getenv("AVATURE_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class AvatureStrategy(ApplyStrategy):
    name = "avature"
    # Whether to WALK the wizard past step 1 (click Continue → Compliance → the final Submit
    # button). OFF by default: advancing transmits the filled step-1 PII to the employer, and the
    # account is created on the final submit — so a plain fill (co-pilot dry-run / human review)
    # stays entirely side-effect-free at the employer. The real auto-submit path sets this True
    # (env AVATURE_ADVANCE=1), the same way the rest of the engine gates its live actions.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        return "avature.net" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        # The board's apply URL is /careers/Job-Application?folderId=<id>; the real
        # application+account wizard is /careers/Register?folderId=<id>. Navigate there.
        url = page.url
        m = re.search(r"folderId=(\d+)", url)
        if m and "/careers/register" not in url.lower():
            base = url.split("/careers/")[0]
            try:
                await page.goto(f"{base}/careers/Register?folderId={m.group(1)}",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
            except Exception as exc:
                logger.debug("avature: register nav failed: %s", exc)

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # super().prefill (via our open_form) fills the shared pipeline on step 1
        # (identity, email, eligibility, résumé upload). We then fill the Avature-specific
        # gaps the generic analyzer can't (account passwords, per-portal deterministic
        # screeners, the Country-dependent State select, Skills), then walk the wizard.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            report["account_password"] = ""
            return report
        try:
            await self._fill_avature_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("avature: gap fill raised: %s", exc)
        report["account_password"] = getattr(self, "_account_pw", "")
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("avature: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("avature: wizard advance raised: %s", exc)
        return report

    # ---- Avature-specific gap fill (label-driven so it generalizes across tenants) ----
    # Deterministic screener answers for a synthetic applicant (truthful for a fresh persona
    # with no employer/clearance/government history). Matched by label substring.
    _SCREENERS = (
        ("employed by maximus", "No"), ("worked for maximus", "No"),
        ("independent contractor", "No"), ("security clearance", "No"),
        ("worked for any government", "No"), ("government agency", "No"),
        ("previously worked for", "No"), ("18 years", "Yes"),
        ("authorized to work", "Yes"), ("require sponsor", "No"),
        ("is current position", "No"),
    )

    async def _fill_avature_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._fill_passwords(page)
        await self._tick_required_checkboxes(page)
        for substr, ans in self._SCREENERS:
            try:
                await self._select_by_label(page, substr, ans)
            except Exception:
                pass
        # State/Province is a Country-dependent select; Country is already United States,
        # so its options are populated by now — pick the persona's state.
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                if not await self._select_by_label(page, "state/province", state):
                    # options may not have loaded yet — re-assert Country to trigger, retry
                    await self._select_by_label(page, "country", "United States")
                    await page.wait_for_timeout(1200)
                    await self._select_by_label(page, "state/province", state)
            except Exception:
                pass
        # Languages-fluent and Skills are REQUIRED select2 autocomplete widgets (the native
        # <select> is hidden with 0 options; options load over AJAX on type). English is always
        # truthful for a US persona; Skills is a light self-report for a CSR role.
        langs = ["English", "Spanish"] if (facts or {}).get("bilingual") else ["English"]
        try:
            await self._fill_select2(page, "language", langs)
        except Exception:
            pass
        try:
            # strict (allow_first=False): only pick skills that actually exist in the taxonomy,
            # never a spurious first result like ".NET Framework" for a CSR persona.
            await self._fill_select2(page, "skills", ["Communication", "Data Entry"],
                                     allow_first=False)
        except Exception:
            pass

    async def _tick_required_checkboxes(self, page: Page) -> None:
        """Tick every REQUIRED, currently-unchecked checkbox that is not a marketing opt-in.
        Avature's 'you agree to the Terms of Service' box is required but its <label> is just
        '*' (the consent prose is a sibling paragraph), so a text-matched consent filler misses
        it and the step's Continue is blocked. We never tick a newsletter/marketing box."""
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
            logger.debug("avature: checkbox tick raised: %s", exc)

    async def _fill_passwords(self, page: Page) -> None:
        """Set BOTH password inputs to one generated password (fill auto-waits for
        actionability; no is_visible race). Stored on the instance for the report."""
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        try:
            boxes = page.locator('input[type="password"]')
            for i in range(await boxes.count()):
                try:
                    await boxes.nth(i).fill(pw, timeout=4000)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("avature: password fill raised: %s", exc)

    async def _select_by_label(self, page: Page, label_substr: str, value_substr: str) -> bool:
        """Find a native <select> whose label contains label_substr and pick the option whose
        text/value contains value_substr — via Playwright select_option (fires change, so a
        Country-dependent State select repopulates correctly)."""
        info = await page.evaluate(
            """([lbl,val])=>{const n=s=>(s||'').toLowerCase();
              const placeholder=t=>!t||/select an option|select a |please select/.test(n(t));
              for(const l of document.querySelectorAll('label')){
                if(!n(l.innerText).includes(lbl)) continue;
                let el=l.getAttribute('for')?document.getElementById(l.getAttribute('for')):null;
                if(!el||el.tagName!=='SELECT') el=(l.parentElement||document).querySelector('select');
                if(!el||el.tagName!=='SELECT') continue;
                // skip a select that is ALREADY answered (so two similar labels — "currently
                // employed" vs "ever been employed" — don't both bind to the first one).
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

    async def _fill_select2(self, page: Page, label_substr: str, values,
                            allow_first: bool = True) -> bool:
        """Fill a select2 autocomplete field (label contains label_substr): open it, type each
        value, and click the matching AJAX-loaded result (or the first result). Handles the
        Avature Languages/Skills widgets whose hidden native <select> has no static options."""
        found = await page.evaluate(
            """(lbl)=>{for(const l of document.querySelectorAll('label')){
                if(!(l.innerText||'').toLowerCase().includes(lbl)) continue;
                const w=l.closest('div')||l.parentElement;
                const c=w&&w.querySelector('.select2-container');
                if(c){c.setAttribute('data-jf2','1');return true;}} return false;}""",
            label_substr.lower())
        if not found:
            return False
        picked = False
        for val in values:
            try:
                await page.click(".select2-container[data-jf2='1'] .select2-selection", timeout=3000)
                await page.wait_for_timeout(400)
                sf = page.locator(".select2-search__field").last
                await sf.fill(val, timeout=3000)
                await page.wait_for_timeout(1100)   # AJAX option load
                opts = page.locator(".select2-results__option[role='option'], .select2-results__option")
                target = opts.filter(has_text=re.compile(re.escape(val.split()[0]), re.I)).first
                if not await target.count() and allow_first:
                    target = opts.filter(has_not_text=re.compile("no results|searching", re.I)).first
                if await target.count():
                    await target.click(timeout=3000)
                    picked = True
                    await page.wait_for_timeout(300)
                else:
                    await page.keyboard.press("Escape")
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        try:
            await page.eval_on_selector(".select2-container[data-jf2='1']",
                                        "e=>e.removeAttribute('data-jf2')")
        except Exception:
            pass
        return picked

    async def _pick_first_option(self, page: Page, label_substr: str, prefer=()) -> bool:
        """For a required self-report select (e.g. Skills), pick a preferred option if present
        else the first real (non-placeholder) option."""
        info = await page.evaluate(
            """([lbl,prefer])=>{const n=s=>(s||'').toLowerCase();
              for(const l of document.querySelectorAll('label')){
                if(!n(l.innerText).includes(lbl)) continue;
                let el=l.getAttribute('for')?document.getElementById(l.getAttribute('for')):null;
                if(!el||el.tagName!=='SELECT') el=(l.parentElement||document).querySelector('select');
                if(!el||el.tagName!=='SELECT') continue;
                const real=[...el.options].filter(o=>o.value && !/select an option|select a/.test(n(o.text)));
                if(!real.length) return null;
                let o=real.find(o=>prefer.some(p=>n(o.text).includes(p)))||real[0];
                el.setAttribute('data-jf','1'); return {value:o.value};
              } return null;}""", [label_substr.lower(), list(prefer)])
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
        return ok

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty visible fields on the current step (so the report's
        `unfilled` reflects the Avature gap fill, and the co-pilot's submit gate is honest)."""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const req=el.required||el.getAttribute('aria-required')==='true';
                    if(!req) continue;
                    let empty;
                    if(t==='checkbox'||t==='radio'){const nm=el.name;
                      empty=![...document.querySelectorAll('[name="'+nm+'"]')].some(x=>x.checked);}
                    else empty=!(el.value||'').trim();
                    if(!empty) continue;
                    let lab='';const id=el.id;
                    if(id){const l=document.querySelector('label[for="'+id+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab){const l=el.closest('label')||(el.parentElement&&el.parentElement.querySelector('label'));if(l)lab=l.innerText.trim();}
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||(el.name||'field');
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  } return out;}""")
        except Exception:
            return []

    async def _step_signature(self, page: Page) -> str:
        """A cheap fingerprint of the current wizard step, to tell whether a Continue
        click actually advanced (Avature keeps the same URL and re-renders in place)."""
        try:
            return await page.evaluate(
                "()=>{const s=document.querySelector('input[name=currentStepIndex]');"
                "const h=document.querySelector('h1,h2,legend,.WizardStepTitle');"
                "return (s?s.value:'')+'|'+(h?h.innerText.trim().slice(0,40):'');}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind) for the step's primary button: kind='submit' on the
        final step, 'advance' on Continue/Next, else None."""
        try:
            for b in await page.query_selector_all(_WIZARD_BTN):
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _SUBMIT_RE.search(txt):
                    return b, "submit"
                if _ADVANCE_RE.search(txt):
                    return b, "advance"
            # fallback: analyzer's submit heuristic (may be Continue on a multi-step form)
            sel = await find_submit_button(page)
            if sel:
                b = await page.query_selector(sel)
                if b:
                    txt = ((await b.inner_text()) or "").strip()
                    return b, ("submit" if _SUBMIT_RE.search(txt) else "advance")
        except Exception as exc:
            logger.debug("avature: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts):
        """Fill an EEO/voluntary/review step: decline demographics, tick required consent,
        and fill any ordinary matched fields the analyzer recognizes."""
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        await self._tick_required_checkboxes(page)
        await self._decline_demographics(page)
        # Voluntary Self-ID selects the shared decline helpers miss on Avature.
        for substr, ans in (("armed forces", "No"), ("member of armed", "No"),
                            ("gender", "not to disclose"), ("veteran status", "not")):
            try:
                await self._select_by_label(page, substr, ans)
            except Exception:
                pass
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("avature: step fill raised: %s", exc)
        # Job-specific screening questions (experience / education / language proficiency /
        # residence / internet) on the final step — answered deterministically & TRUTHFULLY
        # (the persona is located at the job's city and defined bilingual for bilingual roles).
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("avature: screeners raised: %s", exc)

    async def _answer_screeners(self, page: Page, facts) -> None:
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            fields = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const l of document.querySelectorAll('label')){
                    const t=(l.innerText||'').trim(); if(t.length<6) continue;
                    const w=l.closest('div'); if(!w) continue;
                    const nat=(l.getAttribute('for')&&document.getElementById(l.getAttribute('for'))||{}).tagName==='SELECT'
                      ? document.getElementById(l.getAttribute('for')) : w.querySelector('select:not([multiple])');
                    const s2=w.querySelector('.select2-container');
                    if(!nat&&!s2) continue;
                    let answered=false;
                    if(nat) answered=[...nat.selectedOptions].some(o=>o.value);
                    else{const r=s2.querySelector('.select2-selection__rendered,.select2-selection__choice');
                      answered=!!(r && !r.classList.contains('select2-selection__placeholder') &&
                        !/select an option|select a /i.test(r.innerText||''));}
                    const key=t.slice(0,110);
                    if(seen.has(key)) continue; seen.add(key);
                    out.push({label:t, key, answered, s2:!!s2 && !nat});
                  } return out;}""")
        except Exception:
            return
        for f in fields:
            if f.get("answered"):
                continue
            label = (f.get("label") or "").lower()
            key = (f.get("key") or "")[:60]
            # Language-proficiency selects use non-obvious option wording (e.g. "Native or
            # bilingual proficiency") — pick the ranked option: HIGH for English (persona is
            # fluent) and for Spanish only when the persona is bilingual; LOW Spanish otherwise.
            is_prof = bool(re.search(r"proficiency|language", label)
                           and re.search(r"english|spanish", label))
            values = self._screener_answer(label, facts)
            if not values and not is_prof:
                continue
            done = False
            if is_prof and not f.get("s2"):
                high = True if "english" in label else bool(facts.get("bilingual"))
                done = await self._pick_proficiency(page, key, high)
            elif not f.get("s2"):
                for v in values:
                    if await self._select_by_label(page, key, v):
                        done = True
                        break
            if not done:
                await self._fill_select2(page, key, values or ["Native", "Fluent", "Advanced"],
                                         allow_first=is_prof)

    async def _pick_proficiency(self, page: Page, label_key: str, high: bool) -> bool:
        """Pick a language-proficiency option by RANK, not exact text: HIGH → the option matching
        native/fluent/advanced/proficient (else the last real option); LOW → none/basic/limited
        (else the first real option). Truthful given the persona's defined language ability."""
        info = await page.evaluate(
            """([lbl,high])=>{const n=s=>(s||'').toLowerCase();
              const hi=/native|fluent|bilingual|advanced|proficient|expert|full professional/;
              const lo=/no proficiency|none|basic|beginner|limited|elementary/;
              for(const l of document.querySelectorAll('label')){
                if(!n(l.innerText).includes(lbl))continue;
                let el=l.getAttribute('for')?document.getElementById(l.getAttribute('for')):null;
                if(!el||el.tagName!=='SELECT') el=(l.closest('div')||document).querySelector('select');
                if(!el||el.tagName!=='SELECT')continue;
                const real=[...el.options].filter(o=>o.value &&
                  !/select an option|select a |prefer not|decline/.test(n(o.text)));
                if(!real.length)return null;
                let o = high ? (real.find(o=>hi.test(n(o.text)))||real[real.length-1])
                             : (real.find(o=>lo.test(n(o.text)))||real[0]);
                el.setAttribute('data-jf','1');return {value:o.value};
              }return null;}""", [label_key.lower(), high])
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
        return ok

    @staticmethod
    def _screener_answer(t: str, facts: dict):
        """Deterministic, truthful answer candidates for a job screener question (lowercased
        label). Returns an ordered list of option-text candidates, or None to leave it."""
        if re.search(r"acknowledge|i certify|i attest", t):
            return None                                   # handled by _tick_acknowledge
        if re.search(r"spanish", t):
            return (["Fluent", "Native", "Advanced", "Bilingual"] if facts.get("bilingual")
                    else ["None", "No proficiency", "Basic", "Beginner", "Limited"])
        if re.search(r"english", t):
            return ["Fluent", "Native", "Advanced", "Professional"]
        if re.search(r"highest level of education|education (you have )?achieved|level of education", t):
            return [facts.get("education_level") or "Bachelor", "Bachelor", "High School",
                    "Associate", "GED"]
        if re.search(r"experience.*(customer service|call center|contact center|retail|customer)", t):
            return ["3-5 years", "1-3 years", "3+ years", "1-2 years", "More than", "2 years",
                    "1 year", "Yes"]
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat", t):
            return ["Yes"]
        if re.search(r"commitment|interfere|foresee|conflict|impact.*attendance", t):
            return ["No"]
        if re.search(r"willing|able to (work|attend|commit|travel)|onsite|on-site|"
                     r"in.?office|in person|first week|training", t):
            return ["Yes"]
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
        return None

    async def _tick_acknowledge(self, page: Page) -> None:
        """Tick a required certification/acknowledgement radio or checkbox (single affirmative
        option like 'I Acknowledge' / 'I certify')."""
        try:
            ids = await page.evaluate(
                """()=>{const out=[];
                  for(const el of document.querySelectorAll('input[type=radio],input[type=checkbox]')){
                    if(el.checked||!el.id)continue;
                    const l=document.querySelector('label[for="'+el.id+'"]');
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

    async def _decline_demographics(self, page: Page) -> None:
        """On an EEO / Voluntary Self-ID step, answer every UNANSWERED required demographic with a
        decline: pick a 'choose not to disclose / prefer not / decline' RADIO, and tick the
        'I am not a protected veteran' CHECKBOX — never claiming a protected characteristic."""
        try:
            ids = await page.evaluate(
                """()=>{const out={radios:[],checks:[]};
                  const dec=/not to disclose|choose not|prefer not|decline|do not wish|do not want/i;
                  const groups={};
                  for(const r of document.querySelectorAll('input[type=radio]'))
                    (groups[r.name]=groups[r.name]||[]).push(r);
                  for(const nm in groups){const rs=groups[nm];
                    if(rs.some(r=>r.checked))continue;
                    for(const r of rs){const l=document.querySelector('label[for="'+r.id+'"]');
                      const t=(l&&l.innerText)||(r.closest('label')||{}).innerText||'';
                      if(dec.test(t)&&r.id){out.radios.push(r.id);break;}}}
                  for(const c of document.querySelectorAll('input[type=checkbox]')){
                    if(c.checked||!c.id)continue;
                    const l=document.querySelector('label[for="'+c.id+'"]');
                    const t=(l&&l.innerText)||(c.closest('label')||{}).innerText||'';
                    if(/not a protected veteran/i.test(t))out.checks.push(c.id);}
                  return out;}""")
        except Exception:
            return
        for eid in ids.get("radios", []) + ids.get("checks", []):
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

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts):
        """Walk the multi-step wizard: click Continue while it advances (filling each new
        step), and STOP at the final Submit — recording its selector in the report without
        ever clicking it. If a Continue click does NOT advance (Avature validation blocked
        it because a required field is still empty), stop and leave the gaps in `unfilled`."""
        for _ in range(6):
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                # Record the true final-submit button so the co-pilot's gated auto-submit
                # (or a human) uses it directly — never the wizard's Continue.
                report["submit_selector"] = (
                    "button.WizardButtonPrimary:has-text('Submit'), "
                    "button:has-text('Submit'), .WizardButtonPrimary")
                report["wizard_at_submit"] = True
                return
            sig = await self._step_signature(page)
            try:
                await btn.click()
                await page.wait_for_timeout(2000)
            except Exception:
                break
            if await self._step_signature(page) == sig:
                # Did not advance -> a required field on this step is still empty. Stop;
                # the human/next iteration finishes it (dry-run screenshot shows what's left).
                report["wizard_blocked_step"] = sig
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)
