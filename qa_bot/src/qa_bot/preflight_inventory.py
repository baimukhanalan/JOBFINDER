"""Plan, then observe explicitly selected invitation initial screens, read-only.

The launcher owns at most two --preflight Chrome workers. It sends only state
and close, never accepts terms/notices, resumes an attempt, or sends answers.
Reports contain scope identities and classifications, not invitation URLs or
assessment screen text. An unknown state remains unknown.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import time

from qa_bot.parallel_supervisor import ProfileLock, conflicting_processes, read_scope, validate_plan, write_json
from qa_bot.run_audit import LABELS, final_text_status
from qa_bot.jobfinder_source import INVITATION_ERROR_REASONS, InvitationSelectionError

QA_ROOT = Path(__file__).resolve().parents[2]
ANSWER_LOGS = ("actions.jsonl", "choices.jsonl", "typing.jsonl", "personality.jsonl",
               "analytical.jsonl", "computer-actions.jsonl", "sales.jsonl", "writex.jsonl")


def make_inventory_plan(scope, rows, output, *, concurrency=2, timeout=60):
    profiles, digest = read_scope(Path(scope))
    if not rows or len(set(rows)) != len(rows) or any(type(r) is not int or not 1 <= r <= 30 for r in rows):
        raise ValueError("distinct explicit original rows between 1 and 30 required")
    if type(concurrency) is not int or not 1 <= concurrency <= 2:
        raise ValueError("read-only preflight concurrency must be one or two")
    if type(timeout) not in (float, int) or not 5 <= timeout <= 120:
        raise ValueError("preflight observation timeout must be between 5 and 120 seconds")
    output = Path(output).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", output.name):
        raise ValueError("safe output identifier required")
    return {"version": 1, "kind": "read_only_preflight", "scope_file": str(Path(scope).resolve()),
            "scope_sha256": digest, "output": str(output), "concurrency": concurrency,
            "timeout_seconds": timeout,
            "profiles": [{"row": r, "name": profiles[r-1]["name"], "profile_id": f"authorized-row-{r}",
                          "directory": str(output / f"row-{r:02d}")} for r in rows]}


def validate_inventory_plan(plan):
    expected = make_inventory_plan(plan["scope_file"], [r["row"] for r in plan["profiles"]],
                                   plan["output"], concurrency=plan["concurrency"], timeout=plan["timeout_seconds"])
    if plan != expected:
        raise ValueError("preflight scope or plan changed; prepare a new plan")


def classify(state):
    text = state.get("text", "")
    if not isinstance(text, str):
        return "unknown"
    lower = text.lower()
    if "tc100" in lower and ("completed" in lower or "submitted" in lower):
        return "completed_tc100"
    if "to resume your assessment" in lower:
        return "resume_required"
    if state.get("number") is not None or "assessment time out" in lower:
        return "in_progress"
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    if final_text_status(text) == "clean":
        return "completed"
    if final_text_status(text) == "blocked":
        return "confirmation_required"
    if "assessments" in lower and "Assessments\n" in text:
        menu = lines[lines.index("Assessments")+1:] if "Assessments" in lines else []
        statuses = [s for s in menu if s in {"Complete", "Upcoming", "Later", "In Progress", "In progress"}]
        titles = [s for s in menu if s in LABELS]
        if any(s in {"Complete", "In Progress", "In progress"} for s in statuses):
            return "in_progress"
        if titles and len(statuses) == len(titles) and statuses.count("Upcoming") == 1 and all(s in {"Upcoming", "Later"} for s in statuses):
            return "fresh_menu"
        return "unknown"
    if (any(s.lower() in {"terms & conditions", "terms and conditions"} for s in lines)
            and "i agree to terms and conditions" in lower):
        return "fresh_terms"
    if "i confirm i have read and understood this notice" in lower:
        return "notice_required"
    if "assessment description" in lower:
        return "in_progress"
    return "unknown"


def browser_command(spec, plan):
    directory = Path(spec["directory"])
    return [sys.executable, "-u", "-m", "qa_bot.preflight_inventory", "worker", "--", "--selected-profile", spec["name"],
            "--profile-id", spec["profile_id"], "--test", Path(plan["output"]).name + f"-row-{spec['row']:02d}",
            "--output", str(directory / "evidence" / "owned"), "--prompt-dir", str(directory / "prompts"), "--preflight"]


def read_jsonl(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except (ValueError, UnicodeError):
            pass
    return result


def source_error_reason(records, raw_log=""):
    """Accept only allowlisted source diagnostics, never arbitrary stderr text."""
    for record in reversed(records):
        error = record.get("source_error")
        if isinstance(error, dict) and error.get("phase") == "invitation_selection" and isinstance(error.get("reason"), str) and error["reason"] in INVITATION_ERROR_REASONS:
            return error["reason"]
    # Compatibility for workers already launched directly through live_session.
    # The old generic 'one TP invitation required' is intentionally not guessed.
    for line in reversed(raw_log.splitlines()):
        match = re.fullmatch(r"(?:qa_bot\.jobfinder_source\.)?InvitationSelectionError: ([a-z_]+)", line.strip())
        if match and match[1] in INVITATION_ERROR_REASONS:
            return match[1]
    return None


def run_worker(arguments):
    """Reuse the exact current live_session CLI and serialize safe source errors."""
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if "--preflight" not in arguments or any(a == flag or a.startswith(flag + "=") for a in arguments for flag in ("--auto", "--source-browser")):
        raise ValueError("inventory worker requires an isolated read-only preflight")
    from qa_bot.live_session import main as live_main
    original = sys.argv
    try:
        sys.argv = ["qa_bot.live_session", *arguments]
        live_main()
    except InvitationSelectionError as error:
        print(json.dumps({"source_error": error.as_dict()}), flush=True)
        return 2
    finally:
        sys.argv = original
    return 0


def read_only_evidence(directory):
    evidence = directory / "evidence" / "owned"
    metadata = read_jsonl(evidence / "session-metadata.jsonl")
    commands = read_jsonl(evidence / "operator-commands.jsonl")
    bad_commands = [c for c in commands if c.get("action") not in {"state", "close"}]
    answer_records = sum(len(read_jsonl(evidence / name)) for name in ANSWER_LOGS)
    navigation_records = len(read_jsonl(evidence / "navigation.jsonl"))
    configured = bool(metadata) and all(m.get("preflight") is True and m.get("auto") is False for m in metadata)
    return {"preflight_configuration_verified": configured, "answer_records": answer_records,
            "navigation_records": navigation_records, "non_readonly_command_records": len(bad_commands),
            "recorded_actions_read_only": configured and not bad_commands and not answer_records and not navigation_records}


async def observe_one(spec, plan, *, command_factory=browser_command, sample_interval=.55):
    directory = Path(spec["directory"])
    directory.mkdir(parents=True, exist_ok=False)
    report = {"row": spec["row"], "profile_id": spec["profile_id"], "name": spec["name"],
              "classification": "unknown", "fresh_candidate": False, "observations": 0,
              "started_at": time.time()}
    environment = dict(os.environ)
    environment["QA_LOCAL_BRIDGE_TOKEN"] = secrets.token_hex(32)
    process = None
    log = (directory / "worker.log").open("ab", buffering=0)
    read_count = 0
    last_status = None
    last_sample_at = None
    stable = 0
    try:
        process = await asyncio.create_subprocess_exec(*command_factory(spec, plan), cwd=QA_ROOT,
                    stdin=asyncio.subprocess.PIPE, stdout=log, stderr=asyncio.subprocess.STDOUT,
                    env=environment, start_new_session=True)
        report["worker_pid"] = process.pid
        deadline = time.monotonic() + plan["timeout_seconds"]
        ready = False
        pending = False
        next_sample = 0.0
        while time.monotonic() < deadline:
            records = read_jsonl(directory / "worker.log")
            for record in records[read_count:]:
                ready |= record.get("ready") is True
                result = record.get("result")
                if not (pending and isinstance(result, dict) and "text" in result):
                    continue
                pending = False
                status = classify(result)
                now = time.monotonic()
                report["observations"] += 1
                if status == last_status and last_sample_at is not None and now - last_sample_at >= sample_interval * .8:
                    stable += 1
                else:
                    stable = 1
                last_status, last_sample_at = status, now
                raw_number = result.get("number")
                report["observed_question_number"] = int(raw_number) if isinstance(raw_number, (str, int)) and str(raw_number).isdigit() else None
                report["classification"] = status
                report["stable_observations"] = stable
            read_count = len(records)
            safety = read_only_evidence(directory)
            if (safety["answer_records"] or safety["navigation_records"] or safety["non_readonly_command_records"]):
                report["classification"] = "unsafe_preflight_action_observed"
                break
            if stable >= 2 and last_status != "unknown":
                break
            if process.returncode is not None:
                report["classification"] = "unavailable"
                report["worker_exit_code"] = process.returncode
                reason = source_error_reason(read_jsonl(directory / "worker.log"),
                                             (directory / "worker.log").read_text(errors="replace"))
                if reason:
                    report["reason"] = reason
                    report["error_phase"] = "invitation_selection"
                break
            if ready and not pending and time.monotonic() >= next_sample:
                process.stdin.write(b'{"action":"state"}\n')
                await process.stdin.drain()
                pending = True
                next_sample = time.monotonic() + sample_interval
            await asyncio.sleep(.05)
        else:
            report["classification"] = "observation_timeout"
    except asyncio.CancelledError:
        report["classification"] = "interrupted"
        raise
    except Exception as error:
        report["classification"] = "unavailable"
        report["error_class"] = type(error).__name__
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.stdin.write(b'{"action":"close"}\n')
                    await process.stdin.drain()
                    await asyncio.wait_for(process.wait(), 5)
                except (OSError, asyncio.TimeoutError):
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
            if process.stdin:
                process.stdin.close()
            report["worker_closed"] = process.returncode is not None
        log.close()
        report.update(read_only_evidence(directory))
        report["fresh_candidate"] = bool(report["classification"] in {"fresh_terms", "fresh_menu"}
            and stable >= 2 and report["recorded_actions_read_only"])
        if report["classification"] in {"fresh_terms", "fresh_menu"} and not report["fresh_candidate"]:
            report["classification"] = "read_only_evidence_unverified"
        report["finished_at"] = time.time()
        write_json(directory / "result.json", report)
    return report


async def run_inventory(plan, *, command_factory=browser_command, sample_interval=.55, check_processes=True):
    os.umask(0o077)
    validate_inventory_plan(plan)
    output = Path(plan["output"])
    output.mkdir(parents=True, exist_ok=True)
    owner = ProfileLock(output, "inventory-owner")
    results, locks = [], []
    try:
        if (output / "report.json").exists() or any(Path(s["directory"]).exists() for s in plan["profiles"]):
            raise ValueError("preflight output already exists; no overwrite or automatic restart")
        conflicts = set(conflicting_processes(plan["profiles"])) if check_processes else set()
        selected = []
        for spec in plan["profiles"]:
            try:
                if spec["name"] in conflicts or spec["profile_id"] in conflicts:
                    raise ValueError("live process exists")
                locks.append(ProfileLock(Path(plan["scope_file"]).parent / ".parallel-locks", spec["profile_id"]))
                selected.append(spec)
            except ValueError:
                results.append({"row": spec["row"], "name": spec["name"], "profile_id": spec["profile_id"],
                                "classification": "blocked_active_profile", "fresh_candidate": False})
        slots = asyncio.Semaphore(plan["concurrency"])
        async def one(spec):
            async with slots:
                result = await observe_one(spec, plan, command_factory=command_factory, sample_interval=sample_interval)
                results.append(result)
                write_json(output / "report.json", {"state": "running", "profiles": sorted(results, key=lambda r:r["row"]),
                                                     "scope_sha256": plan["scope_sha256"], "updated_at": time.time()})
                print(json.dumps({"row": result["row"], "classification": result["classification"], "fresh_candidate": result["fresh_candidate"], "reason": result.get("reason")}), flush=True)
        tasks = [asyncio.create_task(one(spec)) for spec in selected]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            write_json(output / "report.json", {"state": "interrupted", "profiles": sorted(results, key=lambda r:r["row"]),
                "pending_rows": sorted(s["row"] for s in plan["profiles"] if s["row"] not in {r["row"] for r in results}),
                "scope_sha256": plan["scope_sha256"], "updated_at": time.time()})
            raise
        report = {"state": "finished", "profiles": sorted(results, key=lambda r:r["row"]),
                  "scope_sha256": plan["scope_sha256"], "updated_at": time.time(),
                  "fresh_rows": sorted(r["row"] for r in results if r["fresh_candidate"])}
        write_json(output / "report.json", report)
        return report
    finally:
        for lock in locks:
            lock.close()
        owner.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preparation = sub.add_parser("plan", help="offline only; does not open invitations")
    source = preparation.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="reviewed parallel-supervisor manifest")
    source.add_argument("--rows", help="explicit original row numbers separated by commas")
    preparation.add_argument("--scope", type=Path, default=Path("runs/authorized-scope.json"))
    preparation.add_argument("--output", type=Path, required=True)
    preparation.add_argument("--concurrency", type=int, default=2)
    preparation.add_argument("--timeout", type=float, default=60)
    sub.add_parser("run").add_argument("plan", type=Path)
    sub.add_parser("worker", help=argparse.SUPPRESS).add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "worker":
        return run_worker(args.arguments)
    if args.command == "plan":
        if args.manifest:
            manifest = json.loads(args.manifest.read_text())
            validate_plan(manifest)
            scope, rows = manifest["scope_file"], [s["row"] for s in manifest["attempts"]]
        else:
            scope, rows = args.scope, [int(r) for r in args.rows.split(",")]
        plan = make_inventory_plan(scope, rows, args.output, concurrency=args.concurrency, timeout=args.timeout)
        path = Path(plan["output"]) / "preflight-plan.json"
        if path.exists():
            raise ValueError("preflight plan already exists")
        write_json(path, plan)
        print(json.dumps({"plan": str(path), "profiles": len(rows), "concurrency": args.concurrency, "launched": False}))
    else:
        if not os.environ.get("JOBFINDER_LOGIN") or not os.environ.get("JOBFINDER_PASSWORD"):
            raise ValueError("JobFinder credentials must be present in the local environment")
        plan = json.loads(args.plan.read_text())
        asyncio.run(run_inventory(plan))


if __name__ == "__main__":
    raise SystemExit(main())
