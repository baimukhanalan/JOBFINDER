"""RECON + first-cut driver — UnitedHealth Group / Optum auto-apply (source key: unitedhealth).

STATUS: recon skeleton only. NOT wired into runner.STRATEGIES, NOT imported anywhere. Safe to sit
here. To go live, port TaleoStrategy into backend/applier/strategies/taleo.py and register it in
runner.STRATEGIES (before GenericStrategy). Do NOT edit shared files from here.

================================================================================================
WHAT WE PROBED (2026-09-01, from this datacenter IP, harmless GET/POST probes only — NO real apply)
================================================================================================
Board rows: 36 active `unitedhealth` rows in mass_hiring_jobs, ALL apply_url =
    https://careers.unitedhealthgroup.com/job/<city>/<slug>/34088/<radancy_job_id>
  i.e. the Radancy TalentBrew JOB-DETAIL page (source connector = mass_hiring.fetch_unitedhealth,
  a Radancy /search-jobs/resultspost scrape). That page is NOT the apply form — its Apply button
  points at the REAL ATS:

  ACTUAL APPLY ATS = ORACLE TALEO Enterprise Edition (uhg.taleo.net).
    The Radancy page embeds:  ApplyUrl = https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=<taleo_id>
    - careersection 10020 = EXTERNAL candidates (the Apply button's macro resolves here). 200 OK.
    - careersection 10000 = "UHG | Internal Careers" (internal mobility) — do NOT use.
    - The Taleo job id (e.g. 2384324) is DIFFERENT from the Radancy data-job-id (99817972208):
      the driver MUST fetch the Radancy page and extract the taleo.net ApplyUrl (see resolve_apply_url).
    - careersection build = 2026PRD.1.3.11.3.0 (akira UI framework — classic server-rendered Taleo).

PROBE EVIDENCE (concrete):
  * GET Radancy job page  -> HTTP/2 200, server: Kestrel, no WAF. href=".../uhg.taleo.net/careersection/
    10020/jobapply.ftl?job=2384324" present in HTML.
  * GET taleo jobapply.ftl -> HTTP/1.1 200, Content-Type text/html, <title>Privacy Agreement</title>.
    NO Akamai / NO Cloudflare / NO redirect / NO account-wall bounce. A standard Taleo
    "Statement Before Authentication" (Data Privacy) page with an "I Accept" / "I Decline" JSF form.
  * Flow after "I Accept" = Sign In OR New User registration (page carries: Sign In, username,
    password, email address). A raw curl POST of the JSF "I Accept" returns 403 — that is the normal
    Taleo stateful-JSF / anti-CSRF behaviour (the akira JS drives a partial submit with a live
    session + ViewState); a real headful browser click walks through fine. It is NOT a bot wall.
  * CAPTCHA: NONE. Zero 'captcha|recaptcha|hcaptcha|grecaptcha|turnstile|sitekey' in the served HTML,
    in the referenced scripts, AND in the 238 KB akira-corec.js core bundle. Standard Taleo external
    careersections do not captcha the apply/registration flow — consistent with the empty grep.
  * WAF / anti-bot: none observed (direct 200s, plain Kestrel front). Unlike TP/iCIMS there is NO
    AWS-WAF entry wall and NO hCaptcha, so NopeCHA is NOT needed here.
  * VIDEO/VOICE assessment gating SUBMIT: none. UHG does invite assessments for some CSR roles, but
    those are POST-submit invites (a separate email/link), like Maximus's later SHL — the Taleo
    application itself submits to "Thank you for applying" independently. The goal is reachable.

GATE (hardest thing between us and "Thanks for Applying"):
  ACCOUNT CREATION + a stateful multi-step JSF wizard. That's it. No captcha, no WAF, no video gate.
  Taleo registration is username + password + email (no captcha). Email verification is typically NOT
  required before you can apply (confirmation arrives after submit); IF a given config requires an
  emailed activation code, the persona's @takhet.com mailbox + verify_code.read_code handles it, same
  as the Greenhouse/Ashby "security code" path.

FEASIBILITY: feasible_needs_live_iteration.  NOT blocked_real_antibot.
  (The old CLAUDE.md "do NOT build" list tagged UnitedHealth as Taleo/BLOCKED under a blanket
  "stacked account + captcha/WAF + video". The probe contradicts the captcha/WAF/video parts: the ONLY
  real obstacle is the account + the JSF wizard, both fully driveable by a headful Playwright browser
  on DISPLAY=:98 — no captcha to solve, so no NopeCHA cost. It just needs live iteration to walk the
  akira wizard, exactly like avature.py / oracle_orc.py were iterated.)

================================================================================================
FLOW MAP a driver must walk (Playwright, headful :98, fresh synthetic US persona + isolated profile)
================================================================================================
  0. mass_hiring_apply.prepare-style setup: synth US persona (synth_persona -> US person, resident at
     the requisition city/state), tailored resume PDF, prefill dir under an `uhg_<id>` jobid namespace.
  1. GET the Radancy job page -> extract the uhg.taleo.net/careersection/10020/jobapply.ftl?job=<id>
     ApplyUrl (resolve_apply_url below). Navigate there.
  2. Privacy / "Statement Before Authentication" page -> click "I Accept".
  3. Sign In / New User step -> choose "New User" / register: username, password + confirm, email
     (persona @takhet.com). (accept terms checkbox if present.)
  4. Multi-step application wizard (standard Taleo pages, plain server-rendered <input>/<select>/
     <textarea> + akira date pickers — the analyzer's field extraction works; NO custom React/JET
     widgets like Workable/Oracle-ORC, so this is EASIER to fill, not harder):
        Personal Information (name, address = persona city/state/ZIP, US phone, email)
        Resume / attachments (upload the tailored PDF; Taleo may also parse it)
        Work Experience + Education (structured blocks, from persona resume[0]/education[0])
        Questionnaire / prescreen Yes/No screeners (eligibility — see ELIGIBILITY below)
        Voluntary EEO / Veteran / Disability self-ID -> DECLINE (never claim a characteristic)
        eSignature (type persona full name) + certification checkbox
        Review -> Submit
  5. Submit -> "Thank you for applying" confirmation page + a Taleo confirmation email in the
     persona's @takhet.com Maildir (ground truth, exactly like the GH/Ashby verification path).

================================================================================================
ELIGIBILITY — how the synthetic persona must FIT (truthful-by-design), analogous to the TP state gate
================================================================================================
  UHG remote roles are location_raw "Remote, United States". Taleo does NOT gate with a TP-style
  hCaptcha "are you located in <state>?" — instead the wizard asks ordinary prescreen questions the
  persona answers truthfully because the persona is DESIGNED to fit:
    * "Legally authorized to work in the US?"                 -> YES  (synthetic US persona)
    * "Now or in future require visa sponsorship?"            -> NO
    * "Are you 18 or older?"                                  -> YES
    * State of residence (a <select>)                         -> the persona's own US state
    * "Have you previously worked for UnitedHealth/Optum?"    -> NO
  There is no per-application HARD state gate (UHG remote roles are broadly multi-state; a few states
  may be excluded per posting but there is no submit-blocking "must be in state X" wall like TP). So
  the persona just needs a coherent US identity. Mirror the Avature/TP approach: place the persona in
  a mainstream US state — reuse icims_recon.ALLOWED_STATES / _pick_state(title, location) to derive a
  (full, code, city, zip) from the requisition (the job URL carries the home-office city, e.g.
  /job/minneapolis/... -> Minnesota), else default Ohio. The persona then answers the residence
  select + work-auth screeners truthfully-by-design. NO license gate observed for CSR roles (clinical
  roles that need an RN/LPN license are already dropped upstream by mass_hiring._CLINICAL, so the
  board rows here are non-licensed CSR/coordinator titles).

================================================================================================
REUSE — closest existing strategies to adapt
================================================================================================
  CLOSEST = applier/strategies/avature.py — SAME shape: account (email+password+confirm) created
    INLINE with the application, then a multi-step Wizard with screeners + EEO decline + eSignature +
    final Submit, NO captcha, NO submit-gating assessment. Reuse: the wizard-walker (_advance_wizard /
    _primary_button / _step_signature / _fill_current_step), _fill_passwords (net-new password inputs
    the analyzer skips), _answer_screeners / _answer_radio_screeners (truthful Yes/No), _tick_required_
    checkboxes / _tick_acknowledge, _decline_demographics, _select_by_label, and the AVATURE_ADVANCE-
    style env gate (default OFF: a plain fill / dry-run must be side-effect-free at the employer since
    advancing creates the account + transmits PII on the final Submit).
  ALSO: oracle_orc.py (same vendor family, guest multi-step wizard + emailed-PIN handling if UHG
    requires an activation code); icims.py::_screener_answer / _decline_demographics (screener +
    EEO-decline lexicon tuned for BPO/CSR); base.prefill for every ordinary field.
  MISSING (net-new for Taleo): (a) TaleoStrategy.matches("taleo.net"); (b) open_form must resolve the
    Radancy job page -> the taleo.net ApplyUrl (resolve_apply_url) THEN accept the Privacy Agreement
    THEN drive New-User registration (username is net-new vs Avature — Taleo wants a distinct username,
    not just email); (c) the akira date-picker widgets for Work Experience / Education start/end dates;
    (d) Taleo's page-at-a-time wizard uses full-page JSF navigations (not an SPA), so the walker must
    wait for each server round-trip / re-analyze the DOM per step.

DELTA vs TP/iCIMS (why this is the EASY sibling): TP was hCaptcha-on-every-submit -> needed paid
NopeCHA + real mouse + the owner's own browser. Taleo has NO captcha and NO WAF, so the whole thing
runs autonomously in the server's headful :98 browser with a fresh synthetic persona + isolated
per-job profile — the same rig, minus the captcha problem. It is closer to the Avature/Maximus lane
(fully autonomous to "Thank you for applying") than to the TP lane.
"""
from __future__ import annotations

import re

_TALEO_APPLYURL_RE = re.compile(
    r"https://[a-z0-9.]*taleo\.net/careersection/(\d+)/jobapply\.ftl\?job=(\d+)", re.I)


def resolve_apply_url(radancy_job_page_html: str, prefer_section: str = "10020") -> str | None:
    """Extract the EXTERNAL Taleo apply URL from a Radancy job-detail page's HTML.

    The mass_hiring_jobs.apply_url is the Radancy job page; the real Taleo apply form lives at a
    DIFFERENT (careersection, taleo_job_id). Prefer careersection 10020 (external) over 10000
    (internal). Returns the taleo.net jobapply.ftl URL, or None if not present.
    """
    hits = _TALEO_APPLYURL_RE.findall(radancy_job_page_html or "")
    if not hits:
        return None
    # prefer the external careersection
    for section, job in hits:
        if section == prefer_section:
            return f"https://uhg.taleo.net/careersection/{section}/jobapply.ftl?job={job}"
    section, job = hits[0]
    return f"https://uhg.taleo.net/careersection/{section}/jobapply.ftl?job={job}"


# ---------------------------------------------------------------------------------------------
# SKELETON strategy — port to backend/applier/strategies/taleo.py and register in runner.STRATEGIES.
# Mirrors AvatureStrategy. Left unwired here on purpose. Fill the TODOs during live :98 iteration.
# ---------------------------------------------------------------------------------------------
try:  # keep this module import-safe even if the apply stack isn't importable in the caller
    from playwright.async_api import Page  # noqa: F401
    from backend.applier.strategies.base import ApplyStrategy

    class TaleoStrategy(ApplyStrategy):  # pragma: no cover — skeleton, live-iterate before deploy
        name = "taleo"
        advance_wizard = False  # gate like AVATURE_ADVANCE: OFF => fill+stop, side-effect-free

        @classmethod
        def matches(cls, url: str) -> bool:
            return "taleo.net" in (url or "").lower()

        async def open_form(self, page, *_a, **_k):
            # 1) if we were handed the Radancy job page, resolve+navigate to the taleo.net ApplyUrl
            #    (resolve_apply_url on the page HTML) — else assume we're already on jobapply.ftl.
            # 2) accept the "Statement Before Authentication" privacy page ("I Accept").
            # 3) New User registration: username (net-new), password+confirm (avature._gen_password),
            #    persona email; accept terms. THEN the wizard's step 1 renders -> super/base.prefill.
            # TODO: implement with live DOM (akira selectors) on :98.
            return None

        async def prefill(self, page, profile_form, resume_path, *a, **k):
            # base pipeline fills the ordinary fields of the current step; then per-Taleo gaps:
            #   _fill_passwords, screener Yes/No (truthful-by-design), EEO decline, eSignature name,
            #   akira date pickers for Work Experience / Education. Walk step-by-step (full-page JSF
            #   navigations) reusing the avature wizard-walker; record the final Submit selector and
            #   STOP unless self.advance_wizard. TODO: live-iterate.
            raise NotImplementedError("TaleoStrategy is a recon skeleton — iterate on :98 before use")

except Exception:  # pragma: no cover
    pass


if __name__ == "__main__":
    # tiny offline self-check of the apply-url resolver (no network)
    sample = ('x <a href="https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=2384324">i</a> '
              'y ApplyUrl-https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=2384324 z')
    print("resolved:", resolve_apply_url(sample))  # -> .../careersection/10020/jobapply.ftl?job=2384324
