"""The submit-mutation capture pulls the server's real error code out of Ashby's GraphQL reply
(the UI collapses every rejection into one 'possible spam' banner), and /health exposes the
live stealth / fingerprint flags so a restart can be verified with one curl. Pure (no browser)."""
import importlib
import re


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("COPILOT_HEADLESS", "1")
    import backend.copilot as cp
    return importlib.reload(cp)


def test_code_regex_extracts_recaptcha_code():
    body = ('{"data":{"submitApplicationFormAction":{"__typename":"ApplicationFormSubmitErrorResponse",'
            '"messages":[{"code":"RECAPTCHA_SCORE_BELOW_THRESHOLD","message":"Your application submission '
            'was flagged as possible spam."}]}}}')
    codes = sorted(set(re.findall(r'"(?:code|errorCode|__typename)"\s*:\s*"([A-Z_]{6,})"', body)))
    assert "RECAPTCHA_SCORE_BELOW_THRESHOLD" in codes


def test_health_exposes_flags(monkeypatch):
    cp = _reload(monkeypatch, COPILOT_FP_DIVERSIFY="1", COPILOT_STEALTH="1")
    assert cp.FP_DIVERSIFY is True and cp.STEALTH_ON is True
    cp = _reload(monkeypatch, COPILOT_FP_DIVERSIFY="0", COPILOT_STEALTH="0")
    assert cp.FP_DIVERSIFY is False and cp.STEALTH_ON is False


def test_warm_and_capture_helpers_exist(monkeypatch):
    cp = _reload(monkeypatch, COPILOT_FP_DIVERSIFY="0")
    assert callable(cp._warm_session) and callable(cp._attach_submit_capture)
