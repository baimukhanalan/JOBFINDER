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
from backend.applier.analyzer import analyze_page
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
# The oneclick submit control is NOT a plain <button> — SmartRecruiters renders the footer
# action as an <oc-button data-test="footer-submit"> / <oc-button data-test="footer-next">
# CUSTOM ELEMENT (light DOM) that wraps an <spl-button> whose real <button> lives in a shadow
# root. The ONLY plain <button>s in the light DOM are the "Apply With Indeed"/"Apply With
# LinkedIn" INTEGRATION buttons + cookie controls, so a naive querySelectorAll('button') /
# find_submit_button (which matches `button:has-text('Apply')`) clicks the Indeed button and the
# real form never submits. So we locate the primary action with a shadow-piercing walk that
# excludes those integrations (see _PRIMARY_JS) and tag it `data-jf-sr-primary`; the recorded
# selector below points at that tag (never clicked here — the caller presses it).
_SUBMIT_SELECTOR = "[data-jf-sr-primary]"

# Shadow-piercing finder for the SmartRecruiters footer PRIMARY action. Walks the document +
# every open shadow root, collects candidate <oc-button>/<spl-button>/<button>/[role=button]
# elements, EXCLUDES the LinkedIn/Indeed/Google external-apply integrations, the secondary
# "Add experience/education" buttons, the avatar/file-browse buttons and cookie/privacy
# controls, then tags the footer primary (data-test^="footer-" or type="primary") with
# `data-jf-sr-primary` and returns {kind, text, dtest}. kind='advance' for Continue/Next,
# 'submit' for the final Submit/Apply/Send. Returns null when no primary action is on screen.
_PRIMARY_JS = r"""
() => {
  function* walk(root){
    yield* root.querySelectorAll('*');
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) yield* walk(el.shadowRoot);
  }
  for (const el of walk(document))
    if (el.hasAttribute && el.hasAttribute('data-jf-sr-primary')) el.removeAttribute('data-jf-sr-primary');
  const EXTERNAL = /apply with (indeed|linkedin|google|xing|seek|facebook|glassdoor)/i;
  const ADV = /^(continue|next|save (and|&) continue)$/i;
  const SUB = /(submit application|send application|complete application|^submit$|^finish$|^apply$|i'?m interested)/i;
  const cands = [];
  for (const el of walk(document)){
    const tag = (el.tagName || '').toLowerCase();
    const role = (el.getAttribute && el.getAttribute('role')) || '';
    if (!(tag === 'oc-button' || tag === 'spl-button' || tag === 'button' || role === 'button')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;                 // not visible
    if (el.disabled) continue;
    const cls = (el.className && el.className.toString) ? el.className.toString() : '';
    const dtest = (el.getAttribute && (el.getAttribute('data-test') || el.getAttribute('data-sr-id') || '')) || '';
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    // EXCLUDE integrations / cookie / privacy / avatar / add-more / clear / delete controls.
    if (tag === 'oc-external-apply-button' || tag === 'oc-external-providers-buttons') continue;
    if (/external-apply-button/i.test(cls)) continue;
    if (EXTERNAL.test(text)) continue;
    if (/indeed|linkedin|google|xing|seek/i.test(dtest)) continue;
    if (/cookie|ot-sdk|ot-cookie|policy-link/i.test(cls) || /^(cookie settings|cookie policy|cookies settings|privacy notice)$/i.test(text)) continue;
    if (/^(add-experience|add-education|avatar-browse)$/i.test(dtest)) continue;
    if (/clearButton|delete-button/i.test(dtest) || /clearButton|delete-button/i.test(cls)) continue;
    const typeAttr = (el.getAttribute && (el.getAttribute('type') || '')).toLowerCase();
    const isFooter = /^footer-/i.test(dtest);
    const isPrimary = typeAttr === 'primary' || /c-spl-button--primary/.test(cls);
    const isSecondary = typeAttr === 'secondary' || typeAttr === 'tertiary'
      || /c-spl-button--secondary|c-spl-button--tertiary/.test(cls);
    if (!(isFooter || isPrimary)) continue;
    if (isSecondary && !isFooter) continue;                       // an "Add" is secondary, skip
    cands.push({el, dtest, text, isFooter, isPrimary, y: r.y});
  }
  if (!cands.length) return null;
  // Footer wins over a bare primary; then the lowest-on-page (the action bar sits at the bottom).
  cands.sort((a, b) => (b.isFooter - a.isFooter) || (b.isPrimary - a.isPrimary) || (b.y - a.y));
  const p = cands[0];
  p.el.setAttribute('data-jf-sr-primary', '1');
  let kind;
  if (/^footer-next$/i.test(p.dtest) || ADV.test(p.text)) kind = 'advance';
  else if (/^footer-submit$/i.test(p.dtest) || SUB.test(p.text)) kind = 'submit';
  else kind = ADV.test(p.text) ? 'advance' : 'submit';
  return {kind, text: p.text, dtest: p.dtest};
}
"""

# ---- SmartRecruiters "Preliminary questions" SPL web-component screeners --------------
# The screening screen is built from shadow-DOM components with NO native form controls the
# generic analyzer / native-radio filler can see:
#   <spl-radio-group><span slot="label-content">Q?</span><spl-radio label="Yes" value="1"
#       role="radio" id="spl-form-element_12">…</spl-radio-group>   (custom radios)
#   <spl-autocomplete data-test="question-eeo-gender-select">…<input role="combobox" id="question_…_gender">
#   <spl-checkbox data-test="consent-box" required>…</spl-checkbox>  (privacy declaration)
# so we enumerate them with shadow-piercing walks and drive them by element id (Playwright
# locators pierce open shadow roots). A protected-characteristic question is always DECLINED.
_SR_DECLINE_RE = re.compile(
    r"prefer not|do(?:es)? not want to answer|don'?t wish|do not wish|decline|not to answer|"
    r"not to disclose|not to say|choose not|i do not wish", re.I)
_SR_DEMO_Q_RE = re.compile(
    r"disability|protected veteran|veteran status|are you a[n]? .*veteran|gender identity|"
    r"sexual orientation|\brac(?:e|ial)\b|ethnicit|self-?identif", re.I)
# Optional marketing/opt-in checkboxes are NOT ticked (only required legal/privacy consent is).
_MKTG_RE = re.compile(
    r"contact you|opt.?in|newsletter|marketing|promotional|talent (community|network|pool)|"
    r"future (job|opportunit)|keep me (posted|informed)", re.I)

# All three enumerators share this generator (yields every node across open shadow roots).
_WALK_JS = ("function* walk(root){ yield* root.querySelectorAll('*');"
            " for(const el of root.querySelectorAll('*')) if(el.shadowRoot) yield* walk(el.shadowRoot); }")

_RADIO_GROUPS_JS = r"""
() => {
  %s
  const out = [];
  for (const g of walk(document)) {
    if (g.tagName.toLowerCase() !== 'spl-radio-group') continue;
    const span = g.querySelector('[slot="label-content"]');
    const q = ((span ? span.innerText : g.getAttribute('label')) || '').replace(/\s+/g, ' ').trim();
    const radios = [...g.querySelectorAll('spl-radio')].map(r => ({
      label: (r.getAttribute('label') || r.innerText || '').replace(/\s+/g, ' ').trim(),
      id: r.id || '', checked: r.getAttribute('aria-checked') === 'true'}));
    const required = g.getAttribute('required') != null || g.getAttribute('aria-required') === 'true';
    out.push({q, required, answered: radios.some(r => r.checked), radios});
  }
  return out;
}
""" % _WALK_JS

_TEXT_SCREENERS_JS = r"""
() => {
  %s
  function climb(el){ let node=el,h=0; while(node&&h<8){ let p=node.parentElement;
    if(!p){const rn=node.getRootNode(); p=rn&&rn.host?rn.host:null;} if(!p) break;
    const tx=(p.innerText||'').replace(/\s+/g,' ').trim(); if(tx.length>10) return tx.slice(0,180);
    node=p; h++; } return ''; }
  const out=[];
  for (const el of walk(document)){
    const tag=el.tagName.toLowerCase();
    if (tag!=='input' && tag!=='textarea') continue;
    const t=(el.type||'').toLowerCase();
    if (tag==='input' && t!=='text' && t!=='') continue;
    if ((el.getAttribute('role')||'')==='combobox') continue;   // EEO select handled elsewhere
    const id=el.id||'';
    if (id.indexOf('question_')!==0) continue;                  // only screening question fields
    const req = el.required || el.getAttribute('aria-required')==='true' || !!el.closest('[aria-required="true"]');
    out.push({id, required:req, value:(el.value||''), q:climb(el)});
  }
  return out;
}
""" % _WALK_JS

_AUTOCOMPLETE_JS = r"""
() => {
  %s
  const out=[];
  for (const el of walk(document)){
    if (el.tagName.toLowerCase()!=='spl-autocomplete') continue;
    let native=null;
    for (const d of walk(el)){ if (d.tagName && d.tagName.toLowerCase()==='input'){ native=d; break; } }
    out.push({input_id:(native&&native.id)||el.id||'',
      placeholder: el.getAttribute('placeholder')||'',
      data_test: el.getAttribute('data-test')||'',
      required: el.getAttribute('required')!=null || el.getAttribute('aria-required')==='true',
      value: (native&&native.value)||''});
  }
  return out;
}
""" % _WALK_JS

_SPL_CHECKBOX_JS = r"""
() => {
  %s
  const out=[];
  for (const el of walk(document)){
    if (el.tagName.toLowerCase()!=='spl-checkbox') continue;
    out.push({id: el.id||'', data_test: el.getAttribute('data-test')||'',
      required: el.getAttribute('required')!=null || el.getAttribute('aria-required')==='true',
      checked: el.getAttribute('value')==='true' || el.getAttribute('aria-checked')==='true'
               || el.hasAttribute('checked'),
      label: (el.innerText||'').replace(/\s+/g,' ').trim().slice(0,120)});
  }
  return out;
}
""" % _WALK_JS

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
        # The phone field is an intl widget whose country selector must be set, or the form shows
        # "Please provide a valid phone number" and Next won't advance (intermittently unset).
        try:
            await self._fix_phone_country(page, profile_form)
        except Exception as exc:
            logger.debug("smartrecruiters: phone fix raised: %s", exc)
        # The Google-Places LOCATION typeahead (SmartRecruiters' `location` input) is a
        # combobox the analyzer skips — type the persona's city and pick the first suggestion.
        try:
            await self._fill_location(page, profile_form)
        except Exception as exc:
            logger.debug("smartrecruiters: location fill raised: %s", exc)
        # Pre-screening Yes/No + experience/education/language questions the analyzer misses
        # (custom radio groups / non-native selects), answered deterministically & TRUTHFULLY.
        await self._answer_screeners(page, facts)

    async def _fix_phone_country(self, page: Page, profile_form: dict) -> None:
        """SmartRecruiters' phone field is an intl-tel widget with a country selector; when the
        country isn't set the form shows "Please provide a valid phone number" and Next won't
        advance (the auto-detect is flaky). Re-enter the US number in E.164 (+1XXXXXXXXXX) so the
        widget deterministically selects the US country flag and validates."""
        tel = page.locator('input[type="tel"]').first
        if not await tel.count():
            return
        raw = ((await tel.input_value()) or profile_form.get("phone") or "").strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 10:
            digits = "1" + digits
        if len(digits) != 11 or not digits.startswith("1"):
            return
        e164 = "+" + digits
        try:
            await tel.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            await tel.click(timeout=2500)
            try:
                await tel.press("Control+a")
                await tel.press("Delete")
            except Exception:
                await tel.fill("")
            # typing "+1" first makes the intl widget switch to the US country as the code is read.
            await tel.type(e164, delay=45)
            await page.wait_for_timeout(500)
            await tel.blur()
        except Exception as exc:
            logger.debug("smartrecruiters: phone e164 retype raised: %s", exc)

    async def _fill_location(self, page: Page, profile_form: dict) -> bool:
        """Fill the SmartRecruiters location field. It is a custom `<spl-autocomplete
        data-test="location-autocomplete">` web component rendered in SHADOW DOM — a real
        `<input role=combobox aria-required=true>` whose value only STICKS when a suggestion
        is clicked (an uncommitted typed value is cleared by the component). Playwright locators
        pierce open shadow roots, so target the input directly; the old label-scan used
        `document.querySelectorAll` (does NOT pierce shadow) + Google `.pac-item` selectors and
        so never found the field, leaving City blank → "Please provide your place of residence".
        Best-effort — an optional/absent field is fine."""
        city = (profile_form.get("city")
                or (profile_form.get("location") or "").split(",")[0]).strip()
        if not city:
            return False
        inp = None
        for sel in ('input[data-sr-id="location-autocomplete-search-search-input"]',
                    'spl-autocomplete[data-test="location-autocomplete"] input[role="combobox"]',
                    'input.c-spl-input[aria-controls^="menu-"]'):
            try:
                cand = page.locator(sel).first
                if await cand.count():
                    inp = cand
                    break
            except Exception:
                continue
        if inp is None:
            try:
                cand = page.get_by_role(
                    "combobox", name=re.compile(r"city|location|residence|town", re.I)).first
                if await cand.count():
                    inp = cand
            except Exception:
                inp = None
        if inp is None:
            return False

        async def _type_and_pick(query: str) -> bool:
            try:
                await inp.click(timeout=2500)
                try:
                    await inp.fill("")
                except Exception:
                    pass
                await inp.type(query, delay=90)   # >= the component's minquerylength (3)
                for _ in range(14):               # SR renders role=option items async on type
                    await page.wait_for_timeout(400)
                    opts = page.get_by_role("option")
                    try:
                        if await opts.count():
                            await opts.first.click(timeout=2000)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            return False

        picked = await _type_and_pick(city)
        if not picked:
            # retry with just the city's first token (e.g. "New York City" -> "New York")
            short = city.split(",")[0].split()[0]
            if short and short != city:
                picked = await _type_and_pick(short)
        return picked

    async def _answer_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED screener truthfully for a synthetic US persona located at
        the job's city. The SmartRecruiters "Preliminary questions" screen is built entirely from
        SHADOW-DOM web components — `spl-radio-group` (custom radios, no native <input type=radio>),
        `spl-autocomplete` EEO selects, and a required `spl-checkbox` consent — so alongside the
        native-<select>/native-radio fillers we run the spl-* widget fillers below. Leaves an
        unmatched question for the human, never guesses."""
        facts = facts or {}
        await self._tick_acknowledge(page)
        # order matters: answer the gating radios (e.g. "worked here before? -> No") first, then
        # the conditional free-text, EEO decline, and the required consent checkbox.
        # Tick the required consent BEFORE the EEO comboboxes: the EEO autocompletes leave an
        # overlay open that intercepts the consent label click (its dismissal is timing-bound), so
        # tick consent while nothing is open, then run EEO, then re-tick as an idempotent safety net.
        steps = (
            ("select screeners", self._answer_select_screeners(page, facts)),
            ("radio screeners", self._answer_radio_screeners(page, facts)),
            ("spl radio groups", self._answer_spl_radio_groups(page, facts)),
            ("spl text screeners", self._fill_spl_text_screeners(page)),
            ("spl consent (pre)", self._tick_spl_consent(page)),
            ("eeo autocompletes", self._answer_eeo_autocompletes(page)),
            ("spl consent (post)", self._tick_spl_consent(page)),
        )
        for label, coro in steps:
            try:
                await coro
            except Exception as exc:
                logger.debug("smartrecruiters: %s raised: %s", label, exc)

    # ---- SmartRecruiters SPL web-component screeners (shadow DOM) ----------------
    async def _answer_spl_radio_groups(self, page: Page, facts) -> None:
        """Answer every UNANSWERED <spl-radio-group> (custom radios — no native <input type=radio>,
        so _answer_radio_screeners can't see them). Each group carries its question in a
        <span slot="label-content"> and its options as <spl-radio label=... value=... role=radio id=...>.
        A demographic group (disability / veteran / gender / race) is DECLINED via its non-disclosure
        option; every other group uses the deterministic truthful _screener_answer. Clicks by the
        spl-radio's id (Playwright locators pierce open shadow roots)."""
        facts = facts or {}
        try:
            groups = await page.evaluate(_RADIO_GROUPS_JS)
        except Exception:
            return
        for grp in groups:
            if grp.get("answered"):
                continue
            q = (grp.get("q") or "").lower()
            radios = grp.get("radios") or []
            rid = None
            if _SR_DEMO_Q_RE.search(q):
                # protected characteristic — pick the offered non-disclosure option, never claim one.
                for r in radios:
                    if _SR_DECLINE_RE.search((r.get("label") or "").lower()):
                        rid = r.get("id")
                        break
            if not rid:
                cands = self._screener_answer(q, facts)
                if cands:
                    for c in cands:
                        cl = c.strip().lower()
                        for r in radios:
                            if self._opt_match(cl, (r.get("label") or "").strip().lower()):
                                rid = r.get("id")
                                break
                        if rid:
                            break
            if not rid:
                continue
            await self._click_spl_radio(page, rid)

    async def _click_spl_radio(self, page: Page, rid: str) -> bool:
        loc = page.locator(f'[id="{rid}"]').first
        try:
            if not await loc.count():
                return False
            try:
                await loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            await loc.click(timeout=3000)
            await page.wait_for_timeout(150)
            return True
        except Exception:
            try:
                await loc.click(timeout=2000, force=True)
                return True
            except Exception:
                return False

    async def _fill_spl_text_screeners(self, page: Page) -> None:
        """Fill a REQUIRED empty free-text screening question that is a CONDITIONAL follow-up
        (e.g. "If yes, when? and how many months/years did you work here before?") with "N/A" —
        truthful for a synthetic persona who answered the gating question negatively. A genuinely
        open-ended/behavioral prompt is left for the human (never auto-filled with prose)."""
        try:
            fields = await page.evaluate(_TEXT_SCREENERS_JS)
        except Exception:
            return
        for f in fields:
            if not f.get("required") or (f.get("value") or "").strip():
                continue
            q = (f.get("q") or "").lower()
            conditional = bool(re.search(
                r"if (yes|so|applicable|no)|how many (month|year|hour|day)|how long|"
                r"when did you|which (company|employer)|previous(ly)? employ|"
                r"name of (your )?(company|employer)", q))
            if not (conditional or q == ""):
                continue                          # open-ended/behavioral → leave for the human
            fid = f.get("id")
            if not fid:
                continue
            # The <spl-input> HOST shares its id with the inner native <input>, so a bare
            # [id=…] locator resolves to the host (not fillable) — target the inner input/textarea.
            try:
                await page.locator(
                    f'input[id="{fid}"], textarea[id="{fid}"]').first.fill("N/A", timeout=2500)
            except Exception:
                pass

    async def _answer_eeo_autocompletes(self, page: Page) -> None:
        """Decline the required EEO <spl-autocomplete> self-ID selects (Gender, Race/Ethnicity) by
        opening each and picking its non-disclosure option — never claims a protected characteristic.
        The location autocomplete (also spl-autocomplete) is handled by _fill_location and skipped."""
        try:
            metas = await page.evaluate(_AUTOCOMPLETE_JS)
        except Exception:
            return
        for m in metas:
            dt = (m.get("data_test") or "").lower()
            ph = (m.get("placeholder") or "").lower()
            blob = f"{dt} {ph}"
            is_demo = ("eeo" in dt) or bool(re.search(r"gender|race|ethnic|veteran|disab", blob))
            if not is_demo:
                continue                          # location / non-demographic autocomplete
            if (m.get("value") or "").strip():
                continue
            fid = m.get("input_id")
            if fid:
                await self._pick_autocomplete_decline(page, fid)

    async def _pick_autocomplete_decline(self, page: Page, input_id: str) -> bool:
        """Pick the non-disclosure option in an EEO <spl-autocomplete>. The option label is rendered
        inside each item's <spl-typography-body> SHADOW root, so it is NOT readable via text /
        inner_text — instead we TYPE a decline token to filter the searchable list and click the sole
        survivor (no other EEO gender/race option contains "wish"/"decline"/"prefer not", so the
        filtered result is unambiguous). Falls back to the LAST option (EEO decline convention)."""
        inp = page.locator(f'input[id="{input_id}"]').first
        try:
            if not await inp.count():
                return False
            await inp.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        async def _open() -> int:
            try:
                await inp.click(timeout=2500)
            except Exception:
                return 0
            await page.wait_for_timeout(500)
            try:
                return await page.get_by_role("option").count()
            except Exception:
                return 0

        # Type-to-filter with decline tokens; the survivor is the non-disclosure option.
        for token in ("i do not wish", "do not wish", "don't wish", "wish", "decline",
                      "prefer not", "not to answer", "not to disclose", "no answer"):
            if not await _open():
                continue
            try:
                await inp.fill("")
                await inp.type(token, delay=45)
            except Exception:
                continue
            await page.wait_for_timeout(650)
            opts = page.get_by_role("option")
            try:
                n = await opts.count()
            except Exception:
                n = 0
            if 1 <= n <= 2:
                try:
                    await opts.first.click(timeout=2500)
                    await page.wait_for_timeout(300)
                    if ((await inp.input_value()) or "").strip():
                        await self._close_listbox(page, inp)
                        return True
                except Exception:
                    pass
        # Fallback: open with an empty query and pick the LAST option (decline is conventionally last
        # on an EEO self-ID select). Only reached if no decline token matched.
        try:
            await inp.fill("")
        except Exception:
            pass
        n = await _open()
        if n:
            opts = page.get_by_role("option")
            try:
                await opts.nth(n - 1).click(timeout=2500)
                await page.wait_for_timeout(300)
                ok = bool(((await inp.input_value()) or "").strip())
                await self._close_listbox(page, inp)
                return ok
            except Exception:
                pass
        await self._close_listbox(page, inp)
        return False

    async def _close_listbox(self, page: Page, inp=None) -> None:
        """Close an open autocomplete listbox so its overlay can't intercept the next widget's
        click (an open EEO dropdown was swallowing the consent-checkbox click). Blur the input and
        press Escape twice; do NOT click a heading — that scrolls the page and races the next
        scroll_into_view+click."""
        try:
            if inp is not None:
                await inp.blur()
        except Exception:
            pass
        for _ in range(2):
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(150)

    async def _tick_spl_consent(self, page: Page) -> None:
        """Tick a REQUIRED <spl-checkbox> that is a legal/privacy declaration (e.g. consent-box:
        "You declare that you have read and understand the privacy notice") — you can't submit
        without it. A marketing/opt-in checkbox is left UNticked. Clicking the spl-checkbox HOST or
        its label does nothing (verified); only a force-click on the inner native <input type=checkbox>
        toggles it."""
        try:
            boxes = await page.evaluate(_SPL_CHECKBOX_JS)
        except Exception:
            return
        for b in boxes:
            if b.get("checked") or not b.get("required"):
                continue
            lab = (b.get("label") or "").lower()
            if _MKTG_RE.search(lab):
                continue
            dt = b.get("data_test") or ""
            bid = b.get("id") or ""
            host = (f'spl-checkbox[data-test="{dt}"]' if dt else f'spl-checkbox[id="{bid}"]')
            # Retry across a few ROUNDS: an autocomplete overlay left open by the EEO step
            # intercepts the FIRST label click (its own Escape only clears the overlay for the
            # NEXT round), so close-then-tick, verify, and repeat until it actually sticks.
            for _ in range(4):
                await self._close_listbox(page)
                if await self._force_check_spl(page, host):
                    break
                if await self._spl_checkbox_checked(page, host):
                    break

    async def _spl_checkbox_checked(self, page: Page, host: str) -> bool:
        """Read whether an spl-checkbox (matched by `host` selector) is checked, across shadow."""
        try:
            return await page.evaluate(
                r"""(sel)=>{function* walk(root){ yield* root.querySelectorAll('*');
                    for(const el of root.querySelectorAll('*')) if(el.shadowRoot) yield* walk(el.shadowRoot); }
                  for(const el of walk(document)){
                    if(!(el.matches && el.matches(sel))) continue;
                    if(el.getAttribute('value')==='true'||el.getAttribute('aria-checked')==='true'
                       ||el.hasAttribute('checked')) return true;
                    for(const d of walk(el)) if(d.tagName&&d.tagName.toLowerCase()==='input'
                       &&d.type==='checkbox'&&d.checked) return true;
                    return false; }
                  return false;}""", host)
        except Exception:
            return False

    async def _force_check_spl(self, page: Page, host: str) -> bool:
        """Tick an spl-checkbox reliably. Both the label and the inner native <input> are simple
        TOGGLES (each flips the component value), so click one, VERIFY the component value, and STOP
        the instant it reads true — never click again once ticked (a second toggle would clear it).
        Alternate targets until it sticks; the component value / ng-valid is authoritative."""
        if await self._spl_checkbox_checked(page, host):
            return True
        targets = (f'{host} [slot="label-content"]',
                   f'{host} input[type="checkbox"]',
                   f'{host} [slot="label-content"]',
                   f'{host} input[type="checkbox"]')
        for target in targets:
            try:
                loc = page.locator(target).first
                if await loc.count():
                    try:
                        await loc.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    # the inner input is often 0x0/visually-hidden → force-click it.
                    await loc.click(timeout=2000, force=("input" in target))
            except Exception as exc:
                logger.debug("smartrecruiters: consent click %s raised: %s", target, exc)
            await page.wait_for_timeout(400)
            if await self._spl_checkbox_checked(page, host):
                return True
        return await self._spl_checkbox_checked(page, host)

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
        # A Yes/No "do you have a high school diploma / GED / equivalent?" — a synthetic persona
        # always has at least a HS diploma (its résumé shows a degree), so answer Yes. Distinct
        # from the "highest level" SELECT above (which returns a level, not Yes/No).
        if re.search(r"high school diploma|\bg\.?e\.?d\.?\b|diploma.{0,20}equivalent|"
                     r"(diploma|degree).{0,15}or (higher|equivalent)", t):
            return ["Yes"]
        # "Have you worked for <company> before?" / "are you a former employee?" — a FRESH synthetic
        # persona has not, so answer No (truthful). Scoped so it doesn't catch experience questions.
        if re.search(r"worked (for|at|with|in).{0,25}(before|previous|prior)|"
                     r"(previously|ever) (worked|been employed)|former (employee|staff)|"
                     r"(are|were) you .*(former|previous) (employee|contractor)|rehire", t):
            return ["No"]
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
                  function* walk(root, all){
                    yield* root.querySelectorAll(all?'*':'input,select,textarea');
                    for(const el of root.querySelectorAll('*')) if(el.shadowRoot) yield* walk(el.shadowRoot, all);
                  }
                  for(const el of walk(document)){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||!!el.closest('[aria-required="true"]');
                    if(!req) continue;
                    const root=el.getRootNode();
                    const rootHost=(root&&root.host)?root.host:null;
                    let empty;
                    if(t==='checkbox' && rootHost && rootHost.tagName
                       && rootHost.tagName.toLowerCase()==='spl-checkbox'){
                      // spl-checkbox is a controlled component: its native <input>.checked stays
                      // DESYNCED from the real state, so judge by the component value / ng-valid.
                      empty=!(rootHost.getAttribute('value')==='true'
                        ||(rootHost.className||'').toString().indexOf('ng-valid')>=0);}
                    else if(t==='checkbox'||t==='radio'){const nm=el.name;
                      empty=nm?![...root.querySelectorAll('[name="'+
                        (window.CSS&&CSS.escape?CSS.escape(nm):nm)+'"]')].some(x=>x.checked):!el.checked;}
                    else empty=!(el.value||'').trim();
                    if(!empty) continue;
                    let lab='';const id=el.id;
                    if(id){const l=root.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab){const l=el.closest('label')||
                      (el.parentElement&&el.parentElement.querySelector('label'));if(l)lab=l.innerText.trim();}
                    if(!lab)lab=el.getAttribute('aria-label')||el.getAttribute('label')||'';
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||(el.name||el.getAttribute('data-sr-id')||'field');
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  }
                  // SmartRecruiters custom radio groups have NO native <input type=radio>; a
                  // required, UNANSWERED group (no spl-radio aria-checked) is an honest gap the
                  // native scan above cannot see — report its question so `unfilled` stays truthful.
                  for(const g of walk(document, true)){
                    if(g.tagName.toLowerCase()!=='spl-radio-group') continue;
                    if(!(g.getAttribute('required')!=null||g.getAttribute('aria-required')==='true')) continue;
                    if([...g.querySelectorAll('spl-radio')].some(r=>r.getAttribute('aria-checked')==='true')) continue;
                    const rr=g.getBoundingClientRect(); if(rr.width===0&&rr.height===0) continue;
                    const span=g.querySelector('[slot="label-content"]');
                    let lab=((span?span.innerText:'')||g.getAttribute('label')||'')
                      .replace(/\\s+/g,' ').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||'question';
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  }
                  return out;}""")
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
        advanced (SmartRecruiters re-renders in place, same URL on a multi-screen config). The
        heading + form controls live in SHADOW DOM (spl-input/spl-select), so this walk must
        pierce shadow roots — a light-DOM-only count is ~constant and never detects advance."""
        try:
            return await page.evaluate(
                r"""()=>{
                  function* walk(root){
                    yield* root.querySelectorAll('*');
                    for(const el of root.querySelectorAll('*')) if(el.shadowRoot) yield* walk(el.shadowRoot);
                  }
                  let n=0, groups=0;
                  for(const el of walk(document)){
                    const tg=el.tagName.toLowerCase();
                    if(tg==='input'||tg==='select'||tg==='textarea') n++;
                    else if(tg==='spl-radio-group') groups++;
                  }
                  // pathname changes to /screening on advance — the most reliable step signal; the
                  // page header <h1> is the constant job title, so it is NOT used.
                  return (location.pathname||'')+'|'+n+'|'+groups;}""")
        except Exception:
            return ""

    async def _tag_primary_button(self, page: Page):
        """Shadow-piercing: find the SmartRecruiters footer PRIMARY action (oc-button/spl-button),
        EXCLUDING the Indeed/LinkedIn external-apply integrations, the secondary "Add" buttons and
        cookie/privacy controls. Tags it `data-jf-sr-primary` and returns {kind,text,dtest} or None."""
        try:
            return await page.evaluate(_PRIMARY_JS)
        except Exception as exc:
            logger.debug("smartrecruiters: primary tag raised: %s", exc)
            return None

    async def _primary_button(self, page: Page):
        """Return (locator, kind) for the screen's primary button — the shadow-piercing tagger
        finds the real <oc-button> footer action (never the Indeed/LinkedIn integration) and tags
        it; the locator targets that tag. kind='submit' on the final screen, 'advance' on
        Continue/Next, (None, None) when no primary action is on screen."""
        info = await self._tag_primary_button(page)
        if not info:
            return None, None
        return page.locator("[data-jf-sr-primary]").first, info.get("kind")

    async def click_submit(self, page: Page) -> bool:
        """Find and click the REAL SmartRecruiters submit button (shadow-piercing; excludes the
        Indeed/LinkedIn integration + Add/cookie controls). Returns True only when a button
        classified as the final submit was actually clicked — the caller's honest submit path."""
        info = await self._tag_primary_button(page)
        if not info or info.get("kind") != "submit":
            return False
        loc = page.locator("[data-jf-sr-primary]").first
        try:
            if not await loc.count():
                return False
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await loc.click(timeout=5000)
            return True
        except Exception as exc:
            logger.debug("smartrecruiters: click_submit raised: %s", exc)
            return False

    async def _finalize_screeners(self, page: Page) -> None:
        """After a screen's fill has SETTLED, close any lingering autocomplete overlay and re-tick
        the required consent. The in-fill consent attempt only FOCUSES the box while an EEO overlay
        is still dismissing (leaves it ng-touched but value=false); a settled attempt actually
        toggles it (verified). Idempotent — a no-op when the consent is already ticked."""
        try:
            await self._close_listbox(page)
            await page.wait_for_timeout(400)
            await self._tick_spl_consent(page)
        except Exception as exc:
            logger.debug("smartrecruiters: finalize screeners raised: %s", exc)

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
                await self._finalize_screeners(page)
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
                await self._finalize_screeners(page)
                await captcha_solver.solve_on_page(page)
                report["submit_selector"] = _SUBMIT_SELECTOR
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(page)
                return
            sig = await self._step_signature(page)
            try:
                await btn.click()
            except Exception:
                break
            # Poll for the SPA to re-render / navigate (base -> /screening) up to ~6s; a single
            # instant re-read can miss a still-rendering advance and false-flag "blocked".
            advanced = await self._await_advance(page, sig)
            if not advanced:
                # Validation likely held the click (most often the intl phone country was unset).
                # Re-fix the phone + re-fill THIS screen and retry the primary click ONCE.
                try:
                    await self._fix_phone_country(page, profile_form)
                except Exception:
                    pass
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                try:
                    btn2, _k2 = await self._primary_button(page)
                    if btn2 is not None:
                        await btn2.click()
                except Exception:
                    pass
                advanced = await self._await_advance(page, sig)
            if not advanced:
                # Still didn't advance -> a required field on this screen is genuinely unfilled.
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)

    async def _await_advance(self, page: Page, prev_sig: str) -> bool:
        """Return True once the step signature changes (the screen advanced), polling up to ~6s."""
        for _ in range(12):
            await page.wait_for_timeout(500)
            try:
                if await self._step_signature(page) != prev_sig:
                    return True
            except Exception:
                pass
        return False
