"""Audit trail for a «Подать на все» (bulk apply-all) run.

Leaves two artifacts under <repo>/logs so a run's success/failure survives a
dashboard restart and can be inspected or downloaded:

  bulk_apply.log        append-only text, one line per job + a FINISHED summary
  bulk_apply_last.json  the full last run (counts + per-job records) for the UI

Honest counters: `filled_ok` = the co-pilot filled the form (HTTP 200), which is
NOT the same as a submitted application. `submit_clicked` = the Submit button was
pressed; `submit_confirmed` = an ATS confirmation was seen right after the click
(best-effort — a captcha-gated ATS confirms later, or never, from a datacenter IP).
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOGDIR = Path(__file__).resolve().parents[2] / "logs"
_LOG = _LOGDIR / "bulk_apply.log"
_REPORT = _LOGDIR / "bulk_apply_last.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(line: str) -> None:
    try:
        _LOGDIR.mkdir(parents=True, exist_ok=True)
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("bulk_apply.log append failed", exc_info=True)


def _write_report(run: dict) -> None:
    try:
        _LOGDIR.mkdir(parents=True, exist_ok=True)
        tmp = _REPORT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_REPORT)
    except Exception:
        logger.warning("bulk_apply_last.json write failed", exc_info=True)


def start(total: int) -> dict:
    run = {"run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
           "started": _now(), "finished": None, "state": "running",
           "total": int(total), "done": 0, "filled_ok": 0, "errors": 0,
           "submit_clicked": 0, "submit_confirmed": 0, "submit_blocked": 0,
           "skipped": 0, "jobs": []}
    _append(f'{run["started"]} run={run["run_id"]} START total={total}')
    _write_report(run)
    return run


def record(run: dict, *, jobid, company="", title="", state="", filled=None,
           unfilled=None, unfilled_list=None, submit=None, error=None) -> None:
    submit = submit or {}
    clicked = bool(submit.get("clicked"))
    reason = submit.get("reason") or ("error" if state == "error" else "")
    confirmed, blocked = submit.get("confirmed"), submit.get("blocked")
    run["jobs"].append({
        "ts": _now(), "jobid": jobid, "company": company, "title": title,
        "state": state, "filled": filled, "unfilled": unfilled,
        "unfilled_list": (unfilled_list or [])[:8],
        "submit_clicked": clicked, "submit_reason": reason,
        "confirmed": confirmed, "blocked": blocked, "error": error})
    run["done"] = len(run["jobs"])
    if state == "error":
        run["errors"] += 1
    else:
        run["filled_ok"] += 1
    if clicked:
        run["submit_clicked"] += 1
        run["submit_confirmed"] += 1 if confirmed else 0
        run["submit_blocked"] += 1 if blocked else 0
    elif state != "error" and reason and reason != "clicked":
        run["skipped"] += 1
    _append(
        f'{run["jobs"][-1]["ts"]} run={run["run_id"]} [{run["done"]}/{run["total"]}] '
        f'jobid={jobid} {company!r} "{title}" fill={state or "?"} filled={filled} '
        f'unfilled={unfilled} submit={"clicked" if clicked else (reason or "no")} '
        f'confirmed={confirmed} blocked={blocked}'
        + (f' error={error!r}' if error else ""))
    _write_report(run)   # keep fresh so a mid-run restart still shows progress


def finish(run: dict, state: str = "done") -> None:
    run["state"] = state
    run["finished"] = _now()
    _append(
        f'{run["finished"]} run={run["run_id"]} FINISHED state={state} '
        f'total={run["total"]} done={run["done"]} filled_ok={run["filled_ok"]} '
        f'errors={run["errors"]} submit_clicked={run["submit_clicked"]} '
        f'confirmed={run["submit_confirmed"]} blocked={run["submit_blocked"]} '
        f'skipped={run["skipped"]}')
    _write_report(run)


def last_report() -> dict | None:
    try:
        return json.loads(_REPORT.read_text(encoding="utf-8"))
    except Exception:
        return None


def log_path() -> Path:
    return _LOG
