"""Unit tests for the iCIMS apply strategy (Mass Hiring Teleperformance family).

Pure logic only — NO network, NO browser, NO submission, NO account creation. Covers URL
routing, the ICIMS_ADVANCE gate (must be OFF by default so a plain fill stops at the account
wall and creates nothing), the deterministic truthful screener answers, option matching,
strategy registration, the AWS-WAF/reCAPTCHA solver wiring (graceful no-op without a key), and
the pure helpers.
"""
import asyncio
import re

import pytest

from backend.applier import captcha_solver
from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import ApplyStrategy
from backend.applier.strategies.icims import (
    ICIMSStrategy,
    _env_advance,
    _first,
    _gen_password,
    _is_icims_content_url,
    _last,
)
from backend.tools import mass_hiring_apply

# A live sample apply URL from recon (Teleperformance on iCIMS).
_TP_URL = ("https://careersus-teleperformance.icims.com/jobs/86960/"
           "customer-service-representative-remote/job")


# ---- strategy routing --------------------------------------------------------

def test_matches_icims_hosts():
    assert ICIMSStrategy.matches(_TP_URL)
    assert ICIMSStrategy.matches(_TP_URL.upper())          # case-tolerant
    assert ICIMSStrategy.matches("https://careers-acme.icims.com/jobs/1/x/login")


def test_does_not_match_non_icims():
    # a greenhouse / avature / workday form and an empty URL must all NOT route here.
    assert not ICIMSStrategy.matches("https://boards.greenhouse.io/embed/job_app?token=1")
    assert not ICIMSStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not ICIMSStrategy.matches("https://acme.wd1.myworkdayjobs.com/en-US/careers")
    assert not ICIMSStrategy.matches("")


def test_registered_and_picked_by_url():
    assert ICIMSStrategy in STRATEGIES
    picked = _pick_strategy(_TP_URL)
    assert isinstance(picked, ICIMSStrategy)
    # a non-iCIMS URL must not accidentally route to iCIMS.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"), ICIMSStrategy)


def test_is_an_applystrategy_subclass_with_name():
    # extends ApplyStrategy (so super().prefill resolves to the shared pipeline).
    assert issubclass(ICIMSStrategy, ApplyStrategy)
    assert ICIMSStrategy.name == "icims"


def test_mass_hiring_apply_supports_icims():
    assert mass_hiring_apply.is_supported(_TP_URL)
    # unchanged: the pre-existing supported hosts still resolve.
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert mass_hiring_apply.is_supported(
        "https://fa-x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1")
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- ICIMS_ADVANCE gate (live account+submit switch) -------------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("ICIMS_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("ICIMS_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("ICIMS_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # The account creation + wizard walk (which register the candidate and transmit the
    # application) must be OFF unless ICIMS_ADVANCE is explicitly set — a plain fill stops at
    # the account wall and creates nothing.
    monkeypatch.delenv("ICIMS_ADVANCE", raising=False)

    class _Fresh(ICIMSStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- generated account password ----------------------------------------------

def test_generated_password_meets_complexity():
    for _ in range(20):
        pw = _gen_password()
        assert len(pw) >= 10
        assert re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


# ---- pure helpers ------------------------------------------------------------

def test_is_icims_content_url():
    assert _is_icims_content_url(_TP_URL + "?in_iframe=1")
    assert _is_icims_content_url("https://x.icims.com/jobs/1/y/login")
    assert not _is_icims_content_url("https://x.com/jobs/1")     # not icims.com
    assert not _is_icims_content_url("https://x.icims.com/")     # no job/login/iframe marker
    assert not _is_icims_content_url("")


def test_name_split_helpers():
    assert _first({"full_name": "Jane Q Doe"}) == "Jane"
    assert _last({"full_name": "Jane Q Doe"}) == "Doe"
    assert _first({"name": "Cher"}) == "Cher"
    assert _last({"name": "Cher"}) == ""                          # single token -> no surname
    assert _first({}) == "" and _last({}) == ""


# ---- deterministic truthful screeners ----------------------------------------

def test_screener_answer_availability_and_eligibility():
    A = ICIMSStrategy._screener_answer
    # affirmative screeners a synthetic applicant DESIGNED to fit the job answers Yes
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("are you able to work an 8 hour shift between 7am-7pm cst?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 75 miles of the teleperformance remote site?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    assert A("do you have high-speed internet at home?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / conflict are truthfully No for a fresh authorized persona
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = ICIMSStrategy._screener_answer
    exp = A("how much experience do you have as a csr in a call center?", {})
    assert exp and exp[0] == "5+ years"           # strongest believable tier first
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = ICIMSStrategy._screener_answer
    assert A("what is your english proficiency?", {})[0] == "Native"
    assert A("what is your spanish proficiency?", {"bilingual": True})[0] == "Fluent"
    assert A("what is your spanish proficiency?", {})[0] in ("None", "No proficiency")
    assert A("what is your highest level of education?", {"education_level": "Associate"})[0] == "Associate"
    assert A("highest level of education achieved?", {})[0] == "Bachelor"


def test_screener_answer_unknown_returns_none():
    # an unrecognized/behavioral question is LEFT for the human, never guessed
    assert ICIMSStrategy._screener_answer("describe a time you resolved a conflict", {}) is None
    assert ICIMSStrategy._screener_answer("what is your favorite color?", {}) is None


def test_opt_match_boundary():
    m = ICIMSStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False                # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False


# ---- captcha_solver wiring (graceful no-op without a key) ---------------------

def test_strategy_imports_captcha_solver():
    # the module wires the shared solver service (AWS WAF + reCAPTCHA) at the submit step.
    from backend.applier.strategies import icims
    assert icims.captcha_solver is captcha_solver
    assert hasattr(captcha_solver, "solve_aws_waf")
    assert hasattr(captcha_solver, "solve_on_page")


def test_captcha_solvers_are_noop_without_key(monkeypatch):
    # No provider key => every solve call is a graceful no-op that never touches the page and
    # never raises (so a dry-run / disabled-solver fill is safe). We pass a sentinel page that
    # would blow up if the solver actually dereferenced it — proving the disabled short-circuit.
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    assert captcha_solver.is_enabled() is False

    class _Boom:
        def __getattr__(self, _):
            raise AssertionError("solver touched the page while disabled")

    assert asyncio.run(captcha_solver.solve_on_page(_Boom())) is False
    assert asyncio.run(captcha_solver.solve_aws_waf(_Boom())) is False
