"""Unit tests for the Robert Half apply lane (strategy + recon + probe).

Pure logic only — NO network, NO browser, NO account creation, NO submission. Covers URL
routing, the ROBERTHALF_ADVANCE gate (OFF by default so a plain fill never creates an account
or transmits PII), the Salesforce-account password generator, the shared screener reuse, the
recon state/confirmation helpers + the US-egress wiring (never the KZ phones), and the probe's
log classifier.
"""
import re

from backend.applier.runner import _pick_strategy
from backend.applier.strategies.base import GenericStrategy
from backend.applier.strategies.roberthalf import (
    RobertHalfStrategy,
    _env_advance,
    _gen_password,
)


# ---- strategy routing --------------------------------------------------------
def test_matches_roberthalf_hosts():
    assert RobertHalfStrategy.matches("https://www.roberthalf.com/us/en/job/x/y")
    assert RobertHalfStrategy.matches("https://WWW.ROBERTHALF.COM/us/en/jobs")
    assert RobertHalfStrategy.matches("https://rhcandidate.my.site.com/s/login")
    assert RobertHalfStrategy.matches("https://roberthalf.my.salesforce.com/apply")


def test_does_not_match_non_roberthalf():
    for url in ("https://boards.greenhouse.io/embed/job_app?token=1",
                "https://candidate.adecco.com/easyApply",
                "https://apply.talemetry.com/application/abc",
                "https://account.amazon.jobs/jobs/1/apply", ""):
        assert not RobertHalfStrategy.matches(url), url


def test_generic_fallback_for_unknown_host():
    assert isinstance(_pick_strategy("https://example.com/careers"), GenericStrategy)


# ---- the live gate -----------------------------------------------------------
def test_advance_off_by_default():
    assert RobertHalfStrategy().advance_wizard is False


def test_env_advance_parsing(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("ROBERTHALF_ADVANCE", val)
        assert _env_advance() is True
    for val in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("ROBERTHALF_ADVANCE", val)
        assert _env_advance() is False
    monkeypatch.delenv("ROBERTHALF_ADVANCE", raising=False)
    assert _env_advance() is False


# ---- password complexity -----------------------------------------------------
def test_generated_password_is_strong():
    for _ in range(50):
        pw = _gen_password()
        assert re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[!@#$%^&*?_\-]", pw)
        assert pw == pw.strip() and len(pw) >= 10


# ---- shared screener reuse (calls Amazon's tested table, does not re-declare it) --------------
def test_screener_reuse_sponsorship_is_no():
    assert RobertHalfStrategy._screener_answer(
        "will you now or in the future require visa sponsorship?", {}) == ["No"]


def test_screener_reuse_customer_service_strongest_first():
    ans = RobertHalfStrategy._screener_answer(
        "how many years of customer service experience do you have?", {})
    assert ans and ans[0].startswith("5")


def test_opt_match_word_boundary():
    m = RobertHalfStrategy._opt_match
    assert m("no", "no") and not m("no", "none")


# ---- recon helpers -----------------------------------------------------------
from backend.tools import roberthalf_recon as rr  # noqa: E402


def test_state_from_location_named():
    assert rr._state_from_location("Remote, Houston, Texas, United States") == "Texas"
    assert rr._state_from_location("Remote, New York, United States") == "New York"


def test_state_from_location_stateless_is_blank():
    assert rr._state_from_location("Remote, United States") == ""
    assert rr._state_from_location("United States") == ""
    assert rr._state_from_location("") == ""
    assert rr._state_from_location("Work From Home, USA") == ""


def test_confirmation_by_sender():
    assert rr._is_roberthalf_confirmation("From: no-reply@roberthalf.com", "Subject: hi") is True
    assert rr._is_roberthalf_confirmation("From: careers@rhi.com", "Subject: x") is True


def test_confirmation_by_subject():
    assert rr._is_roberthalf_confirmation(
        "From: x@ex.com", "Subject: Thank you for applying") is True
    assert rr._is_roberthalf_confirmation(
        "From: x@ex.com", "Subject: We've received your application") is True


def test_confirmation_negative():
    assert rr._is_roberthalf_confirmation(
        "From: recruiter@randombpo.com", "Subject: A job you might like") is False


# ---- recon US-egress wiring (never the KZ phone slots) ------------------------
def _clear(monkeypatch):
    for k in ("ROBERTHALF_US", "ROBERTHALF_PROXY", "US_PROXY", "WEBSHARE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_proxy_direct_by_default(monkeypatch):
    _clear(monkeypatch)
    assert rr._lane_proxy("ROBERTHALF_US", "ROBERTHALF_PROXY", "j@takhet.com") is None


def test_proxy_explicit_override_wins(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ROBERTHALF_PROXY", "http://u:p@1.2.3.4:8080")
    assert rr._lane_proxy("ROBERTHALF_US", "ROBERTHALF_PROXY") == {
        "server": "http://1.2.3.4:8080", "username": "u", "password": "p"}


def test_proxy_direct_keyword_forces_direct(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ROBERTHALF_US", "1")
    monkeypatch.setenv("ROBERTHALF_PROXY", "direct")
    import backend.tools.us_egress as ue
    monkeypatch.setattr(ue, "us_proxy",
                        lambda: (_ for _ in ()).throw(AssertionError("resolver consulted")))
    assert rr._lane_proxy("ROBERTHALF_US", "ROBERTHALF_PROXY") is None


def test_proxy_us_opt_in_uses_resolver(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ROBERTHALF_US", "1")
    import backend.tools.us_egress as ue
    monkeypatch.setattr(ue, "us_proxy", lambda: {"server": "http://brd-us:33335",
                                                 "username": "c", "password": "p"})
    assert rr._lane_proxy("ROBERTHALF_US", "ROBERTHALF_PROXY") == {
        "server": "http://brd-us:33335", "username": "c", "password": "p"}


def test_proxy_never_uses_kz_phones(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ROBERTHALF_US", "1")
    import backend.tools.proxy_pool as pp
    monkeypatch.setattr(pp, "residential_slots",
                        lambda: (_ for _ in ()).throw(AssertionError("KZ phone slots consulted")))
    import backend.tools.us_egress as ue
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: None)
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: None)
    assert rr._lane_proxy("ROBERTHALF_US", "ROBERTHALF_PROXY") is None


# ---- probe classifier --------------------------------------------------------
from backend.tools import roberthalf_probe_promote as pp  # noqa: E402


def test_probe_classify_confirmed():
    assert pp.classify("... [application CONFIRMED — receipt in the Maildir] ...")[0] == "confirmed"


def test_probe_classify_submitted_unconfirmed():
    assert pp.classify("[SUBMIT clicked — awaiting the confirmation email]\n"
                       "[no confirmation within --keep]")[0] == "submitted_unconfirmed"


def test_probe_classify_needs_account():
    assert pp.classify("[CEILING: stopped at the Robert Half account wall — ...]")[0] == "needs_account"


def test_probe_classify_error():
    assert pp.classify("[run error: TimeoutError: ...]")[0] == "error"
    assert pp.classify("TIMEOUT")[0] == "error"


def test_probe_classify_pending():
    assert pp.classify("persona: ... egress=DIRECT")[0] == "pending"
