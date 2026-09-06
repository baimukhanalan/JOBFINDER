"""Detached, bounded ownership of explicitly selected authorized QA attempts.

Planning is offline. Starting is a separate command. A finished platform screen
is recorded as platform_complete, not as proof that every answer was correct.
There are no automatic restarts, skipped questions, or terms acceptance.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time

ORIGIN = "https://amcatglobal.aspiringminds.com"
PORT_NAMES = ("speech", "script", "prompt_capture", "recording_capture")
PORT_ENV = ("QA_SPEECH_PORT", "QA_SCRIPT_PORT", "QA_PROMPT_CAPTURE_PORT", "QA_RECORDING_CAPTURE_PORT")
TERMINAL = {"platform_complete", "timed_out", "failed", "interrupted", "cancelled"}
INSPECT_ACTIONS = ("state", "controls", "screenshot", "device_diagnostics")


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def read_scope(path: Path):
    raw = path.read_bytes()
    records = json.loads(raw)["profiles"]
    if (len(records) != 30 or [r["original_row"] for r in records] != list(range(1, 31))
            or len({r["name"] for r in records}) != 30
            or any(not isinstance(r["name"], str) or not r["name"].strip() for r in records)):
        raise ValueError("scope must contain the original 30 distinct profiles in original order")
    return records, hashlib.sha256(raw).hexdigest()


def make_plan(scope: Path, output: Path, rows: list[int], *, concurrency=10,
              base_port=19769, speech_bank_dir: Path | None = None, replay_only=True):
    records, digest = read_scope(scope)
    if (not rows or len(set(rows)) != len(rows) or any(type(r) is not int or r not in range(1, 31) for r in rows)):
        raise ValueError("select distinct original row numbers between 1 and 30")
    if type(concurrency) is not int or not 1 <= concurrency <= 10:
        raise ValueError("concurrency must be between 1 and 10")
    if type(replay_only) is not bool:
        raise ValueError("replay_only must be an explicit boolean")
    if type(base_port) is not int or base_port < 1024 or base_port + 4 * len(rows) > 65536:
        raise ValueError("invalid isolated port range")
    output = output.resolve()
    attempts = []
    for index, row in enumerate(rows):
        key = f"row-{row:02d}"
        attempts.append({"row": row, "name": records[row-1]["name"],
                         "profile_id": f"authorized-row-{row}", "test_id": output.name + "-" + key,
                         "directory": str(output / key),
                         "ports": dict(zip(PORT_NAMES, range(base_port + index*4, base_port + index*4 + 4)))})
    return {"version": 1, "scope_file": str(scope.resolve()), "scope_sha256": digest,
            "output": str(output), "concurrency": concurrency, "replay_only": replay_only,
            "speech_bank_dir": str(speech_bank_dir.resolve()) if speech_bank_dir else None,
            "attempts": attempts}


def validate_plan(plan):
    records, digest = read_scope(Path(plan["scope_file"]))
    if plan.get("version") != 1 or plan.get("scope_sha256") != digest:
        raise ValueError("authorized scope changed; review and make a new plan")
    attempts = plan["attempts"]
    if not attempts:
        raise ValueError("empty plan")
    expected = make_plan(Path(plan["scope_file"]), Path(plan["output"]),
                         [a["row"] for a in attempts], concurrency=plan["concurrency"],
                         base_port=attempts[0]["ports"]["speech"],
                         speech_bank_dir=Path(plan["speech_bank_dir"]) if plan.get("speech_bank_dir") else None,
                         replay_only=plan["replay_only"])
    if plan != expected:
        raise ValueError("plan identity, paths, ports, or replay policy changed; make a new plan")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", Path(plan["output"]).name):
        raise ValueError("output directory name must be a safe test identifier")
    return records


class ProfileLock:
    """An OS lock survives deleted status files and is released on process exit."""
    def __init__(self, directory: Path, profile_id: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = (directory / (profile_id + ".lock")).open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise ValueError(f"profile already reserved: {profile_id}") from None

    def close(self):
        self.handle.close()


def conflicting_processes(attempts, process_text=None):
    if process_text is None:
        process_text = subprocess.check_output(["ps", "-axo", "command="], text=True)
    names = {a["name"] for a in attempts}
    ids = {a["profile_id"] for a in attempts}
    conflicts = set()
    for line in process_text.splitlines():
        if "qa_bot.live_session" not in line:
            continue
        # ps may remove the quotes around a multiword argv entry on macOS.
        for name in names:
            if re.search(r"--selected-profile(?:=|\s+)[\"']?" + re.escape(name) + r"[\"']?(?=\s+--|\s*$)", line):
                conflicts.add(name)
        for profile_id in ids:
            if re.search(r"--profile-id(?:=|\s+)" + re.escape(profile_id) + r"(?=\s|$)", line):
                conflicts.add(profile_id)
    return sorted(conflicts)


def commands(attempt, plan, python=sys.executable):
    path = Path(attempt["directory"])
    ports = attempt["ports"]
    common = [python, "-u", "-m"]
    speech = common + ["qa_bot.speech_bridges", "--origin", ORIGIN,
             "--profile", attempt["profile_id"], "--test", attempt["test_id"],
             "--output", str(path), "--counter-selector", "button.currentQue",
             "--suspension-selector", '[id^="ngdialog"]',
             "--repeat-section", "Section B: Listen and Repeat",
             "--speech-port", str(ports["speech"]), "--script-port", str(ports["script"]),
             "--capture-port", str(ports["prompt_capture"]) ]
    recorder = common + ["qa_bot.audio.capture_bridge", "--origin", ORIGIN,
                         "--output", str(path / "evidence"), "--port", str(ports["recording_capture"])]
    browser = common + ["qa_bot.live_session", "--selected-profile", attempt["name"],
                        "--profile-id", attempt["profile_id"], "--test", attempt["test_id"],
                        "--output", str(path / "evidence" / "owned"),
                        "--prompt-dir", str(path / "prompts"), "--auto"]
    if plan["replay_only"]:
        speech.append("--replay-only")
        browser.append("--replay-only")
    if plan.get("speech_bank_dir"):
        speech += ["--speech-bank-dir", plan["speech_bank_dir"]]
        browser += ["--speech-bank-dir", plan["speech_bank_dir"]]
    return [("speech", speech), ("recorder", recorder), ("browser", browser)]


def ports_available(attempts):
    reservations = []
    try:
        for attempt in attempts:
            for port in attempt["ports"].values():
                sock = socket.socket()
                reservations.append(sock)
                sock.bind(("127.0.0.1", port))
    finally:
        for sock in reservations:
            sock.close()


def listening(ports):
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.2):
                pass
        except OSError:
            return False
    return True


class Attempt:
    def __init__(self, spec, plan, command_factory=commands, require_ports=True):
        self.spec, self.plan = spec, plan
        self.path = Path(spec["directory"])
        self.path.mkdir(parents=True, exist_ok=False)
        self.children = {}
        self.logs = []
        self.offsets = {}
        self.state = "starting"
        self.error_count = 0
        self.diagnostic_count = 0
        self.terms_visible = False
        self.latest_text = ""
        self.final_probe_pending = False
        self.final_probe_requested_at = None
        self.final_probe_last_at = 0.0
        self.completion_audit = None
        self.last_question = None
        self.started_at = time.time()
        self.reason = None
        self.command_sequence = 0
        token = secrets.token_hex(32)
        environment = os.environ.copy()
        environment.update({variable: str(spec["ports"][name]) for name, variable in zip(PORT_NAMES, PORT_ENV)})
        environment.update(QA_LOCAL_BRIDGE_TOKEN=token, QA_CAPTURE_TOKEN=token)
        try:
            for role, command in command_factory(spec, plan):
                if role == "browser" and require_ports:
                    deadline = time.monotonic() + 15
                    while not listening(spec["ports"].values()):
                        if any(p.poll() is not None for p in self.children.values()):
                            raise RuntimeError("speech service exited before browser startup")
                        if time.monotonic() >= deadline:
                            raise RuntimeError("speech services did not become ready")
                        time.sleep(.1)
                log = (self.path / f"{role}.log").open("ab", buffering=0)
                self.logs.append(log)
                self.children[role] = subprocess.Popen(command, stdin=subprocess.PIPE if role == "browser" else subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
            self.state = "running"
        except BaseException:
            self.close()
            raise

    def tail(self, path):
        """Read only complete appended records, preserving partial final JSON."""
        if not path.exists():
            return []
        offset = self.offsets.get(path, 0)
        with path.open("rb") as stream:
            stream.seek(offset)
            payload = stream.read()
        if not payload or b"\n" not in payload:
            return []
        complete, _, _partial = payload.rpartition(b"\n")
        self.offsets[path] = offset + len(complete) + 1
        result = []
        for line in complete.splitlines():
            try:
                result.append(json.loads(line))
            except (ValueError, UnicodeError):
                pass
        return result

    def send(self, command):
        browser = self.children["browser"]
        if browser.poll() is not None:
            raise RuntimeError("browser worker exited")
        browser.stdin.write((json.dumps(command) + "\n").encode())
        browser.stdin.flush()

    def poll(self):
        from qa_bot.run_audit import audit, final_text_status
        evidence = self.path / "evidence" / "owned"
        # Drain only already-written responses before sending a new probe. The
        # supervisor owns stdin; no detached browser owner or second CDP client.
        replies = self.tail(self.path / "browser.log")
        observed = self.tail(evidence / "states.jsonl")
        for reply in replies:
            result = reply.get("result")
            if not (self.final_probe_pending and isinstance(result, dict) and "text" in result):
                continue
            self.final_probe_pending = False
            observed.append(result)
            status = final_text_status(result["text"])
            record = {"time": time.time(), "request_time": self.final_probe_requested_at,
                      "source": "owned_state_response", "text_status": status,
                      "visible_text": result["text"] if status == "clean" else None}
            evidence.mkdir(parents=True, exist_ok=True)
            with (evidence / "final-handshake.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\n")
        for record in observed:
            text = record.get("text", "")
            self.latest_text = text
            self.last_question = record.get("number")
            self.terms_visible = "TERMS & CONDITIONS" in text
            if "Assessment Time out" in text:
                self.state, self.reason = "timed_out", "platform_time_limit"
            elif final_text_status(text) != "absent" and self.state not in TERMINAL:
                self.state, self.reason = "needs_attention", "final_confirmation_or_other_visible_content" if final_text_status(text) == "blocked" else "final_verification_pending"
            elif self.terms_visible and self.state not in TERMINAL:
                self.state = "awaiting_terms" if not self.error_count else "needs_attention"
            elif self.state == "awaiting_terms":
                self.state = "running" if not self.error_count else "needs_attention"
        for path in evidence.glob("*errors.jsonl"):
            added = len(self.tail(path))
            if added:
                if path.name in {"page-errors.jsonl", "observer-errors.jsonl"}:
                    self.diagnostic_count += added
                    continue
                self.error_count += added
                if self.state not in TERMINAL:
                    self.state, self.reason = "needs_attention", path.name
        if self.tail(evidence / "run-failures.jsonl") and self.state not in {"failed", "cancelled", "interrupted"}:
            self.state, self.reason = "timed_out", "runner_reported_failure"
        if self.state not in TERMINAL:
            for role, process in self.children.items():
                result = process.poll()
                if result is not None:
                    self.state, self.reason = "failed", f"{role}_exit_{result}"
                    break
        if self.state not in TERMINAL and final_text_status(self.latest_text) == "clean":
            report = audit(evidence)
            self.completion_audit = {key: report[key] for key in ("expected_scored", "submitted_scored_unique", "completion_coverage_eligible", "final_stability_verified", "completion_verified")}
            if not report["completion_coverage_eligible"]:
                self.state, self.reason = "needs_attention", "final_incomplete_required_modules"
            elif report["completion_verified"]:
                self.state, self.reason = "platform_complete", "stable_final_and_required_submission_coverage_verified"
            elif not self.final_probe_pending and time.monotonic() - self.final_probe_last_at >= 1.05:
                self.send({"action": "state"})
                self.final_probe_pending = True
                self.final_probe_requested_at = time.time()
                self.final_probe_last_at = time.monotonic()
        return self.state

    def control(self):
        request = self.path / "control.json"
        if not request.exists():
            return
        value = json.loads(request.read_text())
        if not isinstance(value, dict) or set(value) != {"sequence", "action"}:
            raise ValueError("control accepts only an action and sequence; arbitrary payloads are not supported")
        sequence = value.get("sequence")
        if type(sequence) is not int or sequence <= self.command_sequence:
            raise ValueError("control sequence must increase")
        action = value.get("action")
        if action == "accept_reviewed_terms" and self.terms_visible and self.state not in TERMINAL:
            # Explicit local operator request only; no automatic terms acceptance.
            self.send({"action": "auto_modules", "module": "accept_terms"})
        elif action == "inspect" and self.state not in TERMINAL:
            # Fixed read-only commands through the existing browser owner. No
            # secondary CDP attachment, custom JavaScript, clicks, or reloads.
            for inspection in INSPECT_ACTIONS:
                self.send({"action": inspection})
        elif action == "close":
            self.state, self.reason = "cancelled", "explicit_operator_close"
        else:
            raise ValueError("only read-only inspect, reviewed terms on the observed terms screen, or close are supported")
        self.command_sequence = sequence
        request.rename(self.path / f"control-{sequence:04d}.applied.json")

    def summary(self):
        return {"row": self.spec["row"], "profile_id": self.spec["profile_id"],
                "state": self.state, "reason": self.reason, "last_question": self.last_question,
                "errors": self.error_count, "diagnostics": self.diagnostic_count,
                "terms_visible": self.terms_visible, "started_at": self.started_at,
                "completion_audit": self.completion_audit,
                "pids": {role: process.pid for role, process in self.children.items()}}

    def close(self):
        # Each child owns a process group, including its Chromium descendants.
        for process in self.children.values():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        for process in self.children.values():
            try:
                process.wait(timeout=max(.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
            if process.stdin:
                process.stdin.close()
        for log in self.logs:
            log.close()


def supervise(plan, *, command_factory=commands, require_ports=True, poll_seconds=.5):
    """Acquire one batch owner before reading or writing its durable status."""
    validate_plan(plan)
    output = Path(plan["output"])
    output.mkdir(parents=True, exist_ok=True)
    owner = ProfileLock(output, "supervisor-owner")
    try:
        return _supervise_locked(plan, command_factory=command_factory,
                                 require_ports=require_ports, poll_seconds=poll_seconds)
    finally:
        owner.close()


def _supervise_locked(plan, *, command_factory, require_ports, poll_seconds):
    os.umask(0o077)
    validate_plan(plan)
    output = Path(plan["output"])
    output.mkdir(parents=True, exist_ok=True)
    if (output / "status.json").exists() or any(Path(a["directory"]).exists() for a in plan["attempts"]):
        raise ValueError("run already exists; no automatic restart or overwrite")
    if conflicts := conflicting_processes(plan["attempts"]):
        raise ValueError("selected profile already has a live process: " + ", ".join(conflicts))
    if require_ports:
        ports_available(plan["attempts"])
    locks, active, finished = [], [], []
    pending = list(plan["attempts"])
    state = "running"
    def save():
        write_json(output / "status.json", {"state": state, "supervisor_pid": os.getpid(),
                   "updated_at": time.time(), "pending_rows": [a["row"] for a in pending],
                   "attempts": finished + [a.summary() for a in active]})
    try:
        # Lock placement is shared across every batch under the same scope file.
        for spec in pending:
            locks.append(ProfileLock(Path(plan["scope_file"]).parent / ".parallel-locks", spec["profile_id"]))
        while pending or active:
            while pending and len(active) < plan["concurrency"]:
                spec = pending.pop(0)
                try:
                    active.append(Attempt(spec, plan, command_factory, require_ports))
                except Exception as error:
                    finished.append({"row": spec["row"], "profile_id": spec["profile_id"],
                                     "state": "failed", "reason": type(error).__name__, "phase": "startup"})
                    # Stop dispatching new attempts after an infrastructure error.
                    state = "needs_attention"
                    save()
                    raise
            for attempt in list(active):
                attempt.poll()
                try:
                    attempt.control()
                except (ValueError, OSError, KeyError) as error:
                    attempt.state, attempt.reason = "needs_attention", "invalid_control_request"
                    request = attempt.path / "control.json"
                    if request.exists():
                        request.rename(attempt.path / f"control-rejected-{time.time_ns()}.json")
                if attempt.state in TERMINAL:
                    attempt.close()
                    finished.append(attempt.summary())
                    active.remove(attempt)
            save()
            if active:
                time.sleep(poll_seconds)
        state = "finished"
        save()
    except BaseException:
        state = "interrupted" if state != "needs_attention" else state
        for attempt in active:
            attempt.state, attempt.reason = "interrupted", "supervisor_stopped"
        save()
        raise
    finally:
        for attempt in active:
            attempt.close()
        for lock in locks:
            lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan", help="create an offline reviewable manifest; never launches anything")
    plan_parser.add_argument("--scope", type=Path, default=Path("runs/authorized-scope.json"))
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--rows", required=True, help="explicit comma-separated original row numbers")
    plan_parser.add_argument("--concurrency", type=int, default=10)
    plan_parser.add_argument("--base-port", type=int, default=19769)
    plan_parser.add_argument("--speech-bank-dir", type=Path)
    plan_parser.add_argument("--learn", action="store_true", help="explicitly permit first-time answer generation; default is replay-only")
    for name in ("start", "run", "status"):
        sub.add_parser(name).add_argument("manifest", type=Path)
    control_parser = sub.add_parser("control", help="an explicit operator action; never used automatically")
    control_parser.add_argument("manifest", type=Path)
    control_parser.add_argument("--row", type=int, required=True)
    control_parser.add_argument("--action", choices=("inspect", "accept_reviewed_terms", "close"), required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        plan = make_plan(args.scope, args.output, [int(r) for r in args.rows.split(",")],
                         concurrency=args.concurrency, base_port=args.base_port, speech_bank_dir=args.speech_bank_dir,
                         replay_only=not args.learn)
        validate_plan(plan)
        destination = Path(plan["output"]) / "manifest.json"
        if destination.exists():
            raise ValueError("manifest already exists; use a new output directory")
        write_json(destination, plan)
        print(json.dumps({"manifest": str(destination), "attempts": len(plan["attempts"]),
                          "concurrency": plan["concurrency"], "launched": False}))
        return
    plan = json.loads(args.manifest.read_text())
    validate_plan(plan)
    if args.command == "control":
        selected = [a for a in plan["attempts"] if a["row"] == args.row]
        if len(selected) != 1:
            raise ValueError("row does not belong to this reviewed manifest")
        directory = Path(selected[0]["directory"])
        if not directory.is_dir():
            raise ValueError("attempt has not started")
        request = directory / "control.json"
        if request.exists():
            raise ValueError("previous control request is still pending")
        write_json(request, {"sequence": time.time_ns(), "action": args.action})
        print(json.dumps({"row": args.row, "action": args.action, "state": "queued"}))
    elif args.command == "status":
        status = Path(plan["output"]) / "status.json"
        if status.exists():
            value = json.loads(status.read_text())
            try:
                os.kill(value["supervisor_pid"], 0)
                value["supervisor_alive"] = True
            except ProcessLookupError:
                value["supervisor_alive"] = False
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"state": "not_started"}))
    elif args.command == "start":
        if not os.environ.get("JOBFINDER_LOGIN") or not os.environ.get("JOBFINDER_PASSWORD"):
            raise ValueError("JobFinder credentials must be supplied through the local environment")
        logfile = Path(plan["output"]) / "supervisor.log"
        with logfile.open("ab", buffering=0) as log:
            process = subprocess.Popen([sys.executable, "-u", "-m", "qa_bot.parallel_supervisor", "run", str(args.manifest.resolve())],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        write_json(Path(plan["output"]) / "launcher.json", {"supervisor_pid": process.pid, "started_at": time.time()})
        print(json.dumps({"supervisor_pid": process.pid, "manifest": str(args.manifest.resolve()), "state": "dispatched"}))
    else:
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        supervise(plan)


if __name__ == "__main__":
    main()
