"""Application-outcome statistics for the CRM dashboard (`/stats`).

Everything the stats page shows is derived from three live sources and joined
in memory:

  * the per-application prefill artifacts on disk
    (`uploads/prefill/<persona>/<jobid>/{persona.json,report.json}`) — where we
    applied. **The unit of account is the jobid (one distinct job posting).** In
    the bulk lane a failed job is often re-attempted under a FRESH synthetic
    persona, so the same jobid can carry several persona dirs; counting personas
    would inflate "applied" (~2x on real data) and dilute every rate. So a job is
    counted ONCE, and its outcome is the furthest any of its personas got.
  * the classified recruiter mail in Postgres `mail_index` — what came back,
    joined to the job by the persona's own mailbox address (`mail_index.mailbox`,
    NOT `.candidate`, which is a display name).
  * `bulk_log.submitted_jobids()` — which jobs actually confirmed a submit.

The heavy part is the filesystem scan (~19k tiny JSON reads), so the whole blob
is TTL-cached and refreshed in a background thread; requests serve the cache.
No brand/stack strings leak here — all labels are set in `stats_ui`.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

log = logging.getLogger("stats")

_PREFILL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        "uploads", "prefill")

# best-of ranking for "the outcome of an application" — the furthest a thread got
_RANK = {"offer": 5, "interview": 4, "action_needed": 3, "rejection": 2,
         "ack": 1, "other": 0}
_OUTCOME_KINDS = ["offer", "interview", "action_needed", "rejection", "ack", "other"]

_TTL = int(os.environ.get("STATS_TTL", "600"))  # seconds a cached blob stays fresh

_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_LOCK = threading.Lock()
_REFRESHING = False


def _rank(k) -> int:
    return _RANK.get(k, -1)


def _norm_company(name: str) -> str:
    return (name or "").strip().casefold()


def _pretty(name: str) -> str:
    """Prettify an all-lowercase board slug ('affirm' -> 'Affirm') for display, but
    leave any name that already carries uppercase ('GitLab', 'OpenAI', '1Password',
    'Remote') untouched."""
    n = (name or "").strip()
    if n and n == n.lower():
        return n[:1].upper() + n[1:]
    return n


def _scan_applications() -> dict:
    """Walk the prefill artifacts, keyed by jobid.

    Returns a dict with:
      jobid_company : jobid -> normalized company key
      jobid_emails  : jobid -> set of persona emails that applied to it (retries)
      display       : company key -> most common original casing
      attempts      : total persona dirs seen (fill attempts, incl. retries)
      app_emails    : set of every application mailbox (for trend scoping)
    """
    jobid_company: dict[str, str] = {}
    jobid_emails: dict[str, set] = defaultdict(set)
    disp: dict[str, Counter] = defaultdict(Counter)
    app_emails: set[str] = set()
    attempts = 0

    for d in glob.glob(os.path.join(_PREFILL, "demo_*", "*", "")):
        jobid = os.path.basename(os.path.dirname(d))
        email = company = None
        try:
            with open(os.path.join(d, "persona.json")) as fh:
                pj = json.load(fh)
            email = ((pj.get("profile") or {}).get("email") or pj.get("email") or "").lower()
        except Exception:
            pass
        try:
            with open(os.path.join(d, "report.json")) as fh:
                rj = json.load(fh)
            company = rj.get("company") or rj.get("company_key")
        except Exception:
            pass
        if not company:
            continue
        attempts += 1
        key = _norm_company(company)
        disp[key][company] += 1
        jobid_company[jobid] = key
        if email:
            jobid_emails[jobid].add(email)
            app_emails.add(email)

    return {
        "jobid_company": jobid_company,
        "jobid_emails": jobid_emails,
        "display": {k: _pretty(c.most_common(1)[0][0]) for k, c in disp.items()},
        "attempts": attempts,
        "app_emails": app_emails,
    }


def _mail_outcomes() -> tuple[dict, list]:
    """(best inbound kind per mailbox, [(ts, kind, mailbox), ...] inbound)."""
    from backend.tools import mail_db
    best: dict[str, str] = {}
    inbound: list[tuple[int, str, str]] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT mailbox, kind, outbound, date_ts FROM mail_index")
        for mb, kind, outbound, ts in cur.fetchall():
            if outbound:
                continue
            mbl = (mb or "").lower()
            if _rank(kind) > _rank(best.get(mbl)):
                best[mbl] = kind
            if ts:
                inbound.append((int(ts), kind or "other", mbl))
    return best, inbound


def _catalog_dims(jobids: set[str]) -> tuple[dict, dict]:
    """jobid -> ats, jobid -> region (primary), for the applied jobs."""
    from backend.tools import mail_db
    jid_ats: dict[str, str] = {}
    jid_region: dict[str, str] = {}
    ints = []
    for j in jobids:
        try:
            ints.append(int(j))
        except (TypeError, ValueError):
            pass
    if not ints:
        return jid_ats, jid_region
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, ats, regions FROM job_catalog WHERE id = ANY(%s)", (ints,))
        for jid, ats, regions in cur.fetchall():
            jid_ats[str(jid)] = ats or "other"
            reg = (regions or [])
            jid_region[str(jid)] = (reg[0] if reg else "UNKNOWN")
    return jid_ats, jid_region


def _day_start(ts: int) -> int:
    """Unix ts of 00:00 UTC of that timestamp's day."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def bulk_submitted() -> set:
    from backend.tools import bulk_log
    return set(str(j) for j in bulk_log.submitted_jobids())


def compute_stats() -> dict:
    """The full aggregation blob. Heavy (~19k file reads) — cache the result."""
    t0 = time.time()
    scan = _scan_applications()
    jobid_company = scan["jobid_company"]
    jobid_emails = scan["jobid_emails"]
    display = scan["display"]
    app_emails = scan["app_emails"]
    best, inbound = _mail_outcomes()
    try:
        submitted = bulk_submitted()
    except Exception as e:  # never let bookkeeping break the page
        log.warning("stats: submitted_jobids failed: %s", e)
        submitted = set()
    jid_ats, jid_region = _catalog_dims(set(jobid_company))

    # the outcome of a JOB = the furthest any of its personas got (None = no reply)
    job_outcome: dict[str, str | None] = {}
    for jid in jobid_company:
        bo = None
        for e in jobid_emails.get(jid, ()):
            k = best.get(e)
            if _rank(k) > _rank(bo):
                bo = k
        job_outcome[jid] = bo

    # per-company aggregation over distinct jobs
    applied = Counter()
    submitted_c = Counter()
    outcome_c: dict[str, Counter] = defaultdict(Counter)
    for jid, comp in jobid_company.items():
        applied[comp] += 1
        if jid in submitted:
            submitted_c[comp] += 1
        bo = job_outcome.get(jid)
        if bo:
            outcome_c[comp][bo] += 1

    # ATS / region breakdown (jobid-based, so consistent with `applied`)
    ats_applied = Counter(jid_ats.get(j, "other") for j in jobid_company)
    region_applied = Counter(jid_region.get(j, "UNKNOWN") for j in jobid_company)
    ats_interview = Counter()
    for jid in jobid_company:
        if job_outcome.get(jid) == "interview":
            ats_interview[jid_ats.get(jid, "other")] += 1

    companies = []
    for comp, n in applied.items():
        cc = outcome_c.get(comp, Counter())
        replied = sum(cc.values())
        interview = cc.get("interview", 0)
        companies.append({
            "key": comp,
            "name": display.get(comp, comp),
            "applied": n,
            "submitted": submitted_c.get(comp, 0),
            "replied": replied,
            "ack": cc.get("ack", 0),
            "action_needed": cc.get("action_needed", 0),
            "interview": interview,
            "rejection": cc.get("rejection", 0),
            "offer": cc.get("offer", 0),
            "reply_rate": round(100.0 * replied / n, 1) if n else 0.0,
            "interview_rate": round(100.0 * interview / n, 1) if n else 0.0,
        })
    companies.sort(key=lambda r: (r["interview"], r["applied"]), reverse=True)

    # global totals / funnel (all jobid-based)
    total_applied = sum(applied.values())
    total_submitted = sum(submitted_c.values())
    outcome_totals = Counter()
    for cc in outcome_c.values():
        outcome_totals.update(cc)
    total_replied = sum(outcome_totals.values())
    total_interview = outcome_totals.get("interview", 0)
    total_offer = outcome_totals.get("offer", 0)
    total_rejection = outcome_totals.get("rejection", 0)

    # daily trend (last 30 active days), inbound mail scoped to OUR applications
    days: dict[int, Counter] = defaultdict(Counter)
    for ts, kind, mb in inbound:
        if mb in app_emails:
            days[_day_start(ts)][kind] += 1
    day_keys = sorted(days)[-30:]
    trend = [{
        "day": dk,
        "interview": days[dk].get("interview", 0),
        "rejection": days[dk].get("rejection", 0),
        "ack": days[dk].get("ack", 0),
        "offer": days[dk].get("offer", 0),
        "total": sum(days[dk].values()),
    } for dk in day_keys]

    blob = {
        "generated_at": int(time.time()),
        "took_ms": int((time.time() - t0) * 1000),
        "totals": {
            "applied": total_applied,          # distinct job postings
            "attempts": scan["attempts"],      # fill attempts incl. retries
            "submitted": total_submitted,
            "replied": total_replied,
            "interview": total_interview,
            "offer": total_offer,
            "rejection": total_rejection,
            "companies": len(companies),
            "reply_rate": round(100.0 * total_replied / total_applied, 1) if total_applied else 0.0,
            "interview_rate": round(100.0 * total_interview / total_applied, 1) if total_applied else 0.0,
        },
        "companies": companies,
        "outcome_totals": {k: outcome_totals.get(k, 0) for k in _OUTCOME_KINDS},
        "ats": sorted(
            ({"ats": a, "applied": ats_applied[a], "interview": ats_interview.get(a, 0)}
             for a in ats_applied),
            key=lambda r: r["applied"], reverse=True),
        "regions": sorted(
            ({"region": r, "applied": region_applied[r]} for r in region_applied),
            key=lambda x: x["applied"], reverse=True),
        "trend": trend,
    }
    log.info("stats computed in %sms: %s companies, %s jobs (%s attempts)",
             blob["took_ms"], len(companies), total_applied, scan["attempts"])
    return blob


def get_stats(force: bool = False) -> dict:
    """Serve the cached blob; recompute synchronously if cold, refresh in the
    background if stale."""
    global _CACHE, _CACHE_AT, _REFRESHING
    now = time.time()
    if _CACHE is not None and not force and (now - _CACHE_AT) < _TTL:
        return _CACHE
    if _CACHE is None or force:
        with _LOCK:
            if _CACHE is None or force:
                _CACHE = compute_stats()
                _CACHE_AT = time.time()
        return _CACHE
    # stale but present: kick a single background refresh, serve stale meanwhile
    with _LOCK:
        if _REFRESHING:
            return _CACHE
        _REFRESHING = True
    threading.Thread(target=_bg_refresh, daemon=True).start()
    return _CACHE


def _bg_refresh() -> None:
    global _CACHE, _CACHE_AT, _REFRESHING
    try:
        blob = compute_stats()
        with _LOCK:
            _CACHE = blob
            _CACHE_AT = time.time()
    except Exception as e:
        log.warning("stats bg refresh failed: %s", e)
    finally:
        _REFRESHING = False
