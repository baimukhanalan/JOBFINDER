"""Human-controlled worker for the isolated company-remote application queue.

The worker may prepare and pre-fill an approved application, but it has no code
path that clicks Submit.  It does not import the legacy catalog, dashboard,
copilot, bulk queue, synthetic-persona generator, or status store.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from backend.applier.profile_validator import validate_profile
from backend.applier.runner import prefill_application
from backend.profiles.store import Profile, get_profile, is_sample_profile
from backend.tools import company_apply_db as apply_db
from backend.tools.company_apply_policy import evaluate


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILES = PROJECT_ROOT / "backend" / "data" / "profiles.json"
DEFAULT_FACTS = PROJECT_ROOT / "backend" / "data" / "facts"
DEFAULT_ARTIFACTS = PROJECT_ROOT / "uploads" / "company_remote_apply"


def _paths() -> tuple[Path, Path, Path]:
    profiles = Path(os.getenv("COMPANY_APPLY_PROFILES_FILE", str(DEFAULT_PROFILES)))
    facts = Path(os.getenv("COMPANY_APPLY_FACTS_DIR", str(DEFAULT_FACTS)))
    artifacts = Path(os.getenv("COMPANY_APPLY_ARTIFACTS_DIR", str(DEFAULT_ARTIFACTS)))
    return profiles, facts, artifacts


def load_candidate(profile_id: str, *, profiles_file: Path | None = None,
                   facts_dir: Path | None = None) -> tuple[Profile, dict]:
    default_profiles, default_facts, _ = _paths()
    profile = get_profile(profile_id, path=profiles_file or default_profiles)
    if is_sample_profile(profile) or profile.is_synthetic:
        raise ValueError("sample or synthetic profiles are forbidden")
    problems = validate_profile(profile.to_form_dict() | {
        "full_name": profile.full_name, "email": profile.email, "phone": profile.phone})
    if problems:
        raise ValueError("profile is not submittable: " + "; ".join(problems))
    path = (facts_dir or default_facts) / f"{profile_id}.json"
    if not path.is_file():
        raise ValueError(f"facts file is missing for {profile_id}")
    facts = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(facts, dict) or not facts:
        raise ValueError(f"facts are empty for {profile_id}")
    return profile, facts


def _policy_job(row: dict) -> dict:
    return {
        **row,
        "status": row.get("job_status") or row.get("status"),
        "company": row.get("company_name") or "",
        "questions": row.get("questions") or [],
        "question_set_hash": row.get("current_revalidation_hash"),
    }


def prepare_one(profile_id: str, worker_id: str, *, min_fit: float = 35,
                store: Any = apply_db,
                candidate_loader: Callable[[str], tuple[Profile, dict]] = load_candidate) -> dict:
    """Policy-check one queued row and stop at explicit human approval."""
    row = store.claim_next(profile_id, worker_id, from_states=("queued",))
    if not row:
        return {"processed": False, "reason": "empty_queue"}
    application_id = int(row["id"])
    try:
        profile, facts = candidate_loader(profile_id)
        decision = evaluate(_policy_job(row), profile, facts, min_fit=min_fit)
        target = "awaiting_approval" if decision["allowed"] else "blocked"
        store.transition(
            application_id, target, worker_id,
            reason="; ".join(decision["blocking_reasons"]) or None,
            expected_revalidation_hash=row["revalidation_hash"],
            payload={"policy_result": decision, "fit_score": decision.get("fit_score")},
        )
        store.record_attempt(application_id, "policy", target, worker_id=worker_id,
                             detail=decision)
        return {"processed": True, "application_id": application_id,
                "state": target, "policy": decision}
    except Exception as exc:
        store.transition(application_id, "failed", worker_id, reason=str(exc),
                         expected_revalidation_hash=row.get("revalidation_hash"),
                         payload={"last_error": str(exc)[:500]})
        store.record_attempt(application_id, "policy", "failed", worker_id=worker_id,
                             detail={"error": str(exc)[:500]})
        return {"processed": True, "application_id": application_id,
                "state": "failed", "error": str(exc)}


@contextmanager
def _runner_facts(facts: dict):
    """Inject facts for a configured external data root; worker concurrency is one."""
    from backend.applier import runner
    original = runner.load_facts
    runner.load_facts = lambda _profile_id: facts
    try:
        yield
    finally:
        runner.load_facts = original


async def fill_one(profile_id: str, worker_id: str, *, use_ai: bool = False,
                   min_fit: float = 35, store: Any = apply_db,
                   candidate_loader: Callable[[str], tuple[Profile, dict]] = load_candidate,
                   prefill: Callable[..., Any] = prefill_application,
                   artifacts_root: Path | None = None) -> dict:
    """Pre-fill one explicitly approved row, then stop for final human review."""
    row = store.claim_next(profile_id, worker_id, from_states=("approved",))
    if not row:
        return {"processed": False, "reason": "no_approved_application"}
    application_id = int(row["id"])
    _, _, default_artifacts = _paths()
    artifact_dir = (artifacts_root or default_artifacts) / profile_id / str(application_id)
    try:
        profile, facts = candidate_loader(profile_id)
        decision = evaluate(_policy_job(row), profile, facts, min_fit=min_fit)
        prior = row.get("policy_result") or {}
        if not decision["allowed"]:
            store.transition(
                application_id, "blocked", worker_id,
                reason="; ".join(decision["blocking_reasons"]),
                expected_revalidation_hash=row["revalidation_hash"],
                payload={"policy_result": decision})
            return {"processed": True, "application_id": application_id, "state": "blocked"}
        if prior and prior.get("revalidation_hash") != decision["revalidation_hash"]:
            store.transition(
                application_id, "needs_input", worker_id,
                reason="profile, facts, JD or questions changed after approval",
                expected_revalidation_hash=row["revalidation_hash"],
                payload={"policy_result": decision})
            return {"processed": True, "application_id": application_id,
                    "state": "needs_input"}

        job = {
            "title": row.get("title") or "",
            "company": row.get("company_name") or "",
            "description": row.get("description") or "",
            "apply_url": row.get("apply_url") or "",
        }
        with _runner_facts(facts):
            report = await prefill(
                job, profile, headless=True, use_ai=use_ai, draft_answers=True,
                use_variants=False, hold_open=False, resume_parser_only=False,
                artifact_dir=artifact_dir, copy_to_downloads=False,
            )
        unfilled = report.get("unfilled") or []
        review_items = report.get("review_items") or []
        page_ok = report.get("page_type") == "application_form"
        target = "ready_for_review" if page_ok and not unfilled and not review_items \
            else "needs_input"
        store.transition(
            application_id, target, worker_id,
            reason=None if target == "ready_for_review" else "live form needs human input",
            expected_revalidation_hash=row["revalidation_hash"],
            payload={"artifact_dir": str(artifact_dir), "report": report,
                     "policy_result": decision, "fit_score": decision.get("fit_score")},
        )
        store.record_attempt(application_id, "prefill", target, worker_id=worker_id,
                             detail={"artifact_dir": str(artifact_dir),
                                     "filled": report.get("filled"),
                                     "unfilled": unfilled, "review_items": review_items})
        return {"processed": True, "application_id": application_id, "state": target,
                "artifact_dir": str(artifact_dir), "filled": report.get("filled"),
                "unfilled": unfilled, "review_items": review_items}
    except Exception as exc:
        store.transition(application_id, "failed", worker_id, reason=str(exc),
                         expected_revalidation_hash=row.get("revalidation_hash"),
                         payload={"last_error": str(exc)[:500],
                                  "artifact_dir": str(artifact_dir)})
        store.record_attempt(application_id, "prefill", "failed", worker_id=worker_id,
                             detail={"error": str(exc)[:500]})
        return {"processed": True, "application_id": application_id,
                "state": "failed", "error": str(exc)}


def _public(row: dict) -> dict:
    return {key: row.get(key) for key in (
        "id", "profile_id", "state", "company_name", "title", "apply_url",
        "fit_score", "artifact_dir", "revalidation_hash", "state_changed_at")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated human-controlled company remote application worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--profile", required=True)
    enqueue.add_argument("--limit", type=int, default=500)
    enqueue.add_argument("--freshness-days", type=int, default=7)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--profile", required=True)
    prepare.add_argument("--limit", type=int, default=10)
    prepare.add_argument("--min-fit", type=float, default=35)
    fill = sub.add_parser("fill-approved")
    fill.add_argument("--profile", required=True)
    fill.add_argument("--limit", type=int, default=1)
    fill.add_argument("--min-fit", type=float, default=35)
    fill.add_argument("--ai", action="store_true")
    review = sub.add_parser("list")
    review.add_argument("--profile")
    review.add_argument("--state", choices=apply_db.STATES)
    review.add_argument("--limit", type=int, default=100)
    approve = sub.add_parser("approve")
    approve.add_argument("--id", type=int, required=True)
    approve.add_argument("--actor", required=True)
    reject = sub.add_parser("reject")
    reject.add_argument("--id", type=int, required=True)
    reject.add_argument("--actor", required=True)
    reject.add_argument("--reason", required=True)
    submitted = sub.add_parser("mark-submitted")
    submitted.add_argument("--id", type=int, required=True)
    submitted.add_argument("--actor", required=True)
    submitted.add_argument("--receipt", default="{}", help="JSON evidence")
    stats = sub.add_parser("stats")
    stats.add_argument("--profile")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    worker = f"{socket.gethostname()}:{os.getpid()}"
    try:
        if args.command == "init":
            apply_db.ensure_schema()
            result = {"initialized": True}
        elif args.command == "enqueue":
            result = {"enqueued": apply_db.enqueue_eligible(
                args.profile, freshness_days=args.freshness_days, limit=args.limit)}
        elif args.command == "prepare":
            results = [prepare_one(args.profile, worker, min_fit=args.min_fit)
                       for _ in range(max(1, args.limit))]
            result = {"results": results}
        elif args.command == "fill-approved":
            results = [asyncio.run(fill_one(
                args.profile, worker, use_ai=args.ai, min_fit=args.min_fit))
                for _ in range(max(1, args.limit))]
            result = {"results": results}
        elif args.command == "list":
            result = {"applications": [_public(row) for row in apply_db.list_applications(
                profile_id=args.profile, state=args.state, limit=args.limit)]}
        elif args.command == "approve":
            row = apply_db.get_application(args.id)
            if not row:
                raise ValueError("application not found")
            result = _public(apply_db.approve(
                args.id, args.actor, row["revalidation_hash"]))
        elif args.command == "reject":
            result = _public(apply_db.reject(args.id, args.actor, args.reason))
        elif args.command == "mark-submitted":
            result = _public(apply_db.mark_human_submitted(
                args.id, args.actor, receipt=json.loads(args.receipt)))
        else:
            result = apply_db.stats(profile_id=args.profile)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
