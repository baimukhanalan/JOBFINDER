"""draft_answers v2: profession-neutral prompt from facts+variant, chunked calls."""
import json
import re

import backend.services.tailor.answers as answers


def _patch(monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        # answer exactly the numbered questions present in this chunk's prompt
        qn = len(re.findall(r"(?m)^\d+\. ", prompt))
        return json.dumps([f"answer {i}" for i in range(qn)])

    monkeypatch.setattr(answers, "_llm_complete", fake)
    return calls


def test_chunks_of_eight(monkeypatch):
    calls = _patch(monkeypatch)
    qs = [f"Question number {i} about the role, long enough?" for i in range(20)]
    out = answers.draft_answers(qs, {"full_name": "Kate Doe"}, {"title": "CS Rep"},
                                facts={"typing_wpm": "70"}, niche_label="chat-email-async")
    assert len(calls) == 3  # 8 + 8 + 4
    assert len(out) == 20


def test_prompt_has_role_facts_niche_no_hardcoded_profession(monkeypatch):
    calls = _patch(monkeypatch)
    answers.draft_answers(["Why do you want to work here, in a few words?"],
                          {"full_name": "Kate Doe"},
                          {"title": "Night Auditor", "company": "Acme"},
                          facts={"typing_wpm": "70"}, niche_label="bpo-voice-qa")
    p = calls[0]
    assert "Night Auditor" in p and "Acme" in p
    assert "typing_wpm" in p and "bpo-voice-qa" in p
    assert "customer-support candidate" not in p


def test_cap_twenty(monkeypatch):
    calls = _patch(monkeypatch)
    qs = [f"Question number {i} about the role, long enough?" for i in range(30)]
    out = answers.draft_answers(qs, {}, {})
    assert len(out) == 20


def test_behavioral_question_forced_to_review(monkeypatch):
    """Deterministic backstop: even when the model forgets the '[review] ' prefix,
    behavioral / give-an-example questions are flagged so the human always
    personalizes them with a real story before Submit."""
    _patch(monkeypatch)  # model returns plain 'answer N', never self-flags
    for q in ("Describe a time you handled an upset customer and what you did.",
              "Tell us about a time when you missed a deadline, please.",
              "Give a specific example of great service you delivered."):
        out = answers.draft_answers([q], {}, {"title": "CS Rep"})
        assert next(iter(out.values())).startswith("[review] "), q


def test_specifics_question_forced_to_review(monkeypatch):
    """Questions demanding verifiable specifics the candidate must supply
    (references with contact, profile links) are flagged deterministically."""
    _patch(monkeypatch)
    for q in ("Please provide a reference with their contact information.",
              "Share a link to your portfolio or GitHub profile, if any."):
        out = answers.draft_answers([q], {}, {})
        assert next(iter(out.values())).startswith("[review] "), q


def test_factual_question_not_forced_to_review(monkeypatch):
    """Plain factual questions stay un-flagged — the human shouldn't have to
    eyeball every answer, only the ones needing his own specifics."""
    _patch(monkeypatch)
    for q in ("How did you hear about this role originally?",
              "What are your salary expectations for this position?"):
        out = answers.draft_answers([q], {}, {})
        assert not next(iter(out.values())).startswith("[review]"), q


def test_no_double_review_prefix(monkeypatch):
    """If the model already self-flagged, the backstop must not double-prefix."""
    def fake(prompt):
        qn = len(re.findall(r"(?m)^\d+\. ", prompt))
        return json.dumps(["[review] already flagged"] * qn)
    monkeypatch.setattr(answers, "_llm_complete", fake)
    out = answers.draft_answers(
        ["Describe a time you led a difficult project to completion."], {}, {})
    assert next(iter(out.values())).count("[review]") == 1


def test_failed_chunk_skipped_not_fatal(monkeypatch):
    state = {"n": 0}

    def fake(prompt):
        state["n"] += 1
        if state["n"] <= answers.DRAFT_ATTEMPTS:  # first chunk fails all attempts
            return "garbage"
        return json.dumps(["ok"] * 8)

    monkeypatch.setattr(answers, "_llm_complete", fake)
    monkeypatch.setattr(answers.time, "sleep", lambda s: None)
    qs = [f"Question number {i} about the role, long enough?" for i in range(16)]
    out = answers.draft_answers(qs, {}, {})
    assert len(out) == 8  # second chunk still drafted


# --- deterministic_answers: the LLM-free identity/factual pre-pass ---------------

def _no_llm(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("deterministic_answers must not call the LLM")
    monkeypatch.setattr(answers, "_llm_complete", boom)


def test_deterministic_answers_fills_telegram_and_hear_about(monkeypatch):
    _no_llm(monkeypatch)
    out = answers.deterministic_answers(
        ["Telegram Username", "Where did you find our job posting?"],
        {"full_name": "Ruslan Baibekov"}, {"title": "Data Scientist"},
        {"referral": "Company careers site"})
    assert out["Telegram Username"] == "@none"
    assert out["Where did you find our job posting?"] == "Company careers site"
    # factual answers -> never review-flagged
    assert not any(v.startswith("[review]") for v in out.values())


def test_deterministic_answers_skips_link_fields(monkeypatch):
    _no_llm(monkeypatch)
    out = answers.deterministic_answers(
        ["Your LinkedIn Profile URL", "GitHub profile link", "Portfolio website"],
        {"linkedin_url": ""}, {}, {})
    assert out == {}  # links are left blank / for the human, never "N/A"


def test_deterministic_answers_notice_and_salary(monkeypatch):
    _no_llm(monkeypatch)
    out = answers.deterministic_answers(
        ["What is your notice period?", "Your expected salary?"],
        {}, {}, {"notice_period": "Immediately", "salary_annual": "$88,000 USD"})
    assert out["What is your notice period?"] == "Immediately"
    assert "88,000" in out["Your expected salary?"]
