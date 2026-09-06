"""Audit recorded module coverage independently of the final platform screen.

This reads local evidence only. It never calls a model, visits a site, or treats
model confidence, a submitted answer, or a simulation advance as correctness.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import time

LABELS = {
    "System Diagnostic Tool": "diagnostic", "SVAR - Spoken English (U.S.)": "svar",
    "Typing": "typing", "Personality": "personality", "Basic Analytical Ability": "analytical",
    "Basic Computer Literacy Simulation (Windows 10)": "computer",
    "WriteX - Email Writing": "writex", "Sales Competency Test": "sales",
}
COUNTS = {"diagnostic": 1, "svar": 27, "typing": 1, "personality": 72,
          "analytical": 19, "computer": 16, "writex": 1, "sales": 20}
FINAL = "Your test is now complete. Thank you!"
STATUS = {"Complete", "Upcoming", "Later", "In Progress", "In progress"}
DIAGNOSTICS = {"page-errors.jsonl", "observer-errors.jsonl"}


class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.malformed = Counter()
        self.cache = {}

    def records(self, name):
        if name not in self.cache:
            path = self.directory / name
            rows = []
            if path.exists():
                payload = path.read_bytes()
                # A running process may still be writing its last JSON record.
                for line in payload.splitlines(keepends=True):
                    if not line.endswith(b"\n"):
                        self.malformed[name] += 1
                        continue
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError
                        rows.append(value)
                    except (ValueError, UnicodeError):
                        self.malformed[name] += 1
            self.cache[name] = rows
        return self.cache[name]


def module_menu(states):
    expected, completed, unknown, count_mismatches = set(), set(), set(), []
    menus = 0
    for state in states:
        text = state.get("text", "")
        if "Assessments\n" not in text:
            continue
        menus += 1
        lines = text.split("Assessments\n", 1)[1].splitlines()
        block = []
        for line in lines:
            line = line.strip()
            if line == "NEXT":
                break
            if line not in STATUS:
                if line:
                    block.append(line)
                continue
            if not block:
                continue
            name = block[0]
            module = LABELS.get(name)
            if module is None:
                unknown.add(name[:160])
            else:
                expected.add(module)
                if line == "Complete":
                    completed.add(module)
                match = re.search(r"\b(\d+)\s+(?:question(?:\(s\))?|email|task)(?:\b|\s)", "\n".join(block[1:]), re.I)
                if match and module != "svar" and int(match[1]) != COUNTS[module]:
                    item = {"module": module, "observed": int(match[1]), "supported": COUNTS[module]}
                    if item not in count_mismatches:
                        count_mismatches.append(item)
            block = []
        if block:
            # A truncated menu cannot silently establish complete requirements.
            unknown.add("incomplete_menu_block")
    return {"menu_observations": menus, "expected": sorted(expected), "platform_marked_complete": sorted(completed),
            "unknown_modules": sorted(unknown), "count_mismatches": count_mismatches}


def number(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    return None


def question_counts(records, expected, *, valid=lambda record: True, timeout_at=None):
    counts, rejected, late, unverified_timing = Counter(), [], [], 0
    for record in records:
        item = number(record.get("number", record.get("question")))
        if item is None or not valid(record):
            rejected.append(item)
            continue
        if timeout_at is not None:
            stamp = record.get("time")
            if not isinstance(stamp, (int, float)):
                unverified_timing += 1
            elif stamp >= timeout_at:
                late.append(item)
        counts[item] += 1
    expected = set(expected)
    actual = set(counts)
    return {"expected": len(expected), "submitted_unique": len(actual & expected),
            "submitted_numbers": sorted(actual & expected), "missing": sorted(expected - actual),
            "duplicates": {str(k): v for k, v in sorted(counts.items()) if v > 1},
            "unexpected": sorted(actual - expected), "rejected_records": len(rejected),
            "after_first_run_timeout": sorted(late), "timing_unverified_records": unverified_timing,
            "coverage_complete": actual == expected and not rejected}


def audit(directory):
    evidence = Evidence(directory)
    states = evidence.records("states.jsonl")
    menu = module_menu(states)
    failures = evidence.records("run-failures.jsonl")
    timed_states = [s for s in states if "Assessment Time out" in s.get("text", "")]
    stamps = [r["time"] for r in failures + timed_states if isinstance(r.get("time"), (int, float))]
    timeout_at = min(stamps) if stamps else None
    timeout = bool(failures or timed_states)
    modules = {}

    # The unscored SVAR sample is question 1; preparation is not submission.
    speech = [r for r in evidence.records("actions.jsonl") if r.get("action") == "submit_speech" and number(r.get("number")) != 1]
    choices = evidence.records("choices.jsonl")
    if "svar" in menu["expected"] or speech or choices:
        sections = {
            "A": question_counts([r for r in speech if number(r.get("number")) in range(2, 14)], range(2, 14), timeout_at=timeout_at),
            "B": question_counts([r for r in speech if number(r.get("number")) in range(14, 23)], range(14, 23), timeout_at=timeout_at),
            "C": question_counts(choices, range(23, 28), timeout_at=timeout_at),
            "D": question_counts([r for r in speech if number(r.get("number")) == 28], [28], timeout_at=timeout_at),
        }
        combined = question_counts(speech + choices, range(2, 29), timeout_at=timeout_at)
        combined["sections"] = sections
        combined["coverage_complete"] &= all(s["coverage_complete"] for s in sections.values())
        modules["svar"] = combined

    specifications = {
        "typing": ("typing.jsonl", [2], lambda r: r.get("exact_match") is True and r.get("practice") is False),
        "personality": ("personality.jsonl", range(1, 73), lambda r: bool(r.get("answer"))),
        "analytical": ("analytical.jsonl", range(1, 20), lambda r: bool(r.get("selected"))),
        "sales": ("sales.jsonl", range(1, 21), lambda r: {s.get("role") for s in r.get("roles", [])} == {"best", "worst"} and len(r.get("roles", [])) == 2),
        "writex": ("writex.jsonl", [1], lambda r: r.get("exact_fields") is True and isinstance(r.get("body_words"), int) and r["body_words"] >= 30),
    }
    for module, (filename, expected, valid) in specifications.items():
        rows = evidence.records(filename)
        if module == "typing":
            rows = [r for r in rows if r.get("practice") is not True]
        if module in menu["expected"] or rows:
            modules[module] = question_counts(rows, expected, valid=valid, timeout_at=timeout_at)

    computer = evidence.records("computer.jsonl")
    if "computer" in menu["expected"] or computer:
        result = question_counts(computer, range(1, 17), valid=lambda r: r.get("advanced") is True, timeout_at=timeout_at)
        result["evidence_kind"] = "observed_advancement_not_correctness"
        actions = evidence.records("computer-actions.jsonl")
        action_numbers = {number(r.get("question", r.get("number"))) for r in actions}
        result["questions_with_actions"] = sorted(n for n in action_numbers if n in range(1, 17))
        result["questions_without_actions"] = sorted(set(range(1, 17)) - action_numbers)
        confirms = evidence.records("computer-final-submit.jsonl")
        result["final_confirmation_observed"] = any(number(r.get("number")) == 16 and r.get("confirmed") is True for r in confirms)
        events = evidence.records("computer-events.jsonl")
        # Store the count only: event schemas have not been validated as grades.
        result["platform_events_observed"] = len(events)
        result["success_verified"] = False
        result["coverage_complete"] &= not result["questions_without_actions"] and result["final_confirmation_observed"]
        modules["computer"] = result

    diagnostic = evidence.records("diagnostic.jsonl")
    diagnostic_complete = "diagnostic" in menu["platform_marked_complete"]
    required = [m for m in menu["expected"] if m != "diagnostic"]
    duplicates = {m: r["duplicates"] for m, r in modules.items() if r["duplicates"]}
    coverage = bool(required) and all(modules.get(m, {}).get("coverage_complete", False) for m in required)
    requirements_known = bool(menu["menu_observations"]) and not menu["unknown_modules"] and not menu["count_mismatches"]
    final = any(FINAL in s.get("text", "") for s in states)
    errors, diagnostics = {}, {}
    for path in evidence.directory.glob("*errors.jsonl"):
        records = evidence.records(path.name)
        if records:
            (diagnostics if path.name in DIAGNOSTICS else errors)[path.name] = len(records)

    # Supervisor controls are explicit and auditable; old interactive sessions
    # lack a complete command log, so absence of records cannot prove autonomy.
    control_directory = evidence.directory.parent.parent
    controls, controls_invalid = [], 0
    for path in control_directory.glob("control-*.applied.json"):
        try:
            value = json.loads(path.read_text())
            controls.append({"action": value["action"], "sequence": value.get("sequence")})
        except (ValueError, KeyError):
            controls_invalid += 1
    commands = evidence.records("operator-commands.jsonl")
    mutation_names = {"click", "auto_modules", "auto_speech", "auto_choices", "retry_read", "close"}
    interventions = [{"action": c.get("action"), "module": str(c["module"]).split(":", 1)[0] if c.get("module") else None} for c in commands
                     if c.get("action") in mutation_names and not (c.get("action") == "auto_modules" and c.get("module") == "accept_terms")]
    answer_controls = [c for c in interventions if c["action"] != "close"]
    expected_total = sum(COUNTS[m] for m in required)
    submitted_total = sum(modules.get(m, {}).get("submitted_unique", 0) for m in required)
    unexpected_modules = sorted(set(modules) - set(required))
    complete = bool(requirements_known and coverage and final and not timeout and not duplicates and not evidence.malformed
                    and not unexpected_modules and ("diagnostic" not in menu["expected"] or diagnostic_complete))
    warnings = []
    if not requirements_known:
        warnings.append("complete_module_requirements_not_verified")
    if final and not coverage:
        warnings.append("final_page_does_not_prove_all_required_answers_were_submitted")
    if timeout:
        warnings.append("timeout_or_runner_failure_observed")
    if not diagnostic_complete and "diagnostic" in menu["expected"]:
        warnings.append("diagnostic_not_marked_complete_by_platform")
    for m in required:
        if not modules.get(m, {}).get("submitted_unique"):
            warnings.append("required_module_has_no_submissions:" + m)
    if duplicates:
        warnings.append("duplicate_submissions_need_review")
    if unexpected_modules:
        warnings.append("submission_modules_not_present_in_observed_requirements")
    if errors:
        warnings.append("module_errors_observed_recovery_requires_review")
    return {"version": 1, "audited_at": time.time(), "state_records": len(states),
            "evidence_directory": str(evidence.directory.resolve()),
            "requirements": menu, "platform_final_observed": final,
            "submission_coverage_complete": coverage, "completion_verified": complete,
            "correctness_verified": False, "autonomy_verified": False,
            "expected_scored": expected_total, "submitted_scored_unique": submitted_total,
            "modules": modules, "unexpected_modules": unexpected_modules,
            "diagnostic": {"log_records": len(diagnostic), "platform_complete": diagnostic_complete},
            "timeouts": {"observed": timeout, "state_records": len(timed_states), "failure_records": len(failures), "first_time": timeout_at},
            "module_errors": errors, "diagnostic_errors": diagnostics, "malformed_records": dict(evidence.malformed),
            "operator": {"explicit_controls": controls, "invalid_controls": controls_invalid,
                         "observed_non_terms_mutations": interventions,
                         "no_logged_manual_answer_controls": not answer_controls,
                         "operator_command_records": len(commands),
                         "operator_command_file_present": (evidence.directory / "operator-commands.jsonl").exists(),
                         "evidence_scope": "Recorded commands only; unlogged commands and physical browser interaction are not excluded.",
                         "full_command_coverage_verified": False},
            "warnings": warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="one run's evidence/owned directory")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = audit(args.evidence)
    if args.output:
        from qa_bot.parallel_supervisor import write_json
        write_json(args.output, report)
    print(json.dumps({k: report[k] for k in ("completion_verified", "platform_final_observed", "expected_scored", "submitted_scored_unique", "warnings")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
