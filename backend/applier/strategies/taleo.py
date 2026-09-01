"""Oracle Taleo Enterprise apply strategy (UnitedHealth `uhg.taleo.net`, TTEC `ttec.taleo.net`).

Taleo is the EASY sibling of Avature/Maximus: an account-gated, server-rendered JSF wizard with
**NO captcha, NO WAF, NO submit-gating assessment** (verified live 2026-09-01 — see
`backend/tools/recon_unitedhealth.py` / `recon_ttec.py`). So it runs fully autonomously in the
headful `:98` browser with a fresh synthetic persona + isolated per-job profile — the same rig as the
Teleperformance lane MINUS the NopeCHA/captcha problem.

`TaleoStrategy` SUBCLASSES `AvatureStrategy` to reuse the whole proven wizard machinery
(`_advance_wizard`, `_fill_current_step`, `_answer_screeners`/`_answer_radio_screeners`,
`_decline_demographics`, `_tick_required_checkboxes`, `_fill_passwords`, `_select_by_label`,
`_step_signature`, `_primary_button`, `_rescan_required`). It overrides only what is Taleo-specific:

  * `matches("taleo.net")`
  * `open_form`  — resolve a Radancy job page → the real `*.taleo.net/careersection/…/jobapply.ftl`
    URL if needed, accept the "Statement Before Authentication" (Privacy Agreement) page, then
    register a NEW USER (Taleo wants a distinct USERNAME + password + email — Avature only needs an
    email), landing on wizard step 1 which the base analyzer then fills.
  * `_fill_avature_gaps` → Taleo gaps: state-of-residence <select>, truthful Yes/No prescreeners,
    required-consent checkboxes. (Passwords are set during registration in `open_form`.)
  * `advance_wizard` gated by env **TALEO_ADVANCE** (default OFF → a plain fill is side-effect-free:
    advancing creates the account + transmits PII on the final Submit).

**LIVE-ITERATION CAVEAT:** the Taleo-classic (akira) selectors below — the "I Accept" button, the
"New User" link, the registration field names, the wizard step Continue buttons — are best-effort from
the recon probes and MUST be verified/tuned against the live JSF DOM on `:98`. They are written
defensively (multi-selector, best-effort, never raise), but this file has NOT been run against a live
Taleo form. Iterate exactly as `icims.py` / `avature.py` were.
"""
from __future__ import annotations

import logging
import os
import re

from playwright.async_api import Page

from backend.applier.strategies.avature import AvatureStrategy, _gen_password

logger = logging.getLogger(__name__)

# The external Taleo apply URL embedded in a Radancy job/listing page. UnitedHealth uses careersection
# 10020 (external); TTEC embeds a section-less jobapply.ftl that 302s to a numbered section — both are
# matched here (host is uhg.taleo.net or ttec.taleo.net).
_TALEO_APPLYURL_RE = re.compile(
    r"https://[a-z0-9.]*taleo\.net/careersection/(?:\d+/)?jobapply\.ftl\?job=[A-Za-z0-9]+", re.I)


def resolve_apply_url(page_html: str, prefer_section: str = "10020") -> str | None:
    """Extract the external Taleo apply URL from a Radancy job page's HTML (UnitedHealth or TTEC).

    Prefer careersection 10020 (UHG external candidates) over 10000 (internal). Returns the
    taleo.net jobapply.ftl URL, or None if the page embeds none.
    """
    hits = _TALEO_APPLYURL_RE.findall(page_html or "")
    if not hits:
        return None
    for u in hits:
        if f"/careersection/{prefer_section}/" in u:
            return u
    # else the first non-internal (10000) hit, else the first
    for u in hits:
        if "/careersection/10000/" not in u:
            return u
    return hits[0]


def _taleo_advance() -> bool:
    """True only when TALEO_ADVANCE is explicitly set — the live-submit switch that lets the wizard
    walk past step 1 (which creates the account + transmits PII on the final Submit)."""
    return os.getenv("TALEO_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class TaleoStrategy(AvatureStrategy):
    name = "taleo"
    advance_wizard = _taleo_advance()

    # Deterministic, truthful-by-design Yes/No prescreeners for a fresh synthetic US persona.
    # Matched by label substring (via the inherited _select_by_label). The residence STATE is a
    # <select> handled separately (set to the persona's own state), so the persona answers any
    # "are you located in <state>?" screener truthfully because it is DESIGNED to reside there.
    _SCREENERS = (
        ("legally authorized to work", "Yes"), ("authorized to work", "Yes"),
        ("right to work", "Yes"), ("eligible to work", "Yes"),
        ("require sponsorship", "No"), ("need sponsorship", "No"), ("visa sponsor", "No"),
        ("18 years", "Yes"), ("at least 18", "Yes"), ("older", "Yes"),
        ("previously worked for", "No"), ("currently employed by", "No"),
        ("ever been employed by", "No"), ("former employee", "No"),
        ("consent to a background", "Yes"), ("background check", "Yes"),
        ("drug screen", "Yes"), ("willing to", "Yes"),
    )

    @classmethod
    def matches(cls, url: str) -> bool:
        return "taleo.net" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        """Reach a clean Taleo wizard step 1 for a FRESH persona: (optionally) resolve a Radancy page
        to the taleo.net apply URL, clear cookies, accept the Privacy Agreement, and register a new
        account. Best-effort + never raises; each sub-step no-ops if its control isn't present."""
        url = page.url or ""
        # 1) If we somehow landed on the Radancy job page (not the Taleo form), resolve + navigate.
        if "taleo.net" not in url.lower():
            try:
                html = await page.content()
                taleo = resolve_apply_url(html)
                if taleo:
                    await page.goto(taleo, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(2000)
            except Exception as exc:
                logger.debug("taleo: radancy->taleo resolve failed: %s", exc)
        # 2) Fresh session — a persisted login from a previous persona would lock the wizard.
        try:
            await page.context.clear_cookies()
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass
        # 3) Privacy Agreement ("Statement Before Authentication") -> I Accept.
        await self._accept_privacy(page)
        # 4) New-User registration (username + password + email). Lands on wizard step 1.
        await self._register_new_user(page)
        await page.wait_for_timeout(1500)

    async def _accept_privacy(self, page: Page) -> None:
        """Click the Taleo 'I Accept' button on the data-privacy page (name ends
        …ContinueButton; text 'I Accept' / 'Accept'). No-op if not present."""
        for sel in ('input[name*="ContinueButton" i]',
                    'button[name*="ContinueButton" i]',
                    'input[value="I Accept"]', 'button:has-text("I Accept")',
                    'a:has-text("I Accept")', 'button:has-text("Accept")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2000)
                    return
            except Exception:
                continue

    async def _register_new_user(self, page: Page) -> None:
        """Drive the Sign-In / New-User page: choose New User, fill a unique username + password
        (both boxes) + the persona email, tick terms, submit. Username = the persona email localpart
        (already globally unique per persona). Best-effort; never raises."""
        # click the "New User" affordance if the page shows a Sign In / New User split
        for sel in ('a:has-text("New User")', 'button:has-text("New User")',
                    'input[value*="New User" i]', 'a:has-text("Register")',
                    'button:has-text("Create Account")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
        email = (getattr(self, "_persona_email", "") or "").strip()
        username = email.split("@", 1)[0] if email else ""
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        # username field (name/id/label contains userName / username / user name)
        if username:
            for sel in ('input[name*="userName" i]', 'input[id*="userName" i]',
                        'input[name*="username" i]', 'input[id*="username" i]',
                        'input[aria-label*="user name" i]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.fill(username, timeout=4000)
                        break
                except Exception:
                    continue
        # email field (skip if the registration form has none — some Taleo tenants use email AS the
        # username; the base analyzer also fills email on the wizard identity step)
        if email:
            for sel in ('input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.fill(email, timeout=4000)
                        break
                except Exception:
                    continue
        # both password inputs (password + confirm) — inherited helper
        try:
            await self._fill_passwords(page)
        except Exception:
            pass
        # required terms/consent checkbox(es) — inherited helper (skips marketing opt-ins)
        try:
            await self._tick_required_checkboxes(page)
        except Exception:
            pass
        # submit the registration (Register / Save / Continue)
        for sel in ('input[value="Register" i]', 'button:has-text("Register")',
                    'input[name*="registerButton" i]', 'button:has-text("Save and Continue")',
                    'input[value*="Save" i]', 'button:has-text("Continue")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5000)
                    await page.wait_for_timeout(2500)
                    return
            except Exception:
                continue

    async def prefill(self, page: Page, profile_form: dict, resume_path: str, *args, **kwargs):
        # Stash the persona email so _register_new_user can derive the username BEFORE the base
        # analyzer runs (open_form is called inside super().prefill).
        self._persona_email = (profile_form or {}).get("email", "")
        self._resume_path = resume_path or ""
        report = await super().prefill(page, profile_form, resume_path, *args, **kwargs)
        print(f"[taleo prefill: page_type={report.get('page_type')} advance={self.advance_wizard} "
              f"submitted={report.get('submitted')}]", flush=True)
        # Drive the wizard walk here too — the base gates _advance_wizard on page_type and can skip it.
        if self.advance_wizard and not report.get("submitted") and \
                report.get("page_type") not in ("login_required", "captcha", "expired"):
            try:
                await self._advance_wizard(page, report, profile_form,
                                           kwargs.get("cover_letter", ""), kwargs.get("facts"))
            except Exception as e:
                print(f"[taleo prefill wizard EXC: {type(e).__name__}: {str(e)[:150]}]", flush=True)
        return report

    async def _advance_wizard(self, page: Page, report, profile_form, cover_letter, facts):
        """Walk the Taleo full-page JSF wizard (TTEC): fill each step, click 'Save and Continue' across
        server round-trips, handle the résumé step + eSignature, and — when TALEO_ADVANCE — click the
        final Submit. Each step is a real navigation, so we re-fill after every round-trip. Stops on the
        confirmation page. Overrides the Avature SPA walker (whose button text/URL-in-place logic don't
        fit Taleo)."""
        import re as _re, os as _os2
        print(f"[taleo _advance_wizard ENTERED advance={self.advance_wizard}]", flush=True)
        name = (profile_form or {}).get("full_name") or (profile_form or {}).get("name") or ""
        _dbg = "/home/projects/jobfinder/logs/taleo_recon/wizard"
        _os2.makedirs(_dbg, exist_ok=True)
        try:
          await self._taleo_walk(page, report, profile_form, cover_letter, facts, name, _dbg, _re, _os2)
        except Exception as _e:
          print(f"[taleo wizard EXC: {type(_e).__name__}: {str(_e)[:160]}]", flush=True)

    async def _taleo_walk(self, page, report, profile_form, cover_letter, facts, name, _dbg, _re, _os2):
        for _step in range(18):
            await page.wait_for_timeout(1800)
            try:
                await page.screenshot(path=f"{_dbg}/step_{_step:02d}.png", full_page=True)
            except Exception:
                pass
            try:
                body = (await page.locator("body").inner_text(timeout=6000)).lower()
            except Exception:
                body = ""
            if _re.search(r"thank you for (your interest|applying|submitting)|your application (has been|was) "
                          r"submitted|application (is )?complete|submission (is )?(complete|confirmed|successful)|"
                          r"successfully submitted|we (have )?received your (application|submission)|"
                          r"thank you for taking the time", body):
                report["submitted"] = True
                logger.info("taleo: confirmation reached")
                return
            # error banner → re-fill (Taleo re-renders the same step listing missing required fields)
            try:
                await self._fill_current_step(page, profile_form, cover_letter, facts)
            except Exception:
                pass
            try:
                await self._fill_avature_gaps(page, profile_form, facts)
            except Exception:
                pass
            try:
                await self._fix_taleo_identity(page, profile_form)   # override the base analyzer's bad name guess
            except Exception:
                pass
            try:
                await self._taleo_wotc(page, profile_form, _dbg)      # Work Opportunity Tax Credit screening
            except Exception as _we:
                print(f"[taleo wotc EXC: {type(_we).__name__}: {str(_we)[:120]}]", flush=True)
            await self._taleo_attach_resume(page)
            await self._taleo_esign(page, name)
            clicked = await self._taleo_click_advance(page)
            try:
                await page.wait_for_timeout(1200)
                await self._taleo_confirm_popup(page)   # final 'confirm your password' popup, if it appeared
            except Exception:
                pass
            print(f"[taleo wizard step {_step}: clicked_advance={clicked} "
                  f"body_head={body[:60]!r}]", flush=True)
            if not clicked:
                logger.info("taleo: no advance/submit button found — stopping wizard walk")
                return
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass

    async def _taleo_click_advance(self, page: Page) -> bool:
        """Click 'Save and Continue' (advance) if present, else the final 'Submit Application'/'Submit'/
        'Finish' (only reached when TALEO_ADVANCE, since a mid-wizard step always offers Save&Continue).
        Skips 'Save as Draft'/'Quit'. Returns True if it clicked."""
        advance = ('input[value="Save and Continue" i]', 'button:has-text("Save and Continue")',
                   'a:has-text("Save and Continue")', 'input[value="Save and continue" i]')
        final = ('input[value="Submit Application" i]', 'button:has-text("Submit Application")',
                 'input[value="Submit" i]', 'button:has-text("Submit")',
                 'input[value="Finish" i]', 'button:has-text("Finish")')
        for group in (advance, final):
            for sel in group:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=6000)
                        return True
                except Exception:
                    continue
        return False

    async def _taleo_confirm_popup(self, page: Page) -> None:
        """Taleo's final 'Confirm your password' popup shown before the application is submitted:
        fill the account password (set at registration) + click Confirm."""
        pw = getattr(self, "_account_pw", "") or ""
        if not pw:
            return
        try:
            box = page.locator('input[id*="ConfmEditPassword" i], input[id*="flowConfmEditPassword" i]').first
            if not (await box.count() and await box.is_visible()):
                return
            await box.fill(pw, timeout=4000)
            for sel in ('input[id*="saveContinueCmdFlowPopUp" i]', 'button[id*="FlowPopUp" i]',
                        'button:has-text("Confirm")', 'input[value="Confirm" i]'):
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    print("[taleo confirm-password popup submitted]", flush=True)
                    return
        except Exception:
            pass

    async def _taleo_esign(self, page: Page, name: str) -> None:
        """eSignature step: type the applicant's full name into the signature field + tick certify."""
        if name:
            for sel in ('input[name*="eSignature" i]', 'input[id*="eSignature" i]',
                        'input[name*="signature" i]', 'input[id*="signature" i]',
                        'input[aria-label*="signature" i]', 'input[aria-label*="type your name" i]',
                        'input[name*="fullName" i][name*="sign" i]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        if not ((await loc.input_value()) or "").strip():
                            await loc.fill(name, timeout=4000)
                        break
                except Exception:
                    continue
        try:
            await self._tick_required_checkboxes(page)
        except Exception:
            pass

    async def _taleo_attach_resume(self, page: Page) -> None:
        """On the résumé-upload step, prefer 'Select the resume file to upload' + stage the résumé so
        the application carries one (the base analyzer picks the 'manual' radio, which submits WITHOUT a
        résumé). No-op if there is no résumé file input on the step."""
        rp = getattr(self, "_resume_path", "") or ""
        if getattr(self, "_resume_done", False):
            return   # upload ONCE — re-uploading each loop re-renders the page and stalls the advance
        try:
            fin = page.locator('input[type=file]').first
            if not await fin.count():
                return
            # select the "upload a resume" radio (the label mentions 'resume'/'upload'), not 'manual'
            try:
                await page.evaluate("""()=>{
                  for(const r of document.querySelectorAll('input[type=radio]')){
                    const w=r.closest('div,li,td,label'); const t=((w&&w.innerText)||'').toLowerCase();
                    if(/select the resume file|upload a resume|attach (a )?resume/.test(t)){ r.checked=true;
                      r.dispatchEvent(new Event('click',{bubbles:true})); r.dispatchEvent(new Event('change',{bubbles:true})); return;}}}""")
            except Exception:
                pass
            import os as _os
            if rp and _os.path.exists(rp):
                await fin.set_input_files(rp, timeout=8000)
                self._resume_done = True
                # WAIT for Taleo to accept + parse the upload (a remove/filename affordance appears)
                # before the caller clicks Save and Continue, else the advance no-ops on the raw page.
                for _ in range(20):
                    await page.wait_for_timeout(600)
                    try:
                        ok = await page.evaluate(
                            "()=>/\\bremove\\b|resume\\.pdf|uploaded|file uploaded|attached/i.test(document.body.innerText||'')")
                        if ok:
                            break
                    except Exception:
                        break
        except Exception:
            pass

    async def _fix_taleo_identity(self, page: Page, profile_form: dict) -> None:
        """Force First/Last/Email to the PERSONA's values, overriding the base analyzer which mis-parses
        the Taleo page header ('in Virginia, you are signed in' → First='in', Last='Virginia'). Matches
        the field by its own label; overwrites First/Last unconditionally, Email only if blank/garbage."""
        pf = profile_form or {}
        full = (pf.get("full_name") or pf.get("name") or "").strip()
        parts = full.split()
        first = (pf.get("first_name") or (parts[0] if parts else "")).strip()
        last = (pf.get("last_name") or (parts[-1] if len(parts) > 1 else "")).strip()
        email = (pf.get("email") or self._persona_email or "").strip()
        if not (first or last):
            return
        await page.evaluate(
            """([first,last,email])=>{
              const n=s=>(s||'').toLowerCase();
              const labOf=el=>{ let t='';
                if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'); if(l)t=l.innerText;}
                if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';
                if(!t){const w=el.closest('div,td,li,tr'); if(w&&(w.innerText||'').length<90)t=w.innerText;}
                return n(t); };
              const set=(el,v)=>{el.value=v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));};
              const bad=v=>{v=(v||'').trim(); return !v||v.length<2||/^(in|virginia|signed|you are|the)$/i.test(v);};
              for(const el of document.querySelectorAll('input[type=text],input:not([type]),input[type=email]')){
                const ty=(el.type||'').toLowerCase(); if(['hidden','submit','button','checkbox','radio','file'].includes(ty))continue;
                const lab=labOf(el);
                if(/first name|given name|legal first/.test(lab)){ if(first)set(el,first); }
                else if(/last name|surname|family name|legal last/.test(lab)){ if(last)set(el,last); }
                else if(/middle name|middle initial/.test(lab)){ if((el.value||'').trim() && bad(el.value)) set(el,''); }
                else if(/e-?mail/.test(lab)){ if(email && bad(el.value)) set(el,email); }
              }
            }""", [first, last, email])

    async def _taleo_wotc(self, page: Page, profile_form: dict, _dbg: str) -> bool:
        """The 'Employment Tax Credit Screening' (WOTC) step: click the linked questionnaire (usually a
        NEW TAB), answer every eligibility question No/decline (a synthetic persona is in no WOTC target
        group — truthful), fill required name/SSN/DOB text, walk it to done, then return to the main
        application page. Returns True if the WOTC step was detected + handled."""
        url0 = (page.url or "").lower()
        on_survey = any(h in url0 for h in ("taxcreditco", "surveyengine", "survey.aspx", "/wotc"))
        try:
            body = (await page.locator("body").inner_text(timeout=5000)).lower()
        except Exception:
            body = ""
        is_wotc = "tax credit screening" in body or "work opportunity tax credit" in body
        survey_intro = any(k in body for k in ("determine this company", "tax credit programs",
                                               "targeted groups", "let's get started"))
        if not (on_survey or is_wotc or survey_intro):
            return False
        # On the Taleo app WITH the questionnaire link → click it to reach the 3rd-party survey.
        # (If we're ALREADY on the survey/intro — a prior partial nav — skip straight to opt-out.)
        if not (on_survey or survey_intro):
            link = None
            for sel in ('a:has-text("Tax Credit Screening Questionnaire")', 'a:has-text("Complete Tax Credit")',
                        'a:has-text("Screening Questionnaire")'):
                loc = page.locator(sel).first
                if await loc.count():
                    link = loc
                    break
            if link is None:
                return False
            try:
                await link.click(timeout=5000)
            except Exception:
                return False
            for _ in range(12):                     # wait for the survey to load
                await page.wait_for_timeout(1200)
                if any(h in (page.url or "").lower() for h in ("taxcreditco", "surveyengine", "survey.aspx", "wotc")):
                    break
                if "taleo.net" not in (page.url or "").lower():
                    break
        # The survey is VOLUNTARY — OPT OUT (no SSN / eligibility answers needed for a synthetic persona).
        # Fall back to answering everything No/decline only if no opt-out affordance exists.
        for i in range(9):
            await page.wait_for_timeout(1300)
            try:
                await page.screenshot(path=f"{_dbg}/wotc_{i:02d}.png", full_page=True)
            except Exception:
                pass
            if "taleo.net" in (page.url or "").lower():   # survey finished → back on the Taleo app
                break
            opted = False
            for sel in ('a:has-text("Opt Out")', 'button:has-text("Opt Out")', 'a:has-text("Opt-Out")',
                        'button:has-text("Opt-Out")', 'a:has-text("Decline")', 'button:has-text("Decline")',
                        'a:has-text("I decline")'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=4000)
                        opted = True
                        break
                except Exception:
                    continue
            if not opted:
                # a confirm dialog ("are you sure you want to opt out?") → Yes/Confirm; else fill+advance
                confirmed = False
                for sel in ('button:has-text("Yes")', 'button:has-text("Confirm")', 'button:has-text("OK")',
                            'input[value="Yes" i]', 'button:has-text("Submit")'):
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() and await loc.is_visible():
                            await loc.click(timeout=4000)
                            confirmed = True
                            break
                    except Exception:
                        continue
                if not confirmed:
                    await self._wotc_fill(page, profile_form)
                    if not await self._wotc_advance(page):
                        break
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
        print(f"[taleo WOTC handled, url={page.url[:70]}]", flush=True)
        return True

    async def _wotc_fill(self, q, profile_form) -> None:
        pf = profile_form or {}
        full = (pf.get("full_name") or pf.get("name") or "").strip()
        parts = full.split()
        first = pf.get("first_name") or (parts[0] if parts else "")
        last = pf.get("last_name") or (parts[-1] if len(parts) > 1 else "")
        ssn = (str(pf.get("ssn") or "").replace("-", "")) or "123456789"
        dob = pf.get("dob") or pf.get("date_of_birth") or "01/01/1995"
        zc = pf.get("zip") or pf.get("postal_code") or ""
        await q.evaluate(
            """([first,last,ssn,dob,zc])=>{
              const n=s=>(s||'').toLowerCase();
              const labOf=el=>{let t='';if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');if(l)t=l.innerText;}
                if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';
                if(!t){const w=el.closest('div,td,li,tr,p');if(w&&(w.innerText||'').length<120)t=w.innerText;} return n(t);};
              const setv=(el,v)=>{if(!v)return;el.value=v;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
              for(const el of document.querySelectorAll('input[type=text],input:not([type]),input[type=tel],input[type=date],input[type=number]')){
                const ty=(el.type||'').toLowerCase(); if(['hidden','submit','button','checkbox','radio','file'].includes(ty))continue;
                if((el.value||'').trim())continue; const lab=labOf(el);
                if(/first name|given name/.test(lab))setv(el,first); else if(/last name|surname|family name/.test(lab))setv(el,last);
                else if(/social security|ssn/.test(lab))setv(el,ssn);
                else if(/date of birth|birth date|\\bdob\\b|d\\.o\\.b/.test(lab))setv(el,dob);
                else if(/zip|postal/.test(lab))setv(el,zc);}
              const rg={}; for(const r of document.querySelectorAll('input[type=radio]')){if(r.name)(rg[r.name]=rg[r.name]||[]).push(r);}
              const rlab=r=>{const l=r.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;return n(((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||''));};
              for(const nm in rg){const rs=rg[nm]; if(rs.some(r=>r.checked))continue;
                let pick=rs.find(r=>/^\\s*no\\b|none of these|does not|do not|decline|not a member|not applicable|n\\/a/.test(rlab(r)))||rs.find(r=>/^\\s*no\\s*$/.test(n(r.value||'')));
                if(pick){pick.checked=true;pick.dispatchEvent(new Event('click',{bubbles:true}));pick.dispatchEvent(new Event('change',{bubbles:true}));}}
              for(const sel of document.querySelectorAll('select')){if(sel.multiple)continue;const cur=sel.options[sel.selectedIndex];
                if(sel.value&&cur&&!/select|choose|^--|no selection/.test(n(cur.text)))continue;
                const o=[...sel.options].find(o=>o.value&&/^\\s*no\\s*$|none|decline|not a\\b|n\\/a/.test(n(o.text)))||[...sel.options].find(o=>o.value&&!/select|choose|^--|no selection/.test(n(o.text)));
                if(o){sel.value=o.value;sel.dispatchEvent(new Event('change',{bubbles:true}));}}
              for(const c of document.querySelectorAll('input[type=checkbox]')){if(c.checked)continue;const lab=labOf(c);
                if(/agree|consent|acknowledge|certify|understand|authorize|i have read|confirm|electronic signature/.test(lab)&&!/marketing|newsletter|opt.?in to receive/.test(lab)){
                  c.checked=true;c.dispatchEvent(new Event('click',{bubbles:true}));c.dispatchEvent(new Event('change',{bubbles:true}));}}
            }""", [first, last, ssn, dob, zc])

    async def _wotc_advance(self, q) -> bool:
        for sel in ('input[value="Submit" i]', 'button:has-text("Submit")', 'input[value="Continue" i]',
                    'button:has-text("Continue")', 'button:has-text("Next")', 'input[value="Next" i]',
                    'button:has-text("Finish")', 'button:has-text("Done")', 'a:has-text("Submit")',
                    'button[type="submit"]', 'input[type="submit"]'):
            try:
                loc = q.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    return True
            except Exception:
                continue
        return False

    async def _fill_avature_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        """Taleo per-step gap fill (called by the inherited prefill after the base analyzer). Passwords
        are already set during registration; here we handle the residence-state <select>, the truthful
        Yes/No prescreeners, and any required consent checkbox on the current wizard step."""
        # Taleo renders the form fields via AJAX AFTER the page navigation — wait for a <select> to
        # attach (else the fill runs on an empty page: querySelectorAll('select') == [] and nothing
        # gets filled, so the step loops forever). Poll up to ~10s.
        try:
            for _ in range(20):
                if await page.evaluate("()=>document.querySelectorAll('select,input[type=radio]').length"):
                    break
                await page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            await self._tick_required_checkboxes(page)
        except Exception:
            pass
        for substr, ans in self._SCREENERS:
            try:
                await self._select_by_label(page, substr, ans)
            except Exception:
                pass
        state = (profile_form or {}).get("state", "").strip()
        if state:
            for lbl in ("state/province", "state of residence", "state", "province"):
                try:
                    if await self._select_by_label(page, lbl, state):
                        break
                except Exception:
                    continue
        # TTEC "Basics" step: fill the remaining REQUIRED <select>s the generic analyzer misses
        # (Source Type, referred-by, SMS consent, military status, metro area, Diversity decline) +
        # any blank required text field (Zip/City) from the persona.
        zc = (profile_form or {}).get("zip", "") or (profile_form or {}).get("postal_code", "")
        city = (profile_form or {}).get("city", "")
        _BASICS_JS = """([zc,city,st])=>{
                  const stRe = st ? new RegExp('^\\\\s*'+st.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'),'i') : null;
                  const ph=t=>!t||/select one|no selection|make a selection|not specified|please select|^--|^\\s*$|choose|select\\.\\.\\.|^select$/i.test((t||'').trim());
                  const labOf=el=>{  // the field's OWN label: native label[for] -> aria -> nearest SHORT
                    if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'); if(l&&(l.innerText||'').trim()) return l.innerText.trim().toLowerCase().slice(0,160);}
                    const alb=el.getAttribute('aria-labelledby'); if(alb){const t=alb.split(/\\s+/).map(id=>{const e=document.getElementById(id);return e?e.innerText:'';}).join(' ').trim(); if(t) return t.toLowerCase().slice(0,160);}
                    const sh=t=>{t=(t||'').replace(/\\s+/g,' ').trim(); return (t.length>1&&t.length<130)?t:'';};  // skip the long form intro
                    let node=el;
                    for(let up=0;up<4&&node;up++){ let p=node.previousElementSibling,h=0;
                      while(p&&h<4){ const t=sh(p.innerText); if(t) return t.toLowerCase().slice(0,160); p=p.previousElementSibling; h++; }
                      node=node.parentElement; }
                    // fallback: the container's own text (a numbered questionnaire item like
                    // '1. Have you ever been employed by TTEC?' sits in the same cell as its select)
                    const box=el.closest('div,td,li,fieldset,tr');
                    if(box){const t=(box.innerText||'').replace(/\\s+/g,' ').trim(); if(t.length<260) return t.toLowerCase();}
                    return '';};
                  const set=(sel,re)=>{const o=[...sel.options].find(o=>o.value&&re.test(o.text)); if(o){sel.value=o.value; sel.dispatchEvent(new Event('change',{bubbles:true})); return true;} return false;};
                  const firstValid=sel=>{const o=[...sel.options].find(o=>o.value && !ph(o.text)); if(o){sel.value=o.value; sel.dispatchEvent(new Event('change',{bubbles:true})); return true;} return false;};
                  // broad decline match for demographic self-ID (never claim a protected characteristic)
                  const DEC=/decline|prefer not|not wish|wish not|do ?n[o'\\u2019]?t wish|not to (answer|disclose|say)|choose not|no answer|undisclosed|not disclosed|do ?n[o'\\u2019]?t (wish|want)/i;
                  for(const sel of document.querySelectorAll('select')){
                    if(sel.multiple) continue;
                    const cur=sel.options[sel.selectedIndex]; if(sel.value && !ph(cur&&cur.text)) continue;
                    const lab=labOf(sel);
                    if(/ethnic|hispanic|latino|\\brace\\b|gender|veteran|protected|military status|disabilit/.test(lab)){
                      // demographic -> DECLINE only; fall back to an explicit not-a-protected/none option, NEVER a characteristic
                      set(sel,DEC) || set(sel,/not a protected|no,? i am not|i am not|none of the above|not applicable/i);
                    }
                    else if(/referred by an employee|were you referred/.test(lab)){ set(sel,/^\\s*no\\s*$/i)||firstValid(sel); }
                    else if(/contacted via sms|sms text|text message|receive text/.test(lab)){ set(sel,/yes|agree|i agree/i)||firstValid(sel); }
                    else if(/source type|how did you (hear|find)|how you found|source track/.test(lab)){ set(sel,/^\\s*other\\s*$|job board|company website|newspaper/i)||set(sel,/indeed|linkedin|search engine/i)||firstValid(sel); }
                    else if(/metropolitan area|municipality|closest|\\bmetro\\b/.test(lab)){ firstValid(sel); }
                    else if(/education|degree/.test(lab)){ set(sel,/high school|bachelor|associate|ged|diploma|some college/i)||firstValid(sel); }
                    else if(/country/.test(lab)){ set(sel,/united states/i)||firstValid(sel); }
                    else if(/state|province/.test(lab)){ (stRe&&set(sel,stRe))||firstValid(sel); }
                    // questionnaire Yes/No + experience selects — pick the TRUTHFUL answer, never a blind firstValid
                    else if(/employed by|worked for|former employee|current(ly)? employ/.test(lab)){ set(sel,/^\\s*no\\s*$/i)||firstValid(sel); }
                    else if(/weekend|willing to work|able to work|overtime|different shift|any shift/.test(lab)){ set(sel,/^\\s*yes|able|willing/i)||firstValid(sel); }
                    else if(/experience/.test(lab)){ set(sel,/1 year or more|more than|3\\+|5\\+|1\\+|1-3|3-5|1 year/i)||firstValid(sel); }
                    else { firstValid(sel); }  // any OTHER leftover blank select (e.g. a Source-Type dependent sub-select) -> don't block
                  }
                  // radio-group questionnaires (experience level, weekend availability, employed-before) —
                  // the JS above only handles <select>; pick the truthful option here.
                  const rg={};
                  for(const r of document.querySelectorAll('input[type=radio]')){ if(r.name)(rg[r.name]=rg[r.name]||[]).push(r); }
                  const rlab=r=>{const l=r.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null; return (((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'')).toLowerCase();};
                  for(const nm in rg){ const rs=rg[nm]; if(rs.some(r=>r.checked)) continue;
                    let box=rs[0].closest('div,td,li,fieldset,tr'); const glab=((box&&box.innerText)||'').toLowerCase();
                    let pick=null;
                    if(/experience|how (long|many)/.test(glab)){ pick=rs.find(r=>/1 year or more|more than|3\\+|5\\+/.test(rlab(r)))||rs.find(r=>/6 months|1 year/.test(rlab(r)))||rs[rs.length-1]; }
                    else if(/weekend|willing|able to work|any shift/.test(glab)){ pick=rs.find(r=>/^\\s*yes|able|willing/.test(rlab(r))); }
                    else if(/employed|worked for|former employee/.test(glab)){ pick=rs.find(r=>/^\\s*no\\b/.test(rlab(r))); }
                    else if(/ethnic|hispanic|latino|\\brace\\b|gender|veteran|protected|disabilit/.test(glab)){ pick=rs.find(r=>DEC.test(rlab(r)))||rs.find(r=>/none|not a protected|i am not/.test(rlab(r))); }
                    if(pick){ pick.checked=true; pick.dispatchEvent(new Event('click',{bubbles:true})); pick.dispatchEvent(new Event('change',{bubbles:true})); }
                  }
                  // military 'share your status' checkbox group -> 'None of the above'
                  for(const c of document.querySelectorAll('input[type=checkbox]')){
                    const w=c.closest('div,li,td,label'); const t=((w&&w.innerText)||'').toLowerCase();
                    if(/none of the above/.test(t) && !c.checked){ c.checked=true; c.dispatchEvent(new Event('click',{bubbles:true})); c.dispatchEvent(new Event('change',{bubbles:true}));}}
                  // required blank text: Zip
                  for(const el of document.querySelectorAll('input[type=text]')){
                    if((el.value||'').trim()) continue;
                    const lab=labOf(el);
                    if(/zip|postal/.test(lab) && zc){ el.value=zc; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); }
                  }
                }"""
        # run twice: the Closest-Metro select is a Country/State-dependent AJAX cascade, so its options
        # aren't present on the first pass — the second pass (after a settle) picks the loaded option.
        try:
            await page.evaluate(_BASICS_JS, [zc, city, state])
            await page.wait_for_timeout(2000)   # let the State→Metro AJAX cascade populate
            await page.evaluate(_BASICS_JS, [zc, city, state])
            await page.wait_for_timeout(1500)
            await page.evaluate(_BASICS_JS, [zc, city, state])
        except Exception:
            pass
        if __import__("os").getenv("TALEO_DUMP"):
            try:
                info = await page.evaluate(
                    """()=>({url:location.href, sel:document.querySelectorAll('select').length,
                       inp:document.querySelectorAll('input').length,
                       btns:[...document.querySelectorAll('button,input[type=submit],a')].map(b=>(b.value||b.innerText||'').trim()).filter(t=>t&&t.length<40).slice(0,12),
                       body:(document.body&&document.body.innerText||'').replace(/\\s+/g,' ').slice(0,500)})""")
                print(f"[TALEO PAGE] url={info['url']} sel={info['sel']} inp={info['inp']} btns={info['btns']}\n  body={info['body']}", flush=True)
            except Exception:
                pass
        if __import__("os").getenv("TALEO_DUMP"):
            try:
                dump = await page.evaluate(
                    """()=>{const out=[];
                      const labOf=el=>{const parts=[]; const w=el.closest('div,td,li,fieldset,tr,p')||el.parentElement;
                        if(w){ parts.push(w.innerText||'');
                          let p=w.previousElementSibling, h=0;
                          while(p&&h<3){ parts.push(p.innerText||''); if((p.innerText||'').trim().length>5) break; p=p.previousElementSibling; h++; }
                          const par=w.parentElement; if(par&&(par.innerText||'').length<350) parts.push(par.innerText||''); }
                        return parts.join(' ').replace(/\\s+/g,' ').slice(0,160);};
                      for(const sel of document.querySelectorAll('select')){
                        const cur=sel.options[sel.selectedIndex];
                        out.push({lab:labOf(sel), val:(cur&&cur.text)||'', opts:[...sel.options].map(o=>o.text).slice(0,6)});}
                      return out;}""")
                import json as _json
                print("[TALEO DUMP selects]\n" + _json.dumps(dump, ensure_ascii=False, indent=0)[:2500], flush=True)
            except Exception as _e:
                print(f"[taleo dump err {_e}]", flush=True)
        # The Closest-Metropolitan-Area section is a Taleo DEPENDENT dropdown (Country->State->Metro):
        # a raw JS value-set does NOT fire the AJAX that loads the next level, so we drive it with
        # Playwright-NATIVE select_option (real change events) — country, then state (waits for the metro
        # AJAX), then the first real metro option.
        try:
            ids = await page.evaluate(
                """([st])=>{ // return [countryId, stateId, metroId] of the metro-cascade section
                  let cont=null, best=1e9;
                  for(const el of document.querySelectorAll('div,td,fieldset,li')){
                    const n=el.querySelectorAll('select').length;
                    if(/closest metropolitan area|municipality/i.test(el.innerText||'') && n>=2 && n<=4 && (el.innerText||'').length<best){ cont=el; best=(el.innerText||'').length; }}
                  if(!cont) return null;
                  const sels=[...cont.querySelectorAll('select')].map(s=>s.id||'');
                  return sels;
                }""", [state])
            if ids and len(ids) >= 2:
                # ids in DOM order: [country, state, metro] (some tenants omit country)
                metro_id = ids[-1]
                state_id = ids[-2] if len(ids) >= 2 else None
                country_id = ids[-3] if len(ids) >= 3 else None
                if country_id:
                    try:
                        await page.select_option(f'#{country_id}', label="United States", timeout=3000)
                    except Exception:
                        pass
                if state_id and state:
                    try:
                        await page.select_option(f'#{state_id}', label=state, timeout=4000)
                        await page.wait_for_timeout(2500)   # metro AJAX loads off the state change
                    except Exception:
                        pass
                if metro_id:
                    try:  # pick the first REAL metro option (index 1 = past the placeholder)
                        await page.select_option(f'#{metro_id}', index=1, timeout=4000)
                    except Exception:
                        pass
        except Exception:
            pass
        # Self-Identification / CC-305 Disability form: decline checkbox + Name + Date (M/d/yy).
        import datetime as _dt2
        _t = _dt2.date.today()
        mdyy = f"{_t.month}/{_t.day}/{_t.strftime('%y')}"   # M/d/yy, e.g. 9/1/26
        nm = (profile_form or {}).get("full_name") or (profile_form or {}).get("name") or \
            ((profile_form or {}).get("first_name", "") + " " + (profile_form or {}).get("last_name", "")).strip()
        _SELFID_JS = """([nm,dt])=>{
                  const sid=/self-identification|voluntary self|cc-?305/.test((document.body&&document.body.innerText||'').toLowerCase());
                  if(!sid) return false;
                  let did=false;
                  const fire=el=>{el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
                  // CC-305 rendered fields carry PURELY-NUMERIC ids (10040=Name, 10041=Date, 10045=EmpID) with
                  // NO labels; the 3 disability checkboxes are '<num>-shadow' in order Yes/No/I-don't-wish.
                  const nums=[...document.querySelectorAll('input[type=text]')].filter(e=>/^\\d+$/.test(e.id||''));
                  if(nums[0]&&nm){nums[0].value=nm;fire(nums[0]);did=true;}          // Name
                  if(nums[1]&&dt){nums[1].value=dt;fire(nums[1]);did=true;}          // Date (M/d/yy)
                  const cbs=[...document.querySelectorAll('input[type=checkbox]')].filter(e=>/^\\d+-shadow$/.test(e.id||''));
                  if(cbs.length){const c=cbs[cbs.length-1];                         // LAST = 'I Don't Wish To Answer'
                    if(!c.checked){c.checked=true;c.dispatchEvent(new Event('click',{bubbles:true}));fire(c);did=true;}
                    if(c.id){const l=document.querySelector('label[for="'+c.id+'"]'); if(l){try{l.click();}catch(e){}}
                      const real=document.getElementById(c.id.replace('-shadow','')); if(real&&!real.checked){real.checked=true;fire(real);}}}
                  if(!did){   // fallback: label-based (a non-numeric-id CC-305 variant)
                    const labOf=el=>{const w=el.closest('div,td,li,fieldset'); return ((w&&w.innerText)||'').toLowerCase().slice(0,220);};
                    for(const c of document.querySelectorAll('input[type=checkbox],input[type=radio]')){const w=c.closest('div,li,td,label');const t=((w&&w.innerText)||'').toLowerCase();
                      if(/do ?n[o'\\u2019]?t want to answer|do ?n[o'\\u2019]?t wish to answer|prefer not to answer|decline to (answer|self)/.test(t)&&!c.checked){c.checked=true;c.dispatchEvent(new Event('click',{bubbles:true}));fire(c);did=true;}}
                    for(const el of document.querySelectorAll('input[type=text]')){const lab=labOf(el);
                      if(/\\bdate\\b/.test(lab)&&dt){el.value=dt;fire(el);did=true;} else if(/\\bname\\b/.test(lab)&&nm&&!(el.value||'').trim()){el.value=nm;fire(el);did=true;}}}
                  return did;
                }"""
        # The CC-305 Self-ID form is often an EMBEDDED iframe — run the fill in EVERY frame, not just main.
        for _fr in page.frames:
            try:
                await _fr.evaluate(_SELFID_JS, [nm, mdyy])
            except Exception:
                pass
            # diagnostic: dump the CC-305 frame's inputs ONCE so we can see why Name/Date don't fill
            try:
                is_sid = await _fr.evaluate("()=>/self-identification|voluntary self|cc-?305/.test((document.body&&document.body.innerText||'').toLowerCase())")
            except Exception:
                is_sid = False
            if is_sid and not getattr(self, "_sid_dumped", False):
                self._sid_dumped = True
                try:
                    dmp = await _fr.evaluate("""()=>[...document.querySelectorAll('input')].map(e=>({t:e.type,id:e.id,nm:e.name,v:(e.value||'').slice(0,18),lab:((e.closest('div,td,li,label,tr,span')||{}).innerText||'').replace(/\\s+/g,' ').trim().slice(0,55)}))""")
                    print(f"[CC305 inputs: {dmp}]", flush=True)
                except Exception as _de:
                    print(f"[CC305 dump err {type(_de).__name__}]", flush=True)
