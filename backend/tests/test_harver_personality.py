"""Offer-optimal personality answering for Harver (bipolar 1..n). The model path is network-gated;
these lock the pure mapping + the keyword-heuristic fallback (must lean to the hireable-CSR pole, never
answer dead-neutral) so a scored personality module earns a strong fit score, not a mediocre one."""
import asyncio

from backend.tools.assessment_harvester.adapters import harver as H


def _no_solvers(monkeypatch):
    for m in ("claude_cli_solver", "openrouter_solver", "anthropic_solver", "openai_solver"):
        mod = __import__("backend.tools.assessment_harvester." + m, fromlist=[m])
        monkeypatch.setattr(mod, "available", lambda: False)


def test_rating_options_maps_index_to_pole():
    o = H.HarverAdapter._rating_options("stays calm", "gets stressed", 6)
    assert len(o) == 6
    assert "calm" in o[0] and o[0].startswith("Strongly")      # rating 1 = fully LEFT
    assert "stressed" in o[5] and o[5].startswith("Strongly")  # rating 6 = fully RIGHT


def test_rating_options_odd_has_neutral_middle():
    o = H.HarverAdapter._rating_options("A", "B", 5)
    assert o[2] == "Neutral / no preference"


def test_heuristic_picks_desirable_pole(monkeypatch):
    _no_solvers(monkeypatch)
    a = H.HarverAdapter("test.persona1@takhet.com")
    run = asyncio.run  # fresh loop per call — robust when a prior test closed the default loop
    # desirable pole on the RIGHT (calm/patient) → rating near n
    assert run(a._solve_personality("", "I get angry with rude customers",
                                    "I stay calm and patient", 6)) >= 5
    # desirable pole on the LEFT (reliable/thorough) → rating near 1
    assert run(a._solve_personality("", "I am reliable and thorough",
                                    "I am careless and forget tasks", 6)) <= 2


def test_heuristic_tie_is_not_dead_neutral(monkeypatch):
    _no_solvers(monkeypatch)
    a = H.HarverAdapter("test.persona2@takhet.com")
    run = asyncio.run  # fresh loop per call — robust when a prior test closed the default loop
    # two poles with no trait keywords → must NOT collapse to the exact center 3 every time
    vals = {run(a._solve_personality("", "I prefer mornings", "I prefer evenings", 6)) for _ in range(4)}
    assert vals - {3}  # at least one answer is off dead-center
