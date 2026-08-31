"""Unit tests for the Working Solutions apply strategy (no network, no browser).

Only the pure/deterministic surface is exercised here — matches(), the env gate, the site
key, and the pure helpers (screener value/answer, dial code). The live DOM interaction
(preferred-name / dial-code / screener / state fills, captcha solve+inject, submit-record)
needs a LIVE dry-run against a real apply.workingsolutions.com page and is NOT covered here.
"""
import importlib

import pytest

from backend.applier.strategies import workingsolutions as ws
from backend.applier.strategies.workingsolutions import (
    WorkingSolutionsStrategy,
    _dial_code,
    ws_screener_value,
)


# ---- strategy routing --------------------------------------------------------

def test_matches_apply_host():
    assert WorkingSolutionsStrategy.matches(
        "https://apply.workingsolutions.com/job/304202")
    assert WorkingSolutionsStrategy.matches(
        "HTTPS://APPLY.WORKINGSOLUTIONS.COM/job/513195")  # case-insensitive


def test_does_not_match_other_hosts():
    # The post-contract agent portal (a different domain) and the marketing site must NOT
    # route here, nor any other ATS.
    assert not WorkingSolutionsStrategy.matches("https://vyne.workingsol.com/login")
    assert not WorkingSolutionsStrategy.matches("https://www.workingsolutions.com/careers")
    assert not WorkingSolutionsStrategy.matches(
        "https://boards.greenhouse.io/embed/job_app?token=1")
    assert not WorkingSolutionsStrategy.matches("https://maximus.avature.net/careers/Register")
    assert not WorkingSolutionsStrategy.matches("")
    assert not WorkingSolutionsStrategy.matches(None)  # type: ignore[arg-type]


# ---- env-gated advance (default OFF) -----------------------------------------

def test_advance_off_by_default(monkeypatch):
    # WS_ADVANCE (solve captcha + record the submit button) must be OFF unless explicitly set —
    # a plain fill / dry-run stays side-effect-free at the employer.
    monkeypatch.delenv("WS_ADVANCE", raising=False)
    importlib.reload(ws)
    assert ws.WorkingSolutionsStrategy().advance_wizard is False
    assert ws._env_advance() is False


def test_advance_on_when_env_set(monkeypatch):
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("WS_ADVANCE", val)
        importlib.reload(ws)
        assert ws._env_advance() is True, val
        assert ws.WorkingSolutionsStrategy().advance_wizard is True
    monkeypatch.setenv("WS_ADVANCE", "0")
    importlib.reload(ws)
    assert ws._env_advance() is False


@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    # Any test that reloads the module with WS_ADVANCE set must leave a clean module behind.
    yield
    monkeypatch.delenv("WS_ADVANCE", raising=False)
    importlib.reload(ws)


# ---- site key ----------------------------------------------------------------

def test_site_key_is_the_recon_value():
    # The reCAPTCHA v2 site key harvested from the live page (recon). A rotation must be a
    # deliberate, visible edit — pin the exact value.
    assert WorkingSolutionsStrategy.SITE_KEY == "6LfJwSsUAAAAAIJJedgq-MJORr9duBa4ta5pw8ju"
    assert len(WorkingSolutionsStrategy.SITE_KEY) == 40  # standard reCAPTCHA site-key length


# ---- named eligibility screeners --------------------------------------------

def test_ws_screener_value_known_names_affirmative():
    for nm in ("independentContractor", "backgroundNoise", "internetService", "trueInfo"):
        assert ws_screener_value(nm) == "1"
        assert ws_screener_value(nm.upper()) == "1"          # case-insensitive
        assert ws_screener_value(f"  {nm}  ") == "1"         # trimmed


def test_ws_screener_value_unknown_names():
    for nm in ("gender", "race", "phone", "email", "firstname", "", None):
        assert ws_screener_value(nm) is None  # type: ignore[arg-type]


# ---- label-driven screener answers (truthful) --------------------------------

def test_screener_answer_the_four_ws_screeners_are_affirmative():
    ans = WorkingSolutionsStrategy._screener_answer
    assert ans("i understand this is an independent contractor position", {})[0] == "Yes"
    assert ans("do you have a workspace free of background noise?", {})[0] == "Yes"
    assert ans("do you have high-speed internet service (cable or fiber)?", {})[0] == "Yes"
    cert = ans("i certify the information provided is true and accurate", {})
    assert cert and cert[0] in ("Yes", "I certify")


def test_screener_answer_generic_eligibility():
    ans = WorkingSolutionsStrategy._screener_answer
    assert ans("are you 18 years or older?", {}) == ["Yes"]
    assert ans("are you authorized to work in the united states?", {}) == ["Yes"]
    assert ans("do you require sponsorship to work in the us?", {}) == ["No"]


def test_screener_answer_leaves_unrelated_questions():
    ans = WorkingSolutionsStrategy._screener_answer
    # A behavioral / open-ended prompt must NOT be treated as a Yes/No screener.
    assert ans("describe a time you resolved a customer conflict", {}) is None
    assert ans("what interests you about this role?", {}) is None
    assert ans("", {}) is None


# ---- intl-tel-input dial code -----------------------------------------------

def test_dial_code_us_and_neighbours():
    assert _dial_code("United States") == "+1"
    assert _dial_code("usa") == "+1"
    assert _dial_code("US") == "+1"
    assert _dial_code("Canada") == "+1"
    assert _dial_code("United Kingdom") == "+44"
    assert _dial_code("GB") == "+44"
    assert _dial_code("Mexico") == "+52"


def test_dial_code_defaults_to_us():
    # The board is US-only, so an unknown/empty country falls back to +1.
    assert _dial_code("") == "+1"
    assert _dial_code("Narnia") == "+1"


# ---- option matcher (word-boundary safety) -----------------------------------

def test_opt_match_short_answers_use_word_boundary():
    # The helper's contract: BOTH args are already lower-cased by the caller.
    m = WorkingSolutionsStrategy._opt_match
    assert m("no", "no")
    assert not m("no", "none")            # 'No' must not match 'None'
    assert m("yes", "yes, i agree")
    assert m("i certify", "i certify that the information is accurate")


# ---- registration ------------------------------------------------------------

def test_registered_in_runner_strategies():
    # Assert by name + routing (not class identity): other tests reload the module, so the
    # class object in STRATEGIES may be a different reload generation than the top-level import.
    from backend.applier.runner import STRATEGIES, _pick_strategy
    assert any(getattr(c, "name", "") == "working_solutions" for c in STRATEGIES)
    picked = _pick_strategy("https://apply.workingsolutions.com/job/304202")
    assert picked.name == "working_solutions"


def test_registered_in_mass_hiring_supported_hosts():
    from backend.tools.mass_hiring_apply import SUPPORTED_HOSTS, is_supported
    assert "apply.workingsolutions.com" in SUPPORTED_HOSTS
    assert is_supported("https://apply.workingsolutions.com/job/304202")
    assert not is_supported("https://vyne.workingsol.com/login")
