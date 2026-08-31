"""Unit tests for the Phenom Mass Hiring ATS family apply strategy (Conduent + Humana).

Pure logic only — NO network, NO browser, NO submission. Covers URL routing for both
sub-families, the per-ATS advance gates (must be OFF by default so a plain fill is
side-effect-free), the byte-identical stock-Workday fallback for non-Humana hosts,
strategy registration/order, and that the Conduent path reuses Oracle's truthful screeners.
"""
import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.oracle_orc import OracleORCStrategy
from backend.applier.strategies.phenom import (
    PhenomStrategy,
    PhenomWorkdayStrategy,
    _env_advance,
    _env_workday_advance,
    _gen_password,
)
from backend.applier.strategies.workday import WorkdayStrategy
from backend.tools import mass_hiring_apply

# Live sample apply URLs from the DB (recon).
_CONDUENT_URL = "https://careers.conduent.com/us/en/job/25588"
_HUMANA_URL = ("https://humana.wd5.myworkdayjobs.com/Humana_External_Career_Site/"
               "job/Remote-Indiana/Indiana-Medicaid-Inbound-Contact-Representative_R-422616/apply")
_CENTERWELL_URL = ("https://humana.wd5.myworkdayjobs.com/CenterWell_External_Career_Site/"
                   "job/Remote-Ohio/Sales-Support-Representative-2_R-423061/apply")
# A NON-Humana, unclaimed Workday host — must stay on the stock WorkdayStrategy and NOT be
# auto-captured by the Phenom family. (A concrete tenant like cnx/cvshealth is deliberately
# NOT used here: the Workday CxS mass-hiring lane legitimately supports those separately.)
_OTHER_WD = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/123"


# ---- strategy routing: Conduent (Phenom → Oracle) ----------------------------

def test_phenom_matches_conduent():
    assert PhenomStrategy.matches(_CONDUENT_URL)
    assert PhenomStrategy.matches(_CONDUENT_URL.upper())          # case-tolerant
    assert PhenomStrategy.matches("https://careers.conduent.com/us/en/job/24642")


def test_phenom_does_not_match_others():
    # Humana Workday, a non-Phenom ATS, and an empty URL must NOT route to the Conduent path.
    assert not PhenomStrategy.matches(_HUMANA_URL)
    assert not PhenomStrategy.matches(_OTHER_WD)
    assert not PhenomStrategy.matches("https://boards.greenhouse.io/embed/job_app?token=1")
    assert not PhenomStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not PhenomStrategy.matches("")


# ---- strategy routing: Humana (Phenom discovery → Workday) -------------------

def test_phenom_workday_matches_humana_only():
    assert PhenomWorkdayStrategy.matches(_HUMANA_URL)
    assert PhenomWorkdayStrategy.matches(_CENTERWELL_URL)         # CenterWell tenant too
    assert PhenomWorkdayStrategy.matches(_HUMANA_URL.upper())     # case-tolerant
    # a hypothetical humana.wd1 host still routes here (tenant-number tolerant)
    assert PhenomWorkdayStrategy.matches(
        "https://humana.wd1.myworkdayjobs.com/Some_Site/job/x/apply")


def test_phenom_workday_does_not_match_other_workday():
    # A NON-Humana Workday host is the stock WorkdayStrategy's job — never PhenomWorkday's.
    assert not PhenomWorkdayStrategy.matches(_OTHER_WD)
    assert not PhenomWorkdayStrategy.matches(
        "https://cvshealth.wd1.myworkdayjobs.com/CVS_Health_Careers/job/1")
    assert not PhenomWorkdayStrategy.matches(_CONDUENT_URL)
    assert not PhenomWorkdayStrategy.matches("")


# ---- registration + pick order ----------------------------------------------

def test_registered():
    assert PhenomStrategy in STRATEGIES
    assert PhenomWorkdayStrategy in STRATEGIES


def test_phenom_workday_precedes_workday_in_registry():
    # Humana's apply_url is a Workday URL, so PhenomWorkdayStrategy MUST be checked before the
    # stock WorkdayStrategy — otherwise Humana would route to Workday and lose the account path.
    assert STRATEGIES.index(PhenomWorkdayStrategy) < STRATEGIES.index(WorkdayStrategy)


def test_pick_conduent_routes_to_phenom():
    assert isinstance(_pick_strategy(_CONDUENT_URL), PhenomStrategy)


def test_pick_humana_routes_to_phenom_workday_not_workday():
    picked = _pick_strategy(_HUMANA_URL)
    assert isinstance(picked, PhenomWorkdayStrategy)
    # PhenomWorkdayStrategy IS a WorkdayStrategy subclass, so assert it's the SPECIALIZED one,
    # not the plain base, by exact type.
    assert type(picked) is PhenomWorkdayStrategy


def test_pick_other_workday_stays_stock():
    picked = _pick_strategy(_OTHER_WD)
    assert type(picked) is WorkdayStrategy          # NOT PhenomWorkdayStrategy


def test_pick_non_phenom_urls():
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"), PhenomStrategy)
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"),
                          PhenomWorkdayStrategy)


# ---- inheritance + names -----------------------------------------------------

def test_phenom_subclasses_oracle():
    # Conduent reuses the whole Oracle CX machinery (JET widgets / screeners / wizard).
    assert issubclass(PhenomStrategy, OracleORCStrategy)
    assert PhenomStrategy.name == "phenom"


def test_phenom_workday_subclasses_workday():
    assert issubclass(PhenomWorkdayStrategy, WorkdayStrategy)
    assert PhenomWorkdayStrategy.name == "phenom_workday"


# ---- advance gates (live-submit switches, default OFF) -----------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("PHENOM_ADVANCE", raising=False)
    monkeypatch.delenv("ORC_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.delenv("ORC_ADVANCE", raising=False)
    monkeypatch.setenv("PHENOM_ADVANCE", val)
    assert _env_advance() is True


def test_env_advance_also_enabled_by_orc_advance(monkeypatch):
    # The shared Mass Hiring batch sets ORC_ADVANCE; a Conduent job in that batch must walk too.
    monkeypatch.delenv("PHENOM_ADVANCE", raising=False)
    monkeypatch.setenv("ORC_ADVANCE", "1")
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.delenv("ORC_ADVANCE", raising=False)
    monkeypatch.setenv("PHENOM_ADVANCE", val)
    assert _env_advance() is False


def test_env_workday_advance_default_off(monkeypatch):
    monkeypatch.delenv("WORKDAY_ADVANCE", raising=False)
    monkeypatch.delenv("PHENOM_ADVANCE", raising=False)
    assert _env_workday_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_env_workday_advance_truthy(monkeypatch, val):
    monkeypatch.delenv("PHENOM_ADVANCE", raising=False)
    monkeypatch.setenv("WORKDAY_ADVANCE", val)
    assert _env_workday_advance() is True


def test_env_workday_advance_also_enabled_by_phenom_advance(monkeypatch):
    monkeypatch.delenv("WORKDAY_ADVANCE", raising=False)
    monkeypatch.setenv("PHENOM_ADVANCE", "1")
    assert _env_workday_advance() is True


def test_advance_off_by_default_both(monkeypatch):
    # The wizard advance (which transmits PII / creates the account / sends on final Submit) must
    # be OFF unless its env flag is explicitly set — a plain fill stays side-effect-free.
    for name in ("PHENOM_ADVANCE", "ORC_ADVANCE", "WORKDAY_ADVANCE"):
        monkeypatch.delenv(name, raising=False)

    class _FreshP(PhenomStrategy):
        advance_wizard = _env_advance()

    class _FreshW(PhenomWorkdayStrategy):
        advance_wizard = _env_workday_advance()

    assert _FreshP().advance_wizard is False
    assert _FreshW().advance_wizard is False


# ---- helpers -----------------------------------------------------------------

def test_generated_password_meets_complexity():
    import re
    for _ in range(20):
        pw = _gen_password()
        assert len(pw) >= 10
        assert re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


# ---- mass_hiring_apply support gate ------------------------------------------

def test_mass_hiring_apply_supports_phenom_family():
    assert mass_hiring_apply.is_supported(_CONDUENT_URL)          # Conduent
    assert mass_hiring_apply.is_supported(_HUMANA_URL)            # Humana Workday
    assert mass_hiring_apply.is_supported(_CENTERWELL_URL)        # CenterWell Workday
    # existing supported hosts are unaffected
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    # a NON-Humana Workday host is NOT auto-fill-supported (Concentrix/CVS stay human-apply)
    assert not mass_hiring_apply.is_supported(_OTHER_WD)
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- Conduent reuses Oracle's truthful screeners -----------------------------

def test_phenom_inherits_truthful_screeners():
    A = PhenomStrategy._screener_answer
    # a US persona: native English, authorized, no sponsorship — the same deterministic answers
    # Oracle ORC uses (Conduent's backend IS Oracle HCM).
    assert A("what is your english proficiency?", {})[0] == "Native"
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # an unrecognized/behavioral prompt is left for the human, never guessed
    assert A("describe a time you resolved a conflict", {}) is None
