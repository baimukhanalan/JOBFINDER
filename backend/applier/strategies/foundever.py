"""Foundever (ex-Sitel Group) SuccessFactors careersection apply strategy.

Foundever's `jobs.foundever.com` postings are SuccessFactors Recruiting Marketing (RMK) pages; the
"Apply now" dropdown's manual-apply option hands off (same tab) to the SuccessFactors **careersection**
application at `career4.successfactors.com/careers?company=SitelPROD`, which renders the WHOLE
application on ONE page:

  * account/identity  — Email + Retype, Password + Retype, First/Middle/Last, phone Country-code
    <select> + Phone, Country/Region of Residence <select> (`fbclc_*` text inputs + two native selects)
  * contact/address   — Street, City, Country + State (SF paginated-select comboboxes), Zip (`tor__*`)
  * screeners         — How-did-you-hear / part-or-full-time / employee-referral (SF comboboxes)
  * EEO / self-ID     — Gender, Race, Military status, Protected/Disabled Veteran, Disability
    (SF comboboxes; answered with the DECLINE option — a synthetic persona never claims a
    protected characteristic)
  * per-job questions — a `fbjq_question_N` Yes/No radio block (min-experience / evening hours /
    hardwired internet / 18+ / HS-or-GED / work-authorized / reside-in-state)
  * consents          — assessment-willingness, SMS consent, e-signature consent + typed signature
  * a required "last 6 digits of SSN" field (a deterministic synthetic value, like the reserved-
    fiction phone/DOB the other synthetic-persona BPO lanes transmit; only sent on the final submit)

There is **NO reCAPTCHA/hCaptcha/Turnstile and no résumé upload** — a captcha-free, Taleo/iCIMS-class
lane that can run headless. Submit is a single `#fbqa_apply` button that BOTH creates the candidate
account AND submits the application; the SuccessFactors "application received" auto-reply lands in the
persona @takhet.com Maildir (the ground truth of success).

`advance` (the real Submit click) is gated by env **FOUNDEVER_ADVANCE=1** — default OFF, so a plain
fill is entirely side-effect-free at the employer (no account, no PII transmitted). The whole fill is
label-driven from the live DOM, so it generalises across Foundever requisitions.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy

logger = logging.getLogger(__name__)

# The SF careersection landing the RMK "Apply now → manual apply" option navigates to. SuccessFactors
# careersections live on either the legacy `careerN.successfactors.com` pods (Foundever = career4) or
# the newer SAP-branded `careerN.sapsf.com` pods (Gainwell = career41); both serve the SAME
# careersection product (identical fbclc_*/tor__*/rcmpaginatedselect/fbjq_question_N field ids), so
# ONE regex recognises the landing host for every SF tenant.
_CAREERSECTION_HOST = "career4.successfactors.com"          # kept for reference (Foundever's pod)
_CAREERSECTION_RE = re.compile(r"career\d*\.(?:successfactors|sapsf)\.com", re.I)
# RMK ("Recruiting Marketing") job-page hosts that hand off to a careersection via the "Apply now →
# manual apply" dropdown. Each new SuccessFactors tenant adds its RMK host here.
_RMK_HOSTS = ("jobs.foundever.com", "jobs.gainwelltechnologies.com")


def _on_careersection(url: str) -> bool:
    return bool(_CAREERSECTION_RE.search(url or ""))


def _on_rmk(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _RMK_HOSTS)

# ---- pure, testable answer logic (no network / no browser) --------------------------------------

_DECLINE_RE = re.compile(
    r"do not wish|don't wish|do not want|prefer not|decline|choose not|"
    r"not to (answer|identif|disclose|say)|not wish to (self.?identif|identif)", re.I)
_PLACEHOLDER_RE = re.compile(r"^\s*(no selection|-\s*select\s*-|select|please select|choose)\s*$", re.I)

# A conservative NEGATIVE-polarity guard for the per-job Yes/No radios: only these get "No". Every
# other Foundever mass-hiring screener seen is an affirmative "are you able / willing / do you have"
# that a synthetic CSR persona answers Yes to.
_NEGATIVE_Q_RE = re.compile(r"convicted|felony|criminal record|terminated for cause|"
                            r"unable to|not able to|do you decline", re.I)


def _real_options(options) -> list[str]:
    """Drop the placeholder / 'No Selection' entries from a combobox option list."""
    return [o for o in (options or []) if o and o.strip() and not _PLACEHOLDER_RE.match(o.strip())]


def combobox_answer(label: str, options, state: str = "", country: str = "United States"):
    """Pick the option TEXT to select for a SuccessFactors combobox, from its live `options` list.

    Deterministic + truthful-by-design for a synthetic US persona:
      * a demographic / EEO self-ID field -> the DECLINE option (never a protected characteristic)
      * How-did-you-hear -> Company Website; part-or-full-time -> Full-time; employee-referral -> No
      * ever-employed-here -> Never Employed; assessment-willingness -> Yes; SMS consent -> Agree;
        e-signature consent -> I agree
      * address Country -> United States; address State -> the persona's own state (placed in the
        job's state so the "reside in <state>" radio is truthful)
    Returns the matching option string, or None to leave the field for review.
    """
    lab = (label or "").lower()
    opts = _real_options(options)
    if not opts:
        return None

    def find(rx: str):
        r = re.compile(rx, re.I)
        for o in opts:
            if r.search(o):
                return o
        return None

    # EEO / demographic self-ID -> decline (checked FIRST; wins over any keyword below)
    if re.search(r"gender|\brace\b|ethnic|hispanic|latino|military status|veteran|disab|"
                 r"self.?identif|orientation|pronoun", lab):
        for o in opts:
            if _DECLINE_RE.search(o):
                return o
        return None
    if "hear about" in lab or "how did you hear" in lab:
        return (find(r"company website") or find(r"\bwebsite\b")
                or find(r"job ?board|indeed|linkedin|search engine") or opts[0])
    if re.search(r"part.?time.*full.?time|full.?time.*part.?time", lab):
        return find(r"^full.?time$") or find(r"full.?time") or find(r"\bboth\b") or opts[0]
    if re.search(r"refer(red)? you to this position|employee refer you|"
                 r"did a current .*employee refer", lab):
        return find(r"^no$") or find(r"\bno\b")
    if re.search(r"employed by .*(sykes|sitel|foundever)|ever been employed|"
                 r"employed (at|by) this company", lab):
        return find(r"never employed") or find(r"\bnever\b") or find(r"^no\b")
    if re.search(r"complete an application assessment|hiring process you will need|"
                 r"complete an assessment|willing to complete|complete an application", lab):
        return find(r"^yes$") or find(r"\byes\b") or find(r"i agree|^agree$")
    if re.search(r"consent to receive text|text message communication|sms", lab):
        return find(r"^agree$") or find(r"\bagree\b") or find(r"^yes$")
    if re.search(r"electronic signature consent|e-?signature consent|signature consent", lab):
        return find(r"i agree") or find(r"\bagree\b") or find(r"^yes$")
    if re.search(r"\bcountry\b|country/region", lab):
        return find(r"^united states$") or find(re.escape(country)) or find(r"united states")
    if re.search(r"\bstate\b|province", lab) and state:
        return find(r"^" + re.escape(state) + r"$") or find(re.escape(state))
    return None


def job_question_answer(prompt: str, state: str = "") -> str:
    """Yes/No for a per-job `fbjq_question_N` radio, truthful for an in-state synthetic CSR persona."""
    p = (prompt or "").lower()
    if _NEGATIVE_Q_RE.search(p):
        return "No"
    if "reside" in p and ("state of" in p or "reside in" in p or "live in" in p):
        # residence gate: Yes iff the persona's placed state is the one named in the prompt
        return "Yes" if (state and state.lower() in p) else "No"
    return "Yes"


def ssn_last6(persona: dict) -> str:
    """A deterministic synthetic 'last 6 digits of SSN' derived from the persona (stable per persona,
    clearly not a real SSN). Analogous to the reserved-fiction phone/DOB the other synthetic-persona
    lanes transmit; only sent on the gated final submit."""
    seed = (persona or {}).get("email") or (persona or {}).get("id") or "foundever"
    n = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % 1000000
    if n < 100000:
        n += 100000
    return f"{n:06d}"


def _foundever_advance() -> bool:
    return os.getenv("FOUNDEVER_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


# ---- the strategy -------------------------------------------------------------------------------

# text input id -> persona field
_TEXT_MAP = {
    "fbclc_userName": "email", "fbclc_emailConf": "email",
    "fbclc_fName": "first_name", "fbclc_lName": "last_name",
    "fbclc_phoneNumber": "phone_local",
    "tor__faddress": "street_address", "tor__fcity": "city", "tor__fzip": "zip",
}


class SuccessFactorsStrategy(ApplyStrategy):
    name = "foundever"
    advance = _foundever_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        return (_on_rmk(u)
                or _on_careersection(u)
                or "successfactors.com/careers" in u
                or "sapsf.com/careers" in u)

    async def open_form(self, page: Page) -> None:
        """Reach the careersection application page. If we're on the RMK job page, accept cookies,
        open the "Apply now" dropdown and click the manual-apply option (same-tab navigation to
        career4.successfactors.com). If already on the careersection, no-op. Never raises."""
        url = page.url or ""
        if _on_careersection(url):
            return
        if not _on_rmk(url):
            return
        await self._accept_cookies(page)
        # open the "Apply now" dropdown-toggle, then its manual-apply menu item. The aria-label casing
        # varies per tenant (Foundever "Apply now", Gainwell "Apply Now"), so match it CASE-INSENSITIVELY
        # (`[aria-label*="apply" i]`) with a class-based + text fallback.
        for sel in ('button.dropdown-toggle[aria-label*="apply" i]',
                    'button.dropdown-toggle:has-text("Apply")',
                    'button:has-text("Apply now")', 'a:has-text("Apply now")'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5000)
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        for sel in ('#applyOption-top-manual', '#applyOption-bottom-manual',
                    'a.applyOption:has-text("Apply Now")'):
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                # WAIT for the dropdown to actually open (the manual item becomes visible), then a REAL
                # click — a force-click on the still-hidden item dispatches the event but does NOT trigger
                # SuccessFactors' SSO navigation to the careersection (proven live on Gainwell). Fall back
                # to force only if the real click can't land (keeps every previously-working tenant green).
                try:
                    await loc.wait_for(state="visible", timeout=4000)
                except Exception:
                    pass
                try:
                    await loc.click(timeout=5000)
                except Exception:
                    await loc.click(timeout=5000, force=True)
                break
            except Exception:
                continue
        # wait for the same-tab navigation to the careersection
        for _ in range(20):
            await page.wait_for_timeout(700)
            if _on_careersection(page.url or ""):
                break
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        await self._accept_cookies(page)

    async def _accept_cookies(self, page: Page) -> None:
        for sel in ('#onetrust-accept-btn-handler', 'button:has-text("Accept All Cookies")',
                    'button:has-text("Accept All")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=3000)
                    await page.wait_for_timeout(800)
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
        report = {"strategy": self.name, "page_type": "application_form", "filled": 0,
                  "failed": 0, "unfilled": [], "review_items": [], "answer_sources": {},
                  "submit_selector": "#fbqa_apply", "submitted": False}
        pf = profile_form or {}
        self._account_pw = getattr(self, "_account_pw", None) or _gen_password()
        try:
            await self.open_form(page)
        except Exception as exc:
            logger.debug("foundever: open_form raised: %s", exc)
        if not _on_careersection(page.url or ""):
            report["page_type"] = "login_required"
            report["note"] = "did not reach the SuccessFactors careersection"
            return report
        state = (pf.get("state") or "").strip()
        try:
            filled = await self._fill_form(page, pf, state)
            report["filled"] = filled
        except Exception as exc:
            logger.debug("foundever: fill raised: %s", exc)
            report["note"] = f"fill error: {type(exc).__name__}"
        report["account_password"] = self._account_pw
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("foundever: rescan raised: %s", exc)
        if self.advance and not report["unfilled"]:
            try:
                report["submitted"] = await self._submit(page)
            except Exception as exc:
                logger.debug("foundever: submit raised: %s", exc)
                report["note"] = f"submit error: {type(exc).__name__}"
        return report

    async def _fill_form(self, page: Page, pf: dict, state: str) -> int:
        """Fill the whole single-page careersection application. Returns a fill count."""
        full = (pf.get("full_name") or pf.get("name") or "").strip()
        parts = full.split()
        vals = {
            "email": (pf.get("email") or "").strip(),
            "first_name": pf.get("first_name") or (parts[0] if parts else ""),
            "last_name": pf.get("last_name") or (parts[-1] if len(parts) > 1 else ""),
            "phone_local": _phone_local(pf.get("phone") or ""),
            "street_address": pf.get("street_address") or pf.get("address") or "1200 Market Street",
            "city": pf.get("city") or "",
            "zip": pf.get("zip") or pf.get("postal_code") or "",
        }
        n = 0
        # 1) plain text inputs (email x2, name, phone, address) by id
        for iid, key in _TEXT_MAP.items():
            v = vals.get(key, "")
            if not v:
                continue
            if await self._fill_text(page, iid, v):
                n += 1
        # passwords x2
        for iid in ("fbclc_pwd", "fbclc_pwdConf"):
            if await self._fill_text(page, iid, self._account_pw):
                n += 1
        # SSN last-6 (required synthetic value)
        ssn = ssn_last6({"email": vals["email"]})
        if await self._fill_text(page, "tor__fpreferredLocYes", ssn):
            n += 1
        # typed e-signature = the persona's full name
        if full and await self._fill_text(page, "tor__fEsignature", full):
            n += 1
        # 2) native selects: phone Country-code + Country/Region of Residence
        if await self._select_native(page, "fbclc_ituCode", r"united states \(\+?1\)"):
            n += 1
        if await self._select_native(page, "fbclc_country", r"^united states$"):
            n += 1
        # 3) SF paginated-select comboboxes (screeners, EEO, address Country/State)
        n += await self._fill_comboboxes(page, state)
        # 4) per-job Yes/No radio questions
        n += await self._fill_job_questions(page, state)
        # 5) required consent checkboxes / data-privacy (never marketing)
        await self._tick_consents(page)
        return n

    async def _fill_text(self, page: Page, input_id: str, value: str) -> bool:
        try:
            loc = page.locator(f'[id="{input_id}"]').first
            if await loc.count():
                await loc.fill(value, timeout=4000)
                return True
        except Exception:
            pass
        return False

    async def _select_native(self, page: Page, sel_id: str, want_rx: str) -> bool:
        """Pick a native <select> option whose text matches want_rx (case-insensitive), firing change."""
        try:
            val = await page.evaluate(
                """([sid,rx])=>{const el=document.getElementById(sid);
                  if(!el||el.tagName!=='SELECT')return null;const r=new RegExp(rx,'i');
                  const o=[...el.options].find(o=>r.test((o.textContent||'').trim()));
                  return o?o.value:null;}""", [sel_id, want_rx])
            if val is None:
                return False
            await page.select_option(f'[id="{sel_id}"]', value=val, timeout=4000)
            return True
        except Exception:
            return False

    async def _combobox_meta(self, page: Page):
        """[(input_id, listbox_id, label)] for every SF paginated-select on the page (incl. the
        State combobox, which lacks role=combobox)."""
        try:
            return await page.evaluate(
                """()=>{const out=[];
                  for(const i of document.querySelectorAll('input.rcmpaginatedselectinput, input[role=combobox]')){
                    out.push([i.id||'', i.getAttribute('aria-owns')||'',
                      (i.getAttribute('aria-label')||i.getAttribute('placeholder')||'').slice(0,90)]);}
                  return out;}""")
        except Exception:
            return []

    async def _fill_comboboxes(self, page: Page, state: str) -> int:
        n = 0
        for input_id, listbox_id, label in await self._combobox_meta(page):
            if not input_id or not label:
                continue
            try:
                if await self._pick_combobox(page, input_id, listbox_id, label, state):
                    n += 1
            except Exception as exc:
                logger.debug("foundever: combobox %s raised: %s", input_id, exc)
        return n

    async def _pick_combobox(self, page: Page, input_id: str, listbox_id: str,
                             label: str, state: str) -> bool:
        """Open a SF paginated-select, read its live options, compute the deterministic answer and
        click it. Country/State are paginated → type to filter first."""
        inp = page.locator(f'[id="{input_id}"]').first
        if not await inp.count():
            return False
        # already answered? (a non-placeholder value present)
        cur = (await inp.input_value() or "").strip()
        if cur and not _PLACEHOLDER_RE.match(cur):
            return False
        needs_filter = bool(re.search(r"\bcountry\b|\bstate\b|province", (label or "").lower()))
        typed = ""
        if needs_filter:
            # SF's paginated-select filters on REAL keystrokes (an .fill() sets the value but does not
            # fire the keyup its autocomplete listens for, so the list stays on page 1 and the target
            # option — e.g. 'United States', far past the alphabetical A's — is never surfaced). Type it.
            typed = "United States" if re.search(r"country", label.lower()) else (state or "")
            if typed:
                try:
                    await inp.click(timeout=4000)
                    await inp.fill("", timeout=3000)
                    await inp.type(typed, delay=35, timeout=6000)
                    await page.wait_for_timeout(1300)
                except Exception:
                    pass
        else:
            try:
                await inp.click(timeout=4000)
                await page.wait_for_timeout(800)
            except Exception:
                return False
        options = await self._read_listbox(page, listbox_id)
        answer = combobox_answer(label, options, state=state)
        if not answer:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        ok = await self._click_option(page, listbox_id, answer)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(250)
        return ok

    async def _read_listbox(self, page: Page, listbox_id: str) -> list:
        try:
            return await page.evaluate(
                """(lid)=>{let list=lid?document.getElementById(lid):null;
                  if(!list){list=[...document.querySelectorAll('[id$=_listSelect],[role=listbox]')]
                    .find(u=>u.getBoundingClientRect().height>1);}
                  if(!list)return [];
                  const seen=new Set(),out=[];
                  for(const x of list.querySelectorAll('li,[role=option],span.rcmpaginatedselectlistitemtext,a')){
                    const t=(x.innerText||x.textContent||'').replace(/\\s+/g,' ').trim();
                    if(t&&!seen.has(t)){seen.add(t);out.push(t);}}
                  return out.slice(0,40);}""", listbox_id)
        except Exception:
            return []

    async def _click_option(self, page: Page, listbox_id: str, want: str) -> bool:
        try:
            return bool(await page.evaluate(
                """([lid,want])=>{let list=lid?document.getElementById(lid):null;
                  if(!list){list=[...document.querySelectorAll('[id$=_listSelect],[role=listbox]')]
                    .find(u=>u.getBoundingClientRect().height>1);}
                  if(!list)return false;const w=(want||'').trim().toLowerCase();
                  const items=[...list.querySelectorAll('li,[role=option],a')];
                  let el=items.find(x=>((x.innerText||x.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase())===w)
                       ||items.find(x=>((x.innerText||x.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase()).includes(w));
                  if(!el)return false;
                  const t=el.querySelector('span.rcmpaginatedselectlistitemtext')||el;
                  t.scrollIntoView({block:'center'});
                  for(const ev of ['mousedown','mouseup','click'])
                    t.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true}));
                  return true;}""", [listbox_id, want]))
        except Exception:
            return False

    async def _fill_job_questions(self, page: Page, state: str) -> int:
        groups = await self._radio_groups(page)
        n = 0
        for g in groups:
            name = g.get("name") or ""
            if not name.startswith("fbjq_question"):
                continue
            if g.get("answered"):
                continue
            ans = job_question_answer(g.get("prompt") or "", state)
            if await self._click_radio(page, name, ans, g.get("options") or []):
                n += 1
        return n

    async def _radio_groups(self, page: Page):
        try:
            return await page.evaluate(
                """()=>{const byName={};
                  for(const r of document.querySelectorAll('input[type=radio]')){
                    const nm=r.name||'';if(!nm)continue;(byName[nm]=byName[nm]||[]).push(r);}
                  const lab=r=>{const l=r.id?document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;
                    return ((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'').trim();};
                  const out=[];
                  for(const nm in byName){const rs=byName[nm];
                    let box=rs[0].parentElement;while(box&&!rs.every(r=>box.contains(r)))box=box.parentElement;
                    let g=0;const optLen=rs.map(r=>lab(r)).join(' ').replace(/\\s+/g,'').length;
                    while(box&&box.parentElement&&g<5){
                      if((box.innerText||'').replace(/\\s+/g,'').length>optLen+12)break;
                      box=box.parentElement;g++;}
                    let qt=box?(box.innerText||''):'';for(const r of rs){const t=lab(r);if(t)qt=qt.split(t).join(' ');}
                    qt=qt.replace(/\\s+/g,' ').trim();
                    out.push({name:nm,prompt:qt.slice(0,240),answered:rs.some(r=>r.checked),
                      options:rs.map(r=>({value:r.value,text:lab(r)}))});}
                  return out;}""")
        except Exception:
            return []

    async def _click_radio(self, page: Page, name: str, answer: str, options) -> bool:
        want = (answer or "").strip().lower()
        value = None
        for o in options:
            if (o.get("text") or "").strip().lower() == want:
                value = o.get("value")
                break
        try:
            found = await page.evaluate(
                """([nm,val,want])=>{
                  const rs=[...document.querySelectorAll('input[type=radio]')].filter(r=>r.name===nm);
                  let el=null;
                  if(val!=null)el=rs.find(r=>r.value===val);
                  if(!el)el=rs.find(r=>{const l=r.id?document.querySelector('label[for="'+
                    (window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;
                    return ((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'')
                      .trim().toLowerCase()===want;});
                  if(!el)return false;el.setAttribute('data-jfr','1');return true;}""",
                [name, value, want])
            if not found:
                return False
            try:
                await page.check('input[data-jfr="1"]', timeout=3000)
            except Exception:
                await page.eval_on_selector(
                    'input[data-jfr="1"]',
                    "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));}")
            await page.eval_on_selector('input[data-jfr="1"]', "e=>e.removeAttribute('data-jfr')")
            return True
        except Exception:
            return False

    async def _tick_consents(self, page: Page) -> None:
        """Uncheck the two pre-checked MARKETING opt-ins (Notification / campaign email — a synthetic
        persona shouldn't get job-alert spam), tick any REQUIRED non-marketing checkbox, then accept
        the required Data Privacy Consent (the 'Terms of Use*' modal)."""
        try:
            await page.evaluate(
                """()=>{const mkt=/notification|receive new job|hear more about career|marketing|newsletter|promotional|subscribe/i;
                  const mktIds=['fbclc_emailenabled','fbclc_campaignemailenabled'];
                  for(const c of document.querySelectorAll('input[type=checkbox]')){
                    const w=c.closest('div,li,fieldset,label,span');const t=((w&&w.innerText)||'').toLowerCase();
                    const isMkt=mkt.test(t)||mktIds.includes((c.id||'').toLowerCase());
                    if(isMkt){ if(c.checked){c.checked=false;
                      c.dispatchEvent(new Event('click',{bubbles:true}));c.dispatchEvent(new Event('change',{bubbles:true}));}
                      continue; }
                    if(c.checked)continue;
                    const req=c.required||c.getAttribute('aria-required')==='true';
                    if(!req&&!/data privacy|i have read|i agree|consent|acknowledge|terms|certify/.test(t))continue;
                    c.checked=true;c.dispatchEvent(new Event('click',{bubbles:true}));
                    c.dispatchEvent(new Event('change',{bubbles:true}));}}""")
        except Exception:
            pass
        await self._accept_data_privacy(page)

    async def _accept_data_privacy(self, page: Page) -> bool:
        """The required 'Terms of Use*' consent: click the '#dataPrivacyId' anchor to open the Data
        Privacy Consent modal (SF only opens it once the rest of the form is valid), then click its
        primary 'Accept' button. Returns True if accepted."""
        try:
            anchor = page.locator('#dataPrivacyId, a:has-text("Read and accept the data privacy")').first
            if not await anchor.count():
                return False
            try:
                await anchor.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await anchor.click(timeout=5000)
        except Exception:
            return False
        await page.wait_for_timeout(1500)
        for sel in ('button.globalPrimaryButton:has-text("Accept")',
                    'button[id^="dlgButton"]:has-text("Accept")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=4000)
                    await page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
        # exact-text fallback (avoid matching "Accept All Cookies")
        try:
            import re as _re
            b = page.get_by_role("button", name=_re.compile(r"^\s*Accept\s*$", _re.I)).first
            if await b.count() and await b.is_visible():
                await b.click(timeout=4000)
                await page.wait_for_timeout(800)
                return True
        except Exception:
            pass
        return False

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty visible fields (so `unfilled` is honest for the submit gate).
        Reads the SF combobox value from its input, radios by group-checked, plain inputs by value."""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  const ph=/^\\s*(no selection|-\\s*select\\s*-|select|please select)\\s*$/i;
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset','image'].includes(t))continue;
                    const r=el.getBoundingClientRect();if(r.width<1&&r.height<1)continue;
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||/appFieldRequired/.test(el.className||'');
                    if(!req)continue;
                    let empty;
                    if(t==='checkbox'||t==='radio'){const nm=el.name;
                      empty=nm?![...document.querySelectorAll('input[name="'+nm+'"]')].some(x=>x.checked):!el.checked;}
                    else if(el.tagName==='SELECT'){const o=el.options[el.selectedIndex];empty=!el.value||ph.test(o&&o.text||'');}
                    else{const v=(el.value||'').trim();empty=!v||ph.test(v);}
                    if(!empty)continue;
                    let lab='';const id=el.id;
                    if(id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab)lab=el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.name||'field';
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').replace(/\\s+/g,' ').trim().slice(0,80);
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}}
                  return out;}""")
        except Exception:
            return []

    async def _submit(self, page: Page) -> bool:
        """Click #fbqa_apply and detect a confirmation page (the Maildir ack is the ground truth,
        checked by the driver). Only reached under FOUNDEVER_ADVANCE with unfilled empty."""
        try:
            btn = page.locator('#fbqa_apply, button:has-text("apply"), input[value="apply" i]').first
            if not await btn.count():
                return False
            await btn.click(timeout=8000)
        except Exception:
            return False
        for _ in range(20):
            await page.wait_for_timeout(1500)
            try:
                body = (await page.locator("body").inner_text(timeout=5000)).lower()
            except Exception:
                body = ""
            if re.search(r"thank you for (your interest|applying|your application)|"
                         r"application (has been|was) (submitted|received|sent)|"
                         r"your application has been sent|application has been sent|"
                         r"successfully (submitted|applied|sent)|we (have )?received your application|"
                         r"your application has been submitted", body):
                return True
        return False


def _gen_password() -> str:
    import secrets
    body = secrets.token_urlsafe(10).replace("-", "x").replace("_", "y")
    return f"Fv{body}7!"


def _phone_local(phone: str) -> str:
    """The 10-digit local part of a US phone (the SF Country-code <select> carries the +1)."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits
