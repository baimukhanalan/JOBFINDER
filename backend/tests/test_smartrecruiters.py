"""Unit tests for the SmartRecruiters (Sutherland) apply strategy.

Pure logic only — NO network, NO browser, NO submission. Covers URL routing, the
SMARTRECRUITERS_ADVANCE gate (must be OFF by default so a plain fill is side-effect-free),
the oneclick apply-URL builder, the deterministic truthful screener answers, option
matching, and strategy/host registration.
"""
import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import GenericStrategy
from backend.applier.strategies.smartrecruiters import (
    SmartRecruitersStrategy,
    _env_advance,
    _oneclick_url,
)
from backend.tools import mass_hiring_apply

# A live sample apply URL from recon (Sutherland on SmartRecruiters).
_SR_URL = "https://jobs.smartrecruiters.com/Sutherland/744000140934239"
_SR_ONECLICK = ("https://jobs.smartrecruiters.com/oneclick-ui/company/"
                "d64fb247-7f1e-4bb0-a0b0-e62dbeac05f8/publication/744000140934239"
                "?dcr_ci=Sutherland")


# ---- strategy routing --------------------------------------------------------

def test_matches_smartrecruiters_hosts():
    assert SmartRecruitersStrategy.matches(_SR_URL)
    assert SmartRecruitersStrategy.matches(_SR_URL.upper())          # case tolerant
    assert SmartRecruitersStrategy.matches(_SR_ONECLICK)             # the oneclick form itself
    assert SmartRecruitersStrategy.matches(
        "https://careers.smartrecruiters.com/Sutherland/744000140934239")


def test_does_not_match_non_smartrecruiters():
    # greenhouse / avature / oracle / workday, an empty URL — none route here.
    assert not SmartRecruitersStrategy.matches(
        "https://boards.greenhouse.io/embed/job_app?token=1")
    assert not SmartRecruitersStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not SmartRecruitersStrategy.matches(
        "https://acme.wd1.myworkdayjobs.com/en-US/careers")
    assert not SmartRecruitersStrategy.matches("")
    # the discovery API host is never an apply page — keep it OUT.
    assert not SmartRecruitersStrategy.matches(
        "https://api.smartrecruiters.com/v1/companies/Sutherland/postings/744000140934239")


def test_registered_and_picked_by_url():
    assert SmartRecruitersStrategy in STRATEGIES
    # registered before the GenericStrategy fallback (which isn't in STRATEGIES).
    assert GenericStrategy not in STRATEGIES
    picked = _pick_strategy(_SR_URL)
    assert isinstance(picked, SmartRecruitersStrategy)
    # a non-SR URL must not accidentally route to SR.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"),
                          SmartRecruitersStrategy)


def test_is_a_generic_subclass_with_name():
    # extends GenericStrategy (so super().prefill resolves to the shared pipeline).
    assert issubclass(SmartRecruitersStrategy, GenericStrategy)
    assert SmartRecruitersStrategy.name == "smartrecruiters"


def test_mass_hiring_apply_supports_smartrecruiters():
    assert mass_hiring_apply.is_supported(_SR_URL)
    assert mass_hiring_apply.is_supported(_SR_ONECLICK)
    # the other supported hosts are unchanged
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- SMARTRECRUITERS_ADVANCE gate (live-submit switch) -----------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("SMARTRECRUITERS_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("SMARTRECRUITERS_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("SMARTRECRUITERS_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # The wizard-advance (which transmits PII + sends the application on the final Submit) must
    # be OFF unless SMARTRECRUITERS_ADVANCE is explicitly set — a plain fill is side-effect-free.
    monkeypatch.delenv("SMARTRECRUITERS_ADVANCE", raising=False)

    class _Fresh(SmartRecruitersStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- oneclick apply-URL builder (pure) ---------------------------------------

def test_oneclick_url_from_default_template():
    data = {"puuid": "d64fb247-7f1e-4bb0-a0b0-e62dbeac05f8",
            "pid": 744000140934239, "cident": "Sutherland"}
    assert _oneclick_url(data) == _SR_ONECLICK


def test_oneclick_url_honours_embedded_template():
    # ONECLICKDATA ships a %s-templated `url` (companyUuid, publicationId, cident) — honour it.
    data = {"puuid": "uuid-1", "pid": "999", "cident": "Acme",
            "url": "https://jobs.smartrecruiters.com/oneclick-ui/company/%s"
                   "/publication/%s?dcr_ci=%s"}
    assert _oneclick_url(data) == (
        "https://jobs.smartrecruiters.com/oneclick-ui/company/uuid-1"
        "/publication/999?dcr_ci=Acme")


def test_oneclick_url_missing_or_garbage_returns_none():
    assert _oneclick_url(None) is None
    assert _oneclick_url({}) is None
    assert _oneclick_url("not a dict") is None
    assert _oneclick_url({"pid": "1"}) is None            # no company uuid
    assert _oneclick_url({"puuid": "u"}) is None          # no publication id


# ---- deterministic truthful screeners ----------------------------------------

def test_screener_answer_availability_and_eligibility():
    A = SmartRecruitersStrategy._screener_answer
    # per-posting affirmative screeners a synthetic applicant DESIGNED to fit answers Yes
    assert A("are you able to work an 8 hour shift between 7am-7pm cst?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 75 miles of the sutherland remote site?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / conflict are truthfully No for a fresh authorized persona
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = SmartRecruitersStrategy._screener_answer
    # SmartRecruiters/Sutherland roles are sales/CSR — the sales lexicon resolves too
    exp = A("how many years of sales or customer service experience do you have?", {})
    assert exp and exp[0] == "5+ years"           # strongest believable tier first
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = SmartRecruitersStrategy._screener_answer
    # English → native tier (a US persona)
    assert A("what is your english proficiency?", {})[0] == "Native"
    # Spanish depends on the persona being bilingual (a bilingual role)
    assert A("what is your spanish proficiency?", {"bilingual": True})[0] == "Fluent"
    assert A("what is your spanish proficiency?", {})[0] in ("None", "No proficiency")
    # education uses the persona's fact when present, else a sane default
    assert A("what is your highest level of education?", {"education_level": "Associate"})[0] == "Associate"
    assert A("highest level of education achieved?", {})[0] == "Bachelor"


def test_screener_answer_unknown_returns_none():
    # an unrecognized/behavioral question is LEFT for the human, never guessed
    assert SmartRecruitersStrategy._screener_answer(
        "describe a time you resolved a conflict", {}) is None
    assert SmartRecruitersStrategy._screener_answer("what is your favorite color?", {}) is None


def test_opt_match_boundary():
    m = SmartRecruitersStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False                # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False


# ---- captcha solver is wired at the submit step ------------------------------

def test_captcha_solver_is_imported_in_strategy():
    # The submit step wires captcha_solver.solve_on_page (a no-op without CAPTCHA_SOLVER_KEY);
    # assert the module is imported so the wiring can't silently vanish in a refactor.
    import backend.applier.strategies.smartrecruiters as sr
    assert hasattr(sr, "captcha_solver")
    assert hasattr(sr.captcha_solver, "solve_on_page")
