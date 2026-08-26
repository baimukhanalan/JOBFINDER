import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.tools import employer_updater as updater


def _clock():
    return datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def test_default_is_read_only_and_does_not_create_checkpoint(tmp_path):
    checkpoint = tmp_path / "updater.json"
    exploding = {stage: lambda _config: pytest.fail("dry-run executed a stage")
                 for stage in updater.STAGES}

    result = updater.run_update(checkpoint=checkpoint, runners=exploding)

    assert result["dry_run"] is True
    assert result["writes_enabled"] is False
    assert result["submit_enabled"] is False
    assert result["stages"] == list(updater.STAGES)
    assert result["stages"][0] == "domain_discovery"
    assert result["config"]["discovery_limit"] == 1000
    assert not checkpoint.exists()


def test_checkpoint_resumes_after_incomplete_stage_with_backoff(tmp_path):
    checkpoint = tmp_path / "updater.json"
    calls = []
    careers_attempts = 0

    def runner(stage):
        def run(_config):
            nonlocal careers_attempts
            calls.append(stage)
            if stage == "careers_ats":
                careers_attempts += 1
                return {"selected": 2, "errors": 1 if careers_attempts == 1 else 0}
            if stage == "jobs":
                return {"companies_selected": 1, "companies_failed": 0,
                        "companies_incomplete": 0, "companies_locked": 0, "errors": []}
            if stage == "questions":
                return {"selected": 1, "complete": 1, "failed": 0, "errors": []}
            return {"selected": 2, "errors": 0}
        return run

    runners = {stage: runner(stage) for stage in updater.STAGES}
    first = updater.run_update(
        apply=True, checkpoint=checkpoint, runners=runners, now=_clock,
        base_backoff=60, max_backoff=600)
    assert first["status"] == "retry_wait"
    assert first["stages"]["domain_discovery"]["status"] == "complete"
    assert first["stages"]["domain_verify"]["status"] == "complete"
    assert first["stages"]["careers_ats"]["metrics"]["errors"] == 1
    assert first["stages"]["careers_ats"]["next_retry_at"] == \
        (_clock() + timedelta(seconds=60)).isoformat(timespec="seconds")

    deferred = updater.run_update(
        apply=True, checkpoint=checkpoint, runners=runners, now=_clock,
        base_backoff=60, max_backoff=600)
    assert deferred["status"] == "retry_wait"
    assert calls == ["domain_discovery", "domain_verify", "careers_ats"]

    complete = updater.run_update(
        apply=True, checkpoint=checkpoint, runners=runners, now=_clock,
        base_backoff=60, max_backoff=600, retry_now=True)
    assert complete["status"] == "complete"
    assert complete["stages"]["careers_ats"]["attempts"] == 2
    assert calls.count("domain_discovery") == 1
    assert calls.count("domain_verify") == 1
    assert calls[-4:] == ["jobs", "incomplete_recovery", "questions", "cohort_score"]
    assert json.loads(checkpoint.read_text())["status"] == "complete"


def test_repeated_incomplete_recovery_exhausts_without_repeating_full_jobs(tmp_path):
    calls = []

    def fail_recovery(_config):
        calls.append("incomplete_recovery")
        return {"companies_failed": 0, "companies_incomplete": 1,
                "companies_locked": 0, "errors": ["challenge"]}

    runners = {
        "domain_discovery": lambda _config: {"errors": 0, "transient": 0},
        "domain_verify": lambda _config: {"errors": 0},
        "careers_ats": lambda _config: {"errors": 0},
        "jobs": lambda _config: {"companies_failed": 0, "companies_incomplete": 137,
                                  "companies_locked": 0, "errors": []},
        "incomplete_recovery": fail_recovery,
        "questions": lambda _config: pytest.fail("questions must remain blocked"),
        "cohort_score": lambda _config: pytest.fail("cohort must remain blocked"),
    }
    checkpoint = tmp_path / "updater.json"
    updater.run_update(apply=True, checkpoint=checkpoint, runners=runners,
                       now=_clock, max_attempts=2)
    result = updater.run_update(apply=True, checkpoint=checkpoint, runners=runners,
                                now=_clock, max_attempts=2, retry_now=True)

    assert result["status"] == "blocked"
    assert result["stages"]["jobs"]["status"] == "complete"
    assert result["stages"]["incomplete_recovery"]["status"] == "exhausted"
    assert result["stages"]["questions"]["status"] == "pending"
    assert calls == ["incomplete_recovery", "incomplete_recovery"]


def test_checkpoint_rejects_changed_runtime_contract(tmp_path):
    checkpoint = tmp_path / "updater.json"
    runners = {stage: lambda _config: {"errors": 0} for stage in updater.STAGES}
    updater.run_update(apply=True, checkpoint=checkpoint, runners=runners,
                       now=_clock, max_stages=1)
    with pytest.raises(ValueError, match="configuration mismatch"):
        updater.run_update(apply=True, checkpoint=checkpoint, runners=runners,
                           now=_clock, workers=2)


def test_domain_discovery_uses_bounded_limit_and_retries_only_transients(monkeypatch):
    calls = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return {"selected": 3, "processed": 3, "transient": 0, "errors": 0}

    monkeypatch.setattr(updater, "enrich_wikidata_search", fake_search)
    runner = updater._default_runners()["domain_discovery"]
    first = runner({"discovery_limit": 250, "workers": 3, "min_interval": 0.4,
                    "stage_attempt": 1})
    retry = runner({"discovery_limit": 250, "workers": 3, "min_interval": 0.4,
                    "stage_attempt": 2})

    assert first["processed"] == 3
    assert calls[0]["limit"] == 250
    assert calls[0]["checkpoint_size"] == 100
    assert calls[0]["retry_transient"] is False
    assert calls[1]["retry_transient"] is True


def test_completed_cycle_waits_before_starting_another_full_scan(tmp_path):
    checkpoint = tmp_path / "updater.json"
    calls = []
    runners = {stage: (lambda _config, name=stage: calls.append(name) or {"errors": 0})
               for stage in updater.STAGES}
    complete = updater.run_update(
        apply=True, checkpoint=checkpoint, runners=runners, now=_clock,
        cycle_interval=3600)
    assert complete["status"] == "complete"

    waiting = updater.run_update(
        apply=True, checkpoint=checkpoint, runners=runners, now=_clock,
        cycle_interval=3600)
    assert waiting["status"] == "cycle_wait"
    assert len(calls) == len(updater.STAGES)


def test_workers_are_bounded_even_in_dry_run():
    with pytest.raises(ValueError, match="between 1 and 4"):
        updater.run_update(workers=5)
