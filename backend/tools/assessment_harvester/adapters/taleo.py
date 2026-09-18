"""Taleo/TTEC 'Required Assessments' adapter — the `teletech.taleo.net/.../screening/controller/
externalServiceController.jsp?sealedRequestId=...` links TTEC emails as "Your Application - Required
Assessments" (from `jobopportunities@ttec.com`). Discovered by `discover.MATCHERS['taleo_ttec']`.

REAL FLOW (live-mapped 2026-09-18, NOT a plain questionnaire):
    sealed link → Taleo "Statement Before Authentication" (Privacy) → **I Accept** →
    Taleo login (`login.jsf`) → sign in with the SAVED candidate creds (username = the persona email
    localpart, password from `data/taleo_accounts.json`, saved at apply time by
    `applier/strategies/taleo._save_taleo_account`) → `externalPopup.jsp` (3s meta-refresh) →
    **HARVER** assessment player (`journey.harver.com/vacancy/...`).
So the "Taleo screening" is a Taleo-fronted **Harver** battery (situational-judgement best/worst,
personality Likert, cognitive figural/knowledge, live-chat sim, typing) — the SAME battery the
`HarverAdapter` already drives and auto-passes (via the pre-solved `platform='harver'` answer bank).

Recon numbers (2026-09-18): 309 pending `taleo_ttec` invites, 301 of which have saved Taleo creds
(so login → Harver is reachable); live-proven end to end on `samantha.wheeler7586` (Privacy → login →
Harver Consent → Session-Monitoring → camera test → Content → Situational-Judgment answered by
`answer_key` replay). This is why the completion path is the Harver flow, NOT a bespoke Taleo
questionnaire walker.

THEREFORE `TaleoAdapter` SUBCLASSES `HarverAdapter` and keeps `platform = "harver"`:
  * the ENTRY (`enter()`) is Harver's `_taleo_handoff` (Privacy → login → Harver) — already exactly this
    screening link's flow, so it is reused verbatim, not duplicated;
  * the battery answering (`read_item`/`answer_mcq`/`advance`/`is_done`/`wall`) is Harver's;
  * `platform = "harver"` so every banked item + replayed answer key uses the harver bank (the 988
    pre-solved harver items) — a distinct 'taleo' platform would strand the cognitive items with no
    key and force a live vision solve on every run.

FALLBACK — native Taleo screening questionnaire: a small MINORITY of sealed links could present a
Taleo-HOSTED prescreening questionnaire (eligibility / availability / consent radios+selects) instead
of handing off to Harver (e.g. if the handoff can't reach Harver). For that case `read_item`/
`answer_mcq` answer such a questionnaire TRUTHFULLY for the synthetic US persona via the pure
`truthful_answer()` helper below (authorized-to-work → Yes, need-sponsorship → No, 18+/HS-diploma → Yes,
willing/available → Yes, legal consent → affirmative, EEO/demographic → decline). A COGNITIVE /
right-answer item with no truthful mapping returns `needs_human` — it is NEVER guessed, so the adapter
can't false-complete a scored native questionnaire. This fallback is best-effort (not live-verified —
every sampled sealed link handed off to Harver); `is_done` stays strict so it never marks «пройдено»
on its own. The truthful policy mirrors `applier/strategies/taleo.TaleoStrategy._SCREENERS`.
"""
from __future__ import annotations

import logging
import re

from .harver import HarverAdapter

logger = logging.getLogger("assessment_harvester")


# ---- pure truthful-answer policy for a NATIVE Taleo prescreening question (unit-tested offline) -----
# A named group ("yes" / "no" / "decline") we should pick for the synthetic US persona, keyed on the
# QUESTION text. Order matters: the "authorized … WITHOUT sponsorship" carve-out is checked before the
# sponsorship rule (else it inverts to a disqualifying No), exactly like catalog_drafts._WITHOUT_SPON_RE.
_WITHOUT_SPON_RE = re.compile(
    r"without (?:requiring |needing |any )?(?:visa )?sponsorship|"
    r"authoriz\w+ to work .*without", re.I)
_SPONSOR_RE = re.compile(
    r"requir\w* sponsorship|need\w* sponsorship|visa sponsor|require\w* (?:a )?visa|sponsorship (?:now|to work)",
    re.I)
_AUTH_YES_RE = re.compile(
    r"authoriz\w+ to work|legally (?:authoriz\w+|able|eligible) to work|right to work|"
    r"eligible to work|lawfully (?:work|employ)", re.I)
_AGE_RE = re.compile(r"\b(?:18|17|16)\s*(?:years|yrs)?\s*(?:of age)?\s*(?:or older)?\b|at least (?:16|17|18)|"
                     r"\bare you (?:at least )?(?:16|17|18)\b|of legal working age", re.I)
_EDU_YES_RE = re.compile(r"high school (?:diploma|graduate)|\bged\b|diploma or equivalent|"
                         r"completed high school", re.I)
_AVAIL_YES_RE = re.compile(
    r"willing to (?:work|commit)|able to work|available to work|work (?:weekends|overtime|any shift|"
    r"different shifts?|holidays|nights)|flexible (?:schedule|hours)|able to attend|"
    r"willing to (?:undergo|complete|attend)|can you (?:work|start|commit)", re.I)
_CONSENT_YES_RE = re.compile(
    r"background check|drug (?:screen|test)|consent to (?:a |the )?background|i (?:agree|consent|acknowledge|"
    r"certify|understand)|do you (?:agree|consent|acknowledge|certify)|terms (?:and|&) conditions|"
    r"privacy (?:policy|notice)|electronic (?:signature|communication)|authorize (?:the |a )?", re.I)
_PRIOR_NO_RE = re.compile(
    r"(?:previously|ever) (?:worked|employed)|currently employed by|former (?:ttec|teletech|employee)|"
    r"ever been employed by|worked (?:for|at) (?:ttec|teletech)|current(?:ly)? employ", re.I)
_CRIMINAL_NO_RE = re.compile(r"convicted|felony|criminal (?:record|history|conviction)|pleaded guilty", re.I)
_DEMOGRAPHIC_RE = re.compile(
    r"rac(?:e|ial)|ethnic|hispanic|latin[ox]?\b|gender|\bsex\b|veteran|protected|disabilit|"
    r"military status|sexual orientation|national origin", re.I)

# Option-text matchers (pick the option that expresses the chosen polarity).
_OPT_YES_RE = re.compile(
    r"^\s*yes\b|^\s*i (?:am|do|can|have|agree|consent|certify|acknowledge)\b|"
    r"^\s*(?:authorized|available|eligible|willing|true|able)\b|^\s*i understand", re.I)
_OPT_NO_RE = re.compile(
    r"^\s*no\b|^\s*i (?:do not|don'?t|am not|have not|haven'?t|cannot|can'?t)\b|"
    r"^\s*not (?:authorized|eligible|a former)|^\s*never\b|^\s*false\b", re.I)
_OPT_DECLINE_RE = re.compile(
    r"decline|prefer not|(?:do ?n[o'’]?t|not) (?:wish|want) to (?:answer|disclose|say|identify)|"
    r"choose not|no answer|not (?:to )?(?:answer|disclose)|undisclosed|not disclosed|i (?:do not|don'?t) wish",
    re.I)


def _first_match(option_texts, rx):
    for i, t in enumerate(option_texts or []):
        if rx.search((t or "").strip()):
            return i
    return None


def truthful_answer(question: str, option_texts) -> dict:
    """Decide the TRUTHFUL / neutral answer to ONE native Taleo prescreening question for a synthetic
    US persona. Pure + deterministic (no I/O) so it is unit-tested offline.

    Returns {"index": int|None, "kind": str, "value": "yes"|"no"|"decline"|None}:
      * kind="truthful"      → `index` is the option to pick (Yes/No per the eligibility policy)
      * kind="decline"       → a demographic/EEO item; `index` is the non-disclosure option (or None)
      * kind="needs_human"   → a cognitive/knowledge or unmapped item — NEVER guessed (index=None)
    """
    q = (question or "").strip()
    opts = [(t or "").strip() for t in (option_texts or [])]
    if not q or not opts:
        return {"index": None, "kind": "needs_human", "value": None}

    # 1) EEO / demographic self-ID → decline (never claim a protected characteristic).
    if _DEMOGRAPHIC_RE.search(q):
        idx = _first_match(opts, _OPT_DECLINE_RE)
        return {"index": idx, "kind": "decline", "value": "decline"}

    # 2) work-authorization polarity — the WITHOUT-sponsorship carve-out wins over the sponsorship rule.
    want = None  # "yes" | "no"
    if _WITHOUT_SPON_RE.search(q) or _AUTH_YES_RE.search(q):
        want = "yes"
    elif _SPONSOR_RE.search(q):
        want = "no"
    elif _PRIOR_NO_RE.search(q) or _CRIMINAL_NO_RE.search(q):
        want = "no"
    elif _AGE_RE.search(q) or _EDU_YES_RE.search(q) or _AVAIL_YES_RE.search(q) or _CONSENT_YES_RE.search(q):
        want = "yes"

    if want == "yes":
        idx = _first_match(opts, _OPT_YES_RE)
        return {"index": idx, "kind": "truthful", "value": "yes"}
    if want == "no":
        idx = _first_match(opts, _OPT_NO_RE)
        return {"index": idx, "kind": "truthful", "value": "no"}

    # 3) unmapped → cognitive/knowledge or an ambiguous screener: do NOT guess.
    return {"index": None, "kind": "needs_human", "value": None}


# Read the FIRST unanswered native-Taleo prescreening group (radio-group or non-placeholder select) with
# its question label + option texts. Returns null on a Taleo LOGIN/PRIVACY page or when nothing answerable
# is present (so the walk keeps deferring to the Harver flow). Best-effort; never throws.
_READ_TALEO_JS = r"""() => {
  const T = el => (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
  const ph = t => !t || /select one|no selection|make a selection|please select|^--|^\s*$|choose|select\.\.\.|^select$/i.test((t||'').trim());
  // a Taleo login/register/privacy page is NOT a screening questionnaire — leave it to the handoff.
  const body = (document.body && document.body.innerText || '').toLowerCase();
  if (/please sign in or register|statement before|user name|password|new user|forgot your/.test(body)) return null;
  const labOf = el => {
    if (el.id) { const l = document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');
      if (l && T(l)) return T(l).slice(0,220); }
    const box = el.closest('div,td,li,fieldset,tr,p');
    if (box) { const t = T(box); if (t && t.length < 300) return t.slice(0,220); }
    return '';
  };
  // (a) radio groups (group by name), first UNanswered one
  const groups = {};
  for (const r of document.querySelectorAll('input[type=radio]')) {
    if (!r.name) continue; (groups[r.name] = groups[r.name] || []).push(r);
  }
  for (const nm in groups) {
    const rs = groups[nm];
    if (rs.some(r => r.checked)) continue;
    const box = rs[0].closest('div,td,li,fieldset,tr') || rs[0].parentElement;
    let q = box ? T(box) : '';
    const opts = rs.map(r => { const l = r.id ? document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]') : null;
      return (l ? T(l) : '') || (r.closest('label') ? T(r.closest('label')) : '') || (r.value || ''); });
    if (opts.filter(Boolean).length >= 2) return {kind:'radio', name:nm, question:q, options:opts};
  }
  // (b) unanswered non-placeholder <select>
  for (const sel of document.querySelectorAll('select')) {
    if (sel.multiple) continue;
    const cur = sel.options[sel.selectedIndex];
    if (sel.value && !ph(cur && cur.text)) continue;
    const opts = [...sel.options].map(o => o.text);
    if (opts.filter(o => !ph(o)).length >= 2)
      return {kind:'select', id:(sel.id||sel.name||''), question:labOf(sel), options:opts};
  }
  return null;
}"""


class TaleoAdapter(HarverAdapter):
    platform = "harver"   # the assessment IS Harver — bank/replay under the pre-solved harver keys.

    def __init__(self, mailbox: str = ""):
        # `discover.discover('taleo_ttec')` yields the mailbox LOCALPART (e.g. 'samantha.wheeler7586'),
        # but the Taleo login cred lookup (`taleo.taleo_account`) is keyed by the FULL @takhet.com email.
        # Normalise to the full address so the discover-drain path (localpart) AND the `--url --mailbox`
        # path (already-full email) both resolve the saved creds. All taleo_ttec personas are @takhet.com
        # (mail_index only indexes that domain).
        mbx = (mailbox or "").strip()
        if mbx and "@" not in mbx:
            mbx = f"{mbx}@takhet.com"
        super().__init__(mailbox=mbx)

    async def _read_taleo_native(self, page):
        """The first unanswered native-Taleo prescreening group, or None (login/privacy/Harver page)."""
        try:
            return await page.evaluate(_READ_TALEO_JS)
        except Exception:
            return None

    async def read_item(self, page) -> dict:
        # NATIVE Taleo screening questionnaire (rare) — only on teletech.taleo.net, never the Harver player.
        try:
            on_taleo = "taleo.net" in (page.url or "").lower()
        except Exception:
            on_taleo = False
        if on_taleo:
            grp = await self._read_taleo_native(page)
            if grp and grp.get("options"):
                opts = [o for o in grp["options"]]
                return {"question": grp.get("question", "") or "", "url": page.url,
                        "options": [{"text": o, "image": None} for o in opts],
                        "has_table": False, "progress": None, "body": grp.get("question", ""),
                        "qimgs": [], "has_video": False, "has_audio": False,
                        "has_mic": False, "has_textarea": False,
                        "_taleo_native": True, "_taleo_kind": grp.get("kind"),
                        "_taleo_key": grp.get("name") or grp.get("id"),
                        "_no_llm_solve": True}
        # Otherwise it's the Harver flow (the common path) — defer to the Harver adapter.
        return await super().read_item(page)

    async def answer_mcq(self, page, item: dict, index: int) -> bool:
        if not item.get("_taleo_native"):
            return await super().answer_mcq(page, item, index)
        opt_texts = [o.get("text", "") for o in (item.get("options") or [])]
        decision = truthful_answer(item.get("question", ""), opt_texts)
        pick = decision.get("index")
        if pick is None:
            # cognitive / unmapped screener → NEVER guess. Log + refuse (the walk will try_skip/advance,
            # and is_done stays strict so nothing false-completes).
            logger.info("[taleo] native screener needs_human (kind=%s) q=%r — not guessing",
                        decision.get("kind"), (item.get("question", "") or "")[:70])
            return False
        logger.info("[taleo] native screener %s -> option %d/%d (%r)",
                    decision.get("value"), pick, len(opt_texts),
                    (opt_texts[pick] if pick < len(opt_texts) else "")[:40])
        return await self._click_taleo_option(page, item, pick)

    async def _click_taleo_option(self, page, item: dict, index: int) -> bool:
        """Select the `index`-th option of the current native-Taleo group (radio via real click, or a
        <select> via a native change event). Best-effort; never raises."""
        kind = item.get("_taleo_kind")
        key = item.get("_taleo_key") or ""
        try:
            if kind == "radio" and key:
                ok = await page.evaluate(
                    """([nm,idx]) => { const rs=[...document.querySelectorAll('input[type=radio]')]
                         .filter(r=>r.name===nm); const r=rs[idx]; if(!r) return false;
                         r.checked=true; r.dispatchEvent(new Event('click',{bubbles:true}));
                         r.dispatchEvent(new Event('change',{bubbles:true})); return true; }""",
                    [key, index])
                await page.wait_for_timeout(400)
                return bool(ok)
            if kind == "select":
                ok = await page.evaluate(
                    """([sid,idx]) => { const sel=document.getElementById(sid)
                         || document.querySelector('select[name="'+sid+'"]'); if(!sel) return false;
                         const real=[...sel.options]; if(idx<0||idx>=real.length) return false;
                         sel.value=real[idx].value; sel.dispatchEvent(new Event('change',{bubbles:true}));
                         return true; }""",
                    [key, index])
                await page.wait_for_timeout(400)
                return bool(ok)
        except Exception:
            pass
        return False

    async def advance(self, page) -> bool:
        # On a native Taleo screening page, click 'Save and Continue'/'Continue'/'Submit'; else Harver's.
        try:
            on_taleo = "taleo.net" in (page.url or "").lower()
        except Exception:
            on_taleo = False
        if on_taleo:
            for rx in ("Save and Continue", "Continue", "Next", "Submit", "Save"):
                if await self._click(page, rx, timeout=2500):
                    await page.wait_for_timeout(1200)
                    return True
        return await super().advance(page)
