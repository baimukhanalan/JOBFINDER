"""iCIMS ATS pre-fill strategy (careers-<co>.icims.com).

Extended 2026-08-31 for the Mass Hiring **Teleperformance** family
(careersus-teleperformance.icims.com), which is the classic account-gated iCIMS iForm:

    job page (career-portal shell)
      -> the posting + the whole apply flow render INSIDE a same-origin iframe,
         `<iframe id="icims_content_iframe">` (the top-frame analyzer can't pierce it)
      -> "Apply for this job online" -> an EMAIL-FIRST register wall (enter email +
         GDPR consent -> Next -> create a password -> email-verification code)
      -> the iForm (identity / résumé / Yes-No screeners / EEO self-ID)
      -> Submit, fronted by AWS WAF CAPTCHA (CloudFront, x-amzn-waf-action: captcha)
         + a probable inner iCIMS reCAPTCHA.

Two things the generic engine can't do here: (a) the fields live in an iframe, so every
DOM op must target that CONTENT FRAME as its fill root (the shared analyzer/filler only
see the top frame); (b) the account is created INLINE before the iForm, and the submit is
captcha-gated. This strategy adds a frame-aware fill layer, an email-first register walk,
the deterministic truthful screeners (ported from the Avature/Oracle mass-hiring lanes),
EEO self-ID decline, and a wizard walk that RECORDS the final submit without clicking —
wiring `captcha_solver` (AWS WAF + reCAPTCHA) at that submit step.

Like every strategy it fills and STOPS: nothing here clicks the final Submit. Unlike
Greenhouse/Ashby (login-less) and Avature/Oracle (the account is only created on the final
Submit), iCIMS creates the candidate account BEFORE the iForm — so BOTH account creation
AND the wizard walk are gated behind env `ICIMS_ADVANCE` (default OFF, mirrors
AVATURE_ADVANCE / ORC_ADVANCE). With the gate off the strategy stops at the register wall
as `login_required` and creates nothing — a plain fill / dry-run is side-effect-free at the
employer, exactly as the pre-existing iCIMS handling behaved.

GO-LIVE: needs a CapSolver key (CAPTCHA_SOLVER_KEY — the AWS WAF token + inner reCAPTCHA)
AND a US residential egress (Bright Data zone alibaba_res — the datacenter IP trips the AWS
WAF risk score); the residential session must stay PINNED for the whole fill so the WAF
token, cookies, and egress IP all match (see CLAUDE.md's alibaba_res routing note). The
post-submit language/typing (Versant) + gamified/video assessments are a downstream HIRE
stage, email-invited AFTER submit — they do NOT gate the application submit.
"""
import logging
import os
import re
import secrets

from playwright.async_api import Frame, Page

from backend.applier import captcha_solver
from backend.applier.analyzer import find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.strategies.base import ApplyStrategy

logger = logging.getLogger(__name__)

# iCIMS renders the posting + apply flow in a same-origin iframe id="icims_content_iframe".
_ICIMS_FRAME_ID = "icims_content_iframe"
# A wizard "advance"/"submit" button. The iForm walks Next/Continue through sections and
# ends on Submit; we advance on next/continue and STOP (record the selector) on submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review|proceed)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit application|submit|finish|complete|send application", re.I)


def _env_advance() -> bool:
    """True only when ICIMS_ADVANCE is explicitly set — the live-submit switch that lets the
    strategy create the candidate ACCOUNT and walk the iForm past the register wall to the
    final Submit. OFF by default: without it the strategy stops at the account wall and creates
    nothing, so a plain fill / dry-run is side-effect-free at the employer. Mirrors Avature's
    AVATURE_ADVANCE and Oracle's ORC_ADVANCE gates."""
    return os.getenv("ICIMS_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _gen_password() -> str:
    """A strong password that satisfies typical ATS complexity (upper+lower+digit+symbol)."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Jf{body}9!"


def _is_icims_content_url(url: str) -> bool:
    """True for a same-origin iCIMS content-iframe URL (…icims.com/jobs/<id>/…?in_iframe=1 or
    …/login). Used to pick the content frame out of page.frames when the id lookup misses."""
    u = (url or "").lower()
    return "icims.com" in u and ("in_iframe=1" in u or "/jobs/" in u or "/login" in u)


class ICIMSStrategy(ApplyStrategy):
    name = "icims"
    # Whether to CREATE THE ACCOUNT and walk the iForm past the register wall to the real
    # Submit. OFF by default (see _env_advance) — a plain fill stops at the account wall and
    # creates nothing. The real auto-submit path sets this True (env ICIMS_ADVANCE=1).
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        return "icims.com" in (url or "").lower()

    # ------------------------------------------------------------------ frame root
    async def _content_frame(self, page: Page) -> Frame | None:
        """The same-origin iCIMS content iframe (id=icims_content_iframe), where the posting,
        register wall and iForm all live — or None if the page isn't framed. Every iForm DOM op
        targets this frame, since the shared top-frame analyzer/filler can't reach into it."""
        try:
            el = await page.query_selector(
                f'iframe#{_ICIMS_FRAME_ID}, iframe[name*="icims_content"]')
            if el:
                fr = await el.content_frame()
                if fr:
                    return fr
        except Exception:
            pass
        try:
            for fr in page.frames:
                if fr is page.main_frame:
                    continue
                if _is_icims_content_url(fr.url):
                    return fr
        except Exception:
            pass
        return None

    async def open_form(self, page: Page) -> None:
        # Called inside super().prefill. Best-effort: clear an AWS WAF challenge (no-op without a
        # solver key), then click "Apply for this job online" INSIDE the content frame to reveal
        # the register wall. Clicking Apply only navigates to the login screen — it creates no
        # account, so this stays side-effect-free even with the advance gate off.
        try:
            await captcha_solver.solve_aws_waf(page)
        except Exception as exc:
            logger.debug("icims: aws-waf on open raised: %s", exc)
        frame = await self._content_frame(page)
        root = frame or page
        for sel in ('a:has-text("Apply for this job online")',
                    'button:has-text("Apply for this job online")',
                    'a:has-text("Apply Online")', 'a:has-text("Apply")',
                    'button:has-text("Apply")'):
            try:
                btn = root.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

    # --------------------------------------------------------------------- prefill
    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # super().prefill runs our open_form (Apply click) + the shared top-frame pipeline. For
        # the iframe-embedded iForm the top frame carries no fields, so this is effectively a
        # no-op that returns filled=0 — the real work is our frame-aware layer below. (Keeping
        # the super() call preserves the reference shape and any top-frame iCIMS variant.)
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["strategy"] = self.name

        frame = await self._content_frame(page)
        root = frame or page

        # The register/login wall gates the iForm. With the advance gate OFF we stop here and
        # create nothing (the pre-existing iCIMS behaviour) — a side-effect-free dry-run.
        try:
            at_wall = await self._at_register_wall(root)
        except Exception:
            at_wall = False
        if not self.advance_wizard:
            if at_wall:
                report["page_type"] = "login_required"
                report["note"] = ("iCIMS account wall — set ICIMS_ADVANCE=1 (+ a CapSolver key "
                                  "and a residential egress) for the live account+submit path")
            return report

        # --- Live account+submit path (ICIMS_ADVANCE=1) --------------------------
        if at_wall:
            try:
                created = await self._register_account(page, root, profile_form)
                report["account_created"] = created
                report["account_password"] = getattr(self, "_account_pw", "")
            except Exception as exc:
                logger.debug("icims: register raised: %s", exc)
            # the iForm loads in the (possibly re-created) content frame after register
            frame = await self._content_frame(page)
            root = frame or page

        try:
            await self._fill_icims_gaps(page, root, profile_form, resume_path, facts)
        except Exception as exc:
            logger.debug("icims: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(root)
        except Exception as exc:
            logger.debug("icims: rescan raised: %s", exc)
        try:
            await self._advance_wizard(page, report, profile_form, cover_letter, facts)
        except Exception as exc:
            logger.debug("icims: wizard advance raised: %s", exc)
        return report

    # ------------------------------------------------------------- register wall
    async def _at_register_wall(self, root) -> bool:
        """True when the content frame shows the email-first register/login wall (an email
        input + a Next/Register control) rather than the iForm itself."""
        try:
            return bool(await root.evaluate(
                r"""()=>{
                  const e=document.querySelector('#email, input[name="css_loginName"], input[type="email"]');
                  const btn=document.querySelector('#enterEmailSubmitButton, .gdprAnswerSubmitButton, [id*="register" i], [name*="register" i]');
                  const txt=(document.body&&document.body.innerText||'').toLowerCase();
                  const wall=/new to our|create (a )?(new )?account|register|returning|sign in|log ?in/.test(txt);
                  return !!(e && (btn || wall));}"""))
        except Exception:
            return False

    async def _register_account(self, page: Page, root, profile_form: dict) -> bool:
        """Email-first account creation on the iCIMS register wall (ICIMS_ADVANCE only): enter
        the persona email, accept the GDPR consent, click Next, choose the 'new account' path,
        set a generated password (both inputs), submit, and finish an emailed verification code
        from the persona's own Maildir. Best-effort + heavily guarded — a step it can't complete
        just leaves the wall for the human. Returns True if it believes an account was created."""
        email = (profile_form.get("email") or "").strip()
        if not email:
            return False
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        # 1) email + GDPR consent -> Next
        try:
            await root.locator('#email, input[name="css_loginName"], input[type="email"]'
                               ).first.fill(email, timeout=5000)
        except Exception:
            pass
        await self._tick_required_checkboxes(page, root)
        for sel in ('#enterEmailSubmitButton', '.gdprAnswerSubmitButton',
                    'input[type="submit"][value="Next"]', 'button:has-text("Next")'):
            try:
                b = root.locator(sel).first
                if await b.count() and await b.is_visible(timeout=1000):
                    await b.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
        # 2) pick the "new account" / register path if iCIMS offers a chooser
        for sel in ('a:has-text("Create a new account")', 'button:has-text("Create Account")',
                    'a:has-text("Register")', 'button:has-text("Register")',
                    'a:has-text("New to our")', 'button:has-text("Continue")'):
            try:
                b = root.locator(sel).first
                if await b.count() and await b.is_visible(timeout=800):
                    await b.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
        # 3) set the password (both inputs) + tick any newly-required consent, submit register
        try:
            boxes = root.locator('input[type="password"]')
            for i in range(await boxes.count()):
                try:
                    await boxes.nth(i).fill(pw, timeout=4000)
                except Exception:
                    continue
        except Exception:
            pass
        await self._tick_required_checkboxes(page, root)
        for sel in ('button:has-text("Create Account")', 'input[value="Create Account"]',
                    'button:has-text("Register")', 'input[type="submit"][value="Register"]',
                    'button[type="submit"]', 'input[type="submit"]'):
            try:
                b = root.locator(sel).first
                if await b.count() and await b.is_visible(timeout=800):
                    await captcha_solver.solve_on_page(page)   # inner reCAPTCHA on register
                    await b.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue
        # 4) email-verification code (iCIMS mails a code to confirm the account) — read it from
        # the persona's OWN Maildir and enter it. Best-effort; skipped if no code arrives.
        try:
            await self._enter_email_code(page, root, email)
        except Exception as exc:
            logger.debug("icims: email-code step raised: %s", exc)
        return True

    async def _enter_email_code(self, page: Page, root, email: str) -> bool:
        """Poll the persona's Maildir for an iCIMS verification code and type it into the code
        field. Reuses the shared emailed-code reader (candidate's own mailbox only)."""
        try:
            from backend.tools.verify_code import read_code
        except Exception:
            return False
        import time as _t
        since = _t.time()
        code = None
        for _ in range(20):                         # ~ up to 100s for the mail to land
            code = read_code(email, since_ts=since - 120)
            if code:
                break
            await page.wait_for_timeout(5000)
        if not code:
            return False
        for sel in ('input[name*="code" i]', 'input[id*="code" i]',
                    'input[name*="verification" i]', 'input[type="text"]'):
            try:
                fld = root.locator(sel).first
                if await fld.count() and await fld.is_visible(timeout=800):
                    await fld.fill(code, timeout=3000)
                    break
            except Exception:
                continue
        for sel in ('button:has-text("Verify")', 'button:has-text("Submit")',
                    'input[type="submit"]', 'button[type="submit"]'):
            try:
                b = root.locator(sel).first
                if await b.count() and await b.is_visible(timeout=800):
                    await b.click()
                    await page.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
        return False

    # -------------------------------------------------------------- iForm gap fill
    async def _fill_icims_gaps(self, page: Page, root, profile_form: dict,
                               resume_path: str, facts=None) -> None:
        """Fill the iForm inside the content frame: identity text fields, résumé attach, required
        acknowledgements, deterministic truthful Yes/No + experience/education/language screeners,
        EEO self-ID decline, and required legal consent. Frame-aware — every DOM op runs against
        `root` (the content frame), timeouts/keyboard via `page`."""
        await self._fill_identity(root, profile_form)
        try:
            await self._attach_resume_in_frame(root, resume_path)
        except Exception as exc:
            logger.debug("icims: résumé attach raised: %s", exc)
        await self._tick_acknowledge(page, root)
        # The shared demographic-decline / consent helpers operate on the TOP frame; on the
        # (common) iframe iForm they're harmless no-ops, so we also run our own frame-aware
        # decline below. They still help a rare top-frame iCIMS variant.
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        await self._decline_demographics(root, (profile_form or {}).get("full_name") or "")
        await self._tick_required_checkboxes(page, root)
        # State/Province is a Country-dependent select; Country is usually United States already.
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                await self._select_by_label(root, "state", state)
            except Exception:
                pass
        await self._answer_screeners(page, root, facts)

    async def _fill_identity(self, root, profile_form: dict) -> None:
        """Fill the iForm's plain identity text inputs by nearby-label match (frame-aware). Only
        sets an EMPTY field, so it never clobbers a value the iCIMS résumé parser populated."""
        pf = profile_form or {}
        pairs = [
            (r"first name|given name", pf.get("first_name") or _first(pf)),
            (r"last name|surname|family name", pf.get("last_name") or _last(pf)),
            (r"e-?mail", pf.get("email")),
            (r"phone|mobile|telephone", pf.get("phone")),
            (r"address|street", pf.get("street_address") or pf.get("address")),
            (r"\bcity\b|town", pf.get("city")),
            (r"zip|postal", pf.get("zip") or pf.get("postal_code")),
        ]
        for label_re, val in pairs:
            v = (val or "").strip() if isinstance(val, str) else val
            if not v:
                continue
            try:
                await self._fill_text_by_label(root, label_re, str(v))
            except Exception:
                continue

    async def _fill_text_by_label(self, root, label_re: str, value: str) -> bool:
        """Set the value of the first EMPTY, visible text/tel/email input whose label (label[for],
        wrapping label, aria-label, or placeholder) matches `label_re`. Fires input+change so the
        iForm's validation registers it. Returns True if a field was set."""
        return bool(await root.evaluate(
            """([re_src, val])=>{
              const rx=new RegExp(re_src,'i');
              const labtext=el=>{let t='';const id=el.id;
                if(id){const l=document.querySelector('label[for="'+
                  (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)t=l.innerText;}
                if(!t){const w=el.closest('label');if(w)t=w.innerText;}
                if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';
                return t;};
              for(const el of document.querySelectorAll(
                  'input[type=text],input[type=email],input[type=tel],input:not([type])')){
                const t=(el.type||'').toLowerCase();
                if(['hidden','submit','button','file','password'].includes(t))continue;
                if((el.value||'').trim())continue;
                if(!rx.test(labtext(el)))continue;
                el.value=val;
                el.dispatchEvent(new Event('input',{bubbles:true}));
                el.dispatchEvent(new Event('change',{bubbles:true}));
                return true;}
              return false;}""", [label_re, value]))

    async def _attach_resume_in_frame(self, root, resume_path: str) -> bool:
        """Upload the résumé to the iForm's file input inside the content frame (skips
        photo/avatar/logo inputs). iCIMS also parses the attached résumé server-side."""
        if not resume_path:
            return False
        try:
            inputs = root.locator('input[type="file"]')
            for i in range(await inputs.count()):
                inp = inputs.nth(i)
                info = await inp.evaluate(
                    '(el)=>{const c=el.closest("div,section,fieldset,form");'
                    'return {acc:(el.accept||"").toLowerCase(),'
                    ' blob:((el.id||"")+" "+(el.name||"")+" "+(c?c.innerText:"")).toLowerCase()};}')
                if "image/" in (info.get("acc") or ""):
                    continue
                if re.search(r"photo|avatar|picture|headshot|logo",
                             info.get("blob") or ""):
                    continue
                await inp.set_input_files(resume_path, timeout=8000)
                return True
        except Exception as exc:
            logger.debug("icims: attach_resume_in_frame raised: %s", exc)
        return False

    # ------------------------------------------------------------- screeners (frame)
    async def _answer_screeners(self, page: Page, root, facts) -> None:
        """Answer every UNANSWERED screener truthfully for a synthetic US persona: native <select>
        screeners via _answer_select_screeners, radio-group screeners via _answer_radio_screeners.
        Leaves an unmatched/behavioural question for the human rather than guessing."""
        facts = facts or {}
        try:
            await self._answer_select_screeners(page, root, facts)
        except Exception as exc:
            logger.debug("icims: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(root, facts)
        except Exception as exc:
            logger.debug("icims: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, root, facts) -> None:
        try:
            fields = await root.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const l of document.querySelectorAll('label')){
                    const t=(l.innerText||'').trim(); if(t.length<6) continue;
                    const w=l.closest('div,li,fieldset,tr'); if(!w) continue;
                    const nat=(l.getAttribute('for')&&document.getElementById(l.getAttribute('for'))||{}).tagName==='SELECT'
                      ? document.getElementById(l.getAttribute('for')) : w.querySelector('select:not([multiple])');
                    if(!nat) continue;
                    const answered=[...nat.selectedOptions].some(o=>o.value &&
                      !/select an option|select a |please select|choose/i.test(o.text||''));
                    const key=t.slice(0,110);
                    if(seen.has(key)) continue; seen.add(key);
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
                if await self._select_by_label(root, key, v):
                    break

    async def _answer_radio_screeners(self, root, facts) -> None:
        facts = facts or {}
        try:
            groups = await root.evaluate(
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
                await self._click_radio(root, grp["name"], picked.get("value"))
            except Exception:
                pass

    async def _select_by_label(self, root, label_substr: str, value_substr: str) -> bool:
        """Set the native <select> whose label contains label_substr to the option matching
        value_substr — via JS, so it works even when the select is HIDDEN behind an iCIMS fake
        `dropdown-select` overlay (`class="…dropdown-hide"`; Playwright's select_option needs an
        actionable/visible element and silently fails on it — that left State/Province blank). Fires
        `change` (drives the iCIMS onchange + the Country→State AJAX) and syncs the fake overlay
        display (`<id>_fakeSelected_icimsDropdown` / the `.dropdown-text` inside `<id>_icimsDropdown`)
        so the value is visible. Matches the label via `<label for>`, `data-label`, or the container
        text (iCIMS selects often have no `<label for>`). Skips an already-answered select. Frame-aware."""
        return bool(await root.evaluate(
            """([lbl,val])=>{const n=s=>(s||'').toLowerCase();
              const ph=t=>!t||/select an option|select a |please select|choose|make a selection|no states available/.test(n(t));
              const labelOf=el=>{ let t='';
                if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'); if(l)t=l.innerText;}
                if(!t)t=el.getAttribute('data-label')||'';
                if(!t){const b=el.closest('div,li,fieldset,tr,td'); if(b&&(b.innerText||'').length<160)t=b.innerText;}
                return t; };
              for(const el of document.querySelectorAll('select')){
                if(el.multiple) continue;
                if(!n(labelOf(el)).includes(lbl)) continue;
                const cur=el.options[el.selectedIndex];
                if(el.value && !ph(cur&&cur.text)) return true;   // already answered
                const o=[...el.options].find(o=>o.value && (n(o.text).includes(val)||n(o.value).includes(val)));
                if(!o) continue;
                el.value=o.value;
                el.dispatchEvent(new Event('input',{bubbles:true}));
                el.dispatchEvent(new Event('change',{bubbles:true}));
                try{ const f=document.getElementById(el.id+'_fakeSelected_icimsDropdown'); if(f)f.textContent=o.text;
                     const ov=document.getElementById(el.id+'_icimsDropdown'); if(ov){const d=ov.querySelector('.dropdown-text'); (d||ov).textContent=o.text;} }catch(e){}
                return true;
              } return false;}""", [label_substr.lower(), value_substr.lower()]))

    async def _click_radio(self, root, name: str, value) -> bool:
        found = await root.evaluate(
            """([nm,val])=>{for(const r of document.querySelectorAll('input[type=radio]')){
                if(r.name===nm && r.value===val){r.setAttribute('data-jfr','1');return true;}}
              return false;}""", [name, value])
        if not found:
            return False
        ok = True
        try:
            await root.check("input[data-jfr='1']", timeout=3000, force=True)
        except Exception:
            try:
                await root.eval_on_selector(
                    "input[data-jfr='1']",
                    "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));}")
            except Exception:
                ok = False
        try:
            await root.eval_on_selector("input[data-jfr='1']", "e=>e.removeAttribute('data-jfr')")
        except Exception:
            pass
        return ok

    async def _tick_acknowledge(self, page: Page, root) -> None:
        """Tick a required certification/acknowledgement checkbox or radio (a single affirmative
        option like 'I certify' / 'I acknowledge')."""
        try:
            ids = await root.evaluate(
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
                await root.locator(f'[id="{eid}"]').check(force=True, timeout=2500)
            except Exception:
                try:
                    await root.eval_on_selector(
                        f'[id="{eid}"]',
                        "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                        "e.dispatchEvent(new Event('change',{bubbles:true}));}")
                except Exception:
                    pass

    async def _tick_required_checkboxes(self, page: Page, root) -> None:
        """Tick every REQUIRED, currently-unchecked checkbox that is not a marketing opt-in
        (e.g. iCIMS's required GDPR/privacy consent). Never ticks a newsletter/marketing box."""
        try:
            boxes = root.locator('input[type="checkbox"]')
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
                            "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                            "e.dispatchEvent(new Event('change',{bubbles:true}));}")
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("icims: checkbox tick raised: %s", exc)

    async def _decline_demographics(self, root, name: str = "") -> None:
        """Decline every EEO / voluntary self-ID without claiming a protected characteristic:
        a 'do you choose to disclose?' group -> No; gender/race/disability/veteran radios -> the
        decline option; demographic selects -> the decline option else No; tick 'not a protected
        veteran' / 'do not wish to answer' checkboxes; sign the disability form's name field.
        Frame-aware; idempotent."""
        try:
            rids = await root.evaluate(
                """()=>{const out=[];const groups={};
                  const dec=/not to disclose|choose not|prefer not|decline|do not wish|do not want|don't wish|wish not|opt[\\s-]?out|not specified|not to answer|prefer not to say/i;
                  const dq=/choose to disclose|wish to disclose|like to disclose|self-?identify|do you wish/i;
                  const lab=r=>{const l=r.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;
                    return ((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'').trim();};
                  for(const r of document.querySelectorAll('input[type=radio]'))(groups[r.name]=groups[r.name]||[]).push(r);
                  for(const nm in groups){const rs=groups[nm];
                    let box=rs[0].parentElement;while(box&&!rs.every(r=>box.contains(r)))box=box.parentElement;
                    const opts=rs.map(r=>({id:r.id,t:lab(r),checked:r.checked}));
                    let qt=box?(box.innerText||''):'';for(const o of opts)if(o.t)qt=qt.split(o.t).join(' ');
                    qt=qt.replace(/\\s+/g,' ').trim().toLowerCase();
                    let pick=null;
                    if(dq.test(qt)){const no=opts.find(o=>/^\\s*no\\b/i.test(o.t));if(no&&!no.checked)pick=no;}
                    else if(!rs.some(r=>r.checked)){const d=opts.find(o=>dec.test(o.t));if(d)pick=d;}
                    if(pick&&pick.id)out.push(pick.id);}
                  return out;}""")
        except Exception:
            rids = []
        try:
            await root.evaluate(
                """()=>{const dec=/not to disclose|choose not|prefer not|decline|do not wish|do not want|opt[\\s-]?out|not specified|not to answer|prefer not to say/i;
                  const demo=/gender|race|ethnic|hispanic|latino|disabilit|veteran|armed forces|self-?identif|self-?classif|orientation|pronoun/i;
                  for(const el of document.querySelectorAll('select:not([multiple])')){
                    const cur=el.options[el.selectedIndex];
                    if(el.value&&cur&&!/select an option|select a |please select/i.test(cur.text))continue;
                    const l=el.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]'):null;
                    let lt=((l&&l.innerText)||'');if(!lt){const b=el.closest('div,li,fieldset,tr');lt=b?(b.innerText||''):'';}lt=lt.toLowerCase();
                    if(!demo.test(lt)&&![...el.options].some(o=>demo.test(o.text)))continue;
                    const o=[...el.options].find(o=>o.value&&dec.test(o.text))
                          ||[...el.options].find(o=>o.value&&/^\\s*no\\b/i.test(o.text))
                          ||[...el.options].find(o=>o.value&&/i do not|not a /i.test(o.text));
                    if(o){el.value=o.value;el.dispatchEvent(new Event('change',{bubbles:true}));}}}""")
        except Exception:
            pass
        try:
            cids = await root.evaluate(
                """()=>{const out=[];for(const c of document.querySelectorAll('input[type=checkbox]')){
                    if(c.checked||!c.id)continue;const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(c.id):c.id)+'"]');
                    const t=((l&&l.innerText)||(c.closest('label')?c.closest('label').innerText:'')||'');
                    if(/not a protected veteran|do not wish to answer|don't wish to answer|do not wish to self/i.test(t))out.push(c.id);}
                  return out;}""")
        except Exception:
            cids = []
        for eid in rids + cids:
            try:
                await root.locator(f'[id="{eid}"]').check(force=True, timeout=2000)
            except Exception:
                try:
                    await root.eval_on_selector(
                        f'[id="{eid}"]',
                        "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                        "e.dispatchEvent(new Event('change',{bubbles:true}));}")
                except Exception:
                    pass
        if name:
            try:
                await root.evaluate(
                    """(nm)=>{for(const inp of document.querySelectorAll('input[type=text],input:not([type])')){
                        if(inp.value)continue;const l=inp.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(inp.id):inp.id)+'"]'):null;
                        let lt=((l&&l.innerText)||'');if(!lt){const b=inp.closest('div,li,fieldset,tr');lt=b?(b.innerText||''):'';}lt=lt.toLowerCase();
                        if(/your name|employee name|name of employee|signature|please enter your name|please type your name/.test(lt)){
                          inp.value=nm;inp.dispatchEvent(new Event('input',{bubbles:true}));inp.dispatchEvent(new Event('change',{bubbles:true}));}}}""", name)
            except Exception:
                pass

    async def _rescan_required(self, root) -> list:
        """Labels of required-but-empty visible fields in the content frame, so the report's
        `unfilled` reflects the iForm gap fill and the co-pilot's submit gate stays honest."""
        try:
            return await root.evaluate(
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
                    if(!lab){const l=el.closest('label')||(el.parentElement&&el.parentElement.querySelector('label'));if(l)lab=l.innerText.trim();}
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||(el.name||'field');
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  } return out;}""")
        except Exception:
            return []

    # --------------------------------------------------------------- wizard walker
    async def _step_signature(self, page: Page, root) -> str:
        """A cheap fingerprint of the current iForm section, to tell whether a Next click
        actually advanced (iCIMS re-renders the section in place inside the iframe)."""
        try:
            return await root.evaluate(
                "()=>{const h=document.querySelector('h1,h2,legend,.iCIMS_InfoMsg,.title');"
                "return (h?h.innerText.trim().slice(0,50):'')+'|'+"
                "document.querySelectorAll('input,select,textarea').length;}")
        except Exception:
            return ""

    async def _primary_button(self, root):
        """Return (handle, kind) for the section's primary button: kind='submit' on the final
        section, 'advance' on Next/Continue, else None. Frame-aware."""
        try:
            for b in await root.query_selector_all(
                    "button, input[type=submit], a[role='button'], .iCIMS_PrimaryButton"):
                try:
                    if not await b.is_visible():
                        continue
                except Exception:
                    continue
                txt = (((await b.inner_text()) or "")
                       or (await b.get_attribute("value") or "")).strip()
                if _SUBMIT_RE.search(txt) and not _ADVANCE_RE.search(txt):
                    return b, "submit"
                if _ADVANCE_RE.search(txt):
                    return b, "advance"
            sel = await find_submit_button(root)
            if sel:
                b = await root.query_selector(sel)
                if b:
                    txt = ((await b.inner_text()) or "").strip()
                    return b, ("submit" if _SUBMIT_RE.search(txt) else "advance")
        except Exception as exc:
            logger.debug("icims: primary_button raised: %s", exc)
        return None, None

    async def _advance_wizard(self, page: Page, report, profile_form, cover_letter, facts) -> None:
        """Walk the iForm sections: click Next while it advances (filling each new section), and
        STOP at the final Submit — recording its selector in the report WITHOUT clicking it, after
        solving the AWS WAF token + the inner reCAPTCHA (so a later human/co-pilot click has the
        tokens in place). If a Next click does NOT advance (a required field is still empty), stop
        and leave the gaps in `unfilled` for the human / next iteration."""
        for _ in range(8):
            frame = await self._content_frame(page)
            root = frame or page
            btn, kind = await self._primary_button(root)
            if btn is None:
                break
            if kind == "submit":
                # final section: fill anything still on it, solve the submit-step captchas, and
                # record the true final-submit selector (never a Next). We do NOT click it.
                await self._fill_icims_gaps(page, root, profile_form, "", facts)
                try:
                    await captcha_solver.solve_aws_waf(page)      # CloudFront AWS WAF token
                except Exception as exc:
                    logger.debug("icims: submit-step aws-waf raised: %s", exc)
                try:
                    await captcha_solver.solve_on_page(page)      # inner iCIMS reCAPTCHA
                except Exception as exc:
                    logger.debug("icims: submit-step recaptcha raised: %s", exc)
                report["submit_selector"] = (
                    ".iCIMS_PrimaryButton, button:has-text('Submit'), "
                    "input[type=submit], button[type=submit]")
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(root)
                return
            sig = await self._step_signature(page, root)
            try:
                await btn.click()
                await page.wait_for_timeout(2000)
            except Exception:
                break
            frame = await self._content_frame(page)
            root = frame or page
            if await self._step_signature(page, root) == sig:
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(root)
                return
            await self._fill_icims_gaps(page, root, profile_form, "", facts)

    # ------------------------------------------------------ deterministic screeners
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
        if cand[0].isdigit():
            # numeric tier ("5+ years") must not left-match a bigger number ("15+ years")
            return (bool(re.search(r"(?<!\d)" + re.escape(cand), opt))
                    or bool(re.search(r"(?<!\d)" + re.escape(opt), cand)))
        return cand in opt or opt in cand

    @staticmethod
    def _screener_answer(t: str, facts: dict):
        """Deterministic, truthful answer candidates for an iForm screener (lowercased label).
        Returns an ordered list of option-text candidates (strongest first), or None to leave it
        for the human. Truthful for a synthetic US persona DESIGNED to fit the job (located at the
        job's city, native English, bilingual only when the role is). The Teleperformance board is
        CSR / insurance-rep / healthcare-rep, so the CSR-experience + eligibility families apply.
        Ported from the Avature/Oracle mass-hiring lanes."""
        facts = facts or {}
        # --- Teleperformance-specific screeners (deterministic, truthful for a synthetic US persona) ---
        if re.search(r"employed by (a )?(tp|teleperformance|tpusa|senture|alliance ?one)\b|"
                     r"(currently|ever|previously).{0,30}employed by (a )?(tp|teleperformance|company)", t):
            return ["No"]                                 # a fresh persona never worked for TP
        if re.search(r"legal right to work|right to work in|proof of your legal|proof of.*right to work", t):
            return ["Yes"]                                # US persona is authorized
        if re.search(r"graduate.*high school|high school (graduate|diploma|equivalent)|"
                     r"completed high school|do you have a (high school|hs) (diploma|ged)", t):
            return ["Yes"]                                # a US persona has a HS diploma (truthful)
        if re.search(r"graduate (from|of) (uma|ultimate medical academy)|"
                     r"attended.*\b(uma|ultimate medical academy)\b|"
                     r"\buma\b.*(degree|graduate)|(degree|graduate).*\buma\b|"
                     r"\balumni\b.*\b(uma|ultimate medical academy)\b", t):
            return ["No", "N/A", "Not applicable"]        # persona is not an alum of the named school
        if re.search(r"preferred shift|indicate your (preferred )?shift|which shift|shift preference|"
                     r"select.*shift|shift.*(prefer|choose|available to work)", t):
            return ["Any", "Flexible", "Any shift", "All shifts", "Open", "No preference",
                    "First Shift", "First", "Day", "Morning"]
        if re.search(r"acknowledge|i certify|i attest|certify that all|true and accurate", t):
            # a certify/acknowledge SELECT or radio -> the affirmative; a certify CHECKBOX is handled
            # by _tick_acknowledge and never reaches the select/radio screener paths, so no conflict.
            return ["Yes", "I certify", "I agree", "I acknowledge", "Agree", "Confirm", "True"]
        if re.search(r"spanish", t):
            return (["Fluent", "Native", "Advanced", "Bilingual"] if facts.get("bilingual")
                    else ["None", "No proficiency", "Basic", "Beginner", "Limited"])
        if re.search(r"english", t):
            # A US persona is a native English speaker — lead the strongest tier.
            return ["Native", "Native or bilingual", "Fluent", "Advanced", "Professional"]
        if re.search(r"highest level of (completed )?education|level of (completed )?education|"
                     r"education (you have )?achieved|\beducation level\b", t):
            return [facts.get("education_level") or "Bachelor", "Bachelor's", "Bachelor",
                    "High School Diploma", "High School", "Some College", "Associate", "GED"]
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
        if re.search(r"reside (outside|abroad)|located outside|live outside|"
                     r"outside (of )?the (u\.?s\.?|united states)|"
                     r"not (currently )?(reside|located|living|live)\b", t):
            return ["No"]                                 # a US persona does not reside outside the US
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat|"
                     r"located in the state of|located in [a-z]+\?|resident of|"
                     r"based in the state|do you (currently )?live in", t):
            # A state-specific TP posting ('are you located in <state>?') — the persona is DESIGNED
            # to reside in the job's state (icims_recon._pick_state reads location_raw), so Yes is
            # truthful + consistent with the registered address.
            return ["Yes"]
        # A schedule-conflict/attendance screener -> No. Scoped so a behavioural "describe a time
        # you resolved a conflict" open-text prompt isn't mistaken for a Yes/No screener.
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
        if re.search(r"type of internet|internet connection.*(type|use)|what.*(internet|connection).*(type|use)", t):
            # a TYPE select (Cable/Fiber/DSL/Mobile Hotspot/Satellite) — prefer a reliable WIRED option
            # over the weak default ("Mobile Hotspot") a remote CSR role would frown on.
            return ["Cable", "Fiber", "Cable/Fiber", "Broadband", "Fiber Optic", "DSL", "Wired"]
        if re.search(r"download speed|\bmbps\b|high.?speed|cable or fiber|internet|connection", t):
            return ["Yes"]
        if re.search(r"documentation|diploma or ged|provide.*if needed|verify.*education|"
                     r"able to provide", t):
            return ["Yes"]
        if re.search(r"18 (years|and older)|at least 18|over 18|18 or older|18\+|"
                     r"authorized|eligible to work", t):
            return ["Yes"]                                # 18+ age gate is truthful; NOT bare 'older' (age = protected)
        if re.search(r"seasonal|interested in (the |this )?(season|temporary|position|role|opportunity)", t):
            return ["Yes"]
        if re.search(r"citizen of (a country )?other than|dual citizen|non.?u\.?s\.? citizen|"
                     r"citizen of another", t):
            return ["No"]                                 # a US persona is not a non-US / dual citizen
        if re.search(r"u\.?s\.? citizen|united states citizen|american citizen|"
                     r"citizen of the (u\.?s\.?|united states)|are you a (u\.?s\.?|us) citizen", t):
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


def _first(pf: dict) -> str:
    """First name split out of a combined full_name/name field."""
    n = (pf.get("full_name") or pf.get("name") or "").strip()
    return n.split()[0] if n else ""


def _last(pf: dict) -> str:
    """Last name split out of a combined full_name/name field."""
    n = (pf.get("full_name") or pf.get("name") or "").strip()
    parts = n.split()
    return parts[-1] if len(parts) > 1 else ""
