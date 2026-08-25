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
# Persistent ledger of applications that DID NOT complete (need a human to finish the
# captcha/submit), keyed by jobid, accumulated across runs. record() adds a not-
# confirmed job and removes a confirmed one; mark_done() clears one by hand. Survives
# restarts + bulk_apply_last.json being overwritten by the next run.
_LEDGER = _LOGDIR / "unfinished.json"
# Persistent set of jobids that have been CONFIRMED submitted (by any persona, any run) or
# marked done by a human. Used to (a) never re-park a done job in the ledger and (b) skip
# re-applying it in bulk — a job was being applied 10× because "done" lived only per-persona
# in status_store / the ledger, so a re-run under a fresh (mail-less) persona looked new.
_DONE = _LOGDIR / "submitted_jobids.json"


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


def _load_ledger() -> dict:
    try:
        d = json.loads(_LEDGER.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_ledger(d: dict) -> None:
    try:
        _LOGDIR.mkdir(parents=True, exist_ok=True)
        tmp = _LEDGER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_LEDGER)
    except Exception:
        logger.warning("unfinished.json write failed", exc_info=True)


def _load_done() -> set:
    try:
        d = json.loads(_DONE.read_text(encoding="utf-8"))
        return {str(x) for x in d} if isinstance(d, list) else set()
    except Exception:
        return set()


def _save_done(s: set) -> None:
    try:
        _LOGDIR.mkdir(parents=True, exist_ok=True)
        tmp = _DONE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_DONE)
    except Exception:
        logger.warning("submitted_jobids.json write failed", exc_info=True)


def mark_submitted(jobid) -> None:
    """Record a jobid as completed (confirmed submitted or human-finished) so it's never
    re-parked in the ledger or re-applied, regardless of which persona did it."""
    key = str(jobid)
    if not key or key == "None":
        return
    s = _load_done()
    if key not in s:
        s.add(key)
        _save_done(s)


def submitted_jobids() -> set:
    """Jobids already confirmed/finished — excluded from bulk re-application."""
    return _load_done()


def _update_ledger(job: dict, run_id: str, confirmed) -> None:
    """A confirmed submission clears the job from the ledger AND records it done; anything
    else (error, captcha-blocked, skipped, filled-but-unconfirmed) adds/refreshes it —
    UNLESS the job is already known-submitted (by another persona/run), in which case it is
    never re-parked."""
    key = str(job.get("jobid"))
    if not key or key == "None":
        return
    led = _load_ledger()
    if confirmed:
        mark_submitted(key)
        led.pop(key, None)
    elif key in _load_done():
        led.pop(key, None)          # already done elsewhere — don't churn it
    else:
        # Track how many times this job has been re-parked (failed). The drain re-runs a
        # fixable job up to a retry cap, then drops it as dead — so we finish what we can and
        # stop re-spamming what we can't.
        prev = led.get(key)
        retries = (int((prev or {}).get("retries", 0)) + 1) if prev else 0
        led[key] = {**job, "run_id": run_id, "retries": retries}
    _save_ledger(led)


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
           unfilled=None, unfilled_list=None, submit=None, error=None, profile="") -> None:
    submit = submit or {}
    clicked = bool(submit.get("clicked"))
    reason = submit.get("reason") or ("error" if state == "error" else "")
    confirmed, blocked = submit.get("confirmed"), submit.get("blocked")
    run["jobs"].append({
        "ts": _now(), "jobid": jobid, "company": company, "title": title,
        "state": state, "filled": filled, "unfilled": unfilled,
        "unfilled_list": (unfilled_list or [])[:8], "profile": profile,
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
    _update_ledger(run["jobs"][-1], run["run_id"], confirmed)
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


def unfinished() -> list[dict]:
    """Every application still awaiting a human finish, newest first."""
    return sorted(_load_ledger().values(), key=lambda j: j.get("ts") or "",
                  reverse=True)


def unfinished_count() -> int:
    return len(_load_ledger())


def mark_done(jobid) -> bool:
    """Clear one job from the ledger (a human finished it, or the reconciler found its
    receipt) and record it done so it's never re-applied. True if it was in the ledger."""
    key = str(jobid)
    mark_submitted(key)
    led = _load_ledger()
    if led.pop(key, None) is not None:
        _save_ledger(led)
        return True
    return False


def drop_many(jobids) -> int:
    """Remove jobs from the ledger WITHOUT marking them submitted (unlike mark_done) — a
    stale/dead-entry cleanup. A dropped job is NOT recorded done, so a future run may still
    re-attempt it. Returns how many were present and removed."""
    ids = {str(j) for j in (jobids or [])}
    if not ids:
        return 0
    led = _load_ledger()
    n = 0
    for j in ids:
        if led.pop(j, None) is not None:
            n += 1
    if n:
        _save_ledger(led)
    return n


def log_path() -> Path:
    return _LOG
