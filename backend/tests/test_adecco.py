"""Unit tests for the Adecco apply lane (strategy + recon + probe). Network-free."""
from backend.applier.strategies.adecco import AdeccoStrategy, _env_advance
from backend.applier.strategies.roberthalf import RobertHalfStrategy


# ---- routing -----------------------------------------------------------------
def test_matches_adecco_hosts():
    assert AdeccoStrategy.matches("https://www.adecco.com/en-us/job-search/csr-x/")
    assert AdeccoStrategy.matches("https://candidate.adecco.com/easyApply?queryState=abc")


def test_does_not_match_non_adecco():
    for url in ("https://www.roberthalf.com/us/en/job/x",
                "https://apply.talemetry.com/application/abc",
                "https://account.amazon.jobs/jobs/1/apply", ""):
        assert not AdeccoStrategy.matches(url), url


# ---- the live gate -----------------------------------------------------------
def test_advance_off_by_default():
    assert AdeccoStrategy().advance_wizard is False


def test_env_advance_parsing(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("ADECCO_ADVANCE", val)
        assert _env_advance() is True
    for val in ("0", "false", "", "  "):
        monkeypatch.setenv("ADECCO_ADVANCE", val)
        assert _env_advance() is False
    monkeypatch.delenv("ADECCO_ADVANCE", raising=False)
    assert _env_advance() is False


# ---- shares the Robert Half account/wizard machinery (subclass, own host/gate) ---------------
def test_reuses_roberthalf_machinery():
    assert issubclass(AdeccoStrategy, RobertHalfStrategy)
    assert AdeccoStrategy.name == "adecco"
    # the shared, tested screener table is reused verbatim (never re-declared).
    assert AdeccoStrategy._screener_answer(
        "will you now or in the future require visa sponsorship?", {}) == ["No"]


# ---- recon confirmation matcher ----------------------------------------------
from backend.tools import adecco_recon as ar  # noqa: E402


def test_confirmation_by_sender():
    assert ar._is_adecco_confirmation("From: no-reply@adecco.com", "Subject: hi") is True


def test_confirmation_by_subject():
    assert ar._is_adecco_confirmation(
        "From: x@ex.com", "Subject: We received your application") is True


def test_confirmation_negative():
    assert ar._is_adecco_confirmation(
        "From: r@randombpo.com", "Subject: A job you might like") is False


# ---- probe wiring ------------------------------------------------------------
from backend.tools import adecco_probe_promote as app  # noqa: E402


def test_probe_reexports_shared_core():
    assert app.classify("[application CONFIRMED — x]")[0] == "confirmed"
    assert callable(app.run_once) and callable(app.box_is_quiet)
