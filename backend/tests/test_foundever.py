"""Pure unit tests for the Foundever (SuccessFactors) apply lane (no network, no browser)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.applier.strategies.foundever import (  # noqa: E402
    SuccessFactorsStrategy,
    _phone_local,
    combobox_answer,
    job_question_answer,
    ssn_last6,
)

# The real combobox option lists captured live from the SuccessFactors careersection (2026-09-19).
_HEAR = ["No Selection", "Career Fair", "Company Website", "Craigslist", "Email", "Employee Referral",
         "Facebook", "Glassdoor", "HeyJobs", "Indeed", "LinkedIn"]
_PARTFULL = ["No Selection", "Both", "Full-time", "Part-time"]
_REFERRAL = ["No Selection", "No", "Yes"]
_GENDER = ["No Selection", "Female", "I do not wish to self-identify", "Male"]
_RACE = ["No Selection", "American Indian or Alaskan Native (not Hispanic or Latino)",
         "Asian (not Hispanic or Latino)", "Black or African American (not Hispanic or Latino)",
         "Hispanic or Latino", "I do not wish to self-identify",
         "White (not Hispanic or Latino)"]
_MILITARY = ["No Selection", "Active Duty Reserves", "I do not wish to self identify",
             "Never served in military", "Veteran"]
_VETERAN = ["No Selection", "I do not wish to identify", "No", "Yes"]
_EMPLOYED = ["No Selection", "Current Employee", "Former Employee", "Never Employed at this Company"]
_ASSESS = ["No Selection", "No", "Yes"]
_SMS = ["No Selection", "Agree", "Disagree"]
_ESIGN = ["No Selection", "I agree"]
_STATE = ["No Selection", "California", "Connecticut", "Texas"]
_COUNTRY = ["No Selection", "Canada", "Mexico", "United States",
            "United States Minor Outlying Islands"]


# ---- combobox_answer: screeners -----------------------------------------------------------------

def test_hear_about_us_prefers_company_website():
    assert combobox_answer("How did you hear about this position?:", _HEAR) == "Company Website"


def test_part_or_full_time_is_full_time():
    assert combobox_answer("Are you interested in a part-time or a full-time position?",
                           _PARTFULL) == "Full-time"


def test_employee_referral_is_no():
    assert combobox_answer("Did a current Foundever employee refer you to this position?",
                           _REFERRAL) == "No"


def test_ever_employed_here_is_never():
    assert combobox_answer("Have you ever been employed by Sykes or Sitel or Foundever...",
                           _EMPLOYED) == "Never Employed at this Company"


def test_assessment_willingness_is_yes():
    assert combobox_answer("As part of the Foundever hiring process you will need to complete an "
                           "application assessment", _ASSESS) == "Yes"


def test_sms_consent_is_agree():
    assert combobox_answer("I consent to receive text message communications from Foundever",
                           _SMS) == "Agree"


def test_esignature_consent_is_i_agree():
    assert combobox_answer("Electronic Signature Consent", _ESIGN) == "I agree"


# ---- combobox_answer: EEO always declines (never a protected characteristic) ---------------------

def test_gender_declines():
    assert combobox_answer("Gender", _GENDER) == "I do not wish to self-identify"


def test_race_declines():
    assert combobox_answer("Race or Ethnicity (select one, see below for definitions)",
                           _RACE) == "I do not wish to self-identify"


def test_military_declines_exact_wording():
    # note the different wording ('self identify', no hyphen) — must still match the decline regex
    assert combobox_answer("What is your military status?", _MILITARY) == "I do not wish to self identify"


def test_veteran_and_disability_decline():
    for lbl in ("Do you identify as a Protected Veteran?", "Do you identify as a Disabled Veteran?",
                "Do you identify as an individual with a Disability?"):
        assert combobox_answer(lbl, _VETERAN) == "I do not wish to identify"


def test_race_never_returns_a_characteristic():
    # a synthetic persona must NEVER be assigned a real race/ethnicity
    ans = combobox_answer("Race or Ethnicity", _RACE)
    assert "wish" in ans.lower() and "hispanic" not in ans.lower().split("i do")[0]


# ---- combobox_answer: address country/state -----------------------------------------------------

def test_country_is_united_states_exact():
    # must be the exact 'United States', never 'United States Minor Outlying Islands'
    assert combobox_answer("Country:", _COUNTRY) == "United States"


def test_state_matches_persona_state():
    assert combobox_answer("State", _STATE, state="Connecticut") == "Connecticut"


def test_state_returns_none_without_persona_state():
    assert combobox_answer("State", _STATE, state="") is None


# ---- job_question_answer ------------------------------------------------------------------------

def test_standard_screeners_are_yes():
    for p in ("Do you have a minimum of one year customer service experience?",
              "Are you available to work evening hours?",
              "Can you provide hard wired internet? No satellite or WIFI is permitted.",
              "Are you at least 18 years of age?",
              "Do you have a High School Diploma or GED?",
              "Are you authorized to work in the country this position resides in?"):
        assert job_question_answer(p, "Connecticut") == "Yes"


def test_residence_question_is_yes_only_in_state():
    q = "Do you currently reside in the State of Connecticut?"
    assert job_question_answer(q, "Connecticut") == "Yes"
    assert job_question_answer(q, "Texas") == "No"


def test_negative_polarity_question_is_no():
    assert job_question_answer("Have you ever been convicted of a felony?", "Ohio") == "No"


# ---- ssn / phone helpers ------------------------------------------------------------------------

def test_ssn_last6_is_deterministic_six_digits():
    a = ssn_last6({"email": "jane.doe1@takhet.com"})
    b = ssn_last6({"email": "jane.doe1@takhet.com"})
    assert a == b and len(a) == 6 and a.isdigit()
    assert ssn_last6({"email": "other@takhet.com"}) != a


def test_phone_local_strips_country_code_and_formatting():
    assert _phone_local("+1 (507) 555-0143") == "5075550143"
    assert _phone_local("1-765-555-0116") == "7655550116"
    assert _phone_local("(212) 555-0100") == "2125550100"


# ---- strategy URL matching ----------------------------------------------------------------------

def test_matches_rmk_and_careersection():
    m = SuccessFactorsStrategy.matches
    assert m("https://jobs.foundever.com/job/Remote-CSR-Connecticut/1357356800/")
    assert m("https://career4.successfactors.com/careers?company=SitelPROD")
    assert not m("https://boards.greenhouse.io/acme/jobs/123")
    assert not m("")


# ---- driver eligibility (state placement + licensed skip) ---------------------------------------

def test_state_from_row_reads_location_and_tolerates_typo():
    from backend.tools.foundever_recon import _state_from_row
    assert _state_from_row("Healthcare CSR - Connecticut", "Remote, Conneticut, US") == "Connecticut"
    assert _state_from_row("CSR - Mississippi", "Remote, Mississippi, US") == "Mississippi"
    assert _state_from_row("Bilingual CSR", "Remote, Any Location, US") == ""


def test_licensed_roles_skipped():
    from backend.tools.foundever_recon import is_licensed
    assert is_licensed("Remote Licensed Customer Service Representative")
    assert is_licensed("Licensed Property & Casualty Insurance Agent")
    assert not is_licensed("Healthcare Customer Service Associate - Connecticut")
