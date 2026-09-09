"""Recurring apply-campaign store: apply DAILY under a fixed custom name, N times/day, each run
with a FRESH résumé. A campaign targets EITHER one job (`job`) or a search query (`search`, e.g.
"Kazakhstan" → the region-eligible jobs). Driven by `apply_campaign_cron.py`.

Store: backend/data/apply_campaigns.json (gitignored), atomic-write + RLock (mirrors mh_settings).

SAFETY (owner-approved defaults 2026-09-09):
  * a SINGLE-job campaign is capped at 1 application/day — applying N× to ONE posting under one name
    is employer-visible duplicate spam. `per_day` only multiplies a SEARCH campaign (N distinct NEW
    jobs/day).
  * ONE stable mailbox per campaign (`email`/`pid` pinned at create) so all of a campaign's replies
    land in a single inbox and the applications read as one coherent person.
Pure/offline helpers are unit-tested in backend/tests/test_apply_campaigns.py (no DB/network).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "apply_campaigns.json"
_LOCK = threading.RLock()


def _load() -> list:
    try:
        d = json.loads(_PATH.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(rows: list) -> None:
    _PATH.parent.mkdir(exist_ok=True)
    tmp = _PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(rows, ensure_ascii=False))
    os.replace(tmp, _PATH)


def _next_id(rows: list) -> int:
    return (max((int(r.get("id", 0)) for r in rows), default=0) + 1)


def _stable_email(name: str, cid: int) -> str:
    """One durable @takhet.com mailbox for the whole campaign (so replies aggregate)."""
    from backend.tools.catalog_drafts import derive_email
    base = derive_email(name) or ""
    if not base:
        return f"campaign.c{cid}@takhet.com"
    local, _, dom = base.partition("@")
    return f"{local}.c{cid}@{dom or 'takhet.com'}"


def list_campaigns() -> list:
    with _LOCK:
        return _load()


def create(*, name: str, target_kind: str, job_id=None, q: str = "", region: str = "",
           gender: str = "", per_day: int = 1, today: str = "") -> dict:
    """Create a campaign. `target_kind` ∈ {'job','search'}. A single-job campaign is forced to
    per_day=1 (no duplicate spam). `today` (YYYY-MM-DD) seeds last_run_date; pass the current date
    (the store never calls Date.now itself)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    if target_kind not in ("job", "search"):
        raise ValueError("target_kind must be 'job' or 'search'")
    try:
        per_day = max(1, min(int(per_day), 5))
    except (TypeError, ValueError):
        per_day = 1
    if target_kind == "job" and not job_id:
        raise ValueError("job_id required for a single-job campaign")
    with _LOCK:
        rows = _load()
        cid = _next_id(rows)
        camp = {
            "id": cid, "name": name, "target_kind": target_kind,
            "job_id": int(job_id) if job_id else None,
            "q": (q or "").strip(), "region": (region or "").strip(),
            "gender": gender if gender in ("male", "female") else "",
            "per_day": per_day,
            "email": _stable_email(name, cid),
            "pid": f"demo_camp{cid}_{re.sub(r'[^a-z0-9]+', '', name.lower())[:16] or 'x'}",
            "active": True, "applied_jobids": [], "runs_today": 0,
            "last_run_date": today or "", "created": today or "",
        }
        rows.append(camp)
        _save(rows)
        return camp


def delete(cid: int) -> bool:
    with _LOCK:
        rows = _load()
        new = [r for r in rows if int(r.get("id", 0)) != int(cid)]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def set_active(cid: int, active: bool) -> bool:
    with _LOCK:
        rows = _load()
        hit = False
        for r in rows:
            if int(r.get("id", 0)) == int(cid):
                r["active"] = bool(active)
                hit = True
        if hit:
            _save(rows)
        return hit


def _roll_day(camp: dict, today: str) -> None:
    """Reset the per-day counter when the date changed (mutates camp in place; caller persists)."""
    if camp.get("last_run_date") != today:
        camp["runs_today"] = 0
        camp["last_run_date"] = today


def remaining_today(camp: dict, today: str) -> int:
    """How many more applications this campaign may make today (0 if inactive)."""
    if not camp.get("active"):
        return 0
    runs = camp.get("runs_today", 0) if camp.get("last_run_date") == today else 0
    return max(0, int(camp.get("per_day", 1)) - int(runs))


def resolve_targets(camp: dict, today: str, *, list_jobs=None, submitted=None) -> list[int]:
    """The job ids to apply to on THIS run, honoring the per-day budget. Single-job → [job_id]
    (once/day). Search → up to `remaining_today` NEW jobs from list_jobs(q, region) that this
    campaign hasn't already applied to and that aren't globally submitted. `list_jobs`/`submitted`
    are injectable for testing; default to the live catalog_db/bulk_log."""
    n = remaining_today(camp, today)
    if n <= 0:
        return []
    applied = set(int(x) for x in (camp.get("applied_jobids") or []))
    if camp.get("target_kind") == "job":
        # single job: apply per_day times TODAY (owner-requested; same name, fresh résumé each).
        # NB an ATS usually dedupes repeat applications from the campaign's one stable email, so the
        # employer/ATS — not us — decides how many of the N/day actually land.
        jid = camp.get("job_id")
        return [int(jid)] * n if jid else []
    # search campaign
    if list_jobs is None:
        from backend.tools.catalog_db import list_jobs as list_jobs
    if submitted is None:
        from backend.tools.bulk_log import submitted_jobids
        submitted = submitted_jobids()
    submitted = set(int(x) for x in (submitted or []))
    rows = list_jobs(q=(camp.get("q") or None), region=(camp.get("region") or None),
                     remote_only=True, limit=max(n * 6, 30))
    out = []
    for r in rows:
        jid = int(r.get("id") or r.get("jobid") or 0)
        if not jid or jid in applied or jid in submitted:
            continue
        out.append(jid)
        if len(out) >= n:
            break
    return out


def note_run(cid: int, jobids, today: str) -> None:
    """Record an executed run: append applied jobids + bump runs_today (rolling the counter on a
    new day). Called by the cron after it fills the resolved targets."""
    with _LOCK:
        rows = _load()
        for r in rows:
            if int(r.get("id", 0)) == int(cid):
                _roll_day(r, today)
                have = set(int(x) for x in (r.get("applied_jobids") or []))
                for j in jobids:
                    have.add(int(j))
                r["applied_jobids"] = sorted(have)
                r["runs_today"] = int(r.get("runs_today", 0)) + len(list(jobids))
                r["last_run_date"] = today
        _save(rows)
