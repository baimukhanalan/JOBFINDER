"""Per-COMPANY submission velocity guard — shared by EVERY apply path (the campaign cron, the
/catalog bulk drain, the «Незавершённые» re-run).

Root cause it prevents (2026-09-12 post-mortem): Salmon (Ashby `salmon-group`) received 149 fills on
42 jobs — 49 on 2026-08-23 alone, 36 on 08-27, then 39 more on 09-09..12 — from the bulk drain (130,
synthetic personas) + the Dana Erlan campaign (19). That velocity tripped Ashby's per-TENANT spam
filter: Dana Erlan still lands on OTHER Ashby companies (34 acks) but Salmon drops everything from our
source, and every new hit resets the cooldown clock. No single path had a per-company limit, so the
campaign's own quarantine could not stop the bulk drain. A per-company cap makes 149× on one company
impossible from ANY path.

Source of truth = the prefill dirs: EVERY fill attempt (any code path, any persona) writes
`uploads/prefill/<demo_id>/<jobid>/`, so their mtimes are a code-path-agnostic hit log (an ATTEMPT
counts — the ATS saw the submit whether or not it confirmed). jobid → company_key comes from
job_catalog. Caps are ROLLING windows: `COMPANY_CAP_PER_DAY` (default 2) and `COMPANY_CAP_PER_WEEK`
(default 6) per company. `COMPANY_CAP_OFF=1` disables (escape hatch). `guard()` both DROPS companies
already over cap and LIMITS a single batch to each company's remaining budget, so one bulk run can't
fire 10 at one tenant. Every lookup is guarded — on any error the guard lets the jobs through (a
bug must never block a lane); the injectable `jobs_by_ids`/`company_jobids` keep it unit-testable.
"""
from __future__ import annotations

import logging
import os
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger("company_velocity")

PREFILL_DIR = Path(__file__).resolve().parents[2] / "uploads" / "prefill"
_DAY = 24 * 3600
_WEEK = 7 * _DAY


def caps() -> tuple[int, int]:
    """(per_day, per_week) — env-tunable, floor 0 (0 = block the company entirely)."""
    def _env(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return default
    return _env("COMPANY_CAP_PER_DAY", 2), _env("COMPANY_CAP_PER_WEEK", 6)


def enabled() -> bool:
    return os.environ.get("COMPANY_CAP_OFF", "") not in ("1", "true", "yes", "on")


def prefill_hits(prefill_dir: Path | None = None) -> dict[int, list[float]]:
    """jobid -> [mtime, ...] over every `uploads/prefill/<demo>/<jobid>/` dir (one scan, cheap)."""
    root = Path(prefill_dir) if prefill_dir else PREFILL_DIR
    hits: dict[int, list[float]] = {}
    try:
        for demo in os.scandir(root):
            if not demo.is_dir():
                continue
            try:
                for jd in os.scandir(demo.path):
                    if jd.is_dir() and jd.name.isdigit():
                        try:
                            hits.setdefault(int(jd.name), []).append(jd.stat().st_mtime)
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        return {}
    return hits


def _company_of(jobids, jobs_by_ids=None) -> dict[int, str]:
    """jobid -> company_key for the candidate ids (catalog rows; injectable)."""
    ids = [int(j) for j in jobids]
    if not ids:
        return {}
    if jobs_by_ids is None:
        from backend.tools.catalog_db import jobs_by_ids as jobs_by_ids
    rows = jobs_by_ids(ids) or {}
    out: dict[int, str] = {}
    for j in ids:
        r = rows.get(j) if isinstance(rows, dict) else None
        if r is None and isinstance(rows, dict):
            r = rows.get(str(j))
        ck = (r or {}).get("company_key") if isinstance(r, dict) else None
        if ck:
            out[j] = str(ck).lower()
    return out


def _company_jobids(company_keys, company_jobids=None) -> dict[str, set[int]]:
    """company_key -> ALL its catalog jobids (we count hits on the whole tenant, not just the
    candidates in hand). Injectable for tests; default = one job_catalog query."""
    cks = sorted({str(c).lower() for c in company_keys if c})
    if not cks:
        return {}
    if company_jobids is not None:
        return {c: set(int(x) for x in (company_jobids(c) or [])) for c in cks}
    out: dict[str, set[int]] = {c: set() for c in cks}
    try:
        from backend.tools import mail_db
        with mail_db.conn() as cx, cx.cursor() as cur:
            cur.execute("SELECT id, lower(company_key) FROM job_catalog WHERE lower(company_key) = ANY(%s)",
                        (cks,))
            for jid, ck in cur.fetchall():
                out.setdefault(ck, set()).add(int(jid))
    except Exception as exc:
        logger.info("[velocity] company_jobids lookup failed: %s", exc)
    return out


def recent_counts(company_keys, *, company_jobids=None, hits=None, now=None) -> dict[str, dict]:
    """{company_key: {"day": n, "week": n}} — fill ATTEMPTS on the company in the rolling windows."""
    now = time.time() if now is None else now
    hits = prefill_hits() if hits is None else hits
    byco = _company_jobids(company_keys, company_jobids)
    out: dict[str, dict] = {}
    for ck, jids in byco.items():
        day = week = 0
        for j in jids:
            for ts in hits.get(j, ()):
                age = now - ts
                if age < 0:
                    continue
                if age <= _WEEK:
                    week += 1
                    if age <= _DAY:
                        day += 1
        out[ck] = {"day": day, "week": week}
    return out


def remaining(counts: dict, per_day: int | None = None, per_week: int | None = None) -> int:
    """How many more fills a company may take right now (never negative)."""
    pd, pw = caps()
    pd = pd if per_day is None else per_day
    pw = pw if per_week is None else per_week
    return max(0, min(pd - int(counts.get("day", 0)), pw - int(counts.get("week", 0))))


def guard(jobids, *, jobs_by_ids=None, company_jobids=None, hits=None, now=None,
          per_day: int | None = None, per_week: int | None = None) -> tuple[list[int], dict[str, int]]:
    """Filter candidate jobids: drop every company already over cap AND limit this batch to each
    company's remaining budget (order preserved). Returns (kept, {company_key: dropped_count}).
    Disabled (`COMPANY_CAP_OFF=1`) or on ANY internal error → returns the input untouched."""
    ids = [int(j) for j in jobids]
    if not ids or not enabled():
        return ids, {}
    try:
        comp = _company_of(ids, jobs_by_ids)
        counts = recent_counts(set(comp.values()), company_jobids=company_jobids, hits=hits, now=now)
        budget = {ck: remaining(c, per_day, per_week) for ck, c in counts.items()}
        kept: list[int] = []
        dropped: Counter = Counter()
        for j in ids:
            ck = comp.get(j)
            if ck is None or ck not in budget:      # unknown company → never block
                kept.append(j)
                continue
            if budget[ck] > 0:
                budget[ck] -= 1
                kept.append(j)
            else:
                dropped[ck] += 1
        if dropped:
            logger.info("[velocity] per-company cap held back %s",
                        ", ".join(f"{k}×{v}" for k, v in dropped.most_common(8)))
        return kept, dict(dropped)
    except Exception as exc:
        logger.info("[velocity] guard error (letting jobs through): %s", exc)
        return ids, {}
