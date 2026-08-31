"""Unit tests for the Workday Mass-Hiring auto-apply strategy (no network, no browser, no submit).

Covers URL routing (the 4 validated CxS tenants → WorkdayMassHiringStrategy; the stock
WorkdayStrategy still matches every Workday host and keeps its /catalog behaviour byte-identical
by NOT overriding prefill), the WORKDAY_ADVANCE gate (OFF by default so a plain fill is
side-effect-free AND /catalog is untouched), the deterministic truthful screener answers, option
matching, the demographic regex, password complexity, and strategy registration.
"""
import re

import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import ApplyStrategy
from backend.applier.strategies.workday import (
    WorkdayMassHiringStrategy,
    WorkdayStrategy,
    _DEMOGRAPHIC_RE,
    _MASSHIRING_HOST_RE,
    _env_advance,
    _gen_password,
)
from backend.tools import mass_hiring_apply

# Live sample apply URLs (from the DB, mass_hiring_jobs) — one per Mass-Hiring tenant.
_CNX = ("https://cnx.wd1.myworkdayjobs.com/en-US/external_global/job/USA-Work-at-Home/"
        "Seasonal-Licensed-Health-Insurance-Rep--Remote---Evergreen-_R1732661")
_CVS = ("https://cvshealth.wd1.myworkdayjobs.com/en-US/CVS_Health_Careers/job/"
        "TX---Work-from-home/Member-Engagement-Service-Coordinator_R0957391-1")
_CENTENE = ("https://centene.wd5.myworkdayjobs.com/en-US/Centene_External/job/Remote-AR/"
            "Care-Coordinator-II_1643171-1")
_CIGNA = ("https://cigna.wd5.myworkdayjobs.com/en-US/cignacareers/job/Tennessee-Work-at-Home/"
          "Customer-Service-Representative---Accredo---Remote_26009553")
_TENANTS = (_CNX, _CVS, _CENTENE, _CIGNA)
# A /catalog Workday job on some OTHER tenant, and Humana (handled by PhenomWorkdayStrategy).
_GENERIC_WD = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Remote/Engineer_R1"
_HUMANA = "https://humana.wd5.myworkdayjobs.com/en-US/Humana_External/job/Remote/CSR_R2"


# ---- strategy routing --------------------------------------------------------

def test_masshiring_matches_the_four_tenants():
    for u in _TENANTS:
        assert WorkdayMassHiringStrategy.matches(u), u
        assert WorkdayMassHiringStrategy.matches(u.upper()), u  # case-tolerant


def test_masshiring_does_not_match_other_workday_or_non_workday():
    # a generic /catalog Workday tenant, Humana, a greenhouse form, and empty -> NOT mass-hiring.
    assert not WorkdayMassHiringStrategy.matches(_GENERIC_WD)
    assert not WorkdayMassHiringStrategy.matches(_HUMANA)
    assert not WorkdayMassHiringStrategy.matches(
        "https://boards.greenhouse.io/embed/job_app?token=1")
    assert not WorkdayMassHiringStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not WorkdayMassHiringStrategy.matches("")


def test_stock_workday_still_matches_every_workday_host():
    # The stock strategy's broad matching is UNCHANGED — it must still catch the 4 tenants
    # (routing order is what sends them to the subclass), generic tenants, humana, and .workday.com.
    for u in (*_TENANTS, _GENERIC_WD, _HUMANA, "https://x.workday.com/job"):
        assert WorkdayStrategy.matches(u), u
    assert not WorkdayStrategy.matches("https://boards.greenhouse.io/x")
    assert not WorkdayStrategy.matches("")


def test_pick_strategy_routing():
    for u in _TENANTS:
        assert isinstance(_pick_strategy(u), WorkdayMassHiringStrategy), u
    # a generic Workday tenant falls through to the stock strategy (NOT the mass-hiring subclass).
    picked = _pick_strategy(_GENERIC_WD)
    assert isinstance(picked, WorkdayStrategy)
    assert not isinstance(picked, WorkdayMassHiringStrategy)
    # a greenhouse URL never routes to any Workday strategy.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"), WorkdayStrategy)


def test_registered_before_stock_workday():
    assert WorkdayStrategy in STRATEGIES
    assert WorkdayMassHiringStrategy in STRATEGIES
    # the subclass MUST be earlier than the broad stock class, else the stock class would win.
    assert STRATEGIES.index(WorkdayMassHiringStrategy) < STRATEGIES.index(WorkdayStrategy)


def test_masshiring_is_a_workday_subclass_with_name():
    assert issubclass(WorkdayMassHiringStrategy, WorkdayStrategy)
    assert WorkdayMassHiringStrategy.name == "workday_masshiring"
    assert WorkdayStrategy.name == "workday"


# ---- /catalog byte-identical guarantee ---------------------------------------

def test_stock_workday_has_no_prefill_override():
    # CRITICAL: the stock WorkdayStrategy must NOT override prefill — so a /catalog Workday fill
    # resolves to the shared base pipeline exactly as before, AND phenom.PhenomWorkdayStrategy's
    # super().prefill() (which it documents as resolving to base.prefill) keeps working.
    assert WorkdayStrategy.prefill is ApplyStrategy.prefill
    # the mass-hiring subclass DOES override prefill (its account-create + wizard-walk flow).
    assert WorkdayMassHiringStrategy.prefill is not ApplyStrategy.prefill


def test_mass_hiring_apply_supports_the_four_tenants():
    for u in _TENANTS:
        assert mass_hiring_apply.is_supported(u), u
    # unchanged: the other supported hosts still resolve.
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert mass_hiring_apply.is_supported(_HUMANA)  # humana.wd5 is separately listed
    # a NON-listed Workday tenant is NOT silently attempted (tenant-specific, not blanket).
    assert not mass_hiring_apply.is_supported(_GENERIC_WD)
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- WORKDAY_ADVANCE gate (live-submit switch) -------------------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("WORKDAY_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("WORKDAY_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("WORKDAY_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # The account-create + wizard-walk (which transmits PII + creates the account + sends the
    # application on the final Submit) must be OFF unless WORKDAY_ADVANCE is explicitly set.
    monkeypatch.delenv("WORKDAY_ADVANCE", raising=False)

    class _Fresh(WorkdayMassHiringStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- account password --------------------------------------------------------

def test_generated_password_meets_complexity():
    for _ in range(20):
        pw = _gen_password()
        assert len(pw) >= 10
        assert re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


# ---- deterministic truthful screeners ----------------------------------------

def test_screener_answer_availability_and_eligibility():
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("are you able to work an 8 hour shift between 7am-7pm cst?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 50 miles of the remote hub?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / conflict are truthfully No for a fresh authorized persona
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = WorkdayMassHiringStrategy._screener_answer
    # member-services phrasing (CVS/Centene/Cigna) resolves via the customer/member lexicon
    exp = A("how many years of member services experience do you have?", {})
    assert exp and exp[0] == "5+ years"
    csr = A("how much experience do you have in a call center as a customer service rep?", {})
    assert csr and csr[0] == "5+ years"
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("what is your english proficiency?", {})[0] == "Native"
    assert A("what is your spanish proficiency?", {"bilingual": True})[0] == "Fluent"
    assert A("what is your spanish proficiency?", {})[0] in ("None", "No proficiency")
    assert A("what is your highest level of education?", {"education_level": "Associate"})[0] == "Associate"
    assert A("highest level of education achieved?", {})[0] == "Bachelor"


def test_screener_answer_unknown_returns_none():
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("describe a time you resolved a conflict", {}) is None
    assert A("what is your favorite color?", {}) is None


def test_opt_match_boundary():
    m = WorkdayMassHiringStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False               # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False


# ---- pure regexes ------------------------------------------------------------

def test_demographic_regex():
    for lbl in ("What is your gender?", "Race/Ethnicity", "Are you Hispanic or Latino?",
                "Veteran status", "Disability status", "Please self-identify"):
        assert _DEMOGRAPHIC_RE.search(lbl), lbl
    # a geography screener that merely contains "Latin American" is NOT a demographic
    assert not _DEMOGRAPHIC_RE.search("Are you based in a Latin American country?")
    assert not _DEMOGRAPHIC_RE.search("How many years of customer service experience?")


def test_masshiring_host_regex_is_tenant_specific():
    for u in _TENANTS:
        assert _MASSHIRING_HOST_RE.search(u), u
    assert not _MASSHIRING_HOST_RE.search(_GENERIC_WD)
    assert not _MASSHIRING_HOST_RE.search(_HUMANA)
    assert not _MASSHIRING_HOST_RE.search("https://cnx.wd1.example.com/job")
