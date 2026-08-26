"""Question acquisition tests use DOM-shaped fixtures/mocks and no live submissions."""
from __future__ import annotations

from backend.tools import company_job_questions as qx


def test_build_application_url_for_supported_ats_preserves_query():
    assert qx.build_application_url("ashby", "https://jobs.ashbyhq.com/acme/42?src=x") == (
        "https://jobs.ashbyhq.com/acme/42/application?src=x")
    assert qx.build_application_url("lever", "https://jobs.lever.co/acme/42/") == (
        "https://jobs.lever.co/acme/42/apply")
    assert qx.build_application_url("workable", "https://apply.workable.com/acme/j/ABC/apply/") == (
        "https://apply.workable.com/acme/j/ABC/apply")
    assert qx.build_application_url("generic", "https://acme.test/careers/apply#form") == (
        "https://acme.test/careers/apply")
    assert qx.build_application_url("workday", "https://acme.test/job/42") == (
        "https://acme.test/job/42/apply/applyManually")


def test_invalid_relative_application_url_is_rejected():
    try:
        qx.build_application_url("lever", "/acme/42")
    except ValueError as exc:
        assert "absolute" in str(exc)
    else:
        raise AssertionError("relative URL accepted")


def test_normalize_dedup_and_fingerprint_are_stable():
    raw = [
        {"label": " Work authorization? * ", "type": "radio_group", "required": True,
         "options": ["Yes", "No", "Yes"], "section": "Eligibility",
         "raw_evidence": {"selector": "#random-1"}},
        {"label": "Work authorization?", "type": "choice", "required": False,
         "options": ["Yes", "Not sure"], "section": "Eligibility",
         "raw_evidence": {"selector": "#random-2"}},
    ]
    result = qx.normalize_questions(raw)
    assert len(result) == 1
    assert result[0]["required"] is True
    assert result[0]["options"] == ["Yes", "No", "Not sure"]
    assert result[0]["order"] == 0
    assert len(result[0]["fingerprint"]) == 64
    changed_dom = dict(result[0], raw_evidence={"selector": "#another"}, order=99)
    assert qx.question_fingerprint(changed_dom) == result[0]["fingerprint"]
    changed_options = dict(result[0], options=["Yes", "No", "Not sure"])
    assert qx.question_fingerprint(changed_options) == result[0]["fingerprint"]
    assert result[0]["raw_evidence"]["duplicate_evidence"] == [{"selector": "#random-2"}]


def test_greenhouse_api_normalizes_all_fields_and_values():
    payload = {"questions": [{
        "label": "Preferred location", "required": True, "section": "Application",
        "fields": [
            {"name": "office", "type": "single_select",
             "values": [{"label": "Remote"}, {"label": "New York"}]},
            {"name": "timezone", "label": "Time zone", "type": "input"},
        ],
    }]}
    questions = qx.normalize_greenhouse_questions(payload)
    assert [q["label"] for q in questions] == ["Preferred location", "Time zone"]
    assert questions[0]["type"] == "select"
    assert questions[0]["options"] == ["Remote", "New York"]
    assert questions[0]["required"] is True
    assert questions[1]["raw_evidence"]["field_name"] == "timezone"


def test_provider_title_choices_and_id_are_preserved():
    questions = qx.normalize_questions([{
        "id": "q-1", "title": "Which shift?", "inputType": "single_select",
        "choices": [{"label": "Day"}, {"label": "Night"}], "required": True,
    }], source="ashby_api")
    assert questions[0]["label"] == "Which shift?"
    assert questions[0]["type"] == "select"
    assert questions[0]["options"] == ["Day", "Night"]
    assert questions[0]["source_question_id"] == "q-1"
    assert questions[0]["raw_evidence"]["payload"]["id"] == "q-1"


class FakePage:
    def __init__(self, snapshots=None, goto_errors=None, evaluate_error=None):
        self.snapshots = list(snapshots or [])
        self.goto_errors = list(goto_errors or [])
        self.evaluate_error = evaluate_error
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.goto_errors:
            error = self.goto_errors.pop(0)
            if error:
                raise error

    def evaluate(self, _script):
        if self.evaluate_error:
            raise self.evaluate_error
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def wait_for_timeout(self, _milliseconds):
        return None


def _snapshot(*questions, visible=None, unlabeled=0, next_controls=0, challenge=False):
    return {
        "questions": list(questions),
        "evidence": {
            "url": "https://jobs.lever.co/acme/42/apply", "title": "Apply",
            "form_count": 1, "visible_control_count": visible if visible is not None else len(questions),
            "unlabeled_control_count": unlabeled, "submit_control_count": 1,
            "next_control_count": next_controls, "challenge_detected": challenge,
        },
    }


def test_hydrated_ashby_lever_workable_and_generic_forms_are_complete():
    fixtures = {
        "ashby": ("https://jobs.ashbyhq.com/acme/42", {"label": "Shift", "type": "choice", "required": True, "options": ["Day", "Night"], "section": "Questions", "raw_evidence": {"tag": "button"}}),
        "lever": ("https://jobs.lever.co/acme/42", {"label": "Why us?", "type": "textarea", "required": True, "options": [], "section": "Additional", "raw_evidence": {"tag": "textarea"}}),
        "workable": ("https://apply.workable.com/acme/j/ABC", {"label": "Authorized?", "type": "radio", "required": False, "options": ["Yes", "No"], "section": "Details", "raw_evidence": {"tag": "input"}}),
        "generic": ("https://acme.test/apply", {"label": "Portfolio", "type": "url", "required": False, "options": [], "section": "", "raw_evidence": {"tag": "input"}}),
    }
    for ats, (url, question) in fixtures.items():
        snap = _snapshot(question)
        page = FakePage([snap, snap])
        result = qx.scrape_questions_with_page(page, ats, url, cap_s=1, interval_s=0)
        assert result["scrape_state"] == "complete", (ats, result)
        assert result["question_count"] == 1
        assert result["questions"][0]["raw_evidence"]
        assert result["questions"][0]["order"] == 0
        assert result["form_url"].endswith(("/application", "/apply"))


def test_empty_stable_page_is_explicit_partial_not_zero_success():
    empty = _snapshot()
    ticks = iter([0.0, 1.0])
    result = qx.scrape_questions_with_page(
        FakePage([empty]), "generic", "https://acme.test/apply",
        cap_s=0.5, interval_s=0, sleep=lambda _: None, clock=lambda: next(ticks))
    assert result["scrape_state"] == "partial"
    assert result["question_count"] == 0
    assert "no_labelled_questions_detected" in result["reasons"]


def test_unlabelled_control_marks_otherwise_useful_scrape_partial():
    question = {"label": "Experience", "type": "text", "raw_evidence": {"tag": "input"}}
    snap = _snapshot(question, visible=2, unlabeled=1)
    result = qx.scrape_questions_with_page(
        FakePage([snap, snap]), "lever", "https://jobs.lever.co/acme/42",
        cap_s=1, interval_s=0)
    assert result["scrape_state"] == "partial"
    assert "unlabelled_controls_present" in result["reasons"]
    assert result["question_count"] == 1


def test_multistep_and_challenge_are_explicit_partial_states():
    question = {"label": "Email", "type": "email", "raw_evidence": {"tag": "input"}}
    snap = _snapshot(question, next_controls=1, challenge=True)
    result = qx.scrape_questions_with_page(
        FakePage([snap, snap]), "generic", "https://acme.test/apply",
        cap_s=1, interval_s=0)
    assert result["scrape_state"] == "partial"
    assert "anti_bot_challenge_detected" in result["reasons"]
    assert result["scrape_evidence"]["action_controls_clicked"] == 0


class WizardPage:
    """Script-aware browser fixture: navigation never depends on entered values."""
    def __init__(self, snapshots, actions=None):
        self.snapshots = snapshots
        self.actions = list(actions or [])
        self.step = 0
        self.frames = [self]
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    def evaluate(self, script):
        if script == qx._SAFE_NEXT_JS:
            action = self.actions.pop(0) if self.actions else {"status": "not_found"}
            if action.get("status") == "clicked":
                self.step = min(self.step + 1, len(self.snapshots) - 1)
            return action
        return self.snapshots[self.step]

    def wait_for_timeout(self, _milliseconds):
        return None


def test_workday_traverses_safe_next_steps_and_stops_before_final_action():
    identity = _snapshot(
        {"label": "Email", "type": "email", "required": False},
        next_controls=1,
    )
    identity["evidence"]["provider_multistep"] = True
    screening = _snapshot(
        {"label": "Are you authorized to work?", "type": "choice", "required": True,
         "options": ["Yes", "No"]},
    )
    screening["evidence"].update({
        "provider_multistep": True,
        "final_action_control_count": 1,
        "review_boundary_detected": True,
    })
    page = WizardPage([identity, screening], actions=[{"status": "clicked", "label": "Next"}])

    result = qx.scrape_questions_with_page(
        page, "workday", "https://acme.test/job/42", cap_s=1, interval_s=0)

    assert result["state"] == "complete"
    assert [question["label"] for question in result["questions"]] == [
        "Email", "Are you authorized to work?",
    ]
    assert result["scrape_evidence"]["action_controls_clicked"] == 1
    assert result["scrape_evidence"]["form_submission_attempted"] is False
    assert result["scrape_evidence"]["coverage_scope"] == (
        "all_reachable_steps_to_review_boundary")
    assert result["scrape_evidence"]["step_traversal"]["stop_reason"] == (
        "review_or_submit_boundary")
    assert [question["raw_evidence"]["application_step"]
            for question in result["questions"]] == [0, 1]


def test_workday_required_fields_block_navigation_without_synthetic_input():
    identity = _snapshot(
        {"label": "Email", "type": "email", "required": True}, next_controls=1)
    identity["evidence"]["provider_multistep"] = True
    page = WizardPage([identity], actions=[{
        "status": "blocked", "reason": "navigation_control_disabled"}])

    result = qx.scrape_questions_with_page(
        page, "workday", "https://acme.test/job/42", cap_s=1, interval_s=0)

    assert result["state"] == "partial"
    assert "multi_step_navigation_blocked_without_input" in result["reasons"]
    assert result["scrape_evidence"]["action_controls_clicked"] == 0
    assert result["scrape_evidence"]["step_traversal"]["steps_captured"] == 1


def test_navigation_script_has_submit_boundary_and_no_input_path():
    script = qx._SAFE_NEXT_JS
    assert "preventDefault" in script
    assert "requestSubmit" in script
    assert "submit application" in script
    assert ".fill(" not in script
    assert ".press(" not in script
    assert ".value=" not in script


def test_validation_constraints_survive_normalization():
    question = qx.normalize_question({
        "label": "Years of experience", "type": "number", "required": True,
        "validation": {"min": "0", "max": "50", "step": "1", "format": "number"},
    })
    assert question is not None
    assert question["validation"] == {
        "min": "0", "max": "50", "step": "1", "format": "number"}


def test_navigation_and_extraction_errors_are_explicit_failed_states():
    nav = qx.scrape_questions_with_page(
        FakePage(goto_errors=[RuntimeError("timeout"), RuntimeError("blocked")]),
        "workable", "https://apply.workable.com/acme/j/ABC")
    assert nav["scrape_state"] == "failed" and nav["question_count"] == 0
    assert nav["state"] == "failed" and nav["error"]
    assert nav["reasons"][0].startswith("navigation_failed:")

    extraction = qx.scrape_questions_with_page(
        FakePage(evaluate_error=RuntimeError("bad DOM")), "generic", "https://acme.test/apply")
    assert extraction["scrape_state"] == "failed"
    assert extraction["reasons"] == ["extraction_failed:RuntimeError"]


def test_public_scrape_questions_uses_injected_page():
    question = {"label": "Can you work weekends?", "type": "choice", "options": ["Yes", "No"]}
    snap = _snapshot(question)
    result = qx.scrape_questions(
        "lever", "https://jobs.lever.co/acme/42", page=FakePage([snap, snap]))
    assert result["state"] == "complete"
    assert result["error"] is None


def test_domcontentloaded_fallback_can_still_return_complete_with_audit_reason():
    question = {"label": "Are you available?", "type": "choice", "options": ["Yes", "No"]}
    snap = _snapshot(question)
    page = FakePage([snap, snap], goto_errors=[RuntimeError("network never idle"), None])
    result = qx.scrape_questions_with_page(
        page, "ashby", "https://jobs.ashbyhq.com/acme/42", cap_s=1, interval_s=0)
    assert result["scrape_state"] == "complete"
    assert result["reasons"] == ["domcontentloaded_failed:RuntimeError"]
    assert [call[1]["wait_until"] for call in page.goto_calls] == ["domcontentloaded", "commit"]


def test_combobox_option_harvest_has_a_total_time_budget():
    question = {
        "label": "Country", "type": "select", "options": [],
        "raw_evidence": {"role": "combobox", "selector": "#country"},
    }
    ticks = iter([0.0, 2.0])
    unresolved = qx._harvest_combobox_options(
        object(), [question], cap_s=1.0, clock=lambda: next(ticks))
    assert unresolved == ["Country"]
    assert question["raw_evidence"]["combobox_opened"] is False


def test_no_question_fixture_is_misclassified_as_complete_identity_zero():
    # Regression guard: "no questions" is never inferred from an empty extraction.
    empty = _snapshot(visible=3, unlabeled=3)
    ticks = iter([0.0, 2.0])
    result = qx.scrape_questions_with_page(
        FakePage([empty]), "lever", "https://jobs.lever.co/acme/42",
        cap_s=1, interval_s=0, sleep=lambda _: None, clock=lambda: next(ticks))
    assert result["scrape_state"] == "partial"
    assert set(result["reasons"]) >= {"no_labelled_questions_detected", "hydration_not_stable", "unlabelled_controls_present"}
