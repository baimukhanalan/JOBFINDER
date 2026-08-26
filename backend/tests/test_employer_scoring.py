import inspect

from backend.tools.employer_scoring import calculate_scores, score_employers


def test_scores_require_observed_remote_activity_and_penalize_question_gate():
    scores = calculate_scores({
        "active_jobs": 4, "recent_jobs": 2, "entry_jobs": 1,
        "customer_service_jobs": 1, "complete_scans": 2,
        "employee_count_min": 10000, "hiring_sites": 100,
        "ats": "workday", "questions_failed": 4,
        "industry": "technology", "headquarters": "Virginia",
        "careers_url": "https://example.test/careers",
    })
    assert scores["remote_score"] >= 40
    assert scores["hiring_activity_score"] >= 45
    assert scores["application_ease_score"] == 30
    assert 0 < scores["score_confidence"] < 1


def test_no_active_remote_jobs_produces_zero_remote_activity_score():
    scores = calculate_scores({"active_jobs": 0, "complete_scans": 2})
    assert scores["remote_score"] == 0


def test_scoring_rechecks_active_population_before_write():
    source = inspect.getsource(score_employers)
    assert "WHERE company_id=%s AND in_target_population" in source
    assert "if cur.rowcount != 1:" in source
