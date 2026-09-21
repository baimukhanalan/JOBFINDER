"""The LLM circuit-breaker in tailor._llm_complete (network-free).

When the local LLM 5xx's persistently (expired provider token), the naive 4-try backoff
burns ~30s PER call. The breaker trips after `_LLM_BREAKER_THRESHOLD` failed cycles and
then raises IMMEDIATELY (no backoff) for a cooldown, so lane subprocesses fall straight to
the deterministic path instead of wasting minutes. A success closes it again.
"""
import httpx
import pytest

import backend.services.tailor.tailor as t

# capture the real Claude-CLI fn BEFORE the autouse fixture stubs it (for the gating test)
_REAL_CLAUDE_CLI = t._claude_cli_complete


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.request = None
        self.response = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    # deterministic clock + no real sleeping; reset breaker state around each test
    monkeypatch.setattr(t, "_llm_fail_cycles", 0, raising=False)
    monkeypatch.setattr(t, "_llm_down_until", 0.0, raising=False)
    monkeypatch.setattr(t.settings, "llm_url", "http://127.0.0.1:8080/v1", raising=False)
    monkeypatch.setattr(t._time, "sleep", lambda *_a, **_k: None)
    # isolate the Sumrak/breaker behaviour from the Claude-CLI fallback (tested separately)
    monkeypatch.setattr(t, "_claude_cli_complete", lambda *_a, **_k: None)
    yield


def test_breaker_opens_after_threshold_and_short_circuits(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(t._time, "monotonic", lambda: clock["t"])
    calls = {"n": 0}

    def _post(*_a, **_k):
        calls["n"] += 1
        return _Resp(500)

    monkeypatch.setattr(httpx, "post", _post)

    # THRESHOLD (2) full cycles of 4 tries each = 8 POSTs, then the breaker trips
    for _ in range(t._LLM_BREAKER_THRESHOLD):
        with pytest.raises(Exception):
            t._llm_complete("hi")
    assert calls["n"] == 4 * t._LLM_BREAKER_THRESHOLD
    assert t._llm_down_until > 0.0                      # breaker is OPEN

    # while open, the next call raises IMMEDIATELY — zero further POSTs, no backoff
    before = calls["n"]
    with pytest.raises(RuntimeError):
        t._llm_complete("hi")
    assert calls["n"] == before                          # short-circuited, no network


def test_breaker_probes_and_closes_on_success_after_cooldown(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(t._time, "monotonic", lambda: clock["t"])
    state = {"fail": True}

    def _post(*_a, **_k):
        return _Resp(500 if state["fail"] else 200)

    monkeypatch.setattr(httpx, "post", _post)

    for _ in range(t._LLM_BREAKER_THRESHOLD):
        with pytest.raises(Exception):
            t._llm_complete("hi")
    assert t._llm_down_until > clock["t"]                 # open now

    # after the cooldown the breaker probes again; the LLM has recovered -> success closes it
    clock["t"] = t._llm_down_until + 1.0
    state["fail"] = False
    assert t._llm_complete("hi") == "ok"
    assert t._llm_fail_cycles == 0


def test_healthy_llm_is_unchanged(monkeypatch):
    monkeypatch.setattr(t._time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(200))
    assert t._llm_complete("hi") == "ok"
    assert t._llm_down_until == 0.0                       # never trips when healthy


# ---- Claude CLI fallback (subscription, no API key) --------------------------------------
def test_claude_cli_used_when_sumrak_5xxs(monkeypatch):
    # Sumrak 500s every attempt → the Claude CLI serves the completion (no raise, no deterministic)
    monkeypatch.setattr(t._time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(500))
    monkeypatch.setattr(t, "_claude_cli_complete", lambda prompt: "FROM CLAUDE")
    assert t._llm_complete("hi") == "FROM CLAUDE"


def test_claude_cli_used_when_breaker_open(monkeypatch):
    # breaker OPEN → Sumrak is skipped entirely, the Claude CLI serves it
    monkeypatch.setattr(t._time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(t, "_llm_down_until", 999999.0, raising=False)
    calls = {"n": 0}

    def _post(*_a, **_k):
        calls["n"] += 1
        return _Resp(500)

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(t, "_claude_cli_complete", lambda prompt: "CLI")
    assert t._llm_complete("hi") == "CLI"
    assert calls["n"] == 0                                # Sumrak never called while breaker open


def test_claude_cli_complete_gating(monkeypatch):
    import subprocess
    # disabled by env → None, no shell-out
    monkeypatch.setenv("TAILOR_CLAUDE_CLI", "0")
    assert _REAL_CLAUDE_CLI("hi") is None
    # enabled + a resolvable binary + a fake subprocess → returns the trimmed stdout
    monkeypatch.setenv("TAILOR_CLAUDE_CLI", "1")
    monkeypatch.setattr(t, "_claude_bin", lambda: "/x/claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(stdout="  polished text  "))
    assert _REAL_CLAUDE_CLI("hi") == "polished text"
    # enabled but the CLI isn't installed → None (caller uses the next tier)
    monkeypatch.setattr(t, "_claude_bin", lambda: None)
    assert _REAL_CLAUDE_CLI("hi") is None
