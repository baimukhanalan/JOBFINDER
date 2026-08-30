"""Unit tests for backend.applier.captcha_solver — pure config/guard logic, no network."""
import asyncio

from backend.applier import captcha_solver as cs


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    assert cs.is_enabled() is False
    # solve() is a graceful no-op (returns None) when disabled — never touches the network.
    assert asyncio.run(cs.solve("recaptcha_v2", "sitekey", "https://x.com")) is None


def test_enabled_with_key(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    assert cs.is_enabled() is True


def test_solve_none_for_unknown_kind(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    assert asyncio.run(cs.solve("not_a_kind", "k", "https://x.com")) is None
    # missing site_key / url also no-op
    assert asyncio.run(cs.solve("recaptcha_v2", "", "https://x.com")) is None
    assert asyncio.run(cs.solve("recaptcha_v2", "k", "")) is None


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    assert cs._provider() == "capsolver"
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "TwoCaptcha")
    assert cs._provider() == "twocaptcha"


def test_task_type_maps_cover_all_kinds():
    kinds = {"recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile"}
    assert set(cs._CAPSOLVER_TASK) == kinds
    assert set(cs._TWOCAPTCHA_METHOD) == kinds
