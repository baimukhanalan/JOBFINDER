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


# cache the parsed email signals by the immutable message hash (content never changes per hash)
_MSG_CACHE: dict[str, dict] = {}


def _msg_signals(msg_hash: str) -> dict:
    """{deadline_ts, estimated, subject, body} for one interview message hash — the file is
    immutable, so it is read + parsed ONCE per hash and the whole-pool enrich pays each parse a
    single time. Body truncated (role classification only needs the top)."""
    if not msg_hash:
        return {"deadline_ts": None, "estimated": True, "subject": "", "body": ""}
    if msg_hash in _MSG_CACHE:
        return _MSG_CACHE[msg_hash]
    res = {"deadline_ts": None, "estimated": True, "subject": "", "body": ""}
    try:
        from backend.tools import mailcrm
        m = mailcrm.get_message(msg_hash, mark=False)   # mark=False → never flips new/→cur/
        if m:
            subj = m.get("subject") or ""
            body = m.get("plain") or m.get("html") or m.get("snippet") or ""
            dl, est = extract_deadline(subj, body, m.get("date_ts") or 0)
            res = {"deadline_ts": dl, "estimated": est, "subject": subj, "body": body[:2000]}
    except Exception:
        pass
    _MSG_CACHE[msg_hash] = res
    return res


def _deadline_for_hash(msg_hash: str) -> tuple[int | None, bool]:
    s = _msg_signals(msg_hash)
    return s["deadline_ts"], s["estimated"]


def _role_from_email(subject: str, body: str) -> str | None:
    """Recover an APPROXIMATE role_category from the interview invitation itself (the email is
    never pruned, unlike the prefill artifact) — the durable fallback when the persona's jobid is
    gone. Maps the invite's role title via the 13-bucket classifier; None when unclassifiable."""
    try:
        from backend.applier import role_category
        cat, _ = role_category.classify_role(subject or "", "")
        if (not cat or cat == "Other") and body:
            cat, _ = role_category.classify_role(f"{subject} {body[:400]}", "")
        return cat if (cat and cat != "Other") else None
    except Exception:
        return None


def is_expired(g: dict) -> bool:
    """True when the booking window has lapsed (deadline strictly in the past). A None deadline
    (unknown) is NOT expired. Drives «делегировать нельзя» + sink-to-bottom + pool exclusion."""
    d = g.get("deadline_days")
    return d is not None and d < 0


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
      direction (it|nonit|other), role_category, jobid, salary_value, salary_label,
      salary_estimated (True when the salary is a category median, not the job's posted comp).
    `hash_key` is the row field holding the interview message hash the deadline is parsed from
    ('iv_hash' for grouped-inbox rows, 'source_hash' for pool rows).

    Coverage: the persona→job link (prefill artifact) is pruned by retention after 20 days, so
    for most of the (older) interview cohort the exact jobid/comp is gone. We recover it from the
    durable status.json first (pool._base_meta), and where even that is missing OR the role is
    unclassified, fall back to the interview EMAIL — its role title → role_category → direction +
    the category's MEDIAN est-comp — so EVERY candidate gets a direction + an approximate salary.
    Best-effort — any failure just leaves the row without that signal."""
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
    try:
        from backend.interviews.pool import direction_of as _direction_of
    except Exception:
        _direction_of = None
    now = int(time.time())
    for g in groups:
        job = {}
        jid = g.get("jobid")
        if jid and str(jid).isdigit():
            job = jobs.get(int(jid)) or {}
        sig = _msg_signals(g.get(hash_key) or "")
        # refine an unknown / «Other» direction from the interview email's role title
        cat = g.get("role_category")
        if (not cat or cat == "Other"):
            ecat = _role_from_email(sig["subject"], sig["body"])
            if ecat:
                cat = ecat
                g["role_category"] = cat
                if _direction_of:
                    g["direction"] = _direction_of(cat)
        # salary: the job's exact comp when we have it, else the category MEDIAN (approximate)
        has_comp = any(job.get(k) for k in ("comp_min", "comp_max", "est_total_min",
                                            "est_total_max", "est_base_min", "est_base_max"))
        if not has_comp:
            try:
                from backend.applier import est_comp
                job = est_comp.estimate(cat, ["US"])
                g["salary_estimated"] = True
            except Exception:
                pass
        else:
            g["salary_estimated"] = False
        g["salary_value"] = salary_value(job)
        g["salary_label"] = salary_label(job)
        g["deadline_ts"] = sig["deadline_ts"]
        g["deadline_estimated"] = sig["estimated"]
        g["deadline_days"] = days_left(sig["deadline_ts"], now)
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


def _is_actionable(g: dict) -> bool:
    """A booking deadline still in the future (or today) — i.e. one the operator can still act
    on. An OVERDUE window (the invite lapsed) is not actionable and sinks in the urgency sort."""
    d = g.get("deadline_days")
    return d is not None and d >= 0


def sort_groups(groups: list[dict], sort: str) -> list[dict]:
    """Order one section. In BOTH modes a still-bookable interview always outranks an EXPIRED
    one — a months-old lapsed booking window is no longer actionable, so it sinks to the very
    bottom instead of burying (urgency) or being interleaved with (salary) the ones the operator
    can still book. Within the still-bookable group: sort='urgency' → soonest deadline first
    (then higher salary); sort='salary' (default) → highest salary first (then soonest deadline)."""
    if sort == "urgency":
        return sorted(groups, key=lambda g: (
            0 if _is_actionable(g) else 1,
            g.get("deadline_ts") or _DEADLINE_FAR,
            -(g.get("salary_value") or 0)))
    return sorted(groups, key=lambda g: (
        0 if _is_actionable(g) else 1,
        -(g.get("salary_value") or _SALARY_KEY_NA),
        g.get("deadline_ts") or _DEADLINE_FAR))
