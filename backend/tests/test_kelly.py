"""Unit tests for the Kelly (KellyConnect) apply strategy — mykelly.com Gravity Forms.

Pure logic only — NO network, NO browser, NO submission. Covers URL routing, the
KELLY_ADVANCE gate (must be OFF by default so a plain fill is side-effect-free), the
deterministic truthful CSR screener answers (incl. the Kelly-specific Date available /
Desired locations / Employment preference selects), option matching, and registration
in both the strategy list and the Mass Hiring supported-hosts set.
"""
import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import ApplyStrategy
from backend.applier.strategies.kelly import KellyStrategy, _env_advance
from backend.tools import mass_hiring_apply

# A live sample apply URL from recon (a KellyConnect call-center CSR posting).
_KELLY_URL = ("https://www.mykelly.com/job/10277091-call-center-customer-service-"
              "representative-san-diego-ca-united-states/")


# ---- strategy routing --------------------------------------------------------

def test_matches_kelly_hosts():
    assert KellyStrategy.matches(_KELLY_URL)
    assert KellyStrategy.matches(_KELLY_URL.upper())          # tolerant of case
    assert KellyStrategy.matches("https://mykelly.com/job/1-x/")


def test_does_not_match_non_kelly():
    # greenhouse / avature / workday / oracle / smartrecruiters / empty must NOT route here.
    assert not KellyStrategy.matches("https://boards.greenhouse.io/embed/job_app?token=1")
    assert not KellyStrategy.matches("https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not KellyStrategy.matches("https://acme.wd1.myworkdayjobs.com/en-US/careers")
    assert not KellyStrategy.matches("https://jobs.smartrecruiters.com/Sutherland/1-csr")
    assert not KellyStrategy.matches(
        "https://fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1")
    assert not KellyStrategy.matches("")


def test_registered_and_picked_by_url():
    assert KellyStrategy in STRATEGIES
    picked = _pick_strategy(_KELLY_URL)
    assert isinstance(picked, KellyStrategy)
    # a non-Kelly URL must not accidentally route to Kelly.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"), KellyStrategy)
    assert not isinstance(_pick_strategy("https://maximus.avature.net/careers/Register"),
                          KellyStrategy)


def test_is_an_applystrategy_subclass_with_name():
    # extends ApplyStrategy (so super().prefill resolves to the shared pipeline), like Avature.
    assert issubclass(KellyStrategy, ApplyStrategy)
    assert KellyStrategy.name == "kelly"


def test_mass_hiring_apply_supports_kelly():
    assert mass_hiring_apply.is_supported(_KELLY_URL)
    # existing hosts unchanged.
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert mass_hiring_apply.is_supported(
        "https://fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1")
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- KELLY_ADVANCE gate (live-submit switch) ---------------------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("KELLY_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("KELLY_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("KELLY_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # 'Advancing' (record the final Submit + run the captcha solver) must be OFF unless
    # KELLY_ADVANCE is explicitly set — a plain fill stays side-effect-free at the employer.
    monkeypatch.delenv("KELLY_ADVANCE", raising=False)

    class _Fresh(KellyStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- deterministic truthful CSR screeners ------------------------------------

def test_screener_answer_kelly_specific_selects():
    A = KellyStrategy._screener_answer
    # "Date available" → soonest option first (a synthetic applicant designed to fit the role).
    assert A("date available", {})[0] == "Immediately"
    assert A("when can you start?", {})[0] == "Immediately"
    # "Desired locations" → this board is remote-US only.
    assert A("desired locations", {})[0] == "Remote"
    assert A("preferred work location", {})[0] == "Remote"
    # "Employment preference" → full-time CSR.
    assert A("employment preference", {})[0] == "Full-time"
    assert A("employment type", {})[0] == "Full-time"


def test_screener_answer_availability_and_eligibility():
    A = KellyStrategy._screener_answer
    assert A("are you interested in this seasonal opportunity?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 50 miles of the site?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / schedule-conflict are truthfully No for a fresh authorized persona.
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = KellyStrategy._screener_answer
    exp = A("how many years of customer service experience do you have?", {})
    assert exp and exp[0] == "5+ years"           # strongest believable tier first
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = KellyStrategy._screener_answer
    assert A("what is your english proficiency?", {})[0] == "Native"
    assert A("what is your spanish proficiency?", {"bilingual": True})[0] == "Fluent"
    assert A("what is your spanish proficiency?", {})[0] in ("None", "No proficiency")
    assert A("what is your highest level of education?", {"education_level": "Associate"})[0] == "Associate"
    assert A("education level", {})[0] == "Bachelor"


def test_screener_answer_unknown_returns_none():
    # an unrecognized/behavioral question is LEFT for the human, never guessed.
    assert KellyStrategy._screener_answer("describe a time you resolved a conflict", {}) is None
    assert KellyStrategy._screener_answer("what is your favorite color?", {}) is None
    # a certification tick is handled elsewhere, not by the screener table.
    assert KellyStrategy._screener_answer("i certify the above is true", {}) is None


def test_opt_match_boundary():
    m = KellyStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False                # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("full-time", "full-time") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False
