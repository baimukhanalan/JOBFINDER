"""Pure unit tests for the custom-widget closed-question engine (Wave D):
react-select / button-group question shaping, the action-button exclusion
regex, and the merge_custom_pick bookkeeping.

The DOM halves (harvest_react_selects / apply_react_select_choice /
harvest_button_groups / apply_button_choice) are intentionally NOT mocked
here — they are verified against LIVE Greenhouse/Ashby forms in the D6
verification loop (see the wave notes / report). No browser, no LLM.
"""
import pytest

from backend.applier.dropdowns import (
    _BTN_EXCLUDE,
    MAX_RS_OPTIONS,
    shape_button_group,
    shape_react_select,
)
from backend.applier.strategies.base import merge_custom_pick


# ---------------------------------------------------------------------------
# _BTN_EXCLUDE: action/navigation buttons must never become answer options
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Submit application", "Apply", "Next", "Continue", "Back", "Cancel",
    "Upload resume", "Attach file", "Add another", "Remove", "Clear",
    "Save draft", "Sign in", "Log in",
])
def test_btn_exclude_matches_action_buttons(text):
    assert _BTN_EXCLUDE.search(text), f"{text!r} should be excluded"


@pytest.mark.parametrize("text", [
    "Yes", "No", "Maybe", "0-1 years", "2-5 years", "Hybrid", "Remote",
    "On-site", "Full-time", "Part-time",
])
def test_btn_exclude_keeps_answer_options(text):
    assert not _BTN_EXCLUDE.search(text), f"{text!r} is a legit answer option"


# ---------------------------------------------------------------------------
# shape_button_group
# ---------------------------------------------------------------------------

def _raw(q="Do you have frontline sales experience?",
         options=("Yes", "No"), selectors=None):
    options = list(options)
    selectors = selectors or [f'[data-aa-btn="{i}"]' for i in range(len(options))]
    return {"question_text": q, "options": options, "selectors": selectors}


def test_shape_button_group_yes_no():
    g = shape_button_group(_raw())
    assert g == {
        "question_text": "Do you have frontline sales experience?",
        "options": ["Yes", "No"],
        "selectors": ['[data-aa-btn="0"]', '[data-aa-btn="1"]'],
    }


def test_shape_button_group_drops_action_buttons_keeps_alignment():
    """[Yes][No][Clear] -> Yes/No survive with THEIR selectors (not Clear's)."""
    g = shape_button_group(_raw(options=("Yes", "Clear", "No"),
                                selectors=["s0", "s1", "s2"]))
    assert g is not None
    assert g["options"] == ["Yes", "No"]
    assert g["selectors"] == ["s0", "s2"]


def test_shape_button_group_nav_row_rejected():
    """A wizard nav row must not become a question."""
    assert shape_button_group(_raw(q="Application steps navigation",
                                   options=("Back", "Next"))) is None


def test_shape_button_group_single_survivor_rejected():
    assert shape_button_group(_raw(options=("Save", "Preview"))) is None


def test_shape_button_group_too_many_options_rejected():
    opts = tuple(f"Option {i}" for i in range(6))
    assert shape_button_group(_raw(options=opts)) is None


def test_shape_button_group_short_question_rejected():
    assert shape_button_group(_raw(q="Pick one")) is None  # <= 10 chars


def test_shape_button_group_long_option_text_rejected():
    """Defense in depth: options >= 30 chars are prose, not toggle answers."""
    g = shape_button_group(_raw(options=(
        "Yes", "No", "I have read and understood all the terms above")))
    assert g is not None and g["options"] == ["Yes", "No"]


def test_shape_button_group_cleans_question_noise():
    """Internal ids (uuid etc.) must be stripped via the analyzer's _clean_text."""
    g = shape_button_group(_raw(
        q="Do you have sales experience? 123e4567-e89b-12d3-a456-426614174000"))
    assert g is not None
    assert g["question_text"] == "Do you have sales experience?"


def test_shape_button_group_strips_required_star():
    g = shape_button_group(_raw(q="Are you willing to work weekends? *"))
    assert g is not None
    assert g["question_text"] == "Are you willing to work weekends?"


def test_shape_button_group_missing_selector_drops_option():
    g = shape_button_group(_raw(options=("Yes", "No", "Maybe"),
                                selectors=["s0", "", "s2"]))
    assert g is not None
    assert g["options"] == ["Yes", "Maybe"]
    assert g["selectors"] == ["s0", "s2"]


# ---------------------------------------------------------------------------
# shape_react_select
# ---------------------------------------------------------------------------

def test_shape_react_select_basic():
    q = shape_react_select("What region of the world do you live in?*",
                           ["Americas", "EMEA", "APAC"], 2)
    assert q == {
        "question_text": "What region of the world do you live in?",
        "options": ["Americas", "EMEA", "APAC"],
        "container_index": 2,
    }


def test_shape_react_select_caps_options():
    q = shape_react_select("Country of residence",
                           [f"Country {i}" for i in range(80)], 0)
    assert q is not None
    assert len(q["options"]) == MAX_RS_OPTIONS


def test_shape_react_select_too_few_options():
    assert shape_react_select("Pick your office", ["HQ"], 0) is None


def test_shape_react_select_blank_label():
    assert shape_react_select("", ["Yes", "No"], 0) is None


def test_shape_react_select_strips_blank_options():
    q = shape_react_select("Experience level", ["  Junior ", "", "Senior", "  "], 1)
    assert q is not None
    assert q["options"] == ["Junior", "Senior"]


@pytest.mark.parametrize("label", [
    "Gender", "Race/Ethnicity", "Veteran Status", "Disability Status",
    "Are you Hispanic or Latino?", "Pronouns", "Sexual orientation",
])
def test_shape_react_select_skips_demographics(label):
    """EEOC survey dropdowns are intentionally left blank (same as analyzer _skip)."""
    assert shape_react_select(label, ["A", "B", "C"], 0) is None


def test_shape_button_group_skips_demographics():
    assert shape_button_group(_raw(q="Do you identify as a veteran?")) is None


# ---------------------------------------------------------------------------
# merge_custom_pick — base.py bookkeeping, must mirror the native choice loop
# ---------------------------------------------------------------------------

def _state():
    sources = {"rule": 0, "fact": 0, "choice": 0, "choice_review": 0,
               "draft": 0, "draft_review": 0, "human": 0}
    return sources, [], {}, {}, []  # sources, review_items, choice_picks, drafted, unfilled


def test_merge_backed_pick():
    sources, review, picks, drafted, unfilled = _state()
    n = merge_custom_pick("Q1", "Yes", True, True,
                          sources, review, picks, drafted, unfilled)
    assert n == 1
    assert sources["choice"] == 1 and sources["choice_review"] == 0
    assert drafted == {"Q1": "Yes"}          # replayable on reload
    assert picks == {"Q1": {"option": "Yes", "backed": True}}
    assert review == [] and unfilled == []


def test_merge_unbacked_pick_goes_to_review_not_drafted():
    sources, review, picks, drafted, unfilled = _state()
    n = merge_custom_pick("Q1", "No", False, True,
                          sources, review, picks, drafted, unfilled)
    assert n == 1
    assert sources["choice_review"] == 1 and sources["choice"] == 0
    assert drafted == {}                      # guesses never replay silently
    assert picks == {"Q1": {"option": "No", "backed": False}}
    assert review == [{"question": "Q1", "answer": "No", "kind": "choice"}]
    assert unfilled == []


def test_merge_no_pick_lands_in_unfilled():
    sources, review, picks, drafted, unfilled = _state()
    n = merge_custom_pick("Q1", None, False, False,
                          sources, review, picks, drafted, unfilled)
    assert n == 0
    assert unfilled == ["Q1"]
    assert picks == {} and drafted == {} and review == []
    assert sources["choice"] == 0 and sources["choice_review"] == 0


def test_merge_failed_apply_lands_in_unfilled():
    """A pick the widget refused to take must surface, not count as filled."""
    sources, review, picks, drafted, unfilled = _state()
    n = merge_custom_pick("Q1", "Yes", True, False,
                          sources, review, picks, drafted, unfilled)
    assert n == 0
    assert unfilled == ["Q1"]
    assert picks == {} and drafted == {}


def test_merge_unfilled_deduped():
    sources, review, picks, drafted, unfilled = _state()
    merge_custom_pick("Q1", None, False, False,
                      sources, review, picks, drafted, unfilled)
    merge_custom_pick("Q1", None, False, False,
                      sources, review, picks, drafted, unfilled)
    assert unfilled == ["Q1"]
