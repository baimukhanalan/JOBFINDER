"""Checkpointed local updater for the active employer population.

The updater only orchestrates existing evidence and collection pipelines in this
order: exact-name/alias domain discovery, domain verification, careers/ATS
enrichment, remote jobs, application question discovery, then cohort/scoring.
It never submits an application.

Examples::

    python -m backend.tools.employer_updater run
    python -m backend.tools.employer_updater run --apply --resume
    python -m backend.tools.employer_updater status

``run`` is read-only by default.  ``--apply`` is required for every stage that
writes enrichment, jobs, questions, cohort decisions, or scores.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.tools.company_jobs import (
    collect_company_jobs,
    collect_company_jobs_parallel,
    collect_pending_questions_parallel,
)
from backend.tools import company_discovery_db as company_db
from backend.tools.custom_board_recovery import recover_custom_boards
from backend.tools.employer_careers import enrich_verified_careers
from backend.tools.employer_domain_verifier import verify_domains
from backend.tools.employer_hiring_cohort import refresh_hiring_cohort
from backend.tools.employer_identity_enrichment import enrich_wikidata_search
from backend.tools.employer_scoring import score_employers


STAGES = ("domain_discovery", "domain_verify", "careers_ats", "jobs",
          "incomplete_recovery", "questions", "cohort_score")
DISCOVERY_STATUSES = ("novel", "known", "possible_duplicate", "promoted")
DEFAULT_CHECKPOINT = Path(__file__).resolve().parents[2] / \
    ".cache" / "jobfinder" / "employer-updater.json"
STATE_VERSION = 1

Runner = Callable[[Mapping[str, Any]], dict[str, Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _fingerprint(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(config), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_checkpoint(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _run_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another employer updater process owns this checkpoint") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _new_state(config: Mapping[str, Any], *, cycle: int = 1) -> dict[str, Any]:
    now = _iso(_now())
    return {
        "version": STATE_VERSION,
        "fingerprint": _fingerprint(config),
        "config": dict(config),
        "cycle": cycle,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "stages": {name: {"status": "pending", "attempts": 0} for name in STAGES},
    }


def _load_state(path: Path, config: Mapping[str, Any], *, resume: bool,
                current_time: datetime, force: bool = False) -> dict[str, Any]:
    if not resume or not path.exists():
        return _new_state(config)
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise ValueError("checkpoint version mismatch")
    if state.get("fingerprint") != _fingerprint(config):
        raise ValueError("checkpoint configuration mismatch; use --no-resume")
    if state.get("status") == "complete":
        completed = datetime.fromisoformat(
            str(state["completed_at"]).replace("Z", "+00:00"))
        next_cycle = completed + timedelta(seconds=int(config["cycle_interval"]))
        if not force and current_time < next_cycle:
            return {**state, "status": "cycle_wait", "next_cycle_at": _iso(next_cycle)}
        return _new_state(config, cycle=int(state.get("cycle") or 1) + 1)
    return state


def _trim_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(metrics)
    raw_errors = result.get("errors")
    errors = list(raw_errors) if isinstance(raw_errors, (list, tuple)) else []
    if errors:
        result["errors"] = [str(error)[:500] for error in errors[:50]]
        if len(errors) > 50:
            result["errors_truncated"] = len(errors) - 50
    return result


def _incomplete(stage: str, metrics: Mapping[str, Any]) -> bool:
    if stage == "domain_discovery":
        return (int(metrics.get("errors") or 0) > 0
                or int(metrics.get("transient") or 0) > 0)
    if stage in {"domain_verify", "careers_ats"}:
        return int(metrics.get("errors") or 0) > 0
    if stage == "jobs":
        # Partial boards move to the dedicated recovery stage.  Re-running the
        # entire active population because custom boards remain partial would be
        # wasteful and can hammer the same unavailable pages every day.
        return any(int(metrics.get(key) or 0) > 0 for key in (
            "companies_failed", "companies_locked"))
    if stage == "incomplete_recovery":
        return any(int(metrics.get(key) or 0) > 0 for key in (
            "companies_failed", "companies_incomplete", "companies_locked"))
    if stage == "questions":
        return int(metrics.get("failed") or 0) > 0
    return False


def _sum_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {"errors": []}
    for item in items:
        for key, value in item.items():
            if key == "errors":
                total["errors"].extend(value or [])
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


def _default_runners() -> dict[str, Runner]:
    def discovery(config: Mapping[str, Any]) -> dict[str, Any]:
        discovery_limit = int(config["discovery_limit"])
        return enrich_wikidata_search(
            limit=discovery_limit, workers=int(config["workers"]),
            min_interval=float(config["min_interval"]),
            checkpoint_size=min(100, discovery_limit), retries=3,
            retry_transient=int(config.get("stage_attempt") or 1) > 1,
            dry_run=False)

    def domain(config: Mapping[str, Any]) -> dict[str, Any]:
        return verify_domains(limit=int(config["limit"]), workers=int(config["workers"]),
                              min_interval=float(config["min_interval"]))

    def careers(config: Mapping[str, Any]) -> dict[str, Any]:
        return enrich_verified_careers(
            limit=int(config["limit"]), workers=int(config["workers"]),
            min_interval=float(config["min_interval"]))

    def jobs(config: Mapping[str, Any]) -> dict[str, Any]:
        # A per-status quota prevents a large novel pool from starving known or
        # promoted active employers forever.  The total remains bounded by limit.
        limit = int(config["limit"])
        quota = max(1, math.ceil(limit / len(DISCOVERY_STATUSES)))
        remaining = limit
        results = []
        for status in DISCOVERY_STATUSES:
            if remaining <= 0:
                break
            current_limit = min(quota, remaining)
            item = collect_company_jobs_parallel(
                status=status, limit_companies=current_limit,
                workers=int(config["workers"]))
            item["status_scope"] = status
            results.append(item)
            remaining -= int(item.get("companies_selected") or 0)
        return {**_sum_metrics(results), "status_runs": results}

    def incomplete_recovery(config: Mapping[str, Any]) -> dict[str, Any]:
        # Retry only boards whose latest scan is incomplete.  The updater's
        # checkpoint applies exponential backoff to this bounded delta instead
        # of repeating the full jobs stage.
        custom = recover_custom_boards(
            limit=int(config["limit"]), workers=int(config["workers"]), apply=True)
        remaining_limit = max(0, int(config["limit"]) - int(custom.get("selected") or 0))
        with company_db._cur() as cur:
            cur.execute("""
              WITH latest AS (
                SELECT DISTINCT ON (s.company_id,s.source,s.source_board_id)
                  s.company_id,s.source,s.source_board_id,s.scan_complete,s.started_at
                FROM company_remote_job_scans s
                ORDER BY s.company_id,s.source,s.source_board_id,s.started_at DESC,s.id DESC
              )
              SELECT l.company_id,l.source,l.source_board_id
              FROM latest l JOIN company_employer_master m ON m.company_id=l.company_id
              JOIN company_discovery c ON c.id=l.company_id
              WHERE m.in_target_population AND m.domain_verified
                AND l.scan_complete=FALSE AND lower(c.ats)=l.source
                AND c.ats_slug=l.source_board_id
                AND l.source<>'custom'
              ORDER BY (l.source='custom') DESC,l.started_at,l.company_id
              LIMIT %s
            """, (remaining_limit,))
            targets = [dict(row) for row in cur.fetchall()]
        sources = Counter(str(row.get("source") or "") for row in targets)
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=int(config["workers"])) as executor:
            futures = [executor.submit(
                collect_company_jobs, company_id=int(row["company_id"]),
                collect_questions=False) for row in targets]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"companies_selected": 1, "companies_failed": 1,
                                    "errors": [str(exc)]})
        collected = _sum_metrics(results)
        collected["companies_incomplete"] = int(collected.get("companies_incomplete") or 0) \
            + int(custom.get("incomplete") or 0)
        collected["companies_failed"] = int(collected.get("companies_failed") or 0)
        collected["companies_locked"] = int(collected.get("companies_locked") or 0)
        collected["errors"].extend(custom.get("errors") or [])
        return {**collected,
                "recovery_selected": len(targets) + int(custom.get("selected") or 0),
                "custom_recovery": custom,
                "selected_by_ats": dict(sorted(sources.items()))}

    def questions(config: Mapping[str, Any]) -> dict[str, Any]:
        return collect_pending_questions_parallel(
            limit=int(config["question_limit"]), workers=int(config["workers"]),
            headless=True, retry_failed=bool(config["retry_failed_questions"]))

    def cohort_score(config: Mapping[str, Any]) -> dict[str, Any]:
        cohort = refresh_hiring_cohort(limit=int(config["limit"]), apply=True)
        scores = score_employers(limit=int(config["limit"]))
        return {"cohort": cohort, "scores": scores}

    return {"domain_discovery": discovery, "domain_verify": domain,
            "careers_ats": careers, "jobs": jobs,
            "incomplete_recovery": incomplete_recovery,
            "questions": questions, "cohort_score": cohort_score}


def run_update(*, apply: bool = False, checkpoint: str | Path = DEFAULT_CHECKPOINT,
               resume: bool = True, retry_now: bool = False, limit: int = 10_000,
               discovery_limit: int = 1_000,
               question_limit: int = 500, workers: int = 4,
               min_interval: float = 0.25, base_backoff: int = 300,
               max_backoff: int = 86_400, max_attempts: int = 5,
               cycle_interval: int = 86_400,
               max_stages: int = len(STAGES), retry_failed_questions: bool = False,
               runners: Mapping[str, Runner] | None = None,
               now: Callable[[], datetime] = _now) -> dict[str, Any]:
    """Run or plan one resumable updater cycle.

    An incomplete stage blocks dependent stages.  It is retried exponentially
    until ``max_attempts``; exhausted checkpoints require operator intervention
    via ``--no-resume`` after the underlying blocker is understood.
    """
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    if limit < 1 or discovery_limit < 1 or question_limit < 1 or max_stages < 1:
        raise ValueError("limits must be positive")
    if (base_backoff < 1 or max_backoff < base_backoff or max_attempts < 1
            or cycle_interval < 60):
        raise ValueError("invalid retry policy")
    config = {
        "limit": int(limit), "discovery_limit": min(int(discovery_limit), int(limit)),
        "question_limit": int(question_limit),
        "workers": int(workers), "min_interval": float(min_interval),
        "base_backoff": int(base_backoff), "max_backoff": int(max_backoff),
        "max_attempts": int(max_attempts),
        "cycle_interval": int(cycle_interval),
        "retry_failed_questions": bool(retry_failed_questions),
    }
    checkpoint_path = Path(checkpoint)
    if not apply:
        existing = None
        if checkpoint_path.exists():
            try:
                existing = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {"status": "unreadable"}
        return {"dry_run": True, "writes_enabled": False, "submit_enabled": False,
                "stages": list(STAGES), "config": config,
                "checkpoint": str(checkpoint_path), "existing_checkpoint": existing}

    stage_runners = dict(runners or _default_runners())
    missing = [stage for stage in STAGES if stage not in stage_runners]
    if missing:
        raise ValueError(f"missing stage runners: {missing}")

    with _run_lock(checkpoint_path):
        state = _load_state(
            checkpoint_path, config, resume=resume, current_time=now(), force=retry_now)
        if state.get("status") == "cycle_wait":
            return {"dry_run": False, "writes_enabled": True, "submit_enabled": False,
                    "checkpoint": str(checkpoint_path), **state}
        executed = 0
        for stage in STAGES:
            record = state["stages"][stage]
            if record.get("status") == "complete":
                continue
            if record.get("status") == "exhausted":
                state["status"] = "blocked"
                break
            retry_at = record.get("next_retry_at")
            if retry_at and not retry_now:
                due = datetime.fromisoformat(str(retry_at).replace("Z", "+00:00"))
                if now() < due:
                    state["status"] = "retry_wait"
                    break
            if executed >= max_stages:
                state["status"] = "checkpointed"
                break

            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["status"] = "running"
            record["started_at"] = _iso(now())
            record.pop("next_retry_at", None)
            state["status"] = "running"
            state["updated_at"] = _iso(now())
            _write_checkpoint(checkpoint_path, state)
            try:
                metrics = _trim_metrics(stage_runners[stage](
                    {**config, "stage_attempt": int(record["attempts"])}))
                incomplete = _incomplete(stage, metrics)
                error = None
            except Exception as exc:  # Checkpoint the failure; never skip dependencies.
                metrics = {"errors": [f"{type(exc).__name__}: {exc}"]}
                incomplete = True
                error = str(exc)[:500]
            record["metrics"] = metrics
            record["finished_at"] = _iso(now())
            if incomplete:
                attempts = int(record["attempts"])
                if attempts >= max_attempts:
                    record["status"] = "exhausted"
                    state["status"] = "blocked"
                else:
                    delay = min(max_backoff, base_backoff * (2 ** (attempts - 1)))
                    record["status"] = "retry_wait"
                    record["next_retry_at"] = _iso(now() + timedelta(seconds=delay))
                    state["status"] = "retry_wait"
                if error:
                    record["error"] = error
                executed += 1
                state["updated_at"] = _iso(now())
                _write_checkpoint(checkpoint_path, state)
                break
            record["status"] = "complete"
            record.pop("error", None)
            executed += 1
            state["updated_at"] = _iso(now())
            _write_checkpoint(checkpoint_path, state)
        else:
            state["status"] = "complete"
            state["completed_at"] = _iso(now())
            state["next_cycle_at"] = _iso(
                now() + timedelta(seconds=int(config["cycle_interval"])))
            state["updated_at"] = state["completed_at"]
            _write_checkpoint(checkpoint_path, state)
        return {"dry_run": False, "writes_enabled": True, "submit_enabled": False,
                "checkpoint": str(checkpoint_path), **state}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="plan or execute one checkpointed updater cycle")
    run.add_argument("--apply", action="store_true",
                     help="enable DB/network stages; default only prints the plan")
    run.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    run.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--retry-now", action="store_true")
    run.add_argument("--limit", type=int, default=10_000)
    run.add_argument("--discovery-limit", type=int, default=1_000,
                     help="bounded unresolved exact-name/alias searches per cycle")
    run.add_argument("--question-limit", type=int, default=500)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--min-interval", type=float, default=0.25)
    run.add_argument("--base-backoff", type=int, default=300)
    run.add_argument("--max-backoff", type=int, default=86_400)
    run.add_argument("--max-attempts", type=int, default=5)
    run.add_argument("--cycle-interval", type=int, default=86_400,
                     help="minimum seconds between complete full cycles")
    run.add_argument("--max-stages", type=int, default=len(STAGES))
    run.add_argument("--retry-failed-questions", action="store_true")
    status = sub.add_parser("status", help="read the local checkpoint without changes")
    status.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        path = Path(args.checkpoint)
        output = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                  else {"status": "not_started", "checkpoint": str(path)})
    else:
        output = run_update(
            apply=args.apply, checkpoint=args.checkpoint, resume=args.resume,
            retry_now=args.retry_now, limit=args.limit,
            discovery_limit=args.discovery_limit,
            question_limit=args.question_limit, workers=args.workers,
            min_interval=args.min_interval, base_backoff=args.base_backoff,
            max_backoff=args.max_backoff, max_attempts=args.max_attempts,
            cycle_interval=args.cycle_interval,
            max_stages=args.max_stages,
            retry_failed_questions=args.retry_failed_questions,
        )
    print(json.dumps(output, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
