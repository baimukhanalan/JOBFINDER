"""Offline tests for the WriteX email solver + classify routing (no network, no browser)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester import core, writex  # noqa: E402

_TOPIC = ("Compose an email response for the topic provided. Please ensure that your response is a "
          "minimum of 30 words. Your name is David Gonzalez. Write an email to your manager, Sonya "
          "Smith (sonya.smith@swillsys.com), requesting a two-week leave.")


def test_is_writex_true_and_false():
    assert writex.is_writex(_TOPIC)
    assert writex.is_writex("Please compose your response as an email.")
    assert not writex.is_writex("Type the following paragraph exactly as shown in the box below.")


def test_topic_strips_instruction_and_wordcount():
    t = writex.topic(_TOPIC + " Word count: 0")
    assert "Compose an email response" not in t
    assert "David Gonzalez" in t
    assert "Word count" not in t


def test_extract_recipient():
    assert writex.extract_recipient(_TOPIC) == "sonya.smith@swillsys.com"
    assert writex.extract_recipient("no address here") is None


def test_parse_email_roundtrip():
    ans = "To: a@b.com\nSubject: Hello there\n\nThis is the body of the message and it goes on."
    p = writex.parse_email(ans)
    assert p == {"to": "a@b.com", "subject": "Hello there",
                 "body": "This is the body of the message and it goes on."}
    assert writex.parse_email("just some prose, no fields") is None


def test_fallback_email_is_at_least_30_words_and_uses_recipient():
    e = writex._fallback_email(writex.topic(_TOPIC), "sonya.smith@swillsys.com")
    assert e["to"] == "sonya.smith@swillsys.com"
    assert len(e["body"].split()) >= 30
    assert e["subject"]


def test_draft_email_replays_banked_verbatim_without_network():
    banked = {"kind": "email",
              "text": ("To: old@x.com\nSubject: Leave request\n\n" + ("word " * 35).strip()),
              "to": "old@x.com", "subject": "Leave request", "body": ("word " * 35).strip()}
    e = asyncio.run(writex.draft_email(_TOPIC, banked=banked))
    # banked body is reused; recipient is corrected to the one explicit in the topic
    assert e["to"] == "sonya.smith@swillsys.com"
    assert len(e["body"].split()) >= 30


def test_classify_routes_writex_to_writing_and_speed_test_to_typing():
    email_item = {"has_textarea": True, "options": [],
                  "question": "Compose an email response for the topic provided.", "body": ""}
    assert core.classify(email_item) == ("writing", False)
    speed_item = {"has_textarea": True, "options": [],
                  "question": "Type the paragraph shown below as fast and accurately as you can.",
                  "body": ""}
    assert core.classify(speed_item) == ("typing", False)


def test_classify_recognises_sales_best_worst():
    item = {"options": [{"text": "Pursue the lead aggressively"},
                        {"text": "Thank the manager and pass it on"},
                        {"text": "Ignore it"}],
            "question": "You are a salesperson at a heating-equipment company and a lead appears.",
            "body": "Choose the 'best' and the 'worst' action for the given situation."}
    assert core.classify(item) == ("sales", False)
