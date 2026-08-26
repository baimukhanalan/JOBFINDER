"""Browserless enterprise-ATS fixtures; no form is filled or submitted."""
from __future__ import annotations

import inspect

import pytest

from backend.tools import company_job_questions as qx


def _snapshot(*questions, **evidence):
    defaults = {
        "url": "https://example.test/apply", "title": "Application",
        "form_count": 1, "visible_control_count": len(questions),
        "unlabeled_control_count": 0, "submit_control_count": 1,
        "next_control_count": 0, "challenge_detected": False,
        "provider_multistep": False, "account_gate_detected": False,
        "consent_gate_detected": False,
    }
    defaults.update(evidence)
    return {"questions": list(questions), "evidence": defaults}


class FixtureFrame:
    def __init__(self, url, snapshot=None, error=None):
        self.url = url
        self.snapshot = snapshot
        self.error = error

    def evaluate(self, _script):
        if self.error:
            raise self.error
        return self.snapshot


class FixturePage:
    def __init__(self, frames):
        self.frames = frames
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    def wait_for_timeout(self, _milliseconds):
        return None


@pytest.mark.parametrize("ats,url", [
    ("icims", "https://careers.example.test/jobs/123/job"),
    ("successfactors", "https://career.example.test/job/New-York-Role/123"),
    ("oracle", "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/job/123"),
    ("custom", "https://jobs.example.test/openings/123/apply"),
])
def test_enterprise_urls_are_read_only_and_not_guessed(ats, url):
    assert qx.build_application_url(ats, url) == url


def test_icims_questions_are_collected_from_embedded_application_frame():
    main = _snapshot(form_count=0, visible_control_count=0,
                     url="https://careers.example.test/job/123")
    application = _snapshot(
        {"label": "Are you legally authorized to work?", "type": "radioButton",
         "required": True, "options": ["Yes", "No"],
         "raw_evidence": {"selector": "#auth"}},
        {"label": "How many years of experience do you have?", "type": "text",
         "required": True, "raw_evidence": {"selector": "#years"}},
        url="https://careers.icims.com/jobs/123/application",
    )
    page = FixturePage([
        FixtureFrame("https://careers.example.test/job/123", main),
        FixtureFrame("https://careers.icims.com/jobs/123/application", application),
    ])

    result = qx.scrape_questions_with_page(
        page, "icims", "https://careers.example.test/job/123",
        cap_s=1, interval_s=0)

    assert result["state"] == "complete"
    assert [question["label"] for question in result["questions"]] == [
        "Are you legally authorized to work?",
        "How many years of experience do you have?",
    ]
    assert result["questions"][0]["type"] == "choice"
    assert result["questions"][0]["raw_evidence"]["frame_index"] == 1
    assert result["scrape_evidence"]["frames_read"] == 2


@pytest.mark.parametrize(("ats", "flag", "reason"), [
    ("workday", "provider_multistep", "multi_step_navigation_unavailable"),
    ("successfactors", "account_gate_detected", "account_gate_not_traversed"),
    ("oracle", "consent_gate_detected", "consent_gate_not_accepted"),
])
def test_enterprise_gates_preserve_visible_questions_as_explicit_partial(ats, flag, reason):
    question = {
        "displayName": "Email address", "fieldType": "string", "required": True,
        "raw_evidence": {"selector": "#email"},
    }
    snapshot = _snapshot(question, **{flag: True})
    page = FixturePage([FixtureFrame("https://example.test/apply", snapshot)])

    result = qx.scrape_questions_with_page(
        page, ats, "https://example.test/job/123", cap_s=1, interval_s=0)

    assert result["state"] == "partial"
    assert result["question_count"] == 1
    assert reason in result["reasons"]
    assert reason in result["scrape_evidence"]["gate_reasons"]
    assert result["scrape_evidence"]["coverage_scope"] == "visible_steps_only"


def test_unreadable_cross_origin_frame_is_partial_but_keeps_other_questions():
    question = {"label": "Current location", "type": "text",
                "raw_evidence": {"selector": "#location"}}
    page = FixturePage([
        FixtureFrame("https://example.test/apply", _snapshot(question)),
        FixtureFrame("https://blocked.example.test/frame", error=RuntimeError("blocked")),
    ])
    result = qx.scrape_questions_with_page(
        page, "custom", "https://example.test/apply", cap_s=1, interval_s=0)

    assert result["state"] == "partial"
    assert result["question_count"] == 1
    assert "embedded_frame_unreadable" in result["reasons"]
    assert result["scrape_evidence"]["frame_error_count"] == 1


def test_provider_field_shapes_normalize_without_losing_options():
    questions = qx.normalize_questions([
        {"displayName": "Preferred region", "controlType": "multiSelectList",
         "values": [{"text": "Americas"}, {"title": "EMEA"}]},
        {"question": "Additional information", "fieldType": "richText"},
    ], source="enterprise_fixture")
    assert questions[0]["type"] == "multi_select"
    assert questions[0]["options"] == ["Americas", "EMEA"]
    assert questions[1]["type"] == "textarea"


def test_workday_honeypot_is_not_an_application_question():
    questions = qx.normalize_questions([
        {"label": "Email Address", "type": "email"},
        {"label": "Enter website. This input is for robots only, do not enter if you're human.",
         "type": "text"},
        {"label": "Leave this field blank", "type": "text"},
    ])
    assert [question["label"] for question in questions] == ["Email Address"]


def test_collector_has_no_action_or_submit_click_path():
    source = inspect.getsource(qx.scrape_questions_with_page)
    assert ".click(" not in source
    assert ".fill(" not in source
    assert ".press(" not in source

    question = {"label": "Why this role?", "type": "textarea"}
    page = FixturePage([FixtureFrame("https://example.test/apply", _snapshot(question))])
    result = qx.scrape_questions_with_page(
        page, "custom", "https://example.test/apply", cap_s=1, interval_s=0)
    assert result["scrape_evidence"]["form_submission_attempted"] is False
    assert result["scrape_evidence"]["action_controls_clicked"] == 0
