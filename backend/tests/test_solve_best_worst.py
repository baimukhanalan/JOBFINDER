"""solve_best_worst: strong-model SJT best/worst parsing + fail-safe gating (pure, no network)."""
from backend.tools.assessment_harvester import openai_solver as o


def _mock(monkeypatch, reply):
    monkeypatch.setattr(o, "_post", lambda *a, **k: reply)


def test_parses_comma_pair(monkeypatch):
    _mock(monkeypatch, "2,4")
    assert o.solve_best_worst("scenario", ["a", "b", "c", "d", "e"]) == (1, 3)


def test_parses_verbose(monkeypatch):
    _mock(monkeypatch, "best 3 worst 1")
    assert o.solve_best_worst("s", ["a", "b", "c", "d"]) == (2, 0)


def test_solver_failure_returns_none(monkeypatch):
    _mock(monkeypatch, None)   # no credit / HTTP error → caller falls back
    assert o.solve_best_worst("s", ["a", "b", "c"]) is None


def test_same_index_rejected(monkeypatch):
    _mock(monkeypatch, "2,2")
    assert o.solve_best_worst("s", ["a", "b", "c"]) is None


def test_out_of_range_rejected(monkeypatch):
    _mock(monkeypatch, "9,1")
    assert o.solve_best_worst("s", ["a", "b", "c"]) is None


def test_too_few_responses():
    assert o.solve_best_worst("s", ["only one"]) is None
