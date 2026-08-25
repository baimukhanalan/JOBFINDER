import ast
import asyncio
from pathlib import Path

from backend.profiles.store import Profile
from backend.tools import company_applier


def _profile():
    return Profile(
        id="real_us", full_name="Real Person", email="real@takhet.com",
        phone="+1 212 555 9000", country="United States", mailbox="real@takhet.com",
        resume={"summary": "Support", "experience": [], "skills": ["Support"],
                "education": [{"degree": "BA"}],
                "personal_info": {"email": "real@takhet.com"}},
    )


def _row(state="claimed"):
    return {
        "id": 5, "profile_id": "real_us", "state": state,
        "revalidation_hash": "db-hash", "current_revalidation_hash": "db-hash",
        "job_status": "active", "remote_type": "remote", "questions_status": "success",
        "title": "Customer Support", "company_name": "Acme",
        "location_raw": "Remote - United States",
        "description": "Remote customer support and customer assistance.",
        "apply_url": "https://jobs.acme.test/5/apply", "questions": [],
    }


class Store:
    def __init__(self, row):
        self.row = row
        self.transitions = []
        self.attempts = []

    def claim_next(self, *args, **kwargs):
        row, self.row = self.row, None
        return row

    def transition(self, app_id, state, actor, **kwargs):
        self.transitions.append((app_id, state, actor, kwargs))
        return {"id": app_id, "state": state}

    def record_attempt(self, *args, **kwargs):
        self.attempts.append((args, kwargs))


def loader(_profile_id):
    return _profile(), {"availability": "Immediate"}


def test_prepare_stops_at_hash_bound_human_approval():
    store = Store(_row())
    result = company_applier.prepare_one(
        "real_us", "worker", min_fit=0, store=store, candidate_loader=loader)
    assert result["state"] == "awaiting_approval"
    assert store.transitions[0][1] == "awaiting_approval"
    assert store.transitions[0][3]["expected_revalidation_hash"] == "db-hash"


def test_policy_block_never_opens_browser():
    store = Store(_row())

    def bad_loader(_):
        p = _profile()
        p.is_synthetic = True
        return p, {"x": 1}

    result = company_applier.prepare_one(
        "real_us", "worker", min_fit=0, store=store, candidate_loader=bad_loader)
    assert result["state"] == "blocked"


def test_approved_fill_uses_isolated_artifacts_and_never_requests_submit(tmp_path):
    row = _row(state="preparing")
    # Match the stored policy hash to the one recalculated immediately before fill.
    decision = company_applier.evaluate(
        company_applier._policy_job(row), _profile(), {"availability": "Immediate"}, min_fit=0)
    row["policy_result"] = decision
    store = Store(row)
    calls = []

    async def prefill(job, profile, **kwargs):
        calls.append((job, profile, kwargs))
        assert "submit" not in kwargs
        return {"page_type": "application_form", "filled": 12,
                "unfilled": [], "review_items": []}

    result = asyncio.run(company_applier.fill_one(
        "real_us", "worker", min_fit=0, store=store, candidate_loader=loader,
        prefill=prefill, artifacts_root=tmp_path))
    assert result["state"] == "ready_for_review"
    assert calls[0][2]["artifact_dir"] == tmp_path / "real_us" / "5"
    assert calls[0][2]["copy_to_downloads"] is False
    assert store.transitions[-1][1] == "ready_for_review"


def test_live_review_items_force_needs_input(tmp_path):
    row = _row(state="preparing")
    row["policy_result"] = company_applier.evaluate(
        company_applier._policy_job(row), _profile(), {"availability": "Immediate"}, min_fit=0)
    store = Store(row)

    async def prefill(*args, **kwargs):
        return {"page_type": "application_form", "filled": 10, "unfilled": [],
                "review_items": [{"question": "I certify"}]}

    result = asyncio.run(company_applier.fill_one(
        "real_us", "worker", min_fit=0, store=store, candidate_loader=loader,
        prefill=prefill, artifacts_root=tmp_path))
    assert result["state"] == "needs_input"


def test_source_has_no_legacy_or_submission_imports():
    source = Path(company_applier.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    for forbidden in ("catalog_db", "catalog_drafts", "dashboard_app", "copilot",
                      "bulk_log", "status_store", "synth_persona"):
        assert not any(forbidden in imported for imported in imports)
    assert "click_submit(" not in source
