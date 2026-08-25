from backend.tools import company_jobs


def _job(**overrides):
    row = {
        "company_id": 7,
        "source": "lever",
        "source_job_id": "remote-1",
        "title": "Remote Support Agent",
        "remote_type": "remote",
        "is_remote": True,
        "apply_url": "https://jobs.example.test/remote-1/apply",
        "job_url": "https://jobs.example.test/remote-1",
        "description": "Complete JD",
        "raw_payload": {"id": "remote-1"},
        "questions": [],
        "questions_state": "not_available",
    }
    row.update(overrides)
    return row


class Store:
    def __init__(self):
        self.question_calls = []
        self.finished = []

    def list_company_targets(self, status, limit):
        return [{"id": 7, "canonical_name": "Example", "ats": "lever",
                 "ats_slug": "example"}]

    def begin_scan(self, company_id, ats, ats_slug):
        return 19

    def upsert_job(self, company_id, row, scan_id):
        self.row = row
        return {"job_id": 31, "snapshot_created": True}

    def save_questions(self, job_id, questions, state, error=None):
        self.question_calls.append((job_id, questions, state, error))
        return len(questions or []) if state == "success" else 0

    def finish_scan(self, scan_id, seen, complete=True, error=None):
        self.finished.append((scan_id, seen, complete, error))
        return 2 if complete else 0


def test_complete_collection_stores_questions_and_closes_missing_jobs():
    store = Store()
    fetch_calls = []

    def fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        return [_job()]

    def scrape(ats, url, headless=True):
        return {"state": "complete", "questions": [
            {"label": "Authorized to work?", "type": "choice", "required": True,
             "options": ["Yes", "No"]}
        ]}

    result = company_jobs.collect_company_jobs(
        store=store, fetcher=fetch, question_scraper=scrape)

    assert result["companies_succeeded"] == 1
    assert result["remote_jobs_seen"] == 1
    assert result["questions_complete"] == 1
    assert result["jobs_closed"] == 2
    assert store.row["source_board_id"] == "example"
    assert store.row["source_payload"] == {"id": "remote-1"}
    assert store.finished == [(19, ["remote-1"], True, None)]
    assert store.question_calls[0][2] == "success"
    assert "ats_url" in fetch_calls[0][1]


def test_partial_questions_are_failure_and_not_authoritative():
    store = Store()
    result = company_jobs.collect_company_jobs(
        store=store,
        fetcher=lambda *a, **k: [_job()],
        question_scraper=lambda *a, **k: {
            "state": "partial", "questions": [{"label": "Name"}],
            "reasons": ["multi_step_form"],
        },
    )
    assert result["questions_failed"] == 1
    assert store.question_calls == [
        (31, [{"label": "Name"}], "failed", "multi_step_form")]


def test_api_questions_avoid_browser_scrape():
    store = Store()

    def must_not_scrape(*args, **kwargs):
        raise AssertionError("browser should not run for complete API questions")

    result = company_jobs.collect_company_jobs(
        store=store,
        fetcher=lambda *a, **k: [_job(
            source="greenhouse", questions_state="available",
            questions=[{"id": 1, "label": "Email", "required": True,
                        "fields": [{"name": "email", "type": "input_text"}]}])],
        question_scraper=must_not_scrape,
    )
    assert result["questions_complete"] == 1
    assert result["questions_stored"] == 1
    assert store.question_calls[0][1][0]["label"] == "Email"


def test_failed_board_scan_never_closes_missing_jobs():
    store = Store()

    def fail(*args, **kwargs):
        raise RuntimeError("temporary upstream error")

    result = company_jobs.collect_company_jobs(store=store, fetcher=fail)
    assert result["companies_failed"] == 1
    assert result["jobs_closed"] == 0
    assert store.finished == [(19, [], False, "temporary upstream error")]


def test_defensive_remote_gate_rejects_connector_regression():
    store = Store()
    result = company_jobs.collect_company_jobs(
        store=store,
        fetcher=lambda *a, **k: [_job(is_remote=False, remote_type="hybrid")],
    )
    assert result["companies_failed"] == 1
    assert not hasattr(store, "row")
    assert store.finished[0][2] is False


def test_question_limit_records_not_attempted_without_erasing_data():
    store = Store()
    result = company_jobs.collect_company_jobs(
        store=store, fetcher=lambda *a, **k: [_job()], question_limit=0,
        collect_questions=False)
    assert result["questions_not_attempted"] == 1
    assert store.question_calls == [(31, None, "not_attempted", None)]
