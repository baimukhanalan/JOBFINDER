"""SHL / TalentCentral assessment automation for the Maximus mass-hiring lane.

Maximus (and other mass-hiring ATSes) gate hire behind an SHL assessment. The link arrives in the
persona's mailbox (surfaced in the CRM as an «Assessment Link» button, classified `action_needed`).
This module drives a REAL (headful, Xvfb) browser through it. Two modes:

  * DEFAULT (`run_intro`, complete_scored off): fill only the rote INTRO — cookies, the mandatory
    consent toggles, the assessments overview, and the "About You" background questionnaire — and
    STOP the moment a real question appears, leaving the scored test for a human.

  * ETALON (`answer_scored`, complete_scored on — env `SHL_COMPLETE_SCORED` or the param): also
    complete the OPQ scored section for a SYNTHETIC persona. The Maximus OPQ is a Situational-
    Judgement + self-rating personality instrument (no factually-correct answers), so a synthetic
    persona's answers are a designed customer-service work-profile (honest, reliable, helpful) —
    like its name/city, not a claim about a real person. Behavioural/judgement items are chosen by
    the LOCAL model; frequency/agreement self-ratings by a deterministic polarity rule; pacing is
    human-like (SHL flags rapid clicking).

HARD BOUNDARY (both modes): a COGNITIVE / KNOWLEDGE item with objectively-correct answers
(numerical/verbal/logical reasoning, data tables, true/false-cannot-say, a skills test) is NEVER
auto-solved — `_is_ability_item` detects it and the engine STOPS (`needs_human`) for a person.
We also never look up how to "pass" the test.

Headful is mandatory: SHL's item player rejects headless browsers ("unsupported browser"), so run
this on the co-pilot's Xvfb display (DISPLAY=:98) where a human can take over in noVNC. Pass the
ORIGINAL email link (`integration-talentcentral.us.shl.com/Integration/ce/...`) — it follows the
redirect to the per-session player host itself (a cached redirect host goes NXDOMAIN after use).
"""
from __future__ import annotations

import logging
import os
import random
import re

logger = logging.getLogger(__name__)

# A page we may auto-fill: the optional demographic / background / job-context intake + plain
# instruction/landing pages. Matched against the visible page text (lowercased).
_BG_RE = re.compile(
    r"about you|please select your gender|racial/?ethnic|country/?region of residence|"
    r"highest educational|industry sector of the job|business function|level of the job|"
    r"optional information|data protection notice|your responsibilities|"
    r"the following list of assessments|expected time|instructions|practice", re.I)

# A SCORED item — NEVER auto-answered. If any of these show, STOP and hand to a human.
_SCORED_RE = re.compile(
    r"most like me|least like me|which of the following|of \d+ questions?|question \d+ of|"
    r"time remaining|strongly agree|strongly disagree|to what extent|rank the|"
    r"select the (option|response|answer)|choose the (option|answer)|"
    r"how much do you agree|each statement|for each of the following|best describes you|"
    r"describes you best|rate each|typically behave|how you typically|the following statements|"
    r"numerical|verbal reasoning|which (number|figure|shape|word)|complete the (series|pattern)", re.I)

# Count of VISIBLE answer-widgets on the page — real <input>/<select>/<textarea> AND custom
# ARIA widgets (role=radio/checkbox/slider/…), EXCLUDING the ever-present language <select> and
# the 1x1 hidden consent toggles. Zero means the page is a pure landing/instructions page (no
# question to answer), so advancing it answers nothing — provably safe.
_ANSWER_WIDGETS_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 3 && r.height > 3; };
  const isLang = sel => {
    const opts = [...(sel.options||[])].map(o => (o.textContent||'').toLowerCase());
    const langy = opts.filter(t => /english|espa|deutsch|fran|italiano|portug|中文|日本|русск/.test(t)).length;
    const al = (sel.getAttribute('aria-label')||'').toLowerCase();
    return langy >= 3 || al.includes('language') || (sel.id||'').toLowerCase().includes('lang');
  };
  let n = 0;
  document.querySelectorAll('input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset])')
    .forEach(e => { if (vis(e)) n++; });
  document.querySelectorAll('textarea').forEach(e => { if (vis(e)) n++; });
  document.querySelectorAll('select').forEach(e => { if (vis(e) && !isLang(e)) n++; });
  document.querySelectorAll('[role=radio],[role=checkbox],[role=slider],[role=spinbutton],[role=textbox]')
    .forEach(e => { if (e.tagName !== 'INPUT' && vis(e)) n++; });
  return n;
}"""

_DECLINE_RE = re.compile(
    r"prefer not to (answer|say|disclose)|do not wish to (answer|disclose)|"
    r"i choose not|decline to (answer|self)", re.I)


async def _dismiss_cookies(page):
    for name in ("OK", "Allow All Cookies", "Accept All Cookies", "Accept All", "Accept"):
        try:
            b = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I))
            if await b.count():
                await b.first.click(timeout=2000)
                await page.wait_for_timeout(600)
                return
        except Exception:
            continue


async def _next_button(page):
    n = page.get_by_role("button", name=re.compile(r"^(next|continue|proceed)$", re.I))
    return n if await n.count() else None


_FORWARD_RE = re.compile(
    r"^(next|continue|proceed|start|begin|launch|go|"
    r"start assessment|begin assessment|get started|start now)$", re.I)
# SHL wraps a whole overview card in ONE <button type=submit> whose accessible name is the entire
# card text, e.g. "Assessment  expected time  27 mins  Continue" — so an anchored match misses it.
# Tier 2 accepts a forward VERB anywhere in the name, guarded by a denylist so it never clicks a
# destructive/navigation control (Sign Out, Exit, Back, Cancel, …).
_FORWARD_CONTAINS_RE = re.compile(r"\b(continue|next|proceed|begin|launch|get started|start)\b", re.I)
_FORWARD_DENY_RE = re.compile(
    r"sign\s*out|log\s*out|\bexit\b|cancel|\bsave\b|\bhelp\b|\bskip\b|accessibilit|"
    r"previous|\bback\b|restart", re.I)


async def _actionable(loc):
    """The last visible+enabled element of a locator (pages put the primary control last)."""
    try:
        cnt = await loc.count()
    except Exception:
        return None
    for i in range(cnt - 1, -1, -1):
        el = loc.nth(i)
        try:
            if await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue
    return None


async def _forward_control(page):
    """The forward control on a page — a button OR link that moves the flow ahead. Tier 1: an
    accessible name that IS exactly a forward word (Next/Continue/Start/…). Tier 2: a button whose
    name CONTAINS a forward verb (SHL's card-sized 'Continue' submit), excluding any destructive/
    navigation control via `_FORWARD_DENY_RE`. Returns a locator handle, or None."""
    for role in ("button", "link"):
        el = await _actionable(page.get_by_role(role, name=_FORWARD_RE))
        if el:
            return el
    btns = page.locator("button, input[type=submit], [role=button]")
    try:
        n = await btns.count()
    except Exception:
        n = 0
    for i in range(n - 1, -1, -1):
        el = btns.nth(i)
        try:
            nm = (await el.evaluate(
                "e => (e.getAttribute('aria-label')||e.value||e.textContent||'').trim()")) or ""
        except Exception:
            continue
        if not nm or _FORWARD_DENY_RE.search(nm) or not _FORWARD_CONTAINS_RE.search(nm):
            continue
        try:
            if await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue
    return None


async def _click_next(page) -> bool:
    nb = await _next_button(page)
    if nb and await nb.first.is_enabled():
        await nb.first.click(timeout=3000)
        return True
    return False


async def _toggle_consent_and_next(page) -> bool:
    """Accept the MANDATORY consent on an SHL consent gate and click Next. Returns True if it
    advanced. SHL renders each consent as a custom toggle: a 1x1 hidden
    `<input class="_toggleButton mandatorychk">` (the OPTIONAL marketing one is `.optionalchk`)
    whose widget only reacts to a real click event fired on the input — Playwright's `.check()`
    does NOT flip it ("Clicking the checkbox did not change its state"). So we fire the widget's
    own click on every MANDATORY toggle (never the optional one), then click Next. Layered
    fallbacks — the associated `<label>`, then a coordinate sweep on the visible pill — cover a
    non-SHL consent gate. Verified live: JS-clicking `.mandatorychk` sets mand=on, opt=off,
    Next=enabled; label click does the same. Do NOT tick `.optionalchk` (marketing opt-in)."""
    if not await page.get_by_role("checkbox").count():
        return False

    # (A) SHL custom toggles: fire the widget click on the MANDATORY toggles only.
    try:
        n = await page.evaluate("""() => {
          const mand = document.querySelectorAll('input._toggleButton.mandatorychk, input.mandatorychk');
          let c = 0;
          mand.forEach(m => { if (!m.checked) { m.click(); c++; } });
          return c;
        }""")
        if n:
            await page.wait_for_timeout(500)
            if await _click_next(page):
                return True
    except Exception:
        pass

    # (B) generic: click the <label> tied to each unchecked, non-optional checkbox (or JS-click it).
    try:
        cbs = page.get_by_role("checkbox")
        for i in range(await cbs.count()):
            c = cbs.nth(i)
            cls = ((await c.get_attribute("class")) or "").lower()
            if "optional" in cls:
                continue
            try:
                if await c.is_checked():
                    continue
            except Exception:
                pass
            cid = await c.get_attribute("id")
            clicked = False
            if cid:
                lbl = page.locator(f'label[for="{cid}"]')
                if await lbl.count():
                    try:
                        await lbl.first.click(timeout=2000)
                        clicked = True
                    except Exception:
                        pass
            if not clicked:
                try:
                    await c.evaluate("el => el.click()")
                except Exception:
                    pass
        await page.wait_for_timeout(500)
        if await _click_next(page):
            return True
    except Exception:
        pass

    # (C) last resort: coordinate sweep on the visible pill to the right of the first checkbox.
    box = await page.get_by_role("checkbox").first.bounding_box()
    if not box:
        return False
    for dx in (21, 40, 35, 45, 30, 50, 25, 55, 15):
        for dy in (11, 6, 12, 0, -6, 3):
            try:
                await page.mouse.click(box["x"] + dx, box["y"] + dy)
            except Exception:
                continue
            await page.wait_for_timeout(250)
            if await _click_next(page):
                return True
    return False


async def _fill_background_page(page, persona: dict) -> None:
    """Fill the optional 'About You' background intake: decline demographics (never claim a
    protected characteristic), set country=US + the persona's education, leave the rest. Every
    field here is optional research data, so a miss is harmless — we never answer a scored item."""
    persona = persona or {}
    # race/ethnic radio -> the decline option
    try:
        radios = page.get_by_role("radio")
        for i in range(await radios.count()):
            r = radios.nth(i)
            try:
                nm = (await r.get_attribute("aria-label")) or ""
            except Exception:
                nm = ""
            if _DECLINE_RE.search(nm):
                await r.check(timeout=2000)
                break
    except Exception:
        pass
    # selects: gender -> decline; country -> United States; education -> persona level.
    async def pick(label_rx: str, values: list[str]):
        try:
            sels = page.get_by_role("combobox")
            n = await sels.count()
        except Exception:
            return
        for i in range(n):
            el = sels.nth(i)
            try:
                lbl = ((await el.get_attribute("aria-label")) or "").lower()
            except Exception:
                lbl = ""
            if not re.search(label_rx, lbl):
                continue
            for v in values:
                try:
                    await el.select_option(label=re.compile(re.escape(v), re.I))
                    return
                except Exception:
                    continue
    await pick(r"gender", ["Prefer not to answer", "I choose not to disclose", "Decline"])
    await pick(r"country|region", [persona.get("country") or "United States", "United States"])
    await pick(r"education|qualification",
               [persona.get("education_level") or "Bachelor", "Bachelor", "University",
                "Associate", "High School"])
    await pick(r"level of the job", ["Entry", "Individual contributor", "Non-manager", "Staff"])


# ---------------------------------------------------------------------------------------------
# Scored-section answering (the ETALON). GATED OFF by default. The Maximus OPQ is a Situational
# Judgement Test (behavioural scenarios) + self-rating biodata — a JUDGEMENT/PERSONALITY
# instrument with no factually-correct answers, so a synthetic persona's answers are a designed
# work-profile (like its name/city), not a claim about a real person or a faked measured skill.
# HARD BOUNDARY: a COGNITIVE / KNOWLEDGE item (numerical/verbal/logical reasoning, data tables,
# true/false-cannot-say, a skills test) has objectively-correct answers — we NEVER auto-solve it;
# `_is_ability_item` detects it and `answer_scored` STOPS for a human. We also never look up how
# to "pass" the test. Pace is human-like — SHL flags rapid clicking ("you're moving quite quickly").

# SJT behavioural options — reward honest/helpful/professional conduct, punish dishonest/avoidant.
_SJT_GOOD_RE = re.compile(
    r"do not know.*(find|look|get back|check)|will find (it|out)|let (them|the customer) know|"
    r"be honest|apologi[sz]e|double.?check|\bcheck\b|look (it|this|the answer) up|research|"
    r"get back to (them|the customer)|find (out|the (correct|right)|the appropriate)|introduce them|"
    r"listen (to|carefully)|help (them|the customer)|explain (the|clearly|to)|make sure|"
    r"follow (the )?(process|procedure|policy|guidelines)|remain (calm|polite|professional)|"
    r"stay (calm|professional)|reassure|take (ownership|responsibility)|be (accurate|patient)|"
    r"escalate (the customer )?to (my )?(manager|supervisor)|give them the contact", re.I)
_SJT_BAD_RE = re.compile(
    r"make (something|it|an answer) up|something now and correct it later|\bguess\b|pretend|"
    r"redirect the customer|change the subject|\bignore\b|avoid (the|answering)|not my (job|problem)|"
    r"tell (them|the customer) (anything|something)( now)?|deflect|blame|argue|hang up|"
    r"without (checking|knowing|verifying)|do (what i can|things) .*not (allowed|permitted|supposed)|"
    r"break (the )?(policy|rules)|leave (it|them)|say whatever", re.I)

# Self-rating scale favourability (frequency / speed / quality vs "others").
_OPTOUT_RE = re.compile(r"first job|prefer not|do not (wish|want) to (answer|say)|no manager|not applicable|n/?a\b", re.I)
_NEG_TRAIT_RE = re.compile(
    r"inaccurate|incorrect|\berror|\bmistake|\bwrong\b|\blate\b|\bmiss(es|ed|ing)?\b|forget|forgot|"
    r"complain|complaint|upset|angr|frustrat|lose (my )?temper|\bstruggle|difficulty|\bfail|\brude\b|"
    r"\bargue|absent|careless|distract|give up|quit|leave early", re.I)

# Likert agreement scale — a positive work statement gets agreement, a negative one disagreement.
_LIKERT_RE = re.compile(r"strongly agree|strongly disagree|\bagree\b|\bdisagree\b", re.I)

# Ability / knowledge item — objectively-correct answers -> NEVER auto-solved.
_ABILITY_Q_RE = re.compile(
    r"which (number|figure|shape|word|comes next|is (larger|smaller|greater))|"
    r"complete the (series|sequence|pattern)|based on the (passage|information|text) (above|below)?|"
    r"\bcalculate\b|what is the (value|result|total|percentage|sum|difference|average|ratio)|"
    r"how (many|much) (is|would|does)|which of these (numbers|figures)|next in the (series|sequence)|"
    r"numerical reasoning|verbal reasoning|inductive reasoning|deductive", re.I)
_TF_RE = re.compile(r"^(true|false|cannot say|can'?t say|not enough information)$", re.I)

# Attention / instructed-response check ("to show you are paying attention, select X").
_ATTENTION_RE = re.compile(
    r"attention check|paying attention.*(select|choose|pick)|"
    r"(please|kindly) (select|choose|pick) (the )?(option|answer|response)?\s*['\"]?", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _option_fav(text: str):
    """Signed magnitude of a self-rating option on the favourable scale — covers both the
    'vs others' ladder (much less…much more) AND a plain FREQUENCY ladder (never…always). Higher
    = more favourable-when-the-trait-is-positive. None for an opt-out ('first job')."""
    t = text.lower()
    if _OPTOUT_RE.search(t):
        return None
    if re.search(r"\balways\b|all of the time|much (more|faster|better|higher)|far (more|faster|better)|"
                 r"significantly (more|faster|better)|\bmost\b|extremely", t):
        return 3
    if re.search(r"\bnever\b|not at all|much (less|slower|worse|lower)|far (less|slower|worse)|"
                 r"significantly (less|slower)|\bleast\b", t):
        return -3
    if re.search(r"very often|almost always|(more|faster|better|higher) than others|more often|"
                 r"\boften\b|frequently|usually|somewhat (more|faster|better|higher)", t):
        return 2
    if re.search(r"once in a while|hardly ever|\brarely\b|seldom|(less|slower|worse|fewer) than others|"
                 r"less often|somewhat (less|slower|worse|lower)", t):
        return -2
    if re.search(r"about as|as (often|fast|well|much|good)|average|same as others|as (accurate|quick)|"
                 r"\bsometimes\b|occasionally|moderate|neutral", t):
        return 0
    return 0


# A recognised self-rating / attitude SCALE in the OPTIONS (frequency or agreement ladder), even
# when the question is a plain statement ("People should reflect on performance" -> Once in a
# while … Always). Routed to the polarity-aware picker rather than halted as 'unknown'.
_SCALE_OPT_RE = re.compile(
    r"^(never|rarely|seldom|hardly ever|once in a while|occasionally|sometimes|often|frequently|"
    r"usually|very often|almost always|always|strongly disagree|disagree|slightly disagree|"
    r"neither agree nor disagree|neutral|slightly agree|agree|strongly agree)$"
    r"|than others$|^(much|somewhat|far|significantly) (more|less|faster|slower|better|worse)"
    r"|^about as ", re.I)


def _is_scale_options(options: list[str]) -> bool:
    opts = [o.strip() for o in (options or []) if o.strip()]
    if len(opts) < 3:
        return False
    hits = sum(1 for o in opts if _SCALE_OPT_RE.search(o))
    return hits >= max(3, len(opts) - 1)


def _is_negative_trait(question: str) -> bool:
    return bool(_NEG_TRAIT_RE.search(question or ""))


def _is_ability_item(question: str, options: list[str], has_table: bool = False) -> bool:
    """True for a cognitive/knowledge item with objectively-correct answers (never auto-solved)."""
    if has_table:
        return True
    opts = [o for o in (options or []) if o.strip()]
    if opts:
        numeric = sum(1 for o in opts if re.fullmatch(r"[\d.,%$£€+\-*/ ]{1,12}", o.strip()))
        short = sum(1 for o in opts if len(o.strip()) <= 6)
        if numeric >= max(2, len(opts) - 1):
            return True
        if any(_TF_RE.match(o.strip()) for o in opts) and len(opts) <= 4:
            return True
        if short >= len(opts) and len(opts) >= 3 and numeric >= 1:
            return True
    return bool(_ABILITY_Q_RE.search(question or ""))


def _attention_target(question: str, options: list[str]):
    """If the item instructs a specific option (attention check), return that option's index."""
    q = question or ""
    if not _ATTENTION_RE.search(q):
        return None
    m = re.search(r"(select|choose|pick|mark)\s+(the\s+)?(option\s+|answer\s+|response\s+)?['\"]?([A-Za-z][A-Za-z '\-]{2,30})['\"]?", q, re.I)
    if not m:
        return None
    target = _norm(m.group(4)).lower().strip(" .'\"’‘")
    for i, o in enumerate(options or []):
        if target and target in _norm(o).lower():
            return i
    return None


def _pick_sjt(question: str, options: list[str]) -> int:
    """Pick the most honest/helpful/professional behavioural option; avoid dishonest/avoidant."""
    best_i, best_s = 0, -10 ** 9
    for i, o in enumerate(options):
        s = (2 if _SJT_GOOD_RE.search(o) else 0) - (3 if _SJT_BAD_RE.search(o) else 0)
        # a mild nudge toward options that mention the customer positively
        if re.search(r"customer|client", o, re.I) and not _SJT_BAD_RE.search(o):
            s += 0.2
        if s > best_s:
            best_s, best_i = s, i
    return best_i


# Favourability of a personality STATEMENT for a customer-service role — used to rank OPQ
# forced-choice statements deterministically (they are balanced by design, so a consistent
# favourable-leaning choice is valid; no LLM needed, which keeps the long OPQ fast).
_STMT_POS_RE = re.compile(
    r"reliab|depend|\bcalm|patient|\bhelp|\bdetail|accurat|thorough|listen|organi[sz]|responsib|"
    r"conscien|follow through|friendly|polite|courteous|cooperat|\bteam|\blearn|improv|adapt|"
    r"resolv|solve|honest|respect|positive|careful|diligent|committed|hard.?work|punctual|"
    r"on time|consistent|calm under|stay calm|remain calm|support|understand", re.I)
_STMT_NEG_RE = re.compile(
    r"avoid conflict|prefer to work alone|without supervision|on my own|dislike|impatient|\bbored|"
    r"take risks|risk.?tak|\bargue|ignore|lose (my )?temper|frustrat|procrastinat|easily distract|"
    r"\bstruggle|give up|\bblame|impulsive|careless|reluctant|uncomfortable", re.I)
_STMT_CORE_RE = re.compile(  # the CS-core traits — a small tie-breaking weight
    r"\bcalm|patient|\bhelp|customer|client|listen|reliab|depend|resolv", re.I)


def _statement_fav(text: str) -> float:
    t = text or ""
    return (len(_STMT_POS_RE.findall(t)) + 0.5 * len(_STMT_CORE_RE.findall(t))
            - 1.5 * len(_STMT_NEG_RE.findall(t)))


def _pick_forced_choice(question: str, options: list[str]) -> int:
    """Pick the most customer-service-favourable statement from an OPQ forced-choice set."""
    best_i, best_s = 0, -10 ** 9
    for i, o in enumerate(options):
        s = _statement_fav(o)
        if s > best_s:
            best_s, best_i = s, i
    return best_i


def _pick_self_rating(question: str, options: list[str]) -> int:
    """Favourable self-presentation, polarity-aware: for a NEGATIVE trait pick the 'less/rarely'
    end, for a POSITIVE trait the 'more/faster/better' end. Never pick an opt-out ('first job')."""
    neg = _is_negative_trait(question)
    scored = [(i, _option_fav(o)) for i, o in enumerate(options)]
    scored = [(i, f) for i, f in scored if f is not None]
    if not scored:
        return 0
    if neg:
        return min(scored, key=lambda t: t[1])[0]
    return max(scored, key=lambda t: t[1])[0]


def _pick_likert(question: str, options: list[str]) -> int:
    """Agree with positive work statements, disagree with negative ones — pick the strong end."""
    neg = _is_negative_trait(question)
    want_agree = not neg
    # rank options by agreement strength
    def agree_score(o: str) -> float:
        t = o.lower()
        if "strongly agree" in t:
            return 2
        if re.search(r"\bagree\b", t):
            return 1
        if "strongly disagree" in t:
            return -2
        if re.search(r"\bdisagree\b", t):
            return -1
        if re.search(r"neither|neutral", t):
            return 0
        return 0
    scored = [(i, agree_score(o)) for i, o in enumerate(options)]
    if want_agree:
        return max(scored, key=lambda t: t[1])[0]
    return min(scored, key=lambda t: t[1])[0]


# End-of-test FEEDBACK about the assessment itself ("The instructions were: Very clear / …",
# "How would you rate the test?") — not a scored/persona item; answer positively/neutrally.
_FEEDBACK_Q_RE = re.compile(
    r"the (instructions|assessment|questions|statements|test).{0,40}(were|was|is|seemed|measured|are)\b|"
    r"this (assessment|company|organi[sz]|test|questionnaire)|impression of (this |the )?(company|organi[sz])|"
    r"how (clear|easy|difficult|fair) (were|was|did|is)|how did you (find|feel)|how would you rate|"
    r"overall (difficulty|experience|rating|impression)|rate (the|your|how|this)|"
    r"your (experience|opinion|impression|feedback) (of|with|was|about|on|is)|"
    r"found the (assessment|test|instructions)|feedback (about|on)|would (you )?recommend|"
    r"my (opinion|impression|view) (of|is|about)|how (satisfied|happy) (are|were) you|"
    r"after (completing|taking) this", re.I)

_FB_NEG_RE = re.compile(
    r"less favorable|less favourable|unfavorab|unfavourab|unclear|uneasy|\bnot\b|dislike|"
    r"\bpoor\b|very difficult|extremely difficult|disagree|negative", re.I)
_FB_POS1_RE = re.compile(
    r"considerably more|much more|very (clear|good|easy|satisfied|positive|helpful|favorab)|"
    r"excellent|strongly agree|highly (recommend|likely)|definitely", re.I)
_FB_POS2_RE = re.compile(
    r"somewhat more|more favorab|more favourab|fairly (clear|good|easy)|"
    r"\b(clear|good|easy|satisfied|helpful|agree|yes|favorable|favourable|recommend|positive)\b", re.I)
_FB_NEUTRAL_RE = re.compile(r"unchanged|neither|neutral|no change|about the same|fairly", re.I)


def _pick_feedback(question: str, options: list[str]) -> int:
    """Positive/neutral feedback about the assessment/company experience (never a persona claim).
    These end-of-test candidate-reaction items have no scored answer — we give a favourable-but-
    honest response, preferring the strong-positive end, else a mild positive, else neutral."""
    for i, o in enumerate(options):
        if _FB_POS1_RE.search(o) and not _FB_NEG_RE.search(o):
            return i
    for i, o in enumerate(options):
        if _FB_POS2_RE.search(o) and not _FB_NEG_RE.search(o):
            return i
    for i, o in enumerate(options):
        if _FB_NEUTRAL_RE.search(o) and not _FB_NEG_RE.search(o):
            return i
    for i, o in enumerate(options):
        if not _OPTOUT_RE.search(o):
            return i
    return 0


def _classify_item(question: str, options: list[str], has_table: bool = False) -> str:
    """Classify a scored item. 'ability'/'unknown' -> STOP (never auto-answered)."""
    if _is_ability_item(question, options, has_table):
        return "ability"
    if _attention_target(question, options) is not None:
        return "attention"
    q = (question or "")
    blob = q + " " + " ".join(options or [])
    # end-of-test feedback about the assessment itself (clarity/rating) — not scored.
    if _FEEDBACK_Q_RE.search(q):
        return "feedback"
    # OPQ forced-choice block (pick the statement that best describes you, from a balanced set) —
    # answered by the fast deterministic favourability ranker, not the LLM.
    if re.search(r"describes you (the )?best|which statement|of the remaining|remaining (two |statement)", q, re.I):
        return "forced_choice"
    # SJT behavioural SCENARIO ("what would you do") — the LLM ranks these.
    if re.search(r"most likely to do|least likely to do|what would you (be |most |do)|"
                 r"which (response|action)|best describes what you would do|i am most likely to", q, re.I):
        return "sjt"
    if re.search(r"how (often|quickly|well|much)|when we ask your.*(manager|supervisor)|"
                 r"than others|compared (to|with) others", blob, re.I):
        return "self_rating"
    if _LIKERT_RE.search(" ".join(options or [])):
        return "likert"
    # a plain attitude/behaviour STATEMENT answered on a frequency/agreement SCALE (e.g. "People
    # should reflect on their performance" -> Once in a while … Always): route by the scale.
    if _is_scale_options(options):
        if any(re.search(r"agree|disagree", o, re.I) for o in options):
            return "likert"
        return "self_rating"
    # options are full behavioural sentences -> treat as SJT (pick the professional one)
    opts = [o for o in (options or []) if o.strip()]
    if opts and (sum(len(o) for o in opts) / len(opts)) > 25:
        return "sjt"
    return "unknown"


_LLM_SYS = (
    "You are answering a pre-employment questionnaire as a reliable, honest, customer-focused "
    "professional applying for a remote customer-service role. Choose the ONE option a competent, "
    "dependable, ethical customer-service professional would pick — honest and helpful, never "
    "dishonest, evasive, or rule-breaking. Answer truthfully and consistently. Reply with ONLY the "
    "option number.")


async def _llm_pick(question: str, options: list[str]):
    """Ask the LOCAL model to choose the best professional option for a judgement/SJT item (which
    the keyword heuristic can't reliably rank). Returns a 0-based index, or None on any failure —
    the caller then falls back to the deterministic `_pick_sjt`. Uses only the local Sumrak
    endpoint (`settings.llm_url`), never an external service; this is the persona reasoning about
    its own answer, not looking up how to 'pass'."""
    if not options:
        return None
    try:
        from backend.config import settings
    except Exception:
        return None
    if not getattr(settings, "llm_url", None):
        return None
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    prompt = f"{_LLM_SYS}\n\nItem: {question or '(choose the most professional option)'}\n\nOptions:\n{numbered}\n\nBest option number:"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                f"{settings.llm_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_key}",
                         "Content-Type": "application/json"},
                json={"model": settings.llm_model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.1, "max_tokens": 8, "stream": False})
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\d+", txt or "")
        if not m:
            return None
        idx = int(m.group()) - 1
        return idx if 0 <= idx < len(options) else None
    except Exception:
        return None


def _pick_answer(question: str, options: list[str], has_table: bool = False):
    """Return (index, kind) for an answerable item, or (None, kind) when it must be left to a
    human ('ability'/'unknown'). Never returns an index for a cognitive/knowledge item."""
    kind = _classify_item(question, options, has_table)
    if kind in ("ability", "unknown"):
        return None, kind
    if kind == "attention":
        return _attention_target(question, options), kind
    if kind == "forced_choice":
        return _pick_forced_choice(question, options), kind
    if kind == "feedback":
        return _pick_feedback(question, options), kind
    if kind == "sjt":
        return _pick_sjt(question, options), kind
    if kind == "self_rating":
        return _pick_self_rating(question, options), kind
    if kind == "likert":
        return _pick_likert(question, options), kind
    return None, kind


# NB: options MUST use the SAME selector as `_click_option` (label.question-answer-label) so the
# picked index maps to the clickable element. The question is the longest VISIBLE innerText line
# that is neither an option nor chrome (nav/progress/branding) — robust to hidden popup templates.
_ITEM_JS = r"""() => {
  const t = el => (el.textContent||'').replace(/\s+/g,' ').trim();
  const labels = [...document.querySelectorAll('label.question-answer-label')]
    .filter(e => { const r=e.getBoundingClientRect(); return r.width>2 && r.height>2; })
    .map(t);
  const it = (document.body.innerText || '');
  const lines = it.split('\n').map(s => s.trim()).filter(Boolean);
  const labelSet = new Set(labels.map(x => x.trim()));
  const NAV = /^(skip to main content|exit|question|back|help|settings|next|continue|proceed|previous|select language|accessibility.*|sign out|tips|get started!?|©.*|\d+\s*%)$/i;
  const cand = lines.filter(l => !labelSet.has(l) && !NAV.test(l) && l.length > 12);
  cand.sort((a, b) => b.length - a.length);
  const q = cand[0] || '';
  const pm = it.match(/(\d+)\s*%/);
  return {question: q.slice(0, 400), options: labels,
          has_table: document.querySelectorAll('table').length > 0,
          progress: pm ? parseInt(pm[1], 10) : null, body: t(document.body).slice(0, 300)};
}"""

_COMPLETE_RE = re.compile(
    r"you have (completed|finished)|assessment (complete|finished|submitted)|thank you for (completing|taking)|"
    r"successfully (completed|submitted)|no (further|more) (questions|assessments)|"
    r"return to|you may now close|all done|congratulations", re.I)

# SHL sprinkles soft info/nudge modals through the OPQ ("We noticed you are taking your time!",
# "You're moving quite quickly", "Noticing some repetition? This is intentional", inactivity
# prompts, …). They are NOT questions and are keyed by a dismiss button a real item never has —
# so we detect them by the BUTTON, not by enumerating every wording. "Continue"/"Resume" are
# deliberately excluded (they're the interstitial's forward control, handled separately).
_DISMISS_NAMES = ("Close", "OK", "Okay", "Got it", "Got It", "Dismiss", "I understand", "Acknowledge")


async def _dismiss_modal(page) -> bool:
    """Click a modal's dismiss control (Close/OK/Got it/…) if one is present. Fires only on an
    info/nudge overlay — real OPQ items carry no such button — so it can be called unconditionally."""
    for name in _DISMISS_NAMES:
        el = await _actionable(page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I)))
        if el:
            try:
                await el.click(timeout=2500)
                await page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


async def _read_item(page) -> dict:
    try:
        return await page.evaluate(_ITEM_JS)
    except Exception:
        return {"question": "", "options": [], "has_table": False, "progress": None, "body": ""}


async def _await_item(page, *, tries: int = 18, delay: int = 500) -> dict:
    """Poll until the next scored item's option labels render, tolerating the transient blank/
    re-render frame between items (the DOM briefly shows only a hidden popup template). Clicks
    through a benign interstitial (Tips / section break / 'GET STARTED!') if one appears, and
    returns early on completion. Returns the item dict; options may still be empty if the flow
    genuinely has none (completion or a real stall) — the caller decides."""
    item = await _read_item(page)
    for _ in range(tries):
        if item.get("options"):
            return item
        body = item.get("body", "")
        if _COMPLETE_RE.search(body) or "/opq/" not in (page.url or ""):
            return item
        fwd = await _forward_control(page)
        if fwd:
            try:
                await fwd.click(timeout=2500)
                await page.wait_for_timeout(2500)
            except Exception:
                pass
        else:
            await page.wait_for_timeout(delay)
        item = await _read_item(page)
    return item


async def _click_option(page, index: int) -> bool:
    """Click the VISIBLE option label at `index`, re-querying + waiting for stability; retry a few
    times. Must match `_ITEM_JS`'s visible-only option list (a forced-choice block hides the
    already-picked statement, so a plain nth() over all labels would mis-map the index)."""
    for _ in range(4):
        labels = page.locator("label.question-answer-label:visible")
        try:
            cnt = await labels.count()
        except Exception:
            cnt = 0
        if index < cnt:
            el = labels.nth(index)
            try:
                await el.scroll_into_view_if_needed(timeout=2000)
                await el.click(timeout=3000)
                return True
            except Exception:
                await page.wait_for_timeout(600)
                continue
        await page.wait_for_timeout(600)
    return False


async def _click_forced_choice(page, options: list[str], prefer_text: str | None = None) -> int:
    """Click the most CS-favourable CLICKABLE statement and return its index (or -1). A banked
    `prefer_text` (a previously-chosen statement) is tried FIRST for exact replay; then favourability
    order. On a forced-choice screen C ("of the remaining two…") the already-picked statement stays
    visible but INERT (Playwright times out on it), so labels that won't click are skipped — leaving
    the best among the still-selectable statements."""
    order = sorted(range(len(options)), key=lambda i: -_statement_fav(options[i]))
    if prefer_text:
        pref = [i for i, o in enumerate(options) if _bank_norm(o) == prefer_text]
        order = pref + [i for i in order if i not in pref]
    for i in order:
        labels = page.locator("label.question-answer-label:visible")
        try:
            if i >= await labels.count():
                continue
            el = labels.nth(i)
            await el.scroll_into_view_if_needed(timeout=1500)
            await el.click(timeout=2500)
            return i
        except Exception:
            continue
    return -1


async def _pace(min_delay: float, max_delay: float) -> None:
    """Human-like pause; occasionally a longer 'reading' pause. SHL flags rapid clicking."""
    d = random.uniform(min_delay, max_delay)
    if random.random() < 0.12:
        d += random.uniform(2.0, 5.0)
    # asyncio sleep via the page clock is unnecessary; use a plain await
    import asyncio
    await asyncio.sleep(d)


# ---------------------------------------------------------------------------------------------
# Answer bank — the persona-AGNOSTIC store of "this exact question (+ its option set) -> the
# option we chose". The OPQ draws items from a large pool and shuffles them per session, so the
# same items recur; on a recurrence we replay the stored answer INSTANTLY (no LLM), which also
# makes responding perfectly CONSISTENT (a real person answers a repeated statement the same way)
# and lets us measure how many DISTINCT items the pool holds. Keyed on the exact
# question + sorted option-set; the answer is stored as the option TEXT so it still matches when
# SHL reorders the options. Persisted to a gitignored JSON so it grows across every assessment.
import json as _json
import os as _os

_BANK_PATH = _os.path.join(_os.path.dirname(__file__), "..", "data", "shl_answer_bank.json")
_BANK: dict | None = None


def _bank_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _bank_key(question: str, options: list[str]) -> str:
    opts = "|".join(sorted(_bank_norm(o) for o in (options or []) if o and o.strip()))
    return _bank_norm(question) + "||" + opts


def _bank_load() -> dict:
    global _BANK
    if _BANK is None:
        try:
            with open(_BANK_PATH, encoding="utf-8") as f:
                _BANK = _json.load(f)
        except Exception:
            _BANK = {}
    return _BANK


def _bank_save() -> None:
    bank = _bank_load()
    try:
        tmp = f"{_BANK_PATH}.{_os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(bank, f, ensure_ascii=False)
        _os.replace(tmp, _BANK_PATH)
    except Exception:
        pass


def _bank_lookup(question: str, options: list[str]):
    """Return the index of the previously-chosen option for this exact item, or None."""
    entry = _bank_load().get(_bank_key(question, options))
    if not entry:
        return None
    ans = entry.get("answer", "")
    for i, o in enumerate(options):
        if _bank_norm(o) == ans:
            entry["n"] = entry.get("n", 1) + 1
            return i
    return None


def _bank_record(question: str, options: list[str], idx: int, kind: str) -> None:
    if not (0 <= idx < len(options)):
        return
    bank = _bank_load()
    key = _bank_key(question, options)
    prev = bank.get(key, {})
    bank[key] = {"q": (question or "")[:220], "options": [str(o)[:200] for o in options],
                 "answer": _bank_norm(options[idx]), "kind": kind, "n": prev.get("n", 0) + 1}
    _bank_save()


def bank_size() -> int:
    return len(_bank_load())


async def answer_scored(page, persona: dict | None = None, *, max_items: int = 260,
                        min_delay: float = 3.0, max_delay: float = 7.0) -> dict:
    """Complete the SHL OPQ scored section as an ETALON for a SYNTHETIC persona: answer each
    Situational-Judgement / self-rating / Likert item with the designed customer-service profile
    (honest, helpful, reliable), pacing like a human. STOPS and returns needs_human the moment a
    cognitive/knowledge (ability) or unrecognised item appears — those are never auto-solved.
    Assumes the page is at the scored entry (OPQ tips / first item); walks in via the forward
    control, then loops. Returns {status, items_answered, last_progress, note} where status ∈
    {completed, needs_human, stuck, error}."""
    persona = persona or {}
    res = {"status": "error", "items_answered": 0, "last_progress": None, "note": ""}
    answered = 0
    try:
        # walk from the tips/intro into the first real item (no answer-widgets on the way).
        for _ in range(4):
            if await page.locator("label.question-answer-label").count():
                break
            fwd = await _forward_control(page)
            if not fwd:
                break
            try:
                await fwd.click(timeout=3000)
            except Exception:
                break
            await page.wait_for_timeout(3500)

        for _ in range(max_items):
            item = await _await_item(page)
            res["last_progress"] = item.get("progress")
            qb = (item.get("question", "") + " " + item.get("body", "")).lower()
            # an info/nudge modal overlays the item (has a Close/OK/Got it button a real item never
            # has) -> dismiss it and re-read. Detected by the button, so it catches any wording.
            if await _dismiss_modal(page):
                continue
            # OPQ forced-choice interstitial ("Now you'll see the two remaining statements. Pick
            # which one describes you best.") — the remaining statements are pre-rendered but inert
            # until Continue is pressed, so click Continue rather than a label.
            if "remaining statement" in qb or "see the two remaining" in qb:
                fwd = await _forward_control(page)
                if fwd:
                    await fwd.click(timeout=3000)
                    await page.wait_for_timeout(2500)
                    continue
            opts = item.get("options") or []
            if not opts:
                body = item.get("body", "")
                if _COMPLETE_RE.search(body):
                    res["status"] = "completed"
                    res["note"] = f"assessment completed ({answered} items answered)"
                    res["items_answered"] = answered
                    return res
                # No options AND no explicit completion text. Leaving the /opq/ URL or progress>=99 is
                # NOT proof of a real submission (owner-verified false-completion: a run that never even
                # reached /opq/ — 0%, stuck on consent/overview/error — was marked done because the URL
                # lacked "/opq/"). Treat it as a stall so run_one retries / leaves it incomplete; the
                # runner's strict _already_done (overview "0 assessments left") is the reliable done sig.
                res["status"] = "stuck"
                res["note"] = (f"no options + no completion text (progress={item.get('progress')}, "
                               f"off_opq={'/opq/' not in (page.url or '')}, body={body[:60]})")
                res["items_answered"] = answered
                return res

            q_txt = item.get("question", "")
            idx, kind = _pick_answer(q_txt, opts, item.get("has_table", False))
            if idx is None:
                # ability / unrecognised -> HARD STOP, leave for a human (never auto-solve).
                res["status"] = "needs_human"
                res["note"] = (f"{kind} item reached — left for a human (q: {q_txt[:80]})")
                res["items_answered"] = answered
                return res

            # answer-bank: a previously-seen item replays its stored choice INSTANTLY (no LLM); a
            # NEW judgement item (SJT scenario or forced-choice statement) that is not in the bank is
            # handed to the local model (which sees the options) then recorded, so future sessions
            # answer it from the bank. Scale items (self-rating/Likert/feedback) keep the fast,
            # provably-favourable polarity rule. The deterministic pick is the fallback if the model
            # is unavailable/unparseable.
            used_llm = from_bank = False
            banked = _bank_lookup(q_txt, opts)
            if banked is not None:
                idx, from_bank = banked, True
            elif kind in ("sjt", "forced_choice"):
                llm_idx = await _llm_pick(q_txt, opts)
                if llm_idx is not None:
                    idx, used_llm = llm_idx, True
            logger.info("etalon item #%d kind=%s%s pick=%d/%d progress=%s%% q=%r",
                        answered + 1, kind, "+bank" if from_bank else ("+llm" if used_llm else ""),
                        idx, len(opts), item.get("progress"), q_txt[:60])
            # The LLM call (~20s) already served as a human-like reading pause, so only add a short
            # settle then; otherwise pace normally. SHL flags rapid clicking, not slow answering.
            await _pace(0.6, 1.6) if used_llm else await _pace(min_delay, max_delay)
            prev, prev_q, prev_prog = opts, item.get("question", ""), item.get("progress")
            # forced-choice screen C keeps the already-picked statement visible-but-inert, so click
            # by favourability with skip-on-inert (a banked pick is preferred for exact replay);
            # every other kind clicks its specific option. Record what we ACTUALLY clicked.
            if kind == "forced_choice":
                # click the DECIDED statement (bank or model or ranker) first; fall back to
                # favourability order + inert-skip only if that exact one won't click.
                clicked_idx = await _click_forced_choice(page, opts, prefer_text=_bank_norm(opts[idx]))
            else:
                clicked_idx = idx if await _click_option(page, idx) else -1
            if clicked_idx < 0:
                res["status"] = "stuck"
                res["note"] = f"could not click option {idx}/{len(opts)} ({kind})"
                res["items_answered"] = answered
                return res
            answered += 1
            # record what we ACTUALLY clicked (a fresh item only) so a recurrence replays it.
            if not from_bank:
                _bank_record(q_txt, opts, clicked_idx, kind)
            # Wait for advance. NB many self-rating items share the SAME scale options, so an
            # options-only check would false-negative on two consecutive same-scale items — treat
            # a changed QUESTION or an increased progress % as advance too. Also click a Next if one
            # appears (some sections are Next-driven rather than auto-advance).
            advanced = False
            for _ in range(12):
                await page.wait_for_timeout(500)
                nxt = await _read_item(page)
                nq, npg = nxt.get("question", ""), nxt.get("progress")
                if ((nxt.get("options") or []) != prev
                        or (nq and nq != prev_q)
                        or (npg is not None and prev_prog is not None and npg > prev_prog)):
                    advanced = True
                    break
                fwd = await _forward_control(page)
                if fwd:
                    try:
                        await fwd.click(timeout=2000)
                        await page.wait_for_timeout(1500)
                        advanced = True
                        break
                    except Exception:
                        pass
            if not advanced:
                # maybe finished on the last click — but ONLY an explicit completion confirmation
                # counts (NOT merely leaving /opq/, which fires on any error/redirect/stall).
                body = (await _read_item(page)).get("body", "")
                if _COMPLETE_RE.search(body):
                    res["status"] = "completed"
                    res["note"] = f"assessment completed ({answered} items answered)"
                    res["items_answered"] = answered
                    return res
                res["status"] = "stuck"
                res["note"] = f"item did not advance, no completion text ({answered} done)"
                res["items_answered"] = answered
                return res

        res["status"] = "stuck"
        res["note"] = f"max_items reached ({answered} answered)"
        res["items_answered"] = answered
        return res
    except Exception as exc:
        res["note"] = f"{type(exc).__name__}: {exc}"[:200]
        res["items_answered"] = answered
        return res


def _env_complete_scored() -> bool:
    return str(os.environ.get("SHL_COMPLETE_SCORED", "")).strip().lower() in ("1", "true", "yes", "on")


async def run_intro(link: str, persona: dict | None = None, *, page=None,
                    max_steps: int = 16, complete_scored: bool | None = None) -> dict:
    """Drive a HEADFUL browser through the SHL intro (cookies -> consent gates -> overview/
    instructions landings -> optional background page) and STOP the instant a real question
    appears. Pass an existing headful `page` (e.g. the co-pilot's, on Xvfb) so a human can
    continue in noVNC; caller owns the browser. Pass the ORIGINAL email link
    (`integration-talentcentral.us.shl.com/Integration/ce/...`) — it follows the redirect to the
    per-session player host itself; do NOT pass a cached redirect host (they are ephemeral and
    go NXDOMAIN once the session ends). Returns {status, steps, note} where status ∈
    {reached_scored_test, stuck, error} — plus {completed, needs_human} when `complete_scored`
    is on. By DEFAULT (complete_scored None -> env `SHL_COMPLETE_SCORED`, off) it STOPS at the
    scored test and never answers it. With `complete_scored=True` (the ETALON / autonomous mode,
    SYNTHETIC personas only) it hands off to `answer_scored`, which completes the OPQ judgement/
    self-rating items but STILL hard-stops on any cognitive/knowledge (ability) item.

    The safety model is provable, not keyword-luck: a page carrying ZERO visible answer-widgets
    (`_ANSWER_WIDGETS_JS`) is a pure landing/instructions page, so clicking forward on it answers
    nothing. A page that HAS answer-widgets is filled ONLY when it matches the background
    whitelist (`_BG_RE`, demographics we decline / country / education); ANY other page with
    answer-widgets — i.e. an actual question — halts immediately. So even if `_SCORED_RE` misses
    a wording, a scored item is never auto-answered: it has answer-widgets and isn't background."""
    persona = persona or {}
    if complete_scored is None:
        complete_scored = _env_complete_scored()
    result = {"status": "error", "steps": 0, "note": ""}
    if page is None:
        result["note"] = "run_intro needs a headful Playwright page (SHL rejects headless)"
        return result

    async def _reached_scored(note: str) -> dict:
        # Default: STOP at the scored test for a human. When complete_scored is enabled (the
        # ETALON / autonomous mode, synthetic personas only), answer the OPQ scored section.
        if not complete_scored:
            result["status"] = "reached_scored_test"
            result["note"] = note
            return result
        sc = await answer_scored(page, persona)
        sc["steps"] = result["steps"]
        return sc

    try:
        await page.goto(link, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        await _dismiss_cookies(page)
        for step in range(1, max_steps + 1):
            result["steps"] = step
            try:
                text = await page.inner_text("body", timeout=4000)
            except Exception:
                text = ""
            # Already inside the OPQ item player? A RESUMED session lands directly on an item (its
            # option labels are `label.question-answer-label`, not counted answer-widgets, and its
            # text needn't match `_SCORED_RE`), so hand off on the labels themselves.
            try:
                on_item = await page.locator("label.question-answer-label").count()
            except Exception:
                on_item = 0
            if on_item:
                return await _reached_scored("OPQ item player reached")
            # HARD STOP (default): a scored-item signal -> hand to a human, unless completing.
            if _SCORED_RE.search(text):
                return await _reached_scored("scored assessment reached — left for a human (noVNC)")
            # consent gate (mandatory toggle + Next)?
            if await page.get_by_role("checkbox").count() and await _next_button(page):
                if await _toggle_consent_and_next(page):
                    await page.wait_for_timeout(4000)
                    continue
                result["status"] = "stuck"; result["note"] = "consent toggle not accepted"
                return result
            # how many real answer-widgets are on this page (excl. language selector / consent)?
            try:
                n_inputs = await page.evaluate(_ANSWER_WIDGETS_JS)
            except Exception:
                n_inputs = 0
            if n_inputs > 0:
                # a page with fields to answer: fill it ONLY if it's a recognized background
                # page; otherwise it's a real question -> STOP (never auto-answer a scored item).
                if _BG_RE.search(text):
                    await _fill_background_page(page, persona)
                    fwd = await _forward_control(page)
                    if fwd:
                        await fwd.click(timeout=3000)
                        await page.wait_for_timeout(5000)
                        continue
                    result["status"] = "stuck"
                    result["note"] = "background page had no forward control"
                    return result
                return await _reached_scored(
                    "reached a question page (has answer fields, not background) — left for a human")
            # no answer-widgets -> a pure landing / instructions / overview page. Advancing it
            # answers nothing, so walk the human right up to the first real question.
            fwd = await _forward_control(page)
            if fwd:
                await fwd.click(timeout=3000)
                await page.wait_for_timeout(5000)
                continue
            result["status"] = "stuck"
            result["note"] = "landing page had no forward control"
            return result
        result["status"] = "stuck"; result["note"] = "max_steps reached"
        return result
    except Exception as exc:
        result["note"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
