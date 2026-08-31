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


# ---- scored-section answering (the etalon) — real item texts captured live ----

_SJT_Q = ("A customer asks you a question, but you do not know the answer. You feel that they "
          "expect you to know the answer. What would you be most likely to do?")
_SJT_OPTS = [
    "Find your manager and have them answer the question.",
    "Tell the customer something now and correct it later if you find it was not accurate.",
    "Try to redirect the customer's attention to a different topic you know better.",
    "Try to have the customer answer their own question by talking it through with them.",
    "Tell the customer that you do not know the answer, but that you will find it.",
]
_NEG_Q = ("When we ask your most recent manager, how often will they say that you accidentally "
          "provide inaccurate information to customers?")
_NEG_OPTS = ["Much less often than others", "Less often than others", "About as often as others",
             "Somewhat more often than others", "This would be my first job."]
_POS_Q = ("When we ask your most recent manager, how quickly will they say you are able to resolve "
          "really difficult customer problems?")
_POS_OPTS = ["Somewhat slower than others", "About as fast as others", "Somewhat faster than others",
             "Much faster than others", "This would be my first job."]


def test_pick_sjt_chooses_honest_over_dishonest():
    # the honest "you don't know but will find it" option, never "make something up" / "redirect"
    assert sa._pick_sjt(_SJT_Q, _SJT_OPTS) == 4
    assert sa._SJT_BAD_RE.search(_SJT_OPTS[1]) and sa._SJT_BAD_RE.search(_SJT_OPTS[2])
    assert sa._SJT_GOOD_RE.search(_SJT_OPTS[4])


def test_self_rating_polarity():
    # negative trait (inaccurate) -> the 'much less often' end; never the 'first job' opt-out
    assert sa._is_negative_trait(_NEG_Q) is True
    assert sa._pick_self_rating(_NEG_Q, _NEG_OPTS) == 0
    # positive trait (resolve quickly) -> the 'much faster' end
    assert sa._is_negative_trait(_POS_Q) is False
    assert sa._pick_self_rating(_POS_Q, _POS_OPTS) == 3
    assert sa._option_fav("This would be my first job.") is None


def test_classify_item():
    assert sa._classify_item(_SJT_Q, _SJT_OPTS) == "sjt"
    assert sa._classify_item("I am most likely to…", ["find the appropriate employee and introduce them",
                             "do what I can even if it means doing things that are not allowed"]) == "sjt"
    assert sa._classify_item(_NEG_Q, _NEG_OPTS) == "self_rating"
    assert sa._classify_item(_POS_Q, _POS_OPTS) == "self_rating"


_FREQ_OPTS = ["Once in a while", "Sometimes", "Often", "Very Often", "Always"]


def test_option_fav_frequency_ladder():
    assert sa._option_fav("Always") == 3
    assert sa._option_fav("Never") == -3
    assert sa._option_fav("Once in a while") == -2
    assert sa._option_fav("Sometimes") == 0
    assert sa._option_fav("Often") == 2 and sa._option_fav("Very Often") == 2


def test_statement_on_frequency_scale_is_answered_not_unknown():
    # the live item that used to halt as 'unknown': a positive attitude statement + frequency scale
    q = "People should take extra time to reflect on their performance, even if their performance is successful."
    assert sa._is_scale_options(_FREQ_OPTS) is True
    assert sa._classify_item(q, _FREQ_OPTS) == "self_rating"
    # positive statement -> the high-frequency favourable end ("Always")
    assert sa._pick_self_rating(q, _FREQ_OPTS) == 4
    idx, kind = sa._pick_answer(q, _FREQ_OPTS)
    assert kind == "self_rating" and idx == 4


def test_negative_statement_frequency_picks_low_end():
    q = "I lose my temper when a customer is rude to me."
    opts = ["Never", "Rarely", "Sometimes", "Often", "Always"]
    assert sa._is_negative_trait(q) is True
    assert sa._pick_self_rating(q, opts) == 0  # "Never" — favourable for a negative behaviour


def test_forced_choice_block_is_deterministic():
    # OPQ forced-choice ("which statement describes you best" / "of the remaining two ...") is its
    # own kind, ranked deterministically (fast — no LLM on the bulk of the OPQ).
    q = "Which statement describes you best?"
    opts = ["I look for opportunities to learn about other countries",
            "I remain calm when dealing with pressure",
            "I prefer to work alone without much supervision"]
    assert sa._classify_item(q, opts) == "forced_choice"
    idx, kind = sa._pick_answer(q, opts)
    assert kind == "forced_choice" and idx == 1  # "remain calm" is most CS-favourable
    assert sa._classify_item("Of the remaining two statements, which one describes you best?",
                             opts[:2]) == "forced_choice"
    # the 'work alone without supervision' statement is penalised
    assert sa._statement_fav(opts[1]) > sa._statement_fav(opts[2])


def test_end_of_test_feedback_is_answered_positively():
    # the live 92% stall: post-test feedback about the assessment itself -> positive, not 'unknown'
    q = "The instructions for the assessment were:"
    opts = ["Very clear", "Fairly clear", "Fairly unclear", "Very unclear", "Prefer not to answer"]
    assert sa._classify_item(q, opts) == "feedback"
    idx, kind = sa._pick_answer(q, opts)
    assert kind == "feedback" and idx == 0  # "Very clear"
    # the whole end-of-test candidate-reaction battery, incl. the 'favourable' scale
    q2 = "After completing this assessment, my impression of this company is:"
    o2 = ["Considerably more favorable", "Somewhat more favorable", "Unchanged",
          "Somewhat less favorable", "Considerably less favorable", "Prefer not to answer"]
    assert sa._classify_item(q2, o2) == "feedback"
    assert sa._pick_answer(q2, o2)[0] == 0  # "Considerably more favorable"
    assert sa._classify_item("Would you recommend this assessment to others?",
                             ["Definitely", "Probably", "Not sure", "No"]) == "feedback"


def test_modal_dismiss_names():
    # info/nudge modals are dismissed by their button (Close/OK/Got it/…), never by wording, so the
    # handler works for any modal text. "Continue"/"Resume" are excluded (interstitial forward).
    assert "Close" in sa._DISMISS_NAMES and "OK" in sa._DISMISS_NAMES and "Got it" in sa._DISMISS_NAMES
    assert "Continue" not in sa._DISMISS_NAMES and "Resume" not in sa._DISMISS_NAMES


def test_agreement_scale_routes_to_likert():
    opts = ["Strongly Disagree", "Disagree", "Neutral", "Agree", "Strongly Agree"]
    assert sa._classify_item("I enjoy helping customers solve their problems.", opts) == "likert"


def test_ability_items_are_never_auto_answered():
    # numeric options / true-false / an ability question => 'ability' => _pick_answer returns None
    assert sa._is_ability_item("What is the value of 12 × 7?", ["74", "84", "94", "104"]) is True
    assert sa._is_ability_item("Based on the passage above, is the statement true?",
                               ["True", "False", "Cannot say"]) is True
    assert sa._is_ability_item("Which number comes next in the series?", ["16", "20", "24"]) is True
    idx, kind = sa._pick_answer("What is the value of 12 × 7?", ["74", "84", "94", "104"])
    assert idx is None and kind == "ability"
    # a data table present => ability
    assert sa._is_ability_item("Using the table, what was total revenue?", ["$1,200", "$2,400"], has_table=True) is True
    # SJT sentences are NOT ability
    assert sa._is_ability_item(_SJT_Q, _SJT_OPTS) is False


def test_attention_check_selects_instructed_option():
    q = "This is an attention check. To show you are paying attention, please select 'Strongly Agree'."
    opts = ["Strongly Disagree", "Disagree", "Neither", "Agree", "Strongly Agree"]
    assert sa._attention_target(q, opts) == 4
    idx, kind = sa._pick_answer(q, opts)
    assert kind == "attention" and idx == 4


def test_run_intro_requires_headful_page():
    import asyncio
    r = asyncio.run(sa.run_intro("https://x.shl.com/ce/1", {}, page=None))
    assert r["status"] == "error" and "headless" in r["note"].lower()
