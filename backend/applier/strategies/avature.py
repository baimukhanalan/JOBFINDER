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
            await self._fill_avature_gaps(page, profile_form)
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

    async def _fill_avature_gaps(self, page: Page, profile_form: dict) -> None:
        await self._fill_passwords(page)
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
        # Skills is a required self-report multi-select; pick a relevant option (best-effort).
        try:
            await self._pick_first_option(page, "skills",
                                          prefer=("customer", "service", "communication"))
        except Exception:
            pass

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
                    lab=(lab||el.name||'').replace(/\\s*\\*\\s*$/,'').slice(0,80);
                    if(lab&&!seen.has(lab)){seen.add(lab);out.push(lab);}
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
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("avature: step fill raised: %s", exc)

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
