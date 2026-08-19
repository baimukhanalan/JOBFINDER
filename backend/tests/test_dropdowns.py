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
