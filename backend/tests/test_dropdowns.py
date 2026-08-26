"""Pure unit tests for dropdowns._value_for rule matching (B2a/B2b) and
the _CLOSED open-type routing constant (B1).

No browser, no LLM.
"""
import pytest

from backend.applier.dropdowns import _value_for
from backend.applier.strategies.base import _CLOSED


# ---------------------------------------------------------------------------
# B2a: criminal-record phrasing must NOT auto-answer "Yes"
# ---------------------------------------------------------------------------

def test_criminal_record_question_returns_none():
    """'Do you have a criminal record?' should NOT be answered — returns None."""
    assert _value_for("Do you have a criminal record?") is None


def test_criminal_history_question_returns_none():
    """'Have you ever had a criminal history?' should NOT be answered."""
    assert _value_for("Have you ever had a criminal history?") is None


def test_background_check_consent_returns_yes():
    """'Are you willing to undergo a background check?' IS a consent question -> Yes."""
    assert _value_for("Are you willing to undergo a background check?") == "Yes"


def test_consent_to_background_screen_returns_yes():
    """'Do you agree to a background screen?' -> Yes."""
    assert _value_for("Do you agree to a background screen?") == "Yes"


def test_comfortable_background_check_returns_yes():
    """'Are you comfortable with a background check?' -> Yes."""
    assert _value_for("Are you comfortable with a background check?") == "Yes"


# ---------------------------------------------------------------------------
# B2b: visa phrasing — holding a valid visa must NOT auto-answer "No"
# ---------------------------------------------------------------------------

def test_hold_valid_us_visa_returns_none():
    """'Do you currently hold a valid US work visa?' should NOT be answered."""
    assert _value_for("Do you currently hold a valid US work visa?") is None


def test_require_visa_sponsorship_returns_no():
    """'Will you require visa sponsorship?' is sponsorship-need phrasing -> No."""
    assert _value_for("Will you require visa sponsorship?") == "No"


def test_require_sponsorship_now_or_future_returns_no():
    """'Will you need sponsorship now or in the future?' -> No."""
    assert _value_for("Will you need sponsorship now or in the future?") == "No"


def test_visa_sponsor_phrasing_returns_no():
    """'Do you require visa sponsorship to work in the US?' -> No."""
    assert _value_for("Do you require visa sponsorship to work in the US?") == "No"


# ---------------------------------------------------------------------------
# B1: _CLOSED constant — open types (email, input, number) must NOT be in _CLOSED
# ---------------------------------------------------------------------------

def test_closed_excludes_open_input_types():
    """Types the extractor emits for plain inputs must route to open-text drafting."""
    for open_type in ("input", "email", "number", "tel", "url", "search", "text", "textarea", ""):
        assert open_type not in _CLOSED, (
            f"type {open_type!r} incorrectly in _CLOSED — would block open-text drafting"
        )


def test_closed_includes_binary_and_file_types():
    """These types carry discrete options or are file uploads — must stay in _CLOSED."""
    for closed_type in ("select", "select-one", "radio_group", "checkbox_group",
                        "radio", "checkbox", "file"):
        assert closed_type in _CLOSED, (
            f"type {closed_type!r} missing from _CLOSED — would incorrectly route to draft"
        )


# Regression: Greenhouse's Disability Status decline option is "I do not want to answer"
# (want, not wish). _DECLINE_RE matched only "wish to", so Disability was left blank while
# Gender/Hispanic/Veteran declined -> a required demographic blocked the submit (live axon).
def test_decline_re_matches_do_not_want_to_answer():
    from backend.applier.dropdowns import _DECLINE_RE, _DEMOGRAPHIC
    for s in ("I do not want to answer", "I don't want to answer",
              "I do not wish to answer", "Decline to self-identify", "Prefer not to say"):
        assert _DECLINE_RE.search(s), s
    assert not _DECLINE_RE.search("I want to answer honestly")
    # and "Latin American" is not treated as a demographic by dropdowns either
    assert not _DEMOGRAPHIC.search("are you based in a latin american country?")
    assert _DEMOGRAPHIC.search("are you latinx?")


def test_consent_regex_matches_required_not_marketing():
    """fill_required_consent must tick a REQUIRED legal/privacy consent but NEVER a marketing opt-in."""
    from backend.applier.dropdowns import _CONSENT_RE, _CONSENT_SKIP_RE
    # required legal/privacy consent → matched, not skipped
    for good in ("I agree", "I have read the privacy policy", "I understand",
                 "I consent to the processing of my personal data",
                 "I accept the terms and conditions"):
        assert _CONSENT_RE.search(good.lower()), good
        assert not _CONSENT_SKIP_RE.search(good.lower()), good
    # optional marketing opt-ins → must be skipped even if they say "agree"
    for mkt in ("Do you agree to allow 1Password to contact you about job opportunities",
                "subscribe to our newsletter for updates about future roles",
                "add me to your talent community"):
        assert _CONSENT_SKIP_RE.search(mkt.lower()), mkt


def test_demographic_data_consent_ticks_but_selfid_does_not():
    """A GDPR consent to PROCESS the (declined) demographic survey is a required legal box that must
    tick — but a real protected-characteristic self-ID must stay vetoed (dropdowns.py, 2026-08-26)."""
    from backend.applier.dropdowns import (_CONSENT_RE, _CONSENT_SKIP_RE,
                                           _DEMOGRAPHIC, _DEMOGRAPHIC_CONSENT_RE)

    def should_tick(lab: str) -> bool:
        low = lab.lower()
        is_demo = bool(_DEMOGRAPHIC.search(low)) and not _DEMOGRAPHIC_CONSENT_RE.search(low)
        return bool(_CONSENT_RE.search(low)) and not _CONSENT_SKIP_RE.search(low) and not is_demo

    # required demographic-DATA-consent + skillsoft company-subject phrasing → TICK
    for good in ("I consent to Datadog collecting, storing, and processing my responses to the "
                 "demographic data surveys above.",
                 "Skillsoft has my consent to collect, store, and process my data for the purpose "
                 "of considering me for employment."):
        assert should_tick(good), good
    # genuine protected-characteristic self-ID (no consent verb) → must NOT tick
    for selfid in ("I am a person with a disability", "I identify as a protected veteran",
                   "Which of the following describes your gender identity? Prefer not to answer"):
        assert not should_tick(selfid), selfid
