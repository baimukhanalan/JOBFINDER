"""Priority signals for the «Собес» (interview) surface of the Кандидаты screen.

Two per-candidate signals, both best-effort and never fatal to a render:

  1. SCHEDULING DEADLINE — how much time is left to reserve the interview slot. Many
     interview / scheduling mails state it ("schedule within N days", "book by <date>",
     "respond within 48 hours", a Calendly window …). `extract_deadline` parses the latest
     interview message; when nothing explicit is found it falls back to invite-date + a
     sensible default and marks the result ESTIMATED.

  2. POTENTIAL SALARY + DIRECTION — the comp of the job the persona applied to (job_catalog
     via the persona → jobid link, reusing `interviews.pool.enrich`) and its IT / non-IT
     direction (`pool.direction_of`). Used to rank + split the surface.

Neutral Russian labels only — never any stack name. Results are cached (deadline by the
immutable message hash, meta by email) so the whole-pool sort pays the parse cost once.
"""
from __future__ import annotations

import math
import re
import time

# ---- scheduling-deadline extraction ----------------------------------------------

_DAY = 86400
_HOUR = 3600
# When a mail gives no explicit deadline we still surface an ESTIMATED one so every Собес row
# has an urgency signal; scheduling windows are typically a few days.
DEFAULT_DAYS = 5

# Relative windows: "within (the next) N (business) days", "N days to schedule", "next N days".
_REL_DAYS = re.compile(
    r"(?:within|in)\s+(?:the\s+)?(?:next\s+)?(\d{1,3})\s+(?:(?:business|working|calendar|full)\s+)?days?"
    r"|(\d{1,3})\s+(?:(?:business|working|calendar)\s+)?days?\s+(?:to|left|remaining)\b"
    r"|next\s+(\d{1,3})\s+days?"
    r"|в\s+течение\s+(\d{1,3})\s+(?:рабочих\s+)?дн",
    re.I)
_REL_HOURS = re.compile(
    r"(?:within|in)\s+(?:the\s+)?(?:next\s+)?(\d{1,3})\s+hours?"
    r"|(\d{1,3})\s+hours?\s+(?:to|left|remaining)\b"
    r"|next\s+(\d{1,3})\s+hours?"
    r"|в\s+течение\s+(\d{1,3})\s+час",
    re.I)
# Explicit-date triggers — the phrase that introduces a due date; the date itself is parsed by
# dateutil from the ~48 chars that follow the trigger.
_DATE_TRIGGER = re.compile(
    r"(?:by|before|no\s+later\s+than|due(?:\s+by)?|expires?(?:\s+on)?|deadline|"
    r"respond\s+by|reply\s+by|complete(?:\s+this|\s+the)?\s+by|schedule\s+(?:by|before)|"
    r"book\s+(?:by|before)|до)\b[:\s]+(.{0,48})",
    re.I)
# A date-looking substring inside the trigger tail: "March 3", "3 March", "03/14", "2026-03-14",
# "Mar 3rd". Anchored loosely; dateutil does the real parse.
_DATE_LIKE = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)",
    re.I)


def _parse_date(tail: str, ref_ts: int) -> int | None:
    """Parse a due date out of the trigger tail → a unix ts, or None. `ref_ts` (the invite
    time) supplies the default year/month for a bare "March 3"."""
    m = _DATE_LIKE.search(tail or "")
    if not m:
        return None
    from datetime import datetime, timezone
    try:
        from dateutil import parser as _dp
        ref = datetime.fromtimestamp(ref_ts or time.time(), tz=timezone.utc)
        dt = _dp.parse(m.group(1), default=ref, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # a bare "March 3" that resolved to BEFORE the invite date is almost always next year
        # (an invite in December pointing at a January due date).
        if dt.date() < ref.date():
            try:
                dt = dt.replace(year=dt.year + 1)
            except ValueError:                       # Feb 29 → non-leap target
                dt = dt.replace(month=2, day=28, year=dt.year + 1)
        return int(dt.timestamp())
    except Exception:
        return None


def _first_group(m: "re.Match") -> int | None:
    for g in m.groups():
        if g and g.isdigit():
            return int(g)
    return None


def extract_deadline(subject: str, body: str, invite_ts: int) -> tuple[int | None, bool]:
    """(deadline_ts, estimated) for one interview mail. Collects every explicit window/date it
    can find and takes the SOONEST that is not before the invite; when none is found, falls back
    to invite + DEFAULT_DAYS and flags it estimated. Never raises."""
    text = f"{subject or ''}\n{body or ''}"
    invite_ts = int(invite_ts or 0) or int(time.time())
    found: list[int] = []
    try:
        for m in _REL_DAYS.finditer(text):
            n = _first_group(m)
            if n and 0 < n <= 120:
                found.append(invite_ts + n * _DAY)
        for m in _REL_HOURS.finditer(text):
            n = _first_group(m)
            if n and 0 < n <= 24 * 30:
                found.append(invite_ts + n * _HOUR)
        for m in _DATE_TRIGGER.finditer(text):
            ts = _parse_date(m.group(1), invite_ts)
            # a due date must be at/after the invite and within a sane horizon (~120 days)
            if ts and invite_ts - _DAY <= ts <= invite_ts + 120 * _DAY:
                found.append(ts)
    except Exception:
        found = []
    valid = [ts for ts in found if ts >= invite_ts - _DAY]
    if valid:
        return min(valid), False
    return invite_ts + DEFAULT_DAYS * _DAY, True


# cache the parsed deadline by the immutable message hash (email content never changes per hash)
_DL_CACHE: dict[str, tuple[int | None, bool]] = {}


def _deadline_for_hash(iv_hash: str) -> tuple[int | None, bool]:
    if not iv_hash:
        return None, True
    if iv_hash in _DL_CACHE:
        return _DL_CACHE[iv_hash]
    res: tuple[int | None, bool] = (None, True)
    try:
        from backend.tools import mailcrm
        m = mailcrm.get_message(iv_hash, mark=False)   # mark=False → never flips new/→cur/
        if m:
            body = m.get("plain") or m.get("html") or m.get("snippet") or ""
            res = extract_deadline(m.get("subject") or "", body, m.get("date_ts") or 0)
    except Exception:
        res = (None, True)
    _DL_CACHE[iv_hash] = res
    return res


def days_left(deadline_ts: int | None, now: int | None = None) -> int | None:
    """Whole days from now until the deadline (floor). Negative = overdue. None passthrough."""
    if not deadline_ts:
        return None
    now = int(now or time.time())
    return int(math.floor((deadline_ts - now) / _DAY))


def deadline_text(g: dict) -> tuple[str, str]:
    """(label, level) for an enriched row's booking deadline — the single source of the
    urgency wording, reused by the Собес card chip AND the /users priority list. level ∈
    {ok, soon, urgent, over}; «~» prefix + «(оценка)» when the deadline is an estimate. Returns
    ('', 'ok') when the row has no deadline."""
    ts = g.get("deadline_ts")
    d = g.get("deadline_days")
    if not ts or d is None:
        return "", "ok"
    pfx = "~" if g.get("deadline_estimated") else ""
    if d < 0:
        return "срок истёк", "over"
    if d == 0:
        return f"{pfx}сегодня", "urgent"
    lvl = "urgent" if d <= 1 else "soon" if d <= 3 else "ok"
    return f"{pfx}осталось {d} дн", lvl


# ---- potential salary ------------------------------------------------------------
def salary_value(job: dict) -> int:
    """A single comparable annual number for RANKING — the highest meaningful figure on the
    job (posted ceiling / researched total / base). 0 when the job has no comp at all."""
    j = job or {}
    nums = []
    for key in ("est_total_max", "comp_max", "est_base_max",
                "est_total_min", "comp_min", "est_base_min"):
        v = j.get(key)
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            nums.append(iv)
    return max(nums) if nums else 0


def salary_label(job: dict) -> str:
    """A COMPACT comp string for a card chip: the posted range if present, else the estimated
    total / base (prefixed «~»). '' when nothing. Uses comp_fmt so currency stays correct."""
    from backend.tools import comp_fmt
    s = comp_fmt.comp_summary(job or {})
    if s["posted"]:
        return s["posted"]
    if s["est_total"]:
        return "~" + s["est_total"]
    if s["est_base"]:
        return "~" + s["est_base"]
    return ""


# ---- enrichment ------------------------------------------------------------------
def enrich_interview_groups(groups: list[dict], *, hash_key: str = "iv_hash") -> list[dict]:
    """Add priority signals to each interview row (mutates + returns). Adds:
      deadline_ts, deadline_days (whole days left, may be negative), deadline_estimated,
      direction (it|nonit|other), role_category, jobid, salary_value, salary_label.
    `hash_key` is the row field holding the interview message hash the deadline is parsed from
    ('iv_hash' for grouped-inbox rows, 'source_hash' for pool rows). Best-effort — any failure
    just leaves the row without that signal."""
    groups = groups or []
    if not groups:
        return groups
    # direction / jobid / role_category (reuses the pool's per-email cache + one batched
    # job_catalog lookup); safe if the interviews package is degraded. Pool rows already carry
    # direction (pool.enrich ran when they were listed) — skip the extra pass for them.
    if any("direction" not in g for g in groups):
        try:
            from backend.interviews import pool
            pool.enrich(groups)
        except Exception:
            for g in groups:
                g.setdefault("direction", "other")
                g.setdefault("jobid", None)
                g.setdefault("role_category", None)
    # potential salary from job_catalog (one batched query for all jobids)
    jobs: dict = {}
    try:
        from backend.tools import catalog_db
        ids = sorted({int(g["jobid"]) for g in groups
                      if g.get("jobid") and str(g["jobid"]).isdigit()})
        if ids:
            jobs = catalog_db.jobs_by_ids(ids)
    except Exception:
        jobs = {}
    now = int(time.time())
    for g in groups:
        job = {}
        jid = g.get("jobid")
        if jid and str(jid).isdigit():
            job = jobs.get(int(jid)) or {}
        g["salary_value"] = salary_value(job)
        g["salary_label"] = salary_label(job)
        dl_ts, est = _deadline_for_hash(g.get(hash_key) or "")
        g["deadline_ts"] = dl_ts
        g["deadline_estimated"] = est
        g["deadline_days"] = days_left(dl_ts, now)
        g.setdefault("direction", "other")
    return groups


# ---- sort + split ----------------------------------------------------------------
# IT direction → the «сложные / IT» section; everything else (non-IT + unknown) → «простые».
def partition(groups: list[dict]) -> tuple[list[dict], list[dict]]:
    it = [g for g in groups if g.get("direction") == "it"]
    simple = [g for g in groups if g.get("direction") != "it"]
    return it, simple


_SALARY_KEY_NA = -1              # a jobless / no-comp row sorts to the bottom of a salary sort
_DEADLINE_FAR = 10 ** 12         # a deadline-less row sorts to the bottom of an urgency sort


def sort_groups(groups: list[dict], sort: str) -> list[dict]:
    """Order one section. sort='urgency' → soonest deadline first (then higher salary);
    sort='salary' (default) → highest potential salary first (then soonest deadline)."""
    if sort == "urgency":
        return sorted(groups, key=lambda g: (
            g.get("deadline_ts") or _DEADLINE_FAR,
            -(g.get("salary_value") or 0)))
    return sorted(groups, key=lambda g: (
        -(g.get("salary_value") or _SALARY_KEY_NA),
        g.get("deadline_ts") or _DEADLINE_FAR))
