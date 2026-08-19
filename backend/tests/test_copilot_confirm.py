"""looks_submitted: the co-pilot's read-only submit-confirmation matcher.

The text regex is a copy of extension/content.js CONFIRM_RE — if these tests
start disagreeing with the extension's behavior, re-sync the two.
"""
from backend.copilot import looks_submitted

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/4501234"


def test_thank_you_for_applying_matches():
    assert looks_submitted("Thank you for applying to Acme Corp!", APPLY_URL)


def test_application_submitted_matches():
    assert looks_submitted("Application submitted", APPLY_URL)
    assert looks_submitted("Your application has been submitted.", APPLY_URL)
    assert looks_submitted("Successfully submitted — we'll be in touch.", APPLY_URL)


def test_application_received_matches():
    assert looks_submitted("We have received your application.", APPLY_URL)
    assert looks_submitted("Application received", APPLY_URL)


def test_case_insensitive():
    assert looks_submitted("THANK YOU FOR APPLYING", APPLY_URL)


def test_url_heuristic_matches_without_text():
    assert looks_submitted("some unrelated page", "https://jobs.lever.co/acme/thanks")
    assert looks_submitted("", "https://apply.workable.com/acme/confirmation")


def test_jobs_listing_page_does_not_match():
    listing = (
        "Senior Python Engineer — Remote (US)\n"
        "Acme Corp · Full-time · Engineering\n"
        "We build the application platform behind thousands of teams.\n"
        "Apply for this job\n"
        "First Name * Last Name * Email * Phone Resume/CV\n"
        "Submit your details below and our team will review your application.\n"
    )
    assert not looks_submitted(listing, APPLY_URL)


def test_empty_inputs_do_not_match():
    assert not looks_submitted("", "")
    assert not looks_submitted(None, None)
