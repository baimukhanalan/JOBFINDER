"""Pure unit tests for the Foundever (SuccessFactors) apply lane (no network, no browser)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.applier.strategies.foundever import (  # noqa: E402
    SuccessFactorsStrategy,
    _phone_local,
    combobox_answer,
    job_question_answer,
    portal_text_value,
    ssn_last6,
)

# Gainwell "portalcareer" screener combobox option lists (captured live 2026-09-21).
_YESNO = ["No Selection", "No", "Yes"]
_TRAVEL = ["No Selection", "0 - 10%", "11 - 25%", "26 - 50%", "51 - 75%"]
_PHONE_TYPE = ["No Selection", "Business", "Cell", "Home", "Work"]
_HEAR_DETAIL = ["No Selection", "Company Website", "Job Board", "Referral"]

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


# ---- combobox_answer: Gainwell portalcareer eligibility/screener comboboxes ---------------------

def test_work_authorized_is_yes_even_though_label_mentions_country():
    # the label literally contains "country of the job" — it must answer Yes, NOT "United States"
    q = "Are you legally authorized to work in the country of the job for which you are applying?"
    assert combobox_answer(q, _YESNO) == "Yes"


def test_sponsorship_is_no():
    q = "Will you now or in the future require sponsorship for an employment Visa?"
    assert combobox_answer(q, _YESNO) == "No"


def test_current_or_former_employee_is_no():
    assert combobox_answer("Current or former employee?", _YESNO) == "No"


def test_family_or_friends_at_gainwell_is_no():
    q = "Do any of your family members or close personal friends work for Gainwell?"
    assert combobox_answer(q, _YESNO) == "No"


def test_noncompete_agreement_is_no():
    q = ("Have you signed an agreement in the last two years that might restrict your ability to work "
         "for Gainwell?")
    assert combobox_answer(q, _YESNO) == "No"


def test_willingness_to_travel_picks_lowest():
    assert combobox_answer("Willingness to travel", _TRAVEL) == "0 - 10%"


def test_phone_type_prefers_cell_when_no_mobile():
    assert combobox_answer("Primary Phone Type", _PHONE_TYPE) == "Cell"


def test_how_hear_details_takes_first_real_option():
    assert combobox_answer("Details", _HEAR_DETAIL) == "Company Website"


def test_generic_yesno_screener_defaults_yes():
    # an unrecognised Yes/No screener combobox (job-specific) -> Yes for an affirmative question
    q = ("This role requires entering and validating claims-related information while meeting "
         "productivity and quality standards. Are you able to work in this environment?")
    assert combobox_answer(q, _YESNO) == "Yes"


def test_generic_yesno_screener_is_no_for_negative_polarity():
    assert combobox_answer("Have you ever been convicted of a felony?", _YESNO) == "No"


def test_new_branches_do_not_disturb_foundever_referral():
    # the Foundever "current ... employee refer you" label must NOT be caught by the new
    # current-or-former-employee branch (it has no "or former")
    assert combobox_answer("Did a current Foundever employee refer you to this position?",
                           _REFERRAL) == "No"


# ---- portal_text_value: label-driven text fields on the portalcareer page ------------------------

_PF = {"email": "ann.bell1@takhet.com", "first_name": "Ann", "last_name": "Bell",
       "full_name": "Ann Bell", "street_address": "12 Oak St", "address": "12 Oak St",
       "city": "Austin", "zip": "78701", "postal_code": "78701"}
_EX = {"phone_local": "5125550100", "company": "Acme Support Co", "title": "CSR",
       "salary": "18", "years": "3"}


def test_portal_text_identity_and_address():
    assert portal_text_value("* Address", _PF, _EX) == "12 Oak St"
    assert portal_text_value("* City", _PF, _EX) == "Austin"
    assert portal_text_value("* Postal Code", _PF, _EX) == "78701"
    assert portal_text_value("* Legal First Name", _PF, _EX) == "Ann"
    assert portal_text_value("* Legal Last Name", _PF, _EX) == "Bell"
    assert portal_text_value("* Email", _PF, _EX) == "ann.bell1@takhet.com"


def test_portal_text_phone_company_title_signature():
    assert portal_text_value("* Primary Phone", _PF, _EX) == "5125550100"
    assert portal_text_value("* Current Company", _PF, _EX) == "Acme Support Co"
    assert portal_text_value("* Current Title", _PF, _EX) == "CSR"
    assert portal_text_value("* Typed Signature", _PF, _EX) == "Ann Bell"


def test_portal_text_years_and_salary():
    assert portal_text_value(
        "* How many years of call center or high-volume customer service experience do you have?",
        _PF, _EX) == "3"
    assert portal_text_value("* What is your expected hourly salary for this role?", _PF, _EX) == "18"


def test_portal_text_skips_optional_and_conditional():
    assert portal_text_value("Address 2", _PF, _EX) is None
    assert portal_text_value("Middle Name", _PF, _EX) is None
    assert portal_text_value("Alternate Phone", _PF, _EX) is None
    assert portal_text_value("If yes, please indicate Visa status", _PF, _EX) is None


def test_portal_text_capability_question_affirms():
    q = ("* This role requires entering and validating claims-related information while meeting "
         "productivity and quality standards. Are you able to work in this environment?")
    assert portal_text_value(q, _PF, _EX) == "Yes"
    assert portal_text_value("Are you willing to work weekends?", _PF, _EX) == "Yes"


def test_portal_text_open_question_left_blank():
    # an open "describe/what" prompt is NOT auto-answered "Yes" (left for review / job skipped)
    assert portal_text_value("Describe your customer service experience", _PF, _EX) is None
    assert portal_text_value("What interests you about this role?", _PF, _EX) is None


def test_portal_text_typing_speed_is_a_number_not_yes():
    # a compound "This role requires … What is your average typing speed?" field validates as a NUMBER
    q = ("* This role requires entering and validating claims-related information while meeting "
         "production and quality standards. What is your average typing speed?")
    v = portal_text_value(q, _PF, _EX)
    assert v and v.isdigit()


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
    # Gainwell shares the strategy: its RMK host + its SAP-branded careersection pod (career41.sapsf.com)
    assert m("https://jobs.gainwelltechnologies.com/job/Any-city-Healthcare-CSR-Remote-WI-99999/1426420500/")
    assert m("https://career41.sapsf.com/careers?company=gainwellte")
    assert m("https://career2.sapsf.com/careers?company=other")
    assert not m("https://boards.greenhouse.io/acme/jobs/123")
    assert not m("")


def test_careersection_and_rmk_host_helpers():
    from backend.applier.strategies.foundever import _on_careersection, _on_rmk
    assert _on_careersection("https://career4.successfactors.com/careers?company=SitelPROD")
    assert _on_careersection("https://career41.sapsf.com/careers?company=gainwellte")
    assert not _on_careersection("https://jobs.gainwelltechnologies.com/job/x/1/")
    assert _on_rmk("https://jobs.foundever.com/job/x/1/")
    assert _on_rmk("https://jobs.gainwelltechnologies.com/job/x/1/")
    assert not _on_rmk("https://career41.sapsf.com/careers?company=gainwellte")


def test_gainwell_state_from_row_reads_two_letter_code():
    from backend.tools.gainwell_recon import _state_from_row, gainwell_job_ids  # noqa: F401
    # Gainwell's location is "<city>, <state-code>, US, <zip>" — parts[1] 2-letter code resolves
    assert _state_from_row("Healthcare Data Entry Specialist - Remote MT", "Any city, MT, US, 99999") == "Montana"
    assert _state_from_row("CSR (Healthcare) - Remote California", "Any city, CA, US, 99999") == "California"
    assert _state_from_row("Provider Enrollment - REMOTE", "Hamilton, NJ, US, 08619-1288") == "New Jersey"


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
