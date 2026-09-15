"""SHL/Sutherland -> AMCAT harvester GATE handlers (adapters/shl.py).

Two intro steps used to STALL the Sutherland lane before the AMPI personality battery:
  1. the AMCAT "Identity Verification" page (a Take -> Submit camera capture) — mis-treated as a
     1-option question and never advanced (~41 min no progress);
  2. the "Instructions" page whose only control is a "Start Assessment" button.

These tests load STATIC DOM fixtures via Playwright set_content (NO network, like test_dom_fixtures.py)
and assert the new ShlAdapter handlers recognise each page BEFORE the generic random-pick and drive it
forward — while leaving a real AMPI item untouched (tight page-signature guards).

Run: PYTHONPATH=. python3 -m pytest backend/tests/test_harvest_shl_gates.py -q
"""
import asyncio

import pytest

pytest.importorskip("playwright")

from playwright.async_api import async_playwright  # noqa: E402

from backend.tools.assessment_harvester.adapters.shl import ShlAdapter  # noqa: E402


async def _with_inline_page(html: str, coro_factory):
    """Load inline HTML via set_content (no network) and run coro_factory(page)."""
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            return await coro_factory(page)
        finally:
            await browser.close()
    finally:
        await pw.stop()


# ---------------------------------------------------------------------------
# Fixtures — real live-page signatures (2026-09-15 observations)
# ---------------------------------------------------------------------------

# IDV page: heading + "verify your identity" body + camera preview + Take/Submit buttons. Clicking Take
# enables Submit and turns itself into "Retake" (the real capture-then-review flow); Submit navigates on
# to the Instructions page.
_IDV_BUTTONS = """
<h1>Identity Verification</h1>
<p>Before you begin the test, please verify your identity by following the directions below.
   Photographs will only be used to verify your identity.</p>
<ul>
  <li>Face the camera and hold your ID card below your face</li>
  <li>When you are ready, select Take</li>
  <li>If your face or ID is obscured, select Retake</li>
  <li>When you are satisfied, select Submit</li>
</ul>
<video autoplay muted playsinline width="320" height="240"></video>
<button id="take" onclick="window.__took=(window.__took||0)+1;
    document.getElementById('sub').disabled=false; this.textContent='Retake';">Take</button>
<button id="sub" disabled onclick="window.__submitted=(window.__submitted||0)+1;
    document.body.innerHTML='<h1>Instructions</h1>'
      +'<p>Select the language for this assessment: English (US)</p>'
      +'<button>Start Assessment</button>';">Submit</button>
"""

# Same IDV flow but the controls are styled DIVs with role=button (robustness: not real <button>s).
_IDV_DIVS = """
<h2>Identity Verification</h2>
<p>Please verify your identity. Photographs will only be used to verify your identity.</p>
<video autoplay muted></video>
<div id="take" role="button" tabindex="0" onclick="window.__took=1;
    var s=document.getElementById('sub'); s.setAttribute('aria-disabled','false');
    this.textContent='Retake';">Take</div>
<div id="sub" role="button" aria-disabled="true" onclick="if(this.getAttribute('aria-disabled')==='true')return;
    window.__submitted=1; document.body.innerHTML='<h1>Instructions</h1><button>Start Assessment</button>';">Submit</div>
"""

# IDV signature but NO Take/Submit control at all (a malformed/altered page) — the handler must give up,
# not hang.
_IDV_NO_CONTROLS = """
<h1>Identity Verification</h1>
<p>Before you begin the test, please verify your identity.</p>
<video autoplay muted></video>
"""

# Instructions page: heading + language line + Tips + a single "Start Assessment" button.
_INSTRUCTIONS = """
<h1>Instructions</h1>
<p>Select the language for this assessment: English (US)</p>
<div class="tips">Tips: Don't overthink. Try to take the assessment in one sitting.
  You can go back and change your last response. Think about yourself at work.</div>
<button onclick="window.__started=1;
    document.body.innerHTML='<p class=question-answer-label>I enjoy helping customers.</p>'
      +'<label class=question-answer-label><input type=radio name=q> Agree</label>';">Start Assessment</button>
"""

# A genuine AMPI personality item — the IDV / Start-Assessment guards must NOT fire here.
_AMPI_ITEM = """
<div class="item">
  <p class="question-answer-label">I remain calm when a customer is upset.</p>
  <label class="question-answer-label"><input type="radio" name="q" value="1"> Strongly agree</label>
  <label class="question-answer-label"><input type="radio" name="q" value="2"> Agree</label>
  <label class="question-answer-label"><input type="radio" name="q" value="3"> Disagree</label>
  <label class="question-answer-label"><input type="radio" name="q" value="4"> Strongly disagree</label>
  <button>Next</button>
</div>
"""


# ---------------------------------------------------------------------------
# Identity Verification
# ---------------------------------------------------------------------------

def test_idv_page_detected():
    """The IDV signature is recognised (idv=True) with Take present and Submit initially disabled."""
    async def coro(page):
        return await ShlAdapter()._idv_state(page)

    st = asyncio.run(_with_inline_page(_IDV_BUTTONS, coro))
    assert st["idv"] is True, f"IDV page not detected: {st}"
    assert st["has_take"] is True, "the 'Take' control must be detected"
    assert st["has_submit"] is True, "the 'Submit' control must be detected"
    assert st["submit_enabled"] is False, "Submit must read as disabled before a capture is taken"


def test_idv_take_then_submit_sequence():
    """_handle_idv clicks Take -> waits for the capture to settle -> clicks Submit -> lands off IDV."""
    async def coro(page):
        ok = await ShlAdapter()._handle_idv(page)
        took = await page.evaluate("() => window.__took || 0")
        submitted = await page.evaluate("() => window.__submitted || 0")
        idv_after = (await ShlAdapter()._idv_state(page))["idv"]
        return ok, took, submitted, idv_after

    ok, took, submitted, idv_after = asyncio.run(_with_inline_page(_IDV_BUTTONS, coro))
    assert ok is True, "_handle_idv should report it acted"
    assert took >= 1, "Take was never clicked"
    assert submitted >= 1, "Submit was never clicked after the capture"
    assert idv_after is False, "the flow must have navigated off the IDV page after Submit"


def test_idv_handled_with_role_button_divs():
    """Robustness: Take/Submit as styled <div role=button> (not real <button>) are still driven."""
    async def coro(page):
        ok = await ShlAdapter()._handle_idv(page)
        took = await page.evaluate("() => window.__took || 0")
        submitted = await page.evaluate("() => window.__submitted || 0")
        idv_after = (await ShlAdapter()._idv_state(page))["idv"]
        return ok, took, submitted, idv_after

    ok, took, submitted, idv_after = asyncio.run(_with_inline_page(_IDV_DIVS, coro))
    assert ok is True and took >= 1 and submitted >= 1
    assert idv_after is False, "div[role=button] IDV controls must advance off the page too"


def test_idv_via_dismiss_noise():
    """End-to-end through the per-step hook: dismiss_noise recognises the IDV page (before the generic
    random-pick), drives Take->Submit, returns True, and the page has advanced off IDV."""
    async def coro(page):
        acted = await ShlAdapter().dismiss_noise(page)
        idv_after = (await ShlAdapter()._idv_state(page))["idv"]
        return acted, idv_after

    acted, idv_after = asyncio.run(_with_inline_page(_IDV_BUTTONS, coro))
    assert acted is True, "dismiss_noise must handle the IDV page (return True to re-loop the walk)"
    assert idv_after is False, "dismiss_noise must have advanced past the IDV gate"


def test_idv_missing_controls_gives_up_without_hanging():
    """An IDV-signature page with no Take/Submit control must give up (return False) — not hang."""
    async def coro(page):
        a = ShlAdapter()
        # bounded: even repeated calls terminate quickly (no control ever appears)
        return await asyncio.wait_for(a._handle_idv(page), timeout=20)

    ok = asyncio.run(_with_inline_page(_IDV_NO_CONTROLS, coro))
    assert ok is False, "no Take control -> _handle_idv must return False, not hang"


def test_idv_attempts_are_bounded():
    """After several fruitless attempts the handler stops retrying (bounded, so the walk can report)."""
    async def coro(page):
        a = ShlAdapter()
        a._idv_attempts = 4          # already at the cap
        return await a._handle_idv(page)

    ok = asyncio.run(_with_inline_page(_IDV_BUTTONS, coro))
    assert ok is False, "past the attempt cap the handler must bail (False), not keep clicking"


# ---------------------------------------------------------------------------
# Instructions / Start Assessment
# ---------------------------------------------------------------------------

def test_start_assessment_detected_and_clicked():
    """The Instructions page's 'Start Assessment' button is clicked, entering the battery."""
    async def coro(page):
        acted = await ShlAdapter().dismiss_noise(page)
        started = await page.evaluate("() => window.__started || 0")
        return acted, started

    acted, started = asyncio.run(_with_inline_page(_INSTRUCTIONS, coro))
    assert acted is True, "dismiss_noise must click 'Start Assessment'"
    assert started == 1, "the Start Assessment button was not actually clicked"


def test_start_assessment_helper_returns_false_when_absent():
    """_start_assessment is a no-op (False) on a page without a Start-Assessment button."""
    async def coro(page):
        return await ShlAdapter()._start_assessment(page)

    res = asyncio.run(_with_inline_page(_AMPI_ITEM, coro))
    assert res is False, "no 'Start Assessment' control -> must not click anything"


# ---------------------------------------------------------------------------
# Guards — a real AMPI item must be left untouched
# ---------------------------------------------------------------------------

def test_gates_do_not_fire_on_ampi_item():
    """On a genuine AMPI personality item the IDV + Start-Assessment guards stay silent and no answer
    option is clicked — the walk falls through to the normal read/answer path."""
    async def coro(page):
        a = ShlAdapter()
        idv = (await a._idv_state(page))["idv"]
        start = await a._start_assessment(page)
        acted = await a.dismiss_noise(page)
        any_checked = await page.evaluate(
            "() => [...document.querySelectorAll('input[type=radio]')].some(r => r.checked)")
        return idv, start, acted, any_checked

    idv, start, acted, any_checked = asyncio.run(_with_inline_page(_AMPI_ITEM, coro))
    assert idv is False, "AMPI item must NOT match the IDV signature"
    assert start is False, "AMPI item must NOT match Start Assessment"
    assert acted is False, "dismiss_noise must not claim to have handled an AMPI item"
    assert any_checked is False, "dismiss_noise must not select any answer option on an AMPI item"
