"""Unit tests for the Oracle Recruiting Cloud (ORC) / Candidate Experience apply strategy.

Pure logic only — NO network, NO browser, NO submission. Covers URL routing, the
ORC_ADVANCE gate (must be OFF by default so a plain fill is side-effect-free), the
deterministic truthful screener answers, option matching, and strategy registration.
"""
import asyncio

import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import GenericStrategy
from backend.applier.strategies.oracle_orc import OracleORCStrategy, _env_advance
from backend.tools import mass_hiring_apply

# The live sample apply URL from recon (Alorica on Oracle CX).
_ORC_URL = ("https://fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/"
            "CandidateExperience/en/sites/CX_1/job/239440")


# ---- strategy routing --------------------------------------------------------

def test_matches_orc_hosts():
    assert OracleORCStrategy.matches(_ORC_URL)
    # tolerant of case + a shortened /sites/<CX>/job/<id> shape
    assert OracleORCStrategy.matches(_ORC_URL.upper())
    assert OracleORCStrategy.matches(
        "https://fa-abcd.fa.ocs.oraclecloud.com/sites/CX_2/job/551")


def test_does_not_match_non_orc():
    # a greenhouse / avature / workday form, an empty URL, and a NON-CX oraclecloud host
    # (object storage / APEX / docs) must all NOT route here.
    assert not OracleORCStrategy.matches(
        "https://boards.greenhouse.io/embed/job_app?token=1")
    assert not OracleORCStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not OracleORCStrategy.matches(
        "https://acme.wd1.myworkdayjobs.com/en-US/careers")
    assert not OracleORCStrategy.matches("")
    assert not OracleORCStrategy.matches(
        "https://objectstorage.us.oraclecloud.com/n/foo/b/bucket/o/file.pdf")


def test_registered_and_picked_by_url():
    assert OracleORCStrategy in STRATEGIES
    # registered AFTER the specific ATS host strategies (order-independent — more host
    # strategies are appended over time), and the GenericStrategy fallback stays OUT of the list.
    assert GenericStrategy not in STRATEGIES
    picked = _pick_strategy(_ORC_URL)
    assert isinstance(picked, OracleORCStrategy)
    # a non-ORC URL must not accidentally route to ORC.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"),
                          OracleORCStrategy)


def test_is_a_generic_subclass_with_name():
    # extends GenericStrategy (so super().prefill resolves to the shared pipeline).
    assert issubclass(OracleORCStrategy, GenericStrategy)
    assert OracleORCStrategy.name == "oracle_orc"


def test_mass_hiring_apply_supports_oracle():
    assert mass_hiring_apply.is_supported(_ORC_URL)
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")  # unchanged
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- ORC_ADVANCE gate (live-submit switch) -----------------------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("ORC_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("ORC_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("ORC_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # The wizard-advance (which transmits PII + sends the application on the final Submit) must
    # be OFF unless ORC_ADVANCE is explicitly set — a plain fill stays side-effect-free.
    monkeypatch.delenv("ORC_ADVANCE", raising=False)

    class _Fresh(OracleORCStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- deterministic truthful screeners ----------------------------------------

def test_screener_answer_availability_and_eligibility():
    A = OracleORCStrategy._screener_answer
    # per-posting affirmative screeners a synthetic applicant DESIGNED to fit answers Yes
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("are you able to work an 8 hour shift between 7am-7pm cst?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 75 miles of the alorica remote site?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / conflict are truthfully No for a fresh authorized persona
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = OracleORCStrategy._screener_answer
    exp = A("how much experience do you have as a csr in a call center?", {})
    assert exp and exp[0] == "5+ years"           # strongest believable tier first
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = OracleORCStrategy._screener_answer
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
    assert OracleORCStrategy._screener_answer("describe a time you resolved a conflict", {}) is None
    assert OracleORCStrategy._screener_answer("what is your favorite color?", {}) is None


def test_screener_answer_alorica_questions():
    # The exact Alorica (Oracle CX) application-question set that was previously left unanswered.
    A = OracleORCStrategy._screener_answer
    # "Do you HAVE a High School Diploma, GED or equivalent?" is a Yes/No (not a level tier)
    assert A("do you have a high school diploma, ged or equivalent?", {}) == ["Yes"]
    # relatives / other members employed with the company → No (fresh synthetic persona)
    assert A("for security/confidentiality reasons, do you have other members currently "
             "employed with the company?", {}) == ["No"]
    # worked-for / provided-services-before → No
    assert A("have you ever worked for or provided services for alorica?", {}) == ["No"]
    # willing to undergo a background check → Yes
    assert A("upon job offer, are you willing to undergo a background check?", {}) == ["Yes"]
    # legally authorized to work → Yes
    assert A("are you legally authorized to work in the country where this job is located?",
             {}) == ["Yes"]


def test_screener_diploma_yesno_does_not_shadow_education_level():
    # the education-LEVEL tier question must still return a tier (not the diploma Yes/No), since it
    # is checked first — the diploma rule only catches the "do you HAVE a diploma" Yes/No form.
    A = OracleORCStrategy._screener_answer
    assert A("what is your highest level of education?", {})[0] == "Bachelor"
    assert A("highest level of education achieved?", {"education_level": "Associate"})[0] == "Associate"


def test_handle_wotc_optout_off_by_default(monkeypatch):
    # The WOTC opt-out navigates SAME-TAB to the flaky ADP partner; it is OPT-IN, so with no
    # ORC_WOTC_OPTOUT it must short-circuit BEFORE any page use (page=None would raise otherwise) —
    # the WOTC then stays a pending step in `unfilled` (harmless: the phone already blocks Submit).
    monkeypatch.delenv("ORC_WOTC_OPTOUT", raising=False)
    strat = OracleORCStrategy()
    assert asyncio.run(strat._handle_wotc(None, {})) is False


def test_handle_wotc_runs_once_per_fill(monkeypatch):
    # Even OPTED-IN, WOTC runs AT MOST once per fill (it navigates the tab; the form step + the
    # review step both call it, and a 2nd nav would stall) — the guard short-circuits the 2nd call.
    monkeypatch.setenv("ORC_WOTC_OPTOUT", "1")
    strat = OracleORCStrategy()
    strat._wotc_attempted = True
    assert asyncio.run(strat._handle_wotc(None, {})) is False


def test_opt_match_boundary():
    m = OracleORCStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False                # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False
