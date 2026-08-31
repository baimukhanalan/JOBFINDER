"""Safety tests for the SHL intro auto-fill regexes — the scored-item detector must never let a
graded assessment page be treated as fillable background. No network."""
from backend.tools import shl_assessment as sa


def test_scored_detector_catches_graded_items():
    for t in ["Which of the following is most like me?", "Question 3 of 30",
              "Strongly agree ... Strongly disagree", "Time remaining: 12:00",
              "To what extent do you agree", "Select the response that best applies",
              "Complete the series", "verbal reasoning"]:
        assert sa._SCORED_RE.search(t), t


def test_background_detector_matches_intro_pages():
    for t in ["About You", "Please select your gender", "Racial/Ethnic background",
              "What is your country/region of residence?", "highest educational qualification",
              "Your Responsibilities", "Data Protection Notice", "expected time 27 minutes"]:
        assert sa._BG_RE.search(t), t


def test_scored_wins_over_background_ordering():
    # run_intro checks _SCORED_RE BEFORE _BG_RE, so even a page mentioning both halts. Assert the
    # scored detector fires on a realistic mixed personality-item page.
    mixed = "About you: which of the following is most like me? Strongly agree"
    assert sa._SCORED_RE.search(mixed)


def test_decline_detector():
    for t in ["Prefer not to answer", "I choose not to disclose", "I do not wish to answer"]:
        assert sa._DECLINE_RE.search(t)
    assert not sa._DECLINE_RE.search("White (not Hispanic or Latino)")


def test_scored_detector_catches_personality_wordings():
    # OPQ/personality items are behavioural, not "question N of" — the broadened detector must
    # still catch their typical phrasings so a graded page halts before any auto-advance.
    for t in ["How much do you agree with the following statement",
              "For each of the following, choose the one that best describes you",
              "Rate each statement below", "The following statements describe how you work",
              "which best describes you"]:
        assert sa._SCORED_RE.search(t), t


def test_forward_control_regex():
    for t in ["Next", "Continue", "Proceed", "Start", "Begin", "Launch",
              "Start Assessment", "Get Started", "Start now"]:
        assert sa._FORWARD_RE.search(t), t
    # anchored exact match must NOT fire on unrelated / destructive controls
    for t in ["Sign Out", "Exit", "Help", "Cancel", "Save and exit"]:
        assert not sa._FORWARD_RE.search(t), t


def test_forward_contains_matches_card_sized_button_not_destructive():
    # SHL's overview 'Continue' is one submit button whose whole accessible name is the card text.
    card = "Assessment\xa0expected time\xa027\xa0mins\xa0Continue"
    assert sa._FORWARD_CONTAINS_RE.search(card) and not sa._FORWARD_DENY_RE.search(card)
    # destructive / navigation controls are rejected by the denylist even if wordy
    for t in ["Sign Out", "Log out", "Exit", "Cancel", "Save and exit", "Go Back", "Previous",
              "Restart", "Skip to main content", "Accessibility Options"]:
        assert sa._FORWARD_DENY_RE.search(t), t


def test_run_intro_requires_headful_page():
    import asyncio
    r = asyncio.run(sa.run_intro("https://x.shl.com/ce/1", {}, page=None))
    assert r["status"] == "error" and "headless" in r["note"].lower()
