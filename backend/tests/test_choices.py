"""choose_options: the LLM only picks a validated option index — never free text."""
import json

import backend.services.tailor.choices as choices


QS = [
    {"question_text": "Which shift do you prefer?", "options": ["Day", "Night", "Either"]},
    # a neutral yes/no (NOT capability/eligibility/suitability) so the parse-robustness
    # tests below exercise the LLM parse path without a deterministic override firing.
    {"question_text": "Would you like to receive updates about this role?",
     "options": ["Yes", "No"]},
]

FACTS = {"shifts_nights": "Yes"}


def _patch(monkeypatch, replies):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(choices, "_llm_complete", fake)
    return calls


def test_valid_choices_applied(monkeypatch):
    _patch(monkeypatch, [json.dumps([
        {"q": 0, "choice": 2, "backed": True},
        {"q": 1, "choice": 0, "backed": False},
    ])])
    out = choices.choose_options(QS, FACTS, {"title": "CS Rep", "company": "Acme"})
    assert out == [{"index": 2, "backed": True}, {"index": 0, "backed": False}]


def test_out_of_range_choice_dropped(monkeypatch):
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 9, "backed": True},
                                     {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] is None and out[1]["index"] == 1


def test_garbage_retried_then_gives_up(monkeypatch):
    calls = _patch(monkeypatch, ["not json at all", "still garbage"])
    out = choices.choose_options(QS, FACTS, {})
    assert len(calls) == 2  # ATTEMPTS retries
    assert all(o["index"] is None for o in out)


def test_null_choice_left_for_human(monkeypatch):
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": None, "backed": False},
                                     {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] is None and out[1]["index"] == 1


def test_too_many_options_skipped_without_llm(monkeypatch):
    calls = _patch(monkeypatch, ["[]"])
    big = [{"question_text": "Pick", "options": [str(i) for i in range(60)]}]
    out = choices.choose_options(big, {}, {})
    assert out == [{"index": None, "backed": False}] and calls == []


def test_prompt_contains_facts_and_question(monkeypatch):
    calls = _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 0, "backed": True},
                                             {"q": 1, "choice": 0, "backed": True}])])
    choices.choose_options(QS, FACTS, {"title": "CS Rep", "company": "Acme"}, "bpo-voice-qa")
    p = calls[0]
    assert "shifts_nights" in p and "Which shift do you prefer?" in p and "bpo-voice-qa" in p


def test_markdown_fenced_reply_parsed(monkeypatch):
    _patch(monkeypatch, ['```json\n[{"q":0,"choice":1,"backed":true},'
                         '{"q":1,"choice":0,"backed":true}]\n```'])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] == 1 and out[1]["index"] == 0


def test_reply_with_trailing_array_noise(monkeypatch):
    _patch(monkeypatch, ['Answers: [{"q":0,"choice":1,"backed":true},'
                         '{"q":1,"choice":0,"backed":false}] Note: [1, 2] are indices.'])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] == 1 and out[1]["index"] == 0


def test_partial_reply_leaves_rest_for_human(monkeypatch):
    _patch(monkeypatch, [json.dumps([{"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] is None and out[1]["index"] == 1


def test_duplicate_q_last_wins(monkeypatch):
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 0, "backed": True},
                                     {"q": 0, "choice": 2, "backed": True},
                                     {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] == 2


def test_all_invalid_reply_retried(monkeypatch):
    calls = _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 99, "backed": True}]),
                                 json.dumps([{"q": 0, "choice": 1, "backed": True},
                                             {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert len(calls) == 2 and out[0]["index"] == 1


def test_bool_choice_false_not_accepted_as_index_zero(monkeypatch):
    """bool is a subclass of int; 'false' from JSON must NOT select option 0."""
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": False, "backed": False},
                                     {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    # choice=false (bool False) must be treated as invalid — left for human
    assert out[0]["index"] is None
    assert out[1]["index"] == 1


def test_bool_choice_true_not_accepted_as_index_one(monkeypatch):
    """bool True == 1 in Python; must NOT silently select option 1."""
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": True, "backed": True},
                                     {"q": 1, "choice": 0, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] is None
    assert out[1]["index"] == 0


# --- deterministic_choices: LLM-free picks for the standard closed screeners -----

REL_Q = {"question_text": "Are you open to relocation or business trips to the Philippines?",
         "options": ["Yes, ready for relocation", "Long business trips only (few months)",
                     "Short business trips only (few weeks)",
                     "Not ready to come to the Philippines"]}
HEAR_Q = {"question_text": "Where did you find our job posting?",
          "options": ["Job boards (e.g.Indeed, Glassdoor, etc.)", "Linkedin",
                      "From friend/ colleague", "Salmon Career Page",
                      "Telegram * (please specify)", "Other *(please specify)"]}
HOURS_Q = {"question_text": "Our core working hours are 12 PM to 6 PM (GMT+8), does it suit you?",
           "options": ["Yes", "No"]}
ENG_Q = {"question_text": "Your English level",
         "options": ["A1/A2 - Basic (can understand simple words and phrases)",
                     "B1 - Intermediate (can handle everyday work communcation)",
                     "B2 - Upper-Intermediate (comfortable in meetings and discussions)",
                     "C1 - Advanced (fluent, can work fully in English)",
                     "C2 - Proficient/ Native"]}


def _no_llm(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("deterministic_choices must not call the LLM")
    monkeypatch.setattr(choices, "_llm_complete", boom)


def test_deterministic_choices_standard_salmon_screeners(monkeypatch):
    _no_llm(monkeypatch)
    facts = {"willing_to_relocate": "Yes", "referral": "Company careers site"}
    out = choices.deterministic_choices([REL_Q, HEAR_Q, HOURS_Q, ENG_Q], facts)
    assert out[0] == {"index": 0, "backed": True}                  # relocation (fact)
    assert HEAR_Q["options"][out[1]["index"]] == "Salmon Career Page" and out[1]["backed"]
    assert out[2] == {"index": 0, "backed": False}                 # working hours -> Yes
    assert ENG_Q["options"][out[3]["index"]].startswith("B2") and not out[3]["backed"]


def test_deterministic_choices_english_backed_by_fact():
    out = choices.deterministic_choices([ENG_Q], {"english_level": "C1 - Advanced"})
    assert ENG_Q["options"][out[0]["index"]].startswith("C1") and out[0]["backed"]


def test_deterministic_choices_answers_capability_yes_unbacked():
    """Capability/experience yes-no -> Yes for the ideal candidate, but UNBACKED so it
    stays review-flagged (answering No here is the grave error we must avoid)."""
    q = {"question_text": "Do you have call-center experience?", "options": ["Yes", "No"]}
    out = choices.deterministic_choices([q], {})
    assert out == [{"index": 0, "backed": False}]  # index 0 == "Yes", unbacked


def test_capability_no_direction_not_forced_yes():
    """Negative-direction yes-no screeners must NEVER be forced to Yes.
    Prior-employer is answered deterministically No (fresh applicant, unbacked ->
    review); sponsorship is left to the analyzer's own rule and defers here."""
    prior = {"question_text": "Have you ever worked with our company before?",
             "options": ["Yes", "No"]}
    out = choices.deterministic_choices([prior], {})[0]
    assert prior["options"][out["index"]] == "No"
    assert out["backed"] is False  # unbacked -> stays [review]-flagged

    spon = {"question_text": "Will you now or in the future require sponsorship?",
            "options": ["Yes", "No"]}
    assert choices.deterministic_choices([spon], {})[0]["index"] is None


def test_sanctions_screener_answers_no():
    """OFAC / sanctioned-territory screener -> No, unbacked (review)."""
    q = {"question_text": "Are you located in Cuba, Iran, North Korea, Syria, "
         "the Russian Federation, Belarus, Crimea, Donetsk or Luhansk?",
         "options": ["Yes", "No"]}
    out = choices.deterministic_choices([q], {})[0]
    assert q["options"][out["index"]] == "No"
    assert out["backed"] is False


def test_english_yesno_backed_when_fact_meets_level():
    """'Do you master English at C1 level?' -> Yes (backed) with a Fluent fact;
    defers when the fact is below the asked level; the 'English Level' dropdown is
    untouched (still routed to the level picker, not this Yes/No path)."""
    q = {"question_text": "Do you master English at C1 level?", "options": ["Yes", "No"]}
    out = choices.deterministic_choices([q], {"english_level": "Fluent"})[0]
    assert q["options"][out["index"]] == "Yes"
    assert out["backed"] is True
    # below the asked level -> never over-claim
    assert choices.deterministic_choices([q], {"english_level": "B2"})[0]["index"] is None
    # no fact at all -> defer
    assert choices.deterministic_choices([q], {})[0]["index"] is None


def test_sms_consent_picks_affirmative():
    """SMS/text-message contact consent -> the affirmative option, across the shapes it
    takes: the analyzer's 'given'/'notGiven' value pair, the human sentences, and the
    useless 'communicationConsent' label. Unbacked -> review. A plain non-consent Yes/No
    must NOT be caught."""
    for q in (
        {"question_text": "communicationConsent", "options": ["given", "notGiven"]},
        {"question_text": "Phone",
         "options": ["Yes - I consent to receiving text messages",
                     "No - I do not consent to receiving text messages"]},
    ):
        out = choices.deterministic_choices([q], {})[0]
        assert out["index"] == 0
        assert out["backed"] is False
    # unrelated Yes/No is left alone
    assert choices._consent_pick("Are you at least 18?", ["Yes", "No"]) is None


def test_choose_options_forces_capability_yes_over_llm_no(monkeypatch):
    """Even if the weak LLM picks No for a capability question, the override forces Yes."""
    q = {"question_text": "Do you have SaaS experience?", "options": ["Yes", "No"]}
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 1, "backed": False}])])  # LLM: No
    out = choices.choose_options([q], {}, {})
    assert out[0] == {"index": 0, "backed": False}  # forced to Yes, unbacked


def test_deterministic_choices_referral_default_unbacked_without_fact():
    out = choices.deterministic_choices([HEAR_Q], {})  # no referral fact
    assert HEAR_Q["options"][out[0]["index"]] == "Salmon Career Page"
    assert out[0]["backed"] is False


def test_deterministic_choices_referral_never_needs_specify():
    """When only 'please specify' options exist, defer rather than pick one that
    would open a free-text field."""
    q = {"question_text": "How did you hear about us?",
         "options": ["Telegram * (please specify)", "Other *(please specify)"]}
    assert choices.deterministic_choices([q], {"referral": "Telegram"})[0]["index"] is None
