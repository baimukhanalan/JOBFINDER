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
import time

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
    # --- SF "portalcareer" screener comboboxes (the two-step tenant, e.g. Gainwell, renders the
    # eligibility questions as paginated-select comboboxes; Foundever renders them as fbjq_question_N
    # radios so its combobox labels never carry these). Checked BEFORE the country/state branches
    # below because the work-authorization label literally contains "country of the job". ---
    if re.search(r"authorized to work|legally authorized|right to work|eligible to work", lab):
        return find(r"^yes$") or find(r"\byes\b")
    if re.search(r"require sponsorship|need sponsorship|sponsorship for|visa sponsorship", lab):
        return find(r"^no$") or find(r"\bno\b")
    if re.search(r"current or former employee|currently or formerly employed|"
                 r"previously.*(been )?employed by (this|gainwell)", lab):
        return find(r"^no$") or find(r"\bno\b")
    if re.search(r"family members|close personal friends|any relatives", lab):
        return find(r"^no$") or find(r"\bno\b")
    if re.search(r"signed an agreement|non.?compete|restrict your ability to work", lab):
        return find(r"^no$") or find(r"\bno\b")
    if re.search(r"willing.*to travel|able to travel|travel requirement", lab):
        return (find(r"^no$|^none$|not willing|no travel|^0\s*%?$|^0\s*-") or opts[0])
    if re.search(r"phone type", lab):
        return find(r"^mobile$|mobile") or find(r"cell") or find(r"^home$|home") or opts[0]
    if re.search(r"^details$|^detail$", lab):
        return opts[0]
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
    # Generic fallback for an unmatched Yes/No screener combobox: the two-step tenant renders some
    # job-specific screeners ("This role requires… Are you able to…?") as Yes/No comboboxes. Answer via
    # the same truthful Yes/No policy as the radio screeners (Yes, unless a negative-polarity question).
    # Foundever's comboboxes are all matched above, so this never changes Foundever.
    low = [o.strip().lower() for o in opts]
    if any(o == "yes" or o.startswith("yes") for o in low) and \
            any(o == "no" or o.startswith("no") for o in low):
        want = job_question_answer(label, state).lower()
        return find(r"^" + want + r"$") or find(r"\b" + want + r"\b")
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


def portal_text_value(label: str, pf: dict, extras: dict) -> str | None:
    """Value for a SF 'portalcareer' free-text field, matched by its label (the two-step tenant's
    application page uses DYNAMIC numeric ids like '72:_txtFld', so fields are label-driven).

    `pf` supplies identity/address (email/first/last/full_name/street_address/city/zip); `extras`
    supplies phone_local / company / title / salary / years. Returns None to skip (optional or
    unrecognised). Pure + testable."""
    lab = re.sub(r"^\s*\*?\s*", "", (label or "").strip()).lower()
    if not lab:
        return None
    # optional / conditional fields we deliberately leave blank
    if re.search(r"address 2|address line 2|middle name|alternate phone|if yes|"
                 r"if you answered|visa status|please provide the name", lab):
        return None
    if re.search(r"^e-?mail$|e-?mail address", lab):
        return (pf.get("email") or "") or None
    if re.search(r"legal first name|^first name$|^given name", lab):
        return pf.get("first_name") or None
    if re.search(r"legal last name|^last name$|^family name|surname", lab):
        return pf.get("last_name") or None
    if re.search(r"postal code|\bzip\b|zip code", lab):
        return pf.get("zip") or pf.get("postal_code") or None
    if re.search(r"^street|^address$|address line 1|mailing address|home address", lab):
        return pf.get("street_address") or pf.get("address") or None
    if re.search(r"^city$|^town$|city/town", lab):
        return pf.get("city") or None
    if re.search(r"primary phone|^phone( number)?$|mobile number|^telephone", lab):
        return extras.get("phone_local") or None
    if re.search(r"current company|^company$|^employer$|present employer|company name", lab):
        return extras.get("company") or None
    if re.search(r"current title|current position|job title|^title$|present position", lab):
        return extras.get("title") or None
    if re.search(r"typed signature|^signature$|e-?signature|electronic signature", lab):
        return pf.get("full_name") or None
    if re.search(r"expected.*(salary|pay|rate|hourly|compensation|wage)|salary expectation|"
                 r"desired (salary|pay)|hourly (rate|pay|salary)|pay expectation|wage expectation|"
                 r"salary requirement", lab):
        return extras.get("salary") or None
    if re.search(r"typing speed|words per minute|\bwpm\b|average typing|type.{0,15}per minute|"
                 r"keystrokes per", lab):
        return "45"  # a plausible data-entry/CSR typing speed (WPM), a NUMBER not "Yes"
    if re.search(r"how many years|years of (experience|call center)|years.*experience|"
                 r"experience do you have", lab):
        return extras.get("years") or None
    # An unrecognised job-specific screener rendered as a FREE-TEXT field phrased as a capability /
    # availability question ("This role requires … Are you able to …?"). A synthetic CSR persona
    # affirms — a blank required field would block the submit. Scoped to clear yes-answerable phrasings
    # so an open "describe/what/why" prompt is left for review, not answered "Yes".
    if re.search(r"this role requires|are you able|are you willing|are you comfortable|"
                 r"able to (work|perform|handle)|willing to (work|travel|relocate)|"
                 r"can you (work|perform|commit|provide)|do you have the ability|"
                 r"are you capable", lab):
        return "Yes"
    return None


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
        if _on_careersection(page.url or ""):
            return
        if not _on_rmk(page.url or ""):
            return
        await self._accept_cookies(page)
        # Open the "Apply now" dropdown then its manual-apply item, RETRYING the whole sequence: the
        # dropdown is flaky (sometimes the menu doesn't open, so the manual item is still hidden and a
        # force-click dispatches the event WITHOUT triggering SuccessFactors' SSO nav -> stuck on RMK).
        for _attempt in range(3):
            if _on_careersection(page.url or ""):
                break
            # open the dropdown toggle (aria-label casing varies: Foundever "Apply now" / Gainwell
            # "Apply Now"), then click the manual-apply option.
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
            clicked_manual = False
            for sel in ('#applyOption-top-manual', '#applyOption-bottom-manual',
                        'a.applyOption:has-text("Apply Now")'):
                try:
                    loc = page.locator(sel).first
                    if not await loc.count():
                        continue
                    # WAIT for the menu item to become visible before a REAL click — a force-click on the
                    # still-hidden item does NOT trigger the SSO nav (proven live on Gainwell).
                    try:
                        await loc.wait_for(state="visible", timeout=4000)
                    except Exception:
                        pass
                    try:
                        await loc.click(timeout=5000)
                        clicked_manual = True
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
            if _on_careersection(page.url or ""):
                break
            if not clicked_manual:
                await page.wait_for_timeout(1200)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        await self._accept_cookies(page)

    async def _accept_cookies(self, page: Page) -> None:
        for sel in ('#onetrust-accept-btn-handler', '#cookie-acknowledge',
                    'button:has-text("Accept All Cookies")', 'button:has-text("Accept All")'):
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
        # Flow detection: Foundever's manual-apply lands directly on the COMBINED one-page form
        # (it has #fbqa_apply). A two-step SF tenant (Gainwell) lands on the careersection Sign-In
        # page (a "Create an account" link, no #fbqa_apply) or the account-create page. When there is
        # NO #fbqa_apply and a two-step marker is present, run the account-first path; otherwise fall
        # through to the existing combined path so Foundever is byte-identical.
        if not await self._has(page, '#fbqa_apply'):
            two_step = (await self._has(page, '#fbclc_createAccountButton')
                        or await self._has(page, 'a:has-text("Create an account")')
                        or await self._has(page, 'input.rcmpaginatedselectinput'))
            if two_step:
                try:
                    return await self._prefill_two_step(page, pf, state, report)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("gainwell: two-step prefill raised: %s", exc)
                    report["note"] = f"two-step error: {type(exc).__name__}"
                    return report
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
                      (i.getAttribute('aria-label')||i.getAttribute('placeholder')||'').slice(0,200)]);}
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
        # Only the SHORT address Country/State labels are the paginated selects that need typing to
        # filter — NOT a long screener SENTENCE that merely mentions "country" (e.g. "Are you legally
        # authorized to work in the country of the job…"), which is a Yes/No combobox answered by
        # combobox_answer. Guarding on length keeps that question from being typed with "United States".
        _lab = (label or "").strip().lower()
        needs_filter = (len(_lab) <= 40
                        and bool(re.search(r"\bcountry\b|\bstate\b|province|region", _lab)))
        typed = ""
        if needs_filter:
            # SF's paginated-select filters on REAL keystrokes (an .fill() sets the value but does not
            # fire the keyup its autocomplete listens for, so the list stays on page 1 and the target
            # option — e.g. 'United States', far past the alphabetical A's — is never surfaced). Type it.
            typed = "United States" if re.search(r"country|region", _lab) else (state or "")
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

    async def _has(self, page: Page, selector: str) -> bool:
        try:
            return (await page.locator(selector).count()) > 0
        except Exception:
            return False

    async def _accept_data_privacy(self, page: Page) -> bool:
        """The required 'Terms of Use*' consent: open the Data Privacy Consent dialog, then click its
        primary 'Accept' button. Returns True if accepted."""
        if not await self._dpcs_open(page):
            return False
        return await self._click_dpcs_accept(page)

    async def _dpcs_dialog_present(self, page: Page) -> bool:
        """True when the SF Data Privacy Consent dialog (a `<n>:container` overlay, NOT role=dialog)
        is on screen with an 'Accept' control."""
        try:
            return bool(await page.evaluate(
                """()=>{const c=[...document.querySelectorAll('[id$=":container"]')]
                    .find(x=>x.getBoundingClientRect().height>80);
                  if(!c)return false;
                  return !![...c.querySelectorAll('button,a')].find(b=>/^\\s*accept\\s*$/i.test(
                    (b.innerText||b.value||'')));}"""))
        except Exception:
            return False

    async def _dpcs_open(self, page: Page) -> bool:
        """Open the DPCS dialog. Foundever opens it with a click on the '#dataPrivacyId' anchor; the
        Gainwell account-create page ignores a synthetic anchor click, so fall back to calling the SF
        opener `validateAndOpenDpcsDialog(true)` directly."""
        if await self._dpcs_dialog_present(page):
            return True
        try:
            anchor = page.locator(
                '#dataPrivacyId, a:has-text("Read and accept the data privacy")').first
            if await anchor.count():
                try:
                    await anchor.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await anchor.click(timeout=5000)
                except Exception:
                    pass
        except Exception:
            pass
        for _ in range(6):
            await page.wait_for_timeout(400)
            if await self._dpcs_dialog_present(page):
                return True
        try:
            await page.evaluate("()=>{try{validateAndOpenDpcsDialog(true);}catch(e){}}")
        except Exception:
            pass
        for _ in range(20):
            await page.wait_for_timeout(400)
            if await self._dpcs_dialog_present(page):
                return True
        return False

    async def _click_dpcs_accept(self, page: Page) -> bool:
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
        # exact-"Accept" inside the dialog container (avoid matching "Accept All Cookies")
        try:
            ok = await page.evaluate(
                """()=>{const c=[...document.querySelectorAll('[id$=":container"]')]
                    .find(x=>x.getBoundingClientRect().height>80);
                  if(!c)return false;
                  const b=[...c.querySelectorAll('button,a')].find(x=>/^\\s*accept\\s*$/i.test(
                    (x.innerText||x.value||'')));
                  if(!b)return false;b.click();return true;}""")
            if ok:
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
                  // SF JUIC ARIA radiogroups (portalcareer Yes/No screeners) aren't <input>s, so the
                  // loop above misses them: a group with NO checked radio is an unanswered required
                  // screener. The Gender group defaults to a checked "No Selection" so it's not empty.
                  for(const rg of document.querySelectorAll('[role=radiogroup]')){
                    const radios=[...rg.querySelectorAll('[role=radio]')];
                    if(!radios.length)continue;
                    if(radios.some(a=>a.getAttribute('aria-checked')==='true'))continue;
                    let lab='';const lb=rg.getAttribute('aria-labelledby');
                    if(lb){const el=document.getElementById(lb);if(el)lab=(el.innerText||'').trim();}
                    if(!lab){let box=rg;for(let k=0;k<6&&box;k++){box=box.parentElement;if(!box)break;
                      const cand=[...box.querySelectorAll('label,legend')].find(nn=>!rg.contains(nn)&&(nn.innerText||'').trim().length>6);
                      if(cand){lab=(cand.innerText||'').trim();break;}}}
                    lab=(lab||'radio group').replace(/\\s*\\*\\s*$/,'').replace(/\\s+/g,' ').trim().slice(0,80);
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
            if _CONFIRM_RE.search(body):
                return True
        return False

    # ---- two-step SF tenant (Gainwell): sign-in -> create account -> email OTP -> portalcareer ----

    async def _prefill_two_step(self, page: Page, pf: dict, state: str, report: dict) -> dict:
        """Account-first SF flow (e.g. Gainwell's career41.sapsf.com pod): the careersection Sign-In
        page -> 'Create an account' -> account-create page (create the account, verify the emailed
        one-time passcode) -> the 'portalcareer' single application page. Under `advance` it walks the
        whole thing and clicks Apply; a dry run only fills the account page (side-effect-free)."""
        # 1) reach the account-create page from the Sign-In page, retrying the RMK->careersection
        # handoff + the "Create an account" click (the RMK "Apply now" dropdown is occasionally flaky
        # and force-clicks the still-hidden manual item -> no SSO nav -> stuck on the RMK page).
        for _attempt in range(3):
            if await self._has(page, '#fbclc_createAccountButton'):
                break
            url = page.url or ""
            if _on_rmk(url) and not _on_careersection(url):
                try:
                    await self.open_form(page)
                except Exception as exc:
                    logger.debug("gainwell: open_form retry raised: %s", exc)
            try:
                link = page.locator(
                    'a:has-text("Create an account"), a:has-text("Create account")').first
                if await link.count():
                    await link.click(timeout=8000)
                    for _ in range(25):
                        await page.wait_for_timeout(700)
                        if await self._has(page, '#fbclc_userName'):
                            break
            except Exception as exc:
                logger.debug("gainwell: create-account nav raised: %s", exc)
            if not await self._has(page, '#fbclc_createAccountButton'):
                await page.wait_for_timeout(1500)
        if not await self._has(page, '#fbclc_createAccountButton'):
            report["page_type"] = "login_required"
            report["note"] = "did not reach the account-creation page"
            return report
        report["page_type"] = "application_form"
        # 2) fill the account page (fbclc_* email/name/pwd/country + marketing-uncheck + DPCS accept).
        # _fill_form only touches ids present here; the address/combobox/radio parts no-op.
        try:
            await self._fill_form(page, pf, state)
        except Exception as exc:
            logger.debug("gainwell: account fill raised: %s", exc)
        report["account_password"] = self._account_pw
        if not self.advance:
            report["note"] = ("account page filled; set GAINWELL_ADVANCE=1 to create the account, "
                              "verify the emailed passcode and submit")
            report["unfilled"] = []
            return report
        # 3) create the account
        since = time.time()
        try:
            await page.locator('#fbclc_createAccountButton').first.click(timeout=8000)
        except Exception as exc:
            report["note"] = f"create-account click failed: {type(exc).__name__}"
            return report
        # 4) email one-time passcode
        for _ in range(25):
            await page.wait_for_timeout(1000)
            if await self._has(page, '#passcode'):
                break
        if await self._has(page, '#passcode'):
            code = None
            for _ in range(30):
                code = self._read_account_passcode(pf.get("email", ""), since)
                if code:
                    break
                await page.wait_for_timeout(3000)
            if not code:
                report["note"] = "account created but the email passcode never arrived"
                report["unfilled"] = ["email passcode"]
                return report
            try:
                await self._fill_text(page, "passcode", code)
                await page.locator('#continueBtn').first.click(timeout=8000)
            except Exception as exc:
                report["note"] = f"passcode entry failed: {type(exc).__name__}"
                return report
        # 5) the portalcareer application page
        for _ in range(40):
            await page.wait_for_timeout(1000)
            if await self._has(page, 'input.rcmpaginatedselectinput'):
                break
        if not await self._has(page, 'input.rcmpaginatedselectinput'):
            report["note"] = "did not reach the application form after account verification"
            return report
        try:
            report["filled"] = await self._fill_portalcareer(page, pf, state)
        except Exception as exc:
            logger.debug("gainwell: portal fill raised: %s", exc)
            report["note"] = f"portal fill error: {type(exc).__name__}"
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("gainwell: portal rescan raised: %s", exc)
        if not report["unfilled"]:
            try:
                report["submitted"] = await self._submit_portal(page)
            except Exception as exc:
                logger.debug("gainwell: apply raised: %s", exc)
                report["note"] = f"apply error: {type(exc).__name__}"
        return report

    async def _fill_portalcareer(self, page: Page, pf: dict, state: str) -> int:
        """Fill the SF portalcareer single application page: expand every section, fill the
        label-driven text fields + the paginated-select comboboxes (two passes to catch a
        conditionally-revealed required field, e.g. the how-hear 'Details'). Returns a fill count."""
        await self._expand_sections(page)
        # Retry the whole fill until nothing required is left empty (or a cap): opening one SF
        # paginated-select's dropdown occasionally disturbs an adjacent one, so a single pass can
        # intermittently leave ONE combobox blank; a filled field is skipped on the next pass (the
        # "already answered?" guard in _pick_combobox), so only the flaky-empty ones are re-attempted.
        n = 0
        for _attempt in range(4):
            n += await self._fill_portal_text(page, pf)
            n += await self._fill_comboboxes(page, state)
            n += await self._fill_aria_radiogroups(page, state)
            await page.wait_for_timeout(500)
            try:
                if not await self._rescan_required(page):
                    break
            except Exception:
                pass
        return n

    async def _aria_radiogroups(self, page: Page):
        """[{rgid, prompt, opts:[{id, checked, lab}]}] for every SF JUIC ARIA radiogroup — the
        portalcareer Yes/No screeners are `<... role=radiogroup>` holding `<... role=radio ...>` anchors
        with `aria-checked`, NOT native `<input type=radio>`."""
        try:
            return await page.evaluate(
                r"""()=>{const out=[];
                  for(const rg of document.querySelectorAll('[role=radiogroup]')){
                    const opts=[];
                    for(const a of rg.querySelectorAll('[role=radio]')){
                      let lab=a.getAttribute('aria-label')||'';
                      if(!lab)lab=((a.parentElement&&a.parentElement.innerText)||'').replace(/\s+/g,' ').trim();
                      opts.push({id:a.id, checked:a.getAttribute('aria-checked')==='true', lab:lab.slice(0,24)});}
                    let prompt='';const lb=rg.getAttribute('aria-labelledby');
                    if(lb){const el=document.getElementById(lb);if(el)prompt=(el.innerText||'').replace(/\s+/g,' ').trim();}
                    if(!prompt){let box=rg;
                      for(let k=0;k<6&&box;k++){box=box.parentElement;if(!box)break;
                        const cand=[...box.querySelectorAll('label,legend')].find(nn=>!rg.contains(nn)&&(nn.innerText||'').trim().length>6);
                        if(cand){prompt=(cand.innerText||'').replace(/\s+/g,' ').trim();break;}}}
                    out.push({rgid:rg.id, prompt:prompt.slice(0,160), opts});}
                  return out;}""")
        except Exception:
            return []

    async def _fill_aria_radiogroups(self, page: Page, state: str) -> int:
        """Answer the portalcareer Yes/No screener ARIA radiogroups (experience / availability /
        training / video-screening …). Demographic groups (options Female/Male, or a gender/race
        prompt) are LEFT at their 'No Selection' default — a synthetic persona never self-IDs. A group
        that already has a checked option is skipped."""
        n = 0
        for g in await self._aria_radiogroups(page):
            opts = g.get("opts") or []
            if any(o.get("checked") for o in opts):
                continue
            labs = [(o.get("lab") or "").strip().lower() for o in opts]
            if any(l in ("female", "male") for l in labs) or re.search(
                    r"\bgender\b|\brace\b|ethnic|hispanic|latino", (g.get("prompt") or "").lower()):
                continue
            if not any(l == "yes" or l.startswith("yes") for l in labs):
                continue
            want = job_question_answer(g.get("prompt") or "", state).lower()
            target = None
            for o in opts:
                if (o.get("lab") or "").strip().lower().startswith(want):
                    target = o.get("id")
                    break
            if not target:
                continue
            try:
                loc = page.locator(f'[id="{target}"]').first
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                await loc.click(timeout=3000)
                n += 1
            except Exception:
                try:
                    await page.evaluate("(id)=>{const e=document.getElementById(id);if(e)e.click();}",
                                        target)
                    n += 1
                except Exception:
                    pass
        return n

    async def _expand_sections(self, page: Page) -> None:
        for sel in (':text("Expand all sections")', 'a:has-text("Expand all")'):
            try:
                b = page.locator(sel).first
                if await b.count():
                    await b.click(timeout=4000)
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    async def _fill_portal_text(self, page: Page, pf: dict) -> int:
        full = (pf.get("full_name") or pf.get("name") or "").strip()
        parts = full.split()
        base = {
            "email": (pf.get("email") or "").strip(),
            "first_name": pf.get("first_name") or (parts[0] if parts else ""),
            "last_name": pf.get("last_name") or (parts[-1] if len(parts) > 1 else ""),
            "full_name": full,
            "street_address": pf.get("street_address") or pf.get("address") or "",
            "address": pf.get("address") or pf.get("street_address") or "",
            "city": pf.get("city") or "",
            "zip": pf.get("zip") or pf.get("postal_code") or "",
            "postal_code": pf.get("postal_code") or pf.get("zip") or "",
        }
        extras = {
            "phone_local": _phone_local(pf.get("phone") or ""),
            "company": (pf.get("current_company") or "Self-Employed").strip(),
            "title": (pf.get("current_title") or "Customer Service Representative").strip(),
            "salary": str(pf.get("expected_salary") or "18").strip(),
            "years": "3",
        }
        try:
            fields = await page.evaluate(
                """()=>{const out=[];
                  for(const e of document.querySelectorAll('input[type=text],input:not([type]),textarea')){
                    if(e.getAttribute('role')==='combobox')continue;
                    if(/rcmpaginatedselectinput/.test(e.className||''))continue;
                    const r=e.getBoundingClientRect();if(r.width<1&&r.height<1)continue;
                    const v=(e.value||'').trim();
                    let lab='';const id=e.id;
                    if(id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=(l.innerText||'').trim();}
                    if(!lab)lab=e.getAttribute('aria-label')||e.getAttribute('placeholder')||'';
                    out.push({id:e.id,lab:lab.replace(/\\s+/g,' ').slice(0,220),val:v});}
                  return out;}""")
        except Exception:
            return 0
        n = 0
        for f in fields:
            if f.get("val"):
                continue
            val = portal_text_value(f.get("lab") or "", base, extras)
            if val and await self._fill_text(page, f.get("id") or "", val):
                n += 1
        return n

    async def _submit_portal(self, page: Page) -> bool:
        """Click the SF portalcareer 'Apply' control (distinct from Foundever's #fbqa_apply), confirm
        any 'are you sure' dialog, and detect the on-page application confirmation."""
        try:
            cands = await page.evaluate(
                """()=>[...document.querySelectorAll('button,a,input[type=button],input[type=submit]')]
                    .map(b=>({tag:b.tagName,id:b.id,txt:(b.innerText||b.value||'').replace(/\\s+/g,' ').trim().slice(0,20)}))
                    .filter(b=>/apply|submit|save/i.test(b.txt)).slice(0,15)""")
            logger.info("gainwell portal bottom buttons: %s", cands)
        except Exception:
            pass
        clicked = False
        try:
            # SF portalcareer's "Apply" is a <span role=button id="<n>:_submitBtn" class=rcmSaveButton
            # onclick="juic.fire('<n>:','_submit',event)">Apply</span> — NOT a <button>/<a>, so search
            # span/div[role=button] too and prefer the _submitBtn id / the juic '_submit' handler
            # (never the sibling "_saveBtn"/"_backToListing" or the footer nav links).
            clicked = bool(await page.evaluate(
                """()=>{const cs=[...document.querySelectorAll(
                    'span[role=button],div[role=button],button,a,input[type=button],input[type=submit]')];
                  const txt=b=>(b.innerText||b.textContent||b.value||'');
                  let el=cs.find(b=>/:_submitBtn$/.test(b.id||''));
                  if(!el)el=cs.find(b=>/juic\\.fire\\([^)]*_submit\\b/.test(
                    (b.getAttribute&&b.getAttribute('onclick'))||''));
                  if(!el)el=cs.find(b=>/^\\s*apply\\s*$/i.test(txt(b)));
                  if(!el)el=cs.find(b=>/(submit|send)[^a-z]{0,20}application|apply for (this )?(job|position)/i.test(txt(b)));
                  if(!el)return false;el.scrollIntoView({block:'center'});el.click();return true;}"""))
        except Exception:
            clicked = False
        if not clicked:
            try:
                btn = page.locator('#fbqa_apply').first
                if await btn.count():
                    await btn.click(timeout=8000)
                    clicked = True
            except Exception:
                pass
        if not clicked:
            return False
        # a possible "are you sure you want to apply?" confirmation dialog
        for _ in range(6):
            await page.wait_for_timeout(1000)
            try:
                ok = await page.evaluate(
                    """()=>{const c=[...document.querySelectorAll('[id$=":container"]')]
                        .find(x=>x.getBoundingClientRect().height>60);
                      if(!c)return false;
                      const b=[...c.querySelectorAll('button,a')].find(x=>/^\\s*(apply|ok|yes|confirm|submit)\\s*$/i.test(
                        (x.innerText||x.value||'')));
                      if(!b)return false;b.click();return true;}""")
                if ok:
                    break
            except Exception:
                pass
        # Detect the submit outcome STRUCTURALLY: a successful Apply REPLACES the application form with
        # a bare confirmation page ("Back to Job Listings" / "View Profile", form + Apply button gone,
        # no error, not the Sign-In page) — the SF portalcareer shows NO textual "submitted" message
        # (the success graphic is an image), so text matching alone can't see it. A validation failure
        # keeps the form + shows "Please correct the errors below"; a logout lands on the Sign-In page.
        for _ in range(24):
            await page.wait_for_timeout(1500)
            try:
                st = await page.evaluate(
                    r"""()=>{const body=(document.body.innerText||'');
                      return {
                        err:/please correct the errors|correct the errors below|errors were found/i.test(body),
                        signin:/career opportunities: sign in|already have an account\?|not a registered user/i.test(body),
                        hasSubmitBtn:!![...document.querySelectorAll('[id$=":_submitBtn"]')].length,
                        hasForm:document.querySelectorAll('input.rcmpaginatedselectinput,input[role=combobox]').length,
                        backToListings:/back to job listings/i.test(body),
                        text:body.toLowerCase()};}""")
            except Exception:
                continue
            if st.get("err"):
                logger.info("gainwell apply blocked by validation (errors on the page)")
                return False
            if st.get("signin"):
                logger.info("gainwell apply ended on the sign-in page (not confirmed)")
                return False
            if _PORTAL_SUBMIT_RE.search(st.get("text") or ""):
                return True
            if (not st.get("hasSubmitBtn")) and (not st.get("hasForm")) \
                    and st.get("backToListings"):
                return True
        return False

    def _read_account_passcode(self, email_addr: str, since_ts: float) -> str | None:
        """The 4-8 digit one-time passcode SuccessFactors emails to a new candidate account, read
        from the persona's own Maildir (the same access the driver's confirmation read uses). Runs
        under `sg mail`. None if not yet delivered / not found."""
        import email as _email
        from email import policy as _policy
        local, _, domain = (email_addr or "").strip().partition("@")
        if not local or not domain:
            return None
        md = os.path.join("/var/mail/vhosts", domain, local)
        files: list[str] = []
        for sub in ("new", "cur"):
            d = os.path.join(md, sub)
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
                    msg = _email.message_from_binary_file(f, policy=_policy.default)
            except Exception:
                continue
            subj = str(msg.get("Subject", "")).lower()
            if not re.search(r"passcode|one-?time|verification|verify|security code", subj):
                continue
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body = part.get_content()
                            break
                        except Exception:
                            pass
                if not body:
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            try:
                                body = part.get_content()
                                break
                            except Exception:
                                pass
            else:
                try:
                    body = msg.get_content()
                except Exception:
                    body = ""
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body or ""))
            m = re.search(r"passcode[^0-9]{0,60}?(\d{4,8})|verification is[^0-9]{0,25}?(\d{4,8})|"
                          r"\bcode[^0-9]{0,20}?(\d{4,8})", txt, re.I)
            if m:
                return next(g for g in m.groups() if g)
        return None


_CONFIRM_RE = re.compile(
    r"thank you for (your interest|applying|your application)|"
    r"application (has been|was) (submitted|received|sent)|"
    r"your application has been sent|application has been sent|"
    r"successfully (submitted|applied|sent)|we (have )?received your application|"
    r"your application has been submitted|you have (successfully )?applied|"
    r"application (was )?successfully (submitted|sent)")

# Portalcareer (two-step tenant) submit detection: the page header ALWAYS says "Thank you for your
# interest…", so success needs a DISTINCT phrase (NOT "interest"); a validation failure banner is a
# hard negative.
_PORTAL_ERROR_RE = re.compile(
    r"please correct the errors|correct the errors below|errors were found|"
    r"the following errors|error\(s\) (were|was) found")
_PORTAL_SUBMIT_RE = re.compile(
    r"your application (has been|was|is) (submitted|received|complete|sent)|"
    r"application (has been|was) (submitted|received|sent)|"
    r"(application|profile) (successfully )?(submitted|sent)|"
    r"successfully (submitted|applied)|you have (successfully )?applied|"
    r"we (have )?received your application|thank you for applying|"
    r"application is complete|has been submitted successfully|"
    r"your application (for|to) .* (has been|was) (submitted|received)|"
    r"application submitted")


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
