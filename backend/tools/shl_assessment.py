"""SHL / TalentCentral assessment INTRO auto-fill — the ordinary, non-scored pages only.

Maximus (and other mass-hiring ATSes) gate hire behind an SHL assessment. The link arrives
in the persona's mailbox (surfaced in the CRM as an «Assessment Link» button, classified
`action_needed`). This module drives a REAL (headful, Xvfb) browser through the parts that
are just FORM-FILLING — the cookie banner, the consent gates, and the "About You" background
questionnaire (gender/age/race/country/education/job-context) — and then STOPS the moment the
actual SCORED assessment begins (cognitive-ability or personality/behavioural items), leaving
that for a human (watch/continue in noVNC).

WHY it stops there — this is a hard line, not a toggle:
  The candidate attests on the "Your Responsibilities" gate: "I will take this assessment
  honestly and without any assistance from others." Auto-answering the SCORED items is exactly
  that assistance — assessment fraud against the employer — and SHL is built to detect it. So
  this engine only removes the rote intro typing; the graded test stays a human task by design.

Headful is mandatory: SHL's item player rejects headless browsers ("unsupported browser"),
so run this on the co-pilot's Xvfb display (DISPLAY=:98) where a human can take over in noVNC.

Conservative by construction: a page is filled ONLY when it matches the BACKGROUND whitelist
(_BG_RE). Anything else — including any scored-item signal (_SCORED_RE) — halts immediately.
"""
from __future__ import annotations

import logging
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


async def run_intro(link: str, persona: dict | None = None, *, page=None,
                    max_steps: int = 16) -> dict:
    """Drive a HEADFUL browser through the SHL intro (cookies -> consent gates -> overview/
    instructions landings -> optional background page) and STOP the instant a real question
    appears. Pass an existing headful `page` (e.g. the co-pilot's, on Xvfb) so a human can
    continue in noVNC; caller owns the browser. Pass the ORIGINAL email link
    (`integration-talentcentral.us.shl.com/Integration/ce/...`) — it follows the redirect to the
    per-session player host itself; do NOT pass a cached redirect host (they are ephemeral and
    go NXDOMAIN once the session ends). Returns {status, steps, note} where status ∈
    {reached_scored_test, stuck, error}. NEVER answers a scored item.

    The safety model is provable, not keyword-luck: a page carrying ZERO visible answer-widgets
    (`_ANSWER_WIDGETS_JS`) is a pure landing/instructions page, so clicking forward on it answers
    nothing. A page that HAS answer-widgets is filled ONLY when it matches the background
    whitelist (`_BG_RE`, demographics we decline / country / education); ANY other page with
    answer-widgets — i.e. an actual question — halts immediately. So even if `_SCORED_RE` misses
    a wording, a scored item is never auto-answered: it has answer-widgets and isn't background."""
    persona = persona or {}
    result = {"status": "error", "steps": 0, "note": ""}
    if page is None:
        result["note"] = "run_intro needs a headful Playwright page (SHL rejects headless)"
        return result
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
            # HARD STOP: any scored-item signal -> leave it for the human.
            if _SCORED_RE.search(text):
                result["status"] = "reached_scored_test"
                result["note"] = "scored assessment reached — left for a human (noVNC)"
                return result
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
                result["status"] = "reached_scored_test"
                result["note"] = "reached a question page (has answer fields, not background) — left for a human"
                return result
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
