"""Unit tests for the Progressive/Talemetry apply lane (strategy + recon + probe). Network-free."""
from backend.applier.strategies.roberthalf import RobertHalfStrategy
from backend.applier.strategies.talemetry import TalemetryStrategy, _env_advance


# ---- routing -----------------------------------------------------------------
def test_matches_talemetry_and_progressive_hosts():
    assert TalemetryStrategy.matches("https://apply.talemetry.com/application/abc-123")
    assert TalemetryStrategy.matches("https://careers.progressive.com/jobs/1-claims-adjuster/")


def test_does_not_match_others():
    for url in ("https://www.roberthalf.com/us/en/job/x",
                "https://candidate.adecco.com/easyApply",
                "https://progressive.wd5.myworkdayjobs.com/x", ""):
        assert not TalemetryStrategy.matches(url), url


# ---- the live gate -----------------------------------------------------------
def test_advance_off_by_default():
    assert TalemetryStrategy().advance_wizard is False


def test_env_advance_parsing(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("PROGRESSIVE_ADVANCE", val)
        assert _env_advance() is True
    for val in ("0", "false", "", "  "):
        monkeypatch.setenv("PROGRESSIVE_ADVANCE", val)
        assert _env_advance() is False
    monkeypatch.delenv("PROGRESSIVE_ADVANCE", raising=False)
    assert _env_advance() is False


# ---- shares the Robert Half account/wizard machinery -------------------------
def test_reuses_roberthalf_machinery():
    assert issubclass(TalemetryStrategy, RobertHalfStrategy)
    assert TalemetryStrategy.name == "talemetry"
    assert TalemetryStrategy._screener_answer("are you 18 years or older?", {}) == ["Yes"]


# ---- recon confirmation matcher ----------------------------------------------
from backend.tools import progressive_recon as pr  # noqa: E402


def test_confirmation_by_sender():
    assert pr._is_progressive_confirmation(
        "From: no-reply@talemetry.com", "Subject: hi") is True
    assert pr._is_progressive_confirmation(
        "From: careers@progressive.com", "Subject: x") is True


def test_confirmation_by_subject():
    assert pr._is_progressive_confirmation(
        "From: x@ex.com", "Subject: Thank you for your application") is True


def test_confirmation_negative():
    assert pr._is_progressive_confirmation(
        "From: r@randombpo.com", "Subject: A job you might like") is False


# ---- probe wiring ------------------------------------------------------------
from backend.tools import progressive_probe_promote as pp  # noqa: E402


def test_probe_reexports_shared_core():
    assert pp.classify("[SUBMIT clicked — x]")[0] == "submitted_unconfirmed"
    assert callable(pp.run_once)
