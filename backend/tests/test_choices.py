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
    assert out[2] == {"index": 0, "backed": True}                  # working hours -> Yes (auto-submit)
    assert ENG_Q["options"][out[3]["index"]].startswith("B2") and not out[3]["backed"]


def test_deterministic_choices_english_backed_by_fact():
    out = choices.deterministic_choices([ENG_Q], {"english_level": "C1 - Advanced"})
    assert ENG_Q["options"][out[0]["index"]].startswith("C1") and out[0]["backed"]


def test_deterministic_choices_answers_capability_yes_backed():
    """Capability/experience yes-no -> Yes for the ideal candidate, BACKED (2026-08-28): the
    synthetic persona's JD-tailored résumé supports it, so it auto-submits (answering No here is the
    grave error we must avoid)."""
    q = {"question_text": "Do you have call-center experience?", "options": ["Yes", "No"]}
    out = choices.deterministic_choices([q], {})
    assert out == [{"index": 0, "backed": True}]  # index 0 == "Yes", backed -> auto-submit


def test_capability_no_direction_not_forced_yes():
    """Negative-direction yes-no screeners must NEVER be forced to Yes. Prior-employer is
    answered deterministically No and BACKED (owner policy 2026-08-28: a truthful negation for a
    fresh applicant no longer blocks auto-submit); sponsorship defers to the analyzer's own rule."""
    prior = {"question_text": "Have you ever worked with our company before?",
             "options": ["Yes", "No"]}
    out = choices.deterministic_choices([prior], {})[0]
    assert prior["options"][out["index"]] == "No"
    assert out["backed"] is True  # truthful negation -> backed (no longer review-flagged)

    spon = {"question_text": "Will you now or in the future require sponsorship?",
            "options": ["Yes", "No"]}
    assert choices.deterministic_choices([spon], {})[0]["index"] is None


def test_sanctions_screener_answers_no():
    """OFAC / sanctioned-territory screener -> No, BACKED (a US persona is not in a named
    territory — a truthful negation, no longer review-flagged)."""
    q = {"question_text": "Are you located in Cuba, Iran, North Korea, Syria, "
         "the Russian Federation, Belarus, Crimea, Donetsk or Luhansk?",
         "options": ["Yes", "No"]}
    out = choices.deterministic_choices([q], {})[0]
    assert q["options"][out["index"]] == "No"
    assert out["backed"] is True


def test_noncompete_answers_no_backed():
    """Non-compete / restrictive-covenant / conflict-of-interest Yes/No -> No, BACKED (a fresh
    synthetic applicant has no such constraint) — a top review-trigger on the Remote.com template."""
    for qt in ("Do you have a non-compete in place with your previous or current employer?",
               "Are you currently subject to a non-compete agreement or an employment agreement "
               "that would restrict you from joining?",
               "Do you have any conflict of interest?"):
        out = choices.deterministic_choices([{"question_text": qt, "options": ["Yes", "No"]}], {})[0]
        assert out["index"] is not None and out["backed"] is True, qt
        assert ["Yes", "No"][out["index"]] == "No", qt


def test_privacy_notice_acknowledged_backed():
    """Privacy-notice / notice-at-collection acknowledgment SELECT -> the affirmative option, BACKED."""
    for qt in ("Privacy notice", "Notice at Collection for California job applicants"):
        q = {"question_text": qt,
             "options": ["I acknowledge that I have read the notice", "I decline"]}
        out = choices.deterministic_choices([q], {})[0]
        assert out["index"] == 0 and out["backed"] is True, qt


def test_military_service_answers_no_backed():
    """Military-service Yes/No -> No, BACKED (a fresh synthetic applicant never served) — the #2
    unfilled field. Only fires on a clean Yes/No pair, not the multi-option veteran EEO survey."""
    for qt in ("Have you ever served in the military?",
               "Are you a current or former member of the US military?"):
        out = choices.deterministic_choices([{"question_text": qt, "options": ["Yes", "No"]}], {})[0]
        assert out["index"] is not None and out["backed"] is True, qt
        assert ["Yes", "No"][out["index"]] == "No", qt
    # the multi-option veteran EEO self-ID is NOT a Yes/No pair -> left to the demographics layer
    eeo = {"question_text": "Protected veteran status",
           "options": ["I am a protected veteran", "I am not a protected veteran",
                       "I prefer not to answer"]}
    assert choices._military_pick(eeo["question_text"], eeo["options"]) is None


def test_position_type_picks_full_time_backed():
    """'What kind of position...' -> Full-time, BACKED; a job-CATEGORY question is left to the LLM."""
    q = {"question_text": "What kind of position are you interested in obtaining?",
         "options": ["Full-time", "Part-time", "Contract", "Internship"]}
    out = choices.deterministic_choices([q], {})[0]
    assert out["index"] == 0 and out["backed"] is True
    # same phrasing but CATEGORY options (no full-time) -> defer
    assert choices._position_type_pick("What type of role?", ["Engineering", "Sales"]) is None


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
        assert out["backed"] is True  # affirmative default -> backed (auto-submit) 2026-08-28
    # unrelated Yes/No is left alone
    assert choices._consent_pick("Are you at least 18?", ["Yes", "No"]) is None


def test_choose_options_forces_capability_yes_backed(monkeypatch):
    """Even if the weak LLM picks No for a capability question, the deterministic engine forces Yes
    and BACKS it (2026-08-28: a synthetic persona's JD-tailored résumé supports the affirmative, so it
    auto-submits instead of parking for human review)."""
    q = {"question_text": "Do you have SaaS experience?", "options": ["Yes", "No"]}
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 1, "backed": False}])])  # LLM: No
    out = choices.choose_options([q], {}, {})
    assert out[0] == {"index": 0, "backed": True}  # forced to Yes, backed -> auto-submit


def test_deterministic_choices_referral_default_backed_without_fact():
    # hear-about with no referral fact -> a benign default (company/career page), now BACKED
    # (a neutral non-claim that no longer blocks auto-submit — owner policy 2026-08-28).
    out = choices.deterministic_choices([HEAR_Q], {})
    assert HEAR_Q["options"][out[0]["index"]] == "Salmon Career Page"
    assert out[0]["backed"] is True


def test_deterministic_choices_referral_never_needs_specify():
    """When only 'please specify' options exist, defer rather than pick one that
    would open a free-text field."""
    q = {"question_text": "How did you hear about us?",
         "options": ["Telegram * (please specify)", "Other *(please specify)"]}
    assert choices.deterministic_choices([q], {"referral": "Telegram"})[0]["index"] is None


def test_data_consent_select_is_backed_not_review():
    """A required legal 'consent to process my self-identification data' SELECT must be answered
    affirmatively AND backed (no review) — else it blocks the whole submit (was 367 Remote jobs,
    0 auto-submits). It must NOT match a real self-ID or a behavioral question."""
    from backend.services.tailor.choices import deterministic_choices, _DATA_CONSENT_RE
    q = "Please confirm you consent your self-identification data to be processed for the listed purposes"
    opts = ["Yes, I consent", "No, I do not consent"]
    res = deterministic_choices([{"question_text": q, "options": opts}], {})
    assert res[0]["index"] == 0, "must pick the affirmative consent option"
    assert res[0]["backed"] is True, "required data-processing consent must be BACKED (no review)"
    # never a protected-characteristic self-ID, never behavioral
    assert not _DATA_CONSENT_RE.search("Are you a person with a disability?")
    assert not _DATA_CONSENT_RE.search("Which of the following describes your gender identity?")
    assert not _DATA_CONSENT_RE.search("Describe a time you handled a conflict")
    # a plain GDPR personal-data consent is also a required consent -> caught (beneficial)
    assert _DATA_CONSENT_RE.search("I consent to my personal data being processed")
