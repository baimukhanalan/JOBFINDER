"""ADP "myjobs" (myjobs.adp.com) apply strategy — an email-OTP account-create flow.

Afni's public board (`myjobs.adp.com/afniexternalcareers`) is an Angular/AIM ("ADP Identity
Management") career SPA. Unlike a login-less Greenhouse/Ashby form, the guest apply is gated
behind a MANDATORY candidate account — but that gate is NOT a hard wall here, because the account
is bootstrapped with an EMAILED one-time password (OTP) that lands in the persona's own
@takhet.com Maildir (read via verify_code.read_code), exactly like the Amazon Passport lane:

    myjobs.adp.com/afniexternalcareers/cx/job-details/<reqId>
      --("Sign in" / Apply)-->  /afniexternalcareers/auth   (the AIM auth micro-frontend)
        enter EMAIL -> Continue
        "If we don't recognize your info, we'll prompt you to create a profile"
          -> create-profile (name + password) -> one-time-password.generate
          -> EMAILED OTP  (person-otp-account.register / user.authenticate)
      --(authenticated)-->  the apply form
        AckPrivacyStatement + contact info + eligibility screeners + EEO self-ID -> Submit

Recon (2026-09-23): the auth + create-profile step is served by ADP's `aim-*` micro-frontend;
NO captcha is presented at any step (no reCAPTCHA/hCaptcha/Turnstile/AWS-WAF), and the flow is
reachable from the plain server IP. The only "wall" is the OTP account, which is receivable
because the persona mailbox is provisioned before the drive.

Everything that touches the employer — creating the account, GENERATING the OTP (which transmits
the persona email to ADP), and clicking the final Submit — is GATED behind env AFNI_ADVANCE
(mirrors Amazon's AMAZON_ADVANCE / Avature's AVATURE_ADVANCE / Oracle's ORC_ADVANCE). With the
gate OFF — the default — a dry-run navigates to the auth step and FILLS the email box but STOPS
before pressing Continue, so NO account is created, NO OTP is generated and NO PII is transmitted;
`prefill` reports `needs_account`/`login_required`. Even with the gate ON, the account is created
only when Continue is pressed and the application submitted only when the FINAL Submit is pressed
(recorded, never auto-clicked here) — so the strategy always fills-and-stops like every other one.
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

# The AIM auth micro-frontend + apply wizard use plain <button>s: advance on Continue/Next, STOP
# (record the selector) on Submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review|proceed)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
_WIZARD_BTN = "button, a[role='button'], [data-automation-id*='submit' i]"

# ADP AIM create-profile password rules (upper + lower + digit + special, 8+). Generate a strong
# one that also satisfies a stricter ATS.
_PASSWORD_SPECIALS = "!@#$%^&*?_-"


def _gen_password() -> str:
    """A strong password satisfying ADP AIM's create-profile complexity (upper + lower + digit +
    special, length 8+, no surrounding whitespace)."""
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Jf{body}7!"


def _env_advance() -> bool:
    """True only when AFNI_ADVANCE is explicitly set — the live switch that lets the strategy
    bootstrap the account (create it, GENERATE + verify the emailed OTP) and walk the apply wizard.
    OFF by default so a plain fill / dry-run never creates an account or transmits PII. Mirrors
    Amazon's AMAZON_ADVANCE / Avature's AVATURE_ADVANCE. `ADP_ADVANCE` is accepted as an alias."""
    for name in ("AFNI_ADVANCE", "ADP_ADVANCE"):
        if os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


# Senders/subjects that confirm the ADP OTP (so a fallback reader can find the code even if the
# subject wording drifts past verify_code's `verification|security code|verify` filter).
_OTP_SENDER_RE = re.compile(r"adp\.com|afni", re.I)
_OTP_CODE_RE = re.compile(r"\b(\d{6})\b")
_OTP_SUBJECT_RE = re.compile(r"one[\s-]?time|verification|passcode|security code|sign[\s-]?in|verify",
                             re.I)


class AdpStrategy(ApplyStrategy):
    name = "adp"
    # Whether to BOOTSTRAP the account (create the profile, generate + verify the emailed OTP) and
    # then fill + advance the apply wizard. OFF by default — see _env_advance. The afni auto-apply
    # lane sets this True (env AFNI_ADVANCE=1); with it off, a dry-run only ever fills the auth
    # email box and stops (no account created, no OTP generated).
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        if "adp.com" not in u:
            return False
        # The whole ADP "myjobs" candidate surface: the SPA host, the auth micro-frontend, the
        # job-details / apply / cx routes. Keep non-ADP hosts OUT.
        return ("myjobs.adp.com" in u or "my.adp.com" in u
                or "/auth" in u or "/cx/" in u or "job-details" in u
                or "afniexternalcareers" in u)

    # ---- lifecycle ----------------------------------------------------------
    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # Stash context so open_form (called INSIDE super().prefill) can reach the auth step + (when
        # gated) bootstrap the account with the persona's email/name — base.open_form takes only `page`.
        self._pf = profile_form or {}
        self._facts = facts or {}
        self._profile_id = profile_id
        self._resume_path = resume_path
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        report["account_password"] = getattr(self, "_account_pw", "")
        report["reached_auth"] = getattr(self, "_reached_auth", False)
        # Still on the AIM auth / account-create wall? Detect it by HOST/BODY (`_on_auth_wall`), not
        # only via the analyzer's page_type — the auth micro-frontend renders no standard login form
        # the analyzer recognises, so detect_page_type may return `unknown`. A host/body check flags
        # the ceiling correctly so a dry-run reports needs-account rather than a phantom "complete".
        try:
            on_wall = await self._on_auth_wall(page)
        except Exception:
            on_wall = False
        if on_wall or report.get("page_type") in ("login_required", "captcha", "expired"):
            report["needs_account"] = True
            if report.get("page_type") not in ("captcha", "expired"):
                report["page_type"] = "login_required"
            return report
        try:
            await self._fill_adp_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("adp: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("adp: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("adp: wizard advance raised: %s", exc)
        return report

    async def open_form(self, page: Page) -> None:
        # The runner/driver navigated to the job-details URL. Dismiss the OneTrust cookie banner
        # FIRST (it floats over the action bar and intercepts clicks), then reach the AIM auth step
        # (job-details "Sign in"/Apply -> /auth) and fill the email. On a DRY-RUN we STOP right there
        # (email filled, no Continue) — no account, no OTP, no PII. When gated (advance_wizard) we
        # press on: create the profile, generate + verify the emailed OTP, reach the apply form.
        await self._dismiss_cookie_banner(page)
        try:
            await self._go_to_auth(page)
        except Exception as exc:
            logger.debug("adp: go_to_auth raised: %s", exc)
        if not self.advance_wizard:
            # Dry-run: fill the email box so the report proves we reached the account-create step,
            # but do NOT press Continue (which would generate the OTP + start account creation).
            # The AIM auth micro-frontend loads its email input asynchronously, so retry briefly.
            for _ in range(6):
                try:
                    if await self._fill_auth_email(page):
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
            return
        try:
            await self._bootstrap_account(page)
        except Exception as exc:
            logger.debug("adp: account bootstrap raised: %s", exc)
        await self._dismiss_cookie_banner(page)

    # ---- reach the auth step ------------------------------------------------
    async def _go_to_auth(self, page: Page) -> None:
        """From a job-details page, reach the AIM `/auth` step. On the Afni board the apply entry IS
        the "Sign in" control (a candidate must create/sign in to a profile before applying), so we
        click it; if the cookie overlay intercepts the click (or no button is found), fall back to
        navigating the deterministic `<board>/auth` route directly (the auth page loads cold)."""
        if await self._on_auth_wall(page):
            self._reached_auth = True
            return
        for sel in ('button:has-text("Apply")', 'a:has-text("Apply")',
                    'button:has-text("Apply now")', '[data-automation-id*="apply" i]',
                    'button:has-text("Sign in")', 'a:has-text("Sign in")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1200):
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(3500)
                    if await self._on_auth_wall(page):
                        break
            except Exception:
                continue
        if not await self._on_auth_wall(page):
            # Fallback: the /auth route is deterministic (…/<board>/cx/… -> …/<board>/auth).
            try:
                auth_url = self._auth_url(page.url)
                if auth_url:
                    await page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                    await self._dismiss_cookie_banner(page)
            except Exception as exc:
                logger.debug("adp: auth-url fallback raised: %s", exc)
        self._reached_auth = await self._on_auth_wall(page)

    @staticmethod
    def _auth_url(url: str) -> str:
        """The AIM auth URL for an ADP myjobs board URL: strip the /cx/... route tail and append
        /auth (…/afniexternalcareers/cx/job-details/<id> -> …/afniexternalcareers/auth)."""
        u = url or ""
        m = re.match(r"(https?://[^/]+/[^/]+)(?:/cx/|/auth|/$|$)", u)
        if not m:
            return ""
        return m.group(1).rstrip("/") + "/auth"

    async def _on_auth_wall(self, page: Page) -> bool:
        """True when we're on the AIM auth / create-profile / OTP wall, not the apply form."""
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "/auth" in u:
            return True
        try:
            txt = ((await page.inner_text("body"))[:4000] or "").lower()
        except Exception:
            txt = ""
        return bool(re.search(
            r"create a profile|create your profile|let's find your dream job|"
            r"sign in using social|we don't recognize|enter the code|verification code|"
            r"one-time|create an account", txt))

    async def _fill_auth_email(self, page: Page) -> bool:
        """Fill the AIM auth EMAIL box with the persona email (side-effect-free — filling a text box
        transmits nothing). Returns True if a box was filled."""
        email = self._account_email()
        if not email:
            return False
        return await self._fill_first(page, [
            'input[type="email"]', 'input[autocomplete="email"]', 'input[autocomplete="username"]',
            'input[name*="email" i]', 'input[placeholder*="email" i]',
            'input[aria-label*="email" i]', 'input[type="text"]'], email)

    # ---- account bootstrap (LIVE, gated) ------------------------------------
    async def _bootstrap_account(self, page: Page) -> None:
        """Create the ADP candidate profile for the persona and verify the EMAILED OTP, so the apply
        form becomes reachable. Side-effect-free until Continue is pressed; ONLY runs when
        advance_wizard (gated in open_form). NO captcha is presented (ADP AIM has none). The OTP is
        read from the persona's own @takhet.com Maildir (verify_code.read_code + a fallback reader)."""
        if not await self._on_auth_wall(page):
            return
        email = self._account_email()
        if not email:
            return
        pw = getattr(self, "_account_pw", None) or _gen_password()
        self._account_pw = pw
        start_ts = time.time()

        # 1) Email -> Continue (this is the step that GENERATES the OTP / starts account creation).
        # The AIM email input loads asynchronously — retry until it takes before pressing Continue.
        for _ in range(6):
            if await self._fill_auth_email(page):
                break
            await page.wait_for_timeout(1500)
        await self._click_first(page, [
            'button:has-text("Continue")', 'button:has-text("Next")',
            'button[type="submit"]', 'button:has-text("Sign in")'])
        await page.wait_for_timeout(3500)
        await self._dismiss_cookie_banner(page)

        # 2) Create-profile step (rendered when ADP doesn't recognise the email): name + password ×2.
        name = self._persona_name()
        if name:
            await self._fill_first(page, [
                'input[name*="first" i]', 'input[autocomplete="given-name"]',
                'input[placeholder*="first name" i]'], name.split()[0])
            await self._fill_first(page, [
                'input[name*="last" i]', 'input[autocomplete="family-name"]',
                'input[placeholder*="last name" i]'], name.split()[-1])
            await self._fill_first(page, [
                'input[name="name" i]', 'input[autocomplete="name"]',
                'input[placeholder*="full name" i]'], name)
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
        await self._click_first(page, [
            'button:has-text("Create")', 'button:has-text("Continue")',
            'button:has-text("Next")', 'button:has-text("Submit")', 'button[type="submit"]'])
        await page.wait_for_timeout(3500)

        # 3) EMAIL OTP: ADP mails a one-time code to the persona box — read it and enter it.
        code = await self._await_email_code(email, start_ts)
        if code:
            await self._fill_first(page, [
                'input[name*="code" i]', 'input[autocomplete="one-time-code"]',
                'input[placeholder*="code" i]', 'input[inputmode="numeric"]',
                'input[aria-label*="code" i]'], code)
            await self._click_first(page, [
                'button:has-text("Verify")', 'button:has-text("Confirm")',
                'button:has-text("Continue")', 'button:has-text("Submit")', 'button[type="submit"]'])
            await page.wait_for_timeout(3500)

        # 4) The apply form loads once authenticated — let it settle.
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
                                attempts: int = 12, interval: float = 6.0) -> str:
        """Poll the persona's Maildir for ADP's emailed OTP. Uses verify_code.read_code first (the
        shared GH/Ashby security-code reader), then a strategy-local fallback that scans a recent
        ADP-sender email for a bare 6-digit code (ADP's subject wording can drift past read_code's
        subject filter). Returns '' if none arrives."""
        try:
            from backend.tools.verify_code import read_code
        except Exception:
            read_code = None
        for _ in range(max(1, attempts)):
            code = None
            if read_code is not None:
                try:
                    code = read_code(email, since_ts)
                except Exception:
                    code = None
            if not code:
                code = self._read_otp_fallback(email, since_ts)
            if code:
                return code
            try:
                await self._page_sleep(interval)
            except Exception:
                pass
        return ""

    @staticmethod
    def _read_otp_fallback(email: str, since_ts: float) -> str:
        """Fallback OTP reader: the newest ADP-sender email at/after since_ts with a bare 6-digit
        code (mirrors the verify_code Maildir walk but with an ADP-scoped sender/subject match, so a
        'Your one-time password is 123456' subject-less code still resolves). Reads the persona's OWN
        mailbox only; never submits."""
        local, _, domain = (email or "").strip().partition("@")
        if not local or not domain:
            return ""
        root = os.path.join("/var/mail/vhosts", domain, local)
        files: list[str] = []
        for sub in ("new", "cur"):
            d = os.path.join(root, sub)
            try:
                for n in os.listdir(d):
                    p = os.path.join(d, n)
                    try:
                        if os.path.getmtime(p) >= since_ts - 5:
                            files.append(p)
                    except Exception:
                        continue
            except Exception:
                continue
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files:
            try:
                with open(p, "rb") as f:
                    head = f.read(20000).decode("utf-8", "ignore")
            except Exception:
                continue
            subj = re.search(r"^Subject:.*$", head, re.I | re.M)
            frm = re.search(r"^From:.*$", head, re.I | re.M)
            blob = (subj.group(0) if subj else "") + " " + (frm.group(0) if frm else "")
            if not (_OTP_SENDER_RE.search(frm.group(0) if frm else "")
                    or _OTP_SUBJECT_RE.search(subj.group(0) if subj else "")):
                continue
            m = _OTP_CODE_RE.search(re.sub(r"\s+", " ", head))
            if m:
                return m.group(1)
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
                if not await loc.is_visible(timeout=800):
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

    # ---- ADP-specific gap fill (authenticated apply form) -------------------
    async def _fill_adp_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # AckPrivacyStatement + any required consent/acknowledgement, then EEO decline.
        await self._tick_acknowledge(page)
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                if not await self._select_by_label(page, "state", state):
                    await self._select_by_label(page, "country", "United States")
                    await page.wait_for_timeout(1000)
                    await self._select_by_label(page, "state", state)
            except Exception:
                pass
        await self._answer_screeners(page, facts)

    async def _answer_screeners(self, page: Page, facts) -> None:
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("adp: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("adp: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered native <select> screeners; pick the deterministic answer."""
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
            values = self._screener_answer(label, facts)
            if not values:
                continue
            for v in values:
                if await self._select_by_label(page, key, v):
                    break

    async def _answer_radio_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED radio-group screener with a truthful, backed pick; leave an
        unmatched group for the human rather than guessing."""
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
        contains label_substr — via select_option (fires change). Skips an already-answered select."""
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
        """Tick every REQUIRED, currently-unchecked checkbox that is not a marketing opt-in (a
        Terms/AckPrivacyStatement consent box), so the step's Continue is not blocked."""
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
                                 r"contact you about|talent community|opportunities|refer a friend", ctx):
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
            logger.debug("adp: checkbox tick raised: %s", exc)

    async def _tick_acknowledge(self, page: Page) -> None:
        """Tick a required certification/acknowledgement checkbox or radio (single affirmative option
        like 'I acknowledge' / the AckPrivacyStatement consent)."""
        try:
            ids = await page.evaluate(
                """()=>{const out=[];
                  for(const el of document.querySelectorAll('input[type=checkbox],input[type=radio]')){
                    if(el.checked||!el.id)continue;
                    const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');
                    const t=((l&&l.innerText)||(el.closest('label')||{}).innerText||'').toLowerCase();
                    if(/acknowledge|i certify|i attest|i agree|i understand|i confirm|"""
                """privacy statement|read and (understood|accept)/.test(t))
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
        """Deterministic, truthful answer candidates for an Afni CSR / insurance-rep screener
        (lowercased label). Ordered strongest-first; returns None to leave a question for the human.
        Truthful for a synthetic US persona DESIGNED to fit the job (in-state, native English,
        bilingual only when the role is)."""
        facts = facts or {}
        if re.search(r"acknowledge|i certify|i attest|privacy statement", t):
            return None                                   # handled by _tick_acknowledge
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
        _cs = (r"customer service|call center|contact center|technical (?:customer )?support|"
               r"customer support|help ?desk|insurance|claims|retail|customer")
        if re.search(rf"experience.*(?:{_cs})|(?:{_cs}).*experience", t):
            return ["3-5 years", "3+ years", "5+ years", "5 years", "1-3 years", "More than 1",
                    "1 year", "Yes"]
        if re.search(r"how (much|many years?).*experience|years of experience|total years", t):
            return ["3-5 years", "3+ years", "1-3 years", "5 years", "More than", "Yes"]
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat", t):
            return ["Yes"]
        if re.search(r"(?:commitment|obligation|conflict).{0,40}"
                     r"(?:interfere|attendance|schedule|availab|work)"
                     r"|foresee (?:any )?(?:commitment|conflict|obligation)"
                     r"|interfere with (?:your )?(?:attendance|schedule|work|availab)"
                     r"|impact.*attendance", t):
            return ["No"]
        if re.search(r"private|secure|quiet|workspace|distraction|free from|dedicated (work)?space", t):
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
        `unfilled` reflects the gap fill and the submit gate is honest."""
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
        """Close the OneTrust cookie banner that floats over the action bar and intercepts the
        Sign in / Continue / Submit clicks."""
        try:
            await page.click("#onetrust-accept-btn-handler", timeout=1500)
            await page.wait_for_timeout(250)
            return
        except Exception:
            pass
        for name in ("Agree and proceed", "Accept All Cookies", "Accept All",
                     "Accept Cookies", "I Agree", "Reject All", "Deny"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    # ---- wizard walker (mirrors Amazon/Avature/Oracle-ORC) ------------------
    async def _step_signature(self, page: Page) -> str:
        try:
            return await page.evaluate(
                "()=>{const a=document.querySelector('[aria-current=\"step\"],[aria-current=\"true\"],"
                ".progress-current,[class*=active][class*=step]');"
                "const h=document.querySelector('h1,h2,legend,[class*=step-title],[class*=section-title]');"
                "return (a?a.innerText.trim().slice(0,40):'')+'|'+(h?h.innerText.trim().slice(0,40):'');}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind): 'submit' on the final step, 'advance' on Continue/Next, else None."""
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
            logger.debug("adp: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        await self._dismiss_cookie_banner(page)
        await self._tick_acknowledge(page)
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
            logger.debug("adp: step fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("adp: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step apply wizard: click Continue while it advances (filling each new
        step), STOP at the final Submit — recording its selector WITHOUT clicking it. ADP has no
        captcha, so solve_on_page is a graceful no-op. If a Continue click does NOT advance
        (validation blocked it), stop and leave the gaps in `unfilled`."""
        for _ in range(8):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                try:
                    await captcha_solver.solve_on_page(page)
                except Exception:
                    pass
                report["submit_selector"] = (
                    "button:has-text('Submit application'), button:has-text('Submit'), "
                    "button:has-text('Submit Application'), [data-automation-id*='submit' i] button")
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
