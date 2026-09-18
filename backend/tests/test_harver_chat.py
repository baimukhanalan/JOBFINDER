"""Offline tests for the Harver Live-Chat Support Simulation driver (no network, no DB, no browser).

The chat sim is a real-time, timer-bounded, multi-customer roleplay. These tests cover the driver's
own logic without a browser: the live/ended predicate (`_chat_is_live`), the CS reply heuristic
(`_heuristic_chat_pick`), the bank-replay pick (`_pick_chat_response`), and — via a scripted fake page
— that `_drive_chat` answers each Response turn and EXITS once the chat DOM is gone (the module
transitioned), returning True so core advances. `is_done` stays strict (unaffected here).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester.adapters.harver import HarverAdapter  # noqa: E402


# ---- _chat_is_live: distinguishes "chat still up" from "module ended" -----------------------------

def test_chat_is_live_with_responses():
    assert HarverAdapter._chat_is_live({"n_responses": 3}) is True


def test_chat_is_live_with_chat_ui_between_turns():
    # between turns (customer typing) there are no Response options but the persistent chat UI is up.
    assert HarverAdapter._chat_is_live(
        {"n_responses": 0, "has_chat_ui": True, "has_help_another": False}) is True


def test_chat_is_live_with_switch_only():
    assert HarverAdapter._chat_is_live(
        {"n_responses": 0, "has_chat_ui": False, "has_help_another": True}) is True


def test_bare_timer_is_not_chat_live():
    # a bare countdown (a cognitive/typing/speed-test module is also timed) must NOT read as the chat —
    # else the driver would be trapped past the sim's end.
    assert HarverAdapter._chat_is_live(
        {"n_responses": 0, "has_time_remaining": True, "has_chat_ui": False, "has_help_another": False}) is False


def test_chat_is_dead_when_all_gone():
    # the next module (Internet Speed Test) has none of the chat DOM -> the driver must exit.
    assert HarverAdapter._chat_is_live(
        {"n_responses": 0, "has_chat_ui": False, "has_help_another": False}) is False
    assert HarverAdapter._chat_is_live({}) is False


# ---- _heuristic_chat_pick: prefers the helpful / professional reply -------------------------------

def test_heuristic_prefers_helpful_reply():
    a = HarverAdapter(mailbox="x@takhet.com")
    responses = [
        "Not sure, but if you type 'dinnerware set' in a web search you'll see other resellers!",
        "I'm sorry to hear that! I will assist you right away and process your refund.",
        "Too bad, nothing I can do about that.",
    ]
    assert a._heuristic_chat_pick(responses) == 1


def test_heuristic_empty_is_none():
    a = HarverAdapter(mailbox="x@takhet.com")
    assert a._heuristic_chat_pick([]) is None


# ---- _pick_chat_response: REPLAY a pre-solved bank answer key -------------------------------------

def test_pick_chat_response_replays_bank_key(monkeypatch):
    a = HarverAdapter(mailbox="x@takhet.com")
    import backend.tools.assessment_harvester.bank as bank
    # a banked chat item pre-solved to "Response 2" (as the 14 live ones are).
    monkeypatch.setattr(bank, "answer_for", lambda *args, **kw: {"text": "Response 2", "index": 1})
    idx, src = asyncio.run(a._pick_chat_response(
        _StubPage(), "cust q", ["reply a", "reply b", "reply c"], 3))
    assert idx == 1 and src == "answer_key"


def test_pick_chat_response_falls_to_heuristic(monkeypatch):
    a = HarverAdapter(mailbox="x@takhet.com")
    import backend.tools.assessment_harvester.bank as bank
    monkeypatch.setattr(bank, "answer_for", lambda *args, **kw: None)

    async def _no_vision(*args, **kw):
        return None
    monkeypatch.setattr(a, "_vision_pick", _no_vision)
    idx, src = asyncio.run(a._pick_chat_response(
        _StubPage(), "cust q",
        ["I can't help with that", "Of course, I'll help you resolve this and issue a refund", "no idea"], 3))
    assert idx == 1 and src == "heuristic"


# ---- _drive_chat: answers turns, then EXITS when the chat DOM is gone -----------------------------

class _StubLocator:
    async def count(self):
        return 0

    @property
    def first(self):
        return self

    async def click(self, *a, **k):
        return None

    async def scroll_into_view_if_needed(self, *a, **k):
        return None

    def filter(self, *a, **k):
        return self


class _StubPage:
    """Minimal page stand-in: no clickable elements (count 0), no-op waits/screenshots."""
    url = "https://journey.harver.com/vacancy/x"

    def __init__(self, states=None):
        self._states = list(states or [])
        self.state_calls = 0

    async def evaluate(self, js, *args):
        if "n_responses" in js:                       # _CHAT_STATE_JS
            self.state_calls += 1
            if self._states:
                return self._states.pop(0)
            return {"n_responses": 0, "has_time_remaining": False, "has_help_another": False}
        if "is_chat" in js:                            # _READ_CHAT_JS
            return {"is_chat": True, "n": 3, "q": "customer question"}
        if "playbackRate" in js:                       # _play_gating_video
            return {"has": False}
        if "begin assessment" in js.lower():           # _dismiss_chat_gate JS fallback
            return True
        if "response" in js.lower():                   # _click_chat_response JS fallback
            return True
        if "iframes" in js or "location.href" in js:   # _dump_controls
            return {"btn": [], "inp": [], "fr": [], "media": {}, "url": "x", "title": "t"}
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def screenshot(self, *a, **k):
        return None

    def get_by_role(self, *a, **k):
        return _StubLocator()

    def get_by_text(self, *a, **k):
        return _StubLocator()

    def locator(self, *a, **k):
        return _StubLocator()


def test_drive_chat_answers_then_exits_on_transition(monkeypatch):
    a = HarverAdapter(mailbox="x@takhet.com")

    async def _no_vision(*args, **kw):
        return None
    monkeypatch.setattr(a, "_vision_pick", _no_vision)

    banked = []

    async def _fake_bank(page, q, responses, idx):
        banked.append((q, idx))
    monkeypatch.setattr(a, "_bank_chat_turn", _fake_bank)

    async def _no_dump(*a, **k):
        return None
    monkeypatch.setattr(a, "_dump_controls", _no_dump)

    # two live turns, then the chat DOM is gone (module transitioned) — probed twice (top + re-confirm).
    live = {"n_responses": 3, "responses": ["I'll help you", "not sure", "no idea"],
            "has_time_remaining": True, "timer_secs": 600, "has_help_another": True}
    ended = {"n_responses": 0, "has_time_remaining": False, "has_help_another": False}
    page = _StubPage(states=[dict(live), dict(live), dict(ended), dict(ended)])

    result = asyncio.run(a._drive_chat(page))
    assert result is True                 # exited cleanly so core advances to the next module
    assert len(banked) == 2               # answered both live turns (banked each)


def test_drive_chat_dismisses_practice_gate_before_answering(monkeypatch):
    # A "Practice step done → Begin Assessment" modal sits over the frozen practice responses; the driver
    # must dismiss the gate FIRST (not click the covered Response), then answer the real sim, then exit.
    a = HarverAdapter(mailbox="x@takhet.com")

    async def _no_vision(*args, **kw):
        return None
    monkeypatch.setattr(a, "_vision_pick", _no_vision)

    dismissed = {"n": 0}
    real_dismiss = a._dismiss_chat_gate

    async def _counting_dismiss(page):
        dismissed["n"] += 1
        return await real_dismiss(page)
    monkeypatch.setattr(a, "_dismiss_chat_gate", _counting_dismiss)

    answered = []

    async def _fake_bank(page, q, responses, idx):
        answered.append(idx)
    monkeypatch.setattr(a, "_bank_chat_turn", _fake_bank)

    async def _no_dump(*a, **k):
        return None
    monkeypatch.setattr(a, "_dump_controls", _no_dump)

    gate = {"n_responses": 3, "responses": ["r1", "r2", "r3"], "has_chat_ui": True,
            "has_gate": True, "timer_secs": 720}
    live = {"n_responses": 3, "responses": ["I'll help you", "not sure", "no idea"],
            "has_chat_ui": True, "has_gate": False, "timer_secs": 715}
    ended = {"n_responses": 0, "has_chat_ui": False, "has_gate": False, "has_help_another": False}
    page = _StubPage(states=[dict(gate), dict(live), dict(ended), dict(ended)])

    assert asyncio.run(a._drive_chat(page)) is True
    assert dismissed["n"] >= 1            # the gate was dismissed
    assert len(answered) == 1             # exactly the ONE real (non-gate) turn was answered


def test_drive_chat_exits_immediately_when_module_already_ended(monkeypatch):
    a = HarverAdapter(mailbox="x@takhet.com")

    async def _no_dump(*a, **k):
        return None
    monkeypatch.setattr(a, "_dump_controls", _no_dump)
    ended = {"n_responses": 0, "has_time_remaining": False, "has_help_another": False}
    page = _StubPage(states=[dict(ended), dict(ended)])
    assert asyncio.run(a._drive_chat(page)) is True


# ---- is_done stays strict (a chat/module title is never «пройдено») -------------------------------

def test_is_done_strict_on_module_title():
    a = HarverAdapter(mailbox="x@takhet.com")

    class _P:
        url = "https://journey.harver.com/vacancy/x"

        async def title(self):
            return "Live Chat Support Simulation"

        async def inner_text(self, *a, **k):
            return "Thank you for your interest"   # a stray phrase, NOT a whole-assessment completion

        async def evaluate(self, *a, **k):
            return {}

    assert asyncio.run(a.is_done(_P())) is False
