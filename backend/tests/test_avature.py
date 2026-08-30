"""Unit tests for the Avature apply strategy + the synth-persona US-state fix (no network)."""
import re

from backend.applier.strategies.avature import AvatureStrategy, _gen_password
from backend.tools.synth_persona import _build_candidate, _us_state_full


# ---- strategy routing --------------------------------------------------------

def test_matches_avature_hosts():
    assert AvatureStrategy.matches("https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert AvatureStrategy.matches("https://foo.AVATURE.net/careers/Register")
    assert not AvatureStrategy.matches("https://boards.greenhouse.io/embed/job_app?token=1")
    assert not AvatureStrategy.matches("")


def test_advance_off_by_default():
    # The wizard-advance (which transmits PII + creates the account on submit) must be OFF unless
    # AVATURE_ADVANCE is explicitly set — a plain fill stays side-effect-free at the employer.
    assert AvatureStrategy().advance_wizard is False


def test_generated_password_meets_complexity():
    for _ in range(20):
        pw = _gen_password()
        assert len(pw) >= 10
        assert re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


# ---- synth-persona US state coherence ---------------------------------------

def test_us_state_full():
    assert _us_state_full("TX") == "Texas"
    assert _us_state_full("ok") == "Oklahoma"
    assert _us_state_full("Florida") == "Florida"
    assert _us_state_full("Ontario") == ""
    assert _us_state_full("") == ""


def _job():
    return {"title": "Customer Service Representative", "location": "United States"}


def test_persona_state_parsed_from_city():
    p = _build_candidate({"full_name": "Jane Doe", "city": "Miami, FL"}, "United States", _job())
    assert p["profile"]["state"] == "Florida"
    assert p["profile"]["city"] == "Miami"


def test_persona_state_backfilled_when_missing():
    # A bare US city with no state -> a coherent (city, state) from the bank, never empty.
    p = _build_candidate({"full_name": "Jane Doe", "city": ""}, "United States", _job())
    assert p["profile"]["state"] in {"Texas", "Colorado", "Ohio", "Washington"}
    assert p["profile"]["city"]


def test_non_us_persona_has_no_state():
    p = _build_candidate({"full_name": "João Silva", "city": "São Paulo"}, "Brazil", _job())
    assert p["profile"]["state"] == ""
    assert p["profile"]["city"] == "São Paulo"


# ---- step-3 radio screeners (job screening questions) -------------------------

def test_screener_answer_availability_and_eligibility():
    A = AvatureStrategy._screener_answer
    # per-posting Compliance-step screeners a synthetic applicant answers affirmatively
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("work an 8 hour shift between 7am-7pm cst. are you able to meet this requirement?", {}) == ["Yes"]
    assert A("obtain a federal clearance (medium risk public trust). able to meet this requirement?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this requirement?", {}) == ["Yes"]
    assert A("do you reside within 75 miles of the maximus lawrence kansas location?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    # a conflict/commitment screener is still truthfully No
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]
    # CSR experience is a multi-option, not Yes/No
    exp = A("how much experience do you have as a tier i csr in a call center?", {})
    assert exp and any("year" in c.lower() for c in exp)


def test_opt_match_boundary():
    m = AvatureStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False               # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False


def test_screener_answer_supervisor_experience():
    A = AvatureStrategy._screener_answer
    # the Supervisor - Call Center role asks leadership experience, not customer-service
    vals = A("how much supervisor or leadership experience do you have?", {})
    assert vals and any("year" in v.lower() for v in vals)
    # the CSR customer-service experience question still resolves
    vals2 = A("how much experience do you have in a customer service environment as a tier i csr?", {})
    assert vals2 and vals2[0] == "5+ years"   # ETALON: strongest tier first
