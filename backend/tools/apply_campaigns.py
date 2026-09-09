"""Recurring apply-campaign store: apply DAILY under a fixed custom name, N times/day, each run
with a FRESH résumé. A campaign targets one job (`job`), a search query (`search`, e.g.
"Kazakhstan" → the region-eligible jobs), or a SET of catalog jobs the owner hand-picked on
/catalog (`jobs`). Driven by `apply_campaign_cron.py`.

Store: backend/data/apply_campaigns.json (gitignored), atomic-write + RLock (mirrors mh_settings).

SEMANTICS (owner-approved 2026-09-09):
  * SEARCH campaign: `per_day` = N distinct NEW jobs/day (never re-applies a job it already did or one
    already submitted globally), and only auto-submittable ATSes (`_AUTO_ATS`).
  * SINGLE-job campaign: `per_day` applications/day to that ONE posting (owner-requested — same name,
    fresh résumé each). NB the campaign's ONE stable mailbox means an ATS usually dedupes the repeats,
    so the ATS — not us — decides how many actually land; the UI warns about this.
  * JOBS campaign (a checkbox selection on /catalog): `job_ids` is the owner's ordered pick, walked
    ROUND-ROBIN by a persisted `cursor` — `per_day` ids per day starting at the cursor, the same job
    repeating only after every selected one was hit (per_day > len(job_ids) may repeat within a day;
    the ATS dedupes on the one mailbox). The cursor advances ONLY by the jobs a run actually DID, so a
    failed fill is retried next run instead of skipped. Ids whose catalog row is missing or `dead` are
    skipped for THAT run (not removed — a posting can come back), and the applied/submitted exclusions
    of the search kind do NOT apply (the owner chose these jobs; a cycle must revisit them). The name
    may be left EMPTY — one is then generated for the first selected job's country (see
    `_generated_name`) and pinned like a typed one.
  * ONE stable mailbox per campaign (`email`/`pid` pinned at create) so all of a campaign's replies
    land in a single inbox and the applications read as one coherent person.
Pure/offline helpers are unit-tested in backend/tests/test_apply_campaigns.py (no DB/network).
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import re
import threading
from contextlib import contextmanager
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "apply_campaigns.json"
_LOCK = threading.RLock()


@contextmanager
def _file_lock():
    """Cross-PROCESS guard for load-mutate-save: the cron (`apply_campaign_cron`, its own process)
    and the dashboard both rewrite the whole file, and the RLock only covers one process. An
    fcntl lock on a sidecar file serialises them; the write itself stays atomic (tmp + replace)."""
    _PATH.parent.mkdir(exist_ok=True)
    lock_path = _PATH.with_suffix(".lock")
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
        except OSError:
            pass          # a filesystem without flock — fall back to the in-process lock only
        try:
            yield
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            except OSError:
                pass
# Only these ATSes auto-submit end-to-end from the datacenter IP (emailed code, not a live captcha),
# so a SEARCH campaign only targets them — else the budget is spent on jobs that can't complete.
_AUTO_ATS = {"greenhouse", "ashby"}


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


def next_identity(cid: int, exists=None) -> tuple[str, str]:
    """Per-APPLICATION identity for a campaign (owner 2026-09-09: «пусть почта меняется на каждую
    подачу»): the NAME stays the campaign's, every application gets a FRESH @takhet.com mailbox +
    persona id — `first.last<N>@` like any synthetic persona, N from a per-campaign base counter,
    skipping addresses the CRM already knows. `seq` is persisted BEFORE the fill so a crash never
    reuses a mailbox. A campaign with `email_mode != 'per_apply'` keeps its one fixed mailbox."""
    if exists is None:
        def exists(email: str) -> bool:
            try:
                from backend.tools import mailcrm
                return any((c.get("email") or "").lower() == email.lower() for c in mailcrm.candidates())
            except Exception:
                return False
    with _LOCK, _file_lock():
        rows = _load()
        camp = next((r for r in rows if int(r.get("id", 0)) == int(cid)), None)
        if camp is None:
            raise KeyError(cid)
        if camp.get("email_mode", "per_apply") != "per_apply":
            return camp["email"], camp["pid"]
        from backend.tools.catalog_drafts import derive_email
        base = derive_email(camp.get("name") or "") or f"campaign.c{cid}@takhet.com"
        local, _, dom = base.partition("@")
        dom = dom or "takhet.com"
        if not camp.get("seq_base"):
            camp["seq_base"] = random.randint(120, 9000)
        n = int(camp["seq_base"])
        for _ in range(50):
            camp["seq"] = int(camp.get("seq") or 0) + 1
            n = int(camp["seq_base"]) + int(camp["seq"])
            email = f"{local}{n}@{dom}"
            if not exists(email):
                break
        slug = re.sub(r"[^a-z0-9]+", "", (camp.get("name") or "").lower())[:16] or "x"
        pid = f"demo_camp{cid}_{slug}_{n}"
        _save(rows)
        return email, pid


def list_campaigns() -> list:
    with _LOCK:
        return _load()


def _parse_job_ids(job_ids) -> list[int]:
    """The owner's selection as a de-duplicated, order-preserving list of ints. Accepts a list/
    tuple/set of ints (or int-strings) OR the form-encoded "1,2,3" string the /catalog sheet POSTs.
    Blank tokens are ignored; a non-numeric token is a ValueError (a typo must not silently vanish
    from the selection)."""
    if isinstance(job_ids, str):
        toks = [t for t in re.split(r"[,\s]+", job_ids.strip()) if t]
    else:
        toks = [t for t in (job_ids or []) if t is not None and str(t).strip()]
    try:
        return list(dict.fromkeys(int(str(t).strip()) for t in toks))
    except (TypeError, ValueError):
        raise ValueError("job_ids must be integers") from None


def _generated_name(job_ids: list[int], gender: str) -> str:
    """A fresh persona name for a `jobs` campaign the owner left unnamed: random from
    synth_persona's per-country banks, the country taken from the FIRST selected job's catalog row
    (`_country_of`, so the name fits the posting like a one-click fill would), 'United States' when
    the lookup fails or finds nothing. Random at create, then pinned on the row forever (the
    campaign's identity), like a typed name. Both lookups are lazy imports so the store itself
    never touches the DB / persona machinery; tests monkeypatch this function."""
    country = "United States"
    try:
        from backend.tools.catalog_db import jobs_by_ids
        from backend.tools.synth_persona import _country_of
        row = (jobs_by_ids(job_ids[:1]) or {}).get(int(job_ids[0])) if job_ids else None
        if row:
            country = _country_of(row) or country
    except Exception:
        pass
    from backend.tools.synth_persona import _pick_name
    return _pick_name(country, gender or "male")


def create(*, name: str, target_kind: str, job_id=None, job_ids=None, q: str = "",
           region: str = "", gender: str = "", per_day: int = 1, today: str = "",
           name_for=None) -> dict:
    """Create a campaign. `target_kind` ∈ {'job','search','jobs'}. `job` needs `job_id`; `jobs`
    needs a non-empty `job_ids` selection (list of ints or "1,2,3") and may leave `name` empty —
    it is then generated by `name_for(job_ids, gender)` (default: module `_generated_name`). The
    other kinds require a name. per_day is clamped 1..5. `today` (YYYY-MM-DD) seeds
    last_run_date; pass the current date (the store never calls Date.now itself)."""
    name = (name or "").strip()
    if target_kind not in ("job", "search", "jobs"):
        raise ValueError("target_kind must be 'job', 'search' or 'jobs'")
    gender = gender if gender in ("male", "female") else ""
    try:
        per_day = max(1, min(int(per_day), 100))    # owner: a free number, not a 1-5 picker
    except (TypeError, ValueError):
        per_day = 1
    if target_kind == "job" and not job_id:
        raise ValueError("job_id required for a single-job campaign")
    ids: list[int] = []
    if target_kind == "jobs":
        ids = _parse_job_ids(job_ids)
        if not ids:
            raise ValueError("job_ids required for a jobs campaign")
        if not name:
            # Look the module attribute up at call time so a monkeypatched `_generated_name`
            # is honoured even when the caller didn't pass `name_for`.
            name = ((name_for or _generated_name)(ids, gender) or "").strip()
    if not name:
        raise ValueError("name required")
    with _LOCK, _file_lock():
        rows = _load()
        cid = _next_id(rows)
        camp = {
            "id": cid, "name": name, "target_kind": target_kind,
            "job_id": int(job_id) if job_id else None,
            "q": (q or "").strip(), "region": (region or "").strip(),
            "gender": gender,
            "per_day": per_day,
            "email": _stable_email(name, cid),
            "pid": f"demo_camp{cid}_{re.sub(r'[^a-z0-9]+', '', name.lower())[:16] or 'x'}",
            # owner 2026-09-09: a NEW mailbox per application (same name) — see next_identity()
            "email_mode": "per_apply", "seq": 0, "seq_base": random.randint(120, 9000),
            "active": True, "applied_jobids": [], "runs_today": 0,
            "last_run_date": today or "", "created": today or "",
        }
        if target_kind == "jobs":
            camp["job_ids"] = ids
            camp["cursor"] = 0
        rows.append(camp)
        _save(rows)
        return camp


def delete(cid: int) -> bool:
    with _LOCK, _file_lock():
        rows = _load()
        new = [r for r in rows if int(r.get("id", 0)) != int(cid)]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def set_active(cid: int, active: bool) -> bool:
    with _LOCK, _file_lock():
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


def _alive_ids(ids: list[int], jobs_by_ids) -> list[int]:
    """The selection minus ids whose catalog row is missing or `dead` (a posting gone at the ATS —
    `catalog_db.mark_dead`). Order preserved. A lookup failure (DB down) treats every id as alive
    rather than starving the campaign — the fill itself then reports a dead page."""
    if jobs_by_ids is None:
        from backend.tools.catalog_db import jobs_by_ids
    try:
        rows = jobs_by_ids(ids) or {}
    except Exception:
        return list(ids)
    return [j for j in ids if j in rows and not (rows[j] or {}).get("dead")]


def resolve_targets(camp: dict, today: str, *, list_jobs=None, submitted=None,
                    jobs_by_ids=None) -> list[int]:
    """The job ids to apply to on THIS run, honoring the per-day budget. Single-job → [job_id]
    × remaining. Jobs (a /catalog selection) → the next `remaining_today` ids round-robin from
    the persisted `cursor`, skipping missing/dead rows for this run only. Search → up to
    `remaining_today` NEW jobs from list_jobs(q, region) that this campaign hasn't already applied
    to and that aren't globally submitted. `list_jobs`/`submitted`/`jobs_by_ids` are injectable
    for testing; default to the live catalog_db/bulk_log."""
    n = remaining_today(camp, today)
    if n <= 0:
        return []
    applied = set(int(x) for x in (camp.get("applied_jobids") or []))
    kind = camp.get("target_kind")
    if kind == "job":
        # single job: apply per_day times TODAY (owner-requested; same name, fresh résumé each).
        # NB an ATS usually dedupes repeat applications from the campaign's one stable email, so the
        # employer/ATS — not us — decides how many of the N/day actually land.
        jid = camp.get("job_id")
        return [int(jid)] * n if jid else []
    if kind == "jobs":
        # owner-picked set: round-robin from the cursor; NO applied/submitted exclusion (a cycle
        # must revisit every chosen job). The cursor is a position in the FULL selection: walk it
        # from there, SKIPPING ids whose row is missing/dead for this run (never dropped — a posting
        # can come back), wrapping until `n` targets are collected. note_run then places the cursor
        # right after the last job actually done, so both sides index the same list.
        ids = [int(x) for x in (camp.get("job_ids") or [])]
        alive = set(_alive_ids(ids, jobs_by_ids)) if ids else set()
        if not alive:
            return []
        out, pos = [], int(camp.get("cursor") or 0) % len(ids)
        for _ in range(n * len(ids) + len(ids)):      # bounded: at most n full laps
            j = ids[pos % len(ids)]
            pos += 1
            if j in alive:
                out.append(j)
                if len(out) >= n:
                    break
        return out
    # search campaign
    if list_jobs is None:
        from backend.tools.catalog_db import list_jobs as list_jobs
    if submitted is None:
        from backend.tools.bulk_log import submitted_jobids
        submitted = submitted_jobids()
    submitted = set(int(x) for x in (submitted or []))
    rows = list_jobs(q=(camp.get("q") or None), region=(camp.get("region") or None),
                     remote_only=True, limit=max(n * 8, 40))
    out = []
    for r in rows:
        # Only greenhouse/ashby auto-submit end-to-end from the datacenter IP (email-code, not a
        # live captcha); Lever/Workable would be filled but never submitted, then marked applied
        # forever — burning the daily budget on un-completable jobs. Mirrors the bulk lane's
        # _PARA_ATS filter.
        if (r.get("ats") or "") not in _AUTO_ATS:
            continue
        jid = int(r.get("id") or r.get("jobid") or 0)
        if not jid or jid in applied or jid in submitted:
            continue
        out.append(jid)
        if len(out) >= n:
            break
    return out


def note_run(cid: int, jobids, today: str, attempted=None) -> None:
    """Record an executed run: `jobids` = the jobs that really APPLIED (spend the budget, join
    applied_jobids); `attempted` = every job the run tried (default: the same list). A `jobs`
    campaign moves its rotation `cursor` past the LAST ATTEMPTED job, so a job that failed today
    (dead, incomplete, error) doesn't pin the rotation — it comes round again next lap, and a
    dead one is skipped by then (the cron marks it dead)."""
    jobids = [int(j) for j in (jobids or [])]
    attempted = [int(j) for j in (attempted if attempted is not None else jobids)]
    with _LOCK, _file_lock():
        rows = _load()
        for r in rows:
            if int(r.get("id", 0)) == int(cid):
                _roll_day(r, today)
                have = set(int(x) for x in (r.get("applied_jobids") or []))
                have.update(jobids)
                r["applied_jobids"] = sorted(have)
                r["runs_today"] = int(r.get("runs_today", 0)) + len(jobids)
                r["last_run_date"] = today
                if r.get("target_kind") == "jobs" and attempted:
                    # the rotation resumes right AFTER the last job attempted — the same position
                    # space resolve_targets walks (dead ids were skipped, not counted)
                    sel = [int(x) for x in (r.get("job_ids") or [])]
                    if sel and attempted[-1] in sel:
                        r["cursor"] = (sel.index(attempted[-1]) + 1) % len(sel)
        _save(rows)


def fill_counts_as_done(fill_state: dict | None) -> bool:
    """Whether a `dashboard_app._do_fill` outcome (its `_FILL_JOBS[jid]` entry) counts as an
    APPLICATION — i.e. spends the day's budget. Only a fill that really pressed Submit (the
    co-pilot's `submit_result.clicked`/`confirmed`) counts; a filled-but-not-submitted form
    (`incomplete`/`needs_review`), a dead posting (`no_form`) or an `error` does not (the first
    live campaign run billed a `no_form` on a posting gone from the ATS as one of its 3/day)."""
    st = fill_state or {}
    if st.get("state") != "done":
        return False
    sub = st.get("submit")
    if sub is None:
        return True          # a fill without a submit phase (dry-run style) — nothing to judge
    # A pressed Submit is NOT an application on GH/Ashby: the ATS still needs the emailed code
    # (the cron waits for it inline) and may reject the submit outright (Ashby's datacenter-IP
    # «flagged as possible spam» — run #2 of the first campaign: 3 clicked, 0 accepted). Only a
    # CONFIRMED submit counts; a `blocked` one never does.
    if sub.get("blocked"):
        return False
    return bool(sub.get("confirmed"))


def fill_is_dead_posting(fill_state: dict | None) -> bool:
    """The co-pilot found NO form (`submit_result.reason == 'no_form'`): the posting is gone at
    the ATS (a 404/'job not found' page) — mark it dead so the rotation skips it."""
    sub = ((fill_state or {}).get("submit") or {})
    return sub.get("reason") == "no_form"
