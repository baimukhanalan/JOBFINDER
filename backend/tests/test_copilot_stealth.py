"""The co-pilot's reCAPTCHA-v3-friendly launch posture: automation switch dropped + AutomationControlled
off when stealth is on; a plain launch when COPILOT_STEALTH=0. Pure (no browser)."""
import importlib
import os

import pytest


def _reload_copilot(monkeypatch, stealth: str | None):
    if stealth is None:
        monkeypatch.delenv("COPILOT_STEALTH", raising=False)
    else:
        monkeypatch.setenv("COPILOT_STEALTH", stealth)
    monkeypatch.setenv("COPILOT_HEADLESS", "1")
    import backend.copilot as cp
    return importlib.reload(cp)


def test_stealth_on_by_default(monkeypatch):
    cp = _reload_copilot(monkeypatch, None)
    assert cp.STEALTH_ON is True
    kw = cp._stealth_launch_kwargs(["--no-sandbox"])
    assert kw["ignore_default_args"] == ["--enable-automation"]
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert "--no-sandbox" in kw["args"]


def test_stealth_off_is_plain_launch(monkeypatch):
    cp = _reload_copilot(monkeypatch, "0")
    assert cp.STEALTH_ON is False
    kw = cp._stealth_launch_kwargs(["--no-sandbox"])
    assert kw == {"args": ["--no-sandbox"]}


def test_stealth_script_is_importable():
    from backend.applier.browser import _STEALTH
    assert "navigator, 'webdriver'" in _STEALTH
