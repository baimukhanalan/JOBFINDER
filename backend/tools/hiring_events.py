"""Capture Teleperformance "Virtual Hiring Event" invites into an actionable lane.

Teleperformance mass-mails personas a "Join our Virtual Hiring Event" invitation
(sender ``teleperformance…@talent.icims.com``): a LIVE Zoom room where a human joins
and is hired on the spot for a Remote CSR role — with NO assessment. It is a no-test
offer channel, but the mail classifier deliberately routes these mass-blasts to the
``other`` kind (they are not personal 1:1 interviews), so they never reach the «Собес»
interview pool and were previously invisible + un-actioned.

This module is a SEPARATE capture that reads those invites straight out of ``mail_index``
by sender + subject, extracts the event schedule + the Zoom join link (resolving the
icims tracking redirect server-side), and feeds a dedicated «События найма» operator
surface — kept DISTINCT from real interviews so the «Собес» pool stays clean. It does
NOT touch the classifier and does NOT write ``iv_interviews`` rows.

Read-only over ``mail_index`` + the Maildir (no mutation). The resolved-Zoom cache lives
in a gitignored JSON. Neutral user-facing text; no assistant/stack names.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html import escape

from backend.tools import mail_db

# ---- matching --------------------------------------------------------------------
# The distinguishing signal is the SUBJECT ("virtual hiring event" / "hiring event")
# from a Teleperformance icims sender. Kept narrow so ordinary TP/icims application
# mail (confirmations, assessment invites) is never swept in.
_SUBJECT_RE = re.compile(r"(virtual\s+hiring\s+event|hiring\s+event)", re.I)
_SENDER_RE = re.compile(r"(teleperformance|talent\.icims\.com)", re.I)

# SQL predicate mirror of the two regexes above (case-insensitive), for the DB scan.
_MATCH_SQL = (
    "NOT outbound "
    "AND (from_email ILIKE '%teleperformance%' OR from_email ILIKE '%talent.icims.com%') "
    "AND (subject ILIKE '%virtual hiring event%' OR subject ILIKE '%hiring event%')"
)

# icims click-tracking link shape (redirects to the real Zoom room).
_ICIMS_RE = re.compile(r"https://tracking\.icims\.com/f/a/\S+")
# an <a href="…">text</a> anchor (DOTALL for multi-line inner text).
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
# the unsubscribe URL follows "please go to:" in the plain footer.
_UNSUB_RE = re.compile(r"go to:\s*(https://tracking\.icims\.com/\S+)", re.I)
# a direct Zoom URL glued to the "Zoom:" label in the plain body (variant A).
_ZOOM_INLINE_RE = re.compile(r"Zoom[:\s ]*?(https://tracking\.icims\.com/\S+)", re.I)
_ZOOM_ID_RE = re.compile(r"zoom\.us/(?:j|s|w|my)/([A-Za-z0-9]+)", re.I)

_SCHED_DATE_RE = re.compile(r"Hiring Event:\s*(.+)", re.I)
_SCHED_TIME_RE = re.compile(r"\bTime:\s*(.+)", re.I)
_ROLE_BODY_RE = re.compile(r"hiring\s+(Remote[^,.\n\r]+)", re.I)


def is_hiring_event(subject: str, from_email: str) -> bool:
    """True iff this looks like a Teleperformance Virtual Hiring Event invite (icims
    sender + a hiring-event subject). Pure; used both to validate DB rows and by tests."""
    return bool(_SUBJECT_RE.search(subject or "") and _SENDER_RE.search(from_email or ""))


# ---- body extraction -------------------------------------------------------------
def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def extract_schedule(plain: str) -> tuple[str, str]:
    """(date_text, time_text) from the plain body, e.g. ("Monday–Friday",
    "9:30 AM – 5:00 PM ET"). Empty strings when a line is absent."""
    plain = plain or ""
    d = _SCHED_DATE_RE.search(plain)
    t = _SCHED_TIME_RE.search(plain)
    date_text = d.group(1).strip() if d else ""
    time_text = t.group(1).strip() if t else ""
    return date_text, time_text


def extract_role(subject: str, plain: str) -> str:
    """A concise role label: the body's "hiring Remote …" phrase first (most specific),
    else the subject tail after an en/em dash, else a neutral default."""
    m = _ROLE_BODY_RE.search(plain or "")
    if m:
        return m.group(1).strip()
    subj = subject or ""
    for sep in ("–", "—", " - "):
        if sep in subj:
            tail = subj.split(sep, 1)[1].strip()
            if tail:
                return tail
    return "Remote CSR"


def extract_join(plain: str, html: str) -> tuple[str | None, str | None]:
    """(join_tracking_url, unsubscribe_url) — the icims tracking link that opens the Zoom
    room, and the unsubscribe link (so it is never mistaken for the join link).

    Resolution order for the join link:
      1. an inline "Zoom:<url>" in the plain body (variant A);
      2. the HTML anchor whose visible text says "Join …" (variant B);
      3. the first icims tracking link that is NOT the unsubscribe link.
    Returns (None, …) if only the unsubscribe link exists."""
    plain = plain or ""
    html = html or ""
    unsub = None
    mu = _UNSUB_RE.search(plain)
    if mu:
        unsub = mu.group(1).rstrip(".,)")

    anchors = [(href, _strip_tags(txt)) for href, txt in _ANCHOR_RE.findall(html)
               if _ICIMS_RE.match(href)]
    if unsub is None:
        # fall back to the LAST icims anchor whose visible text is a bare URL / "unsubscribe"
        for href, txt in reversed(anchors):
            if txt.lower().startswith("http") or "unsub" in txt.lower():
                unsub = href
                break

    # 1: inline plain URL right after the Zoom label
    mi = _ZOOM_INLINE_RE.search(plain)
    if mi:
        cand = mi.group(1).rstrip(".,)")
        if cand != unsub:
            return cand, unsub

    # 2: the HTML "Join …" anchor
    for href, txt in anchors:
        if href != unsub and re.search(r"\bjoin\b", txt, re.I):
            return href, unsub

    # 3: first icims link that is not the unsubscribe link
    for href, _txt in anchors:
        if href != unsub:
            return href, unsub
    for m in _ICIMS_RE.finditer(plain):
        cand = m.group(0).rstrip(".,)")
        if cand != unsub:
            return cand, unsub
    return None, unsub


def zoom_meeting_id(url: str | None) -> str | None:
    """The Zoom meeting id from a resolved ``*.zoom.us/j/<id>`` URL, else None."""
    if not url:
        return None
    m = _ZOOM_ID_RE.search(url)
    return m.group(1) if m else None


# ---- candidate résumé + detail (per persona, from uploads/prefill) ---------------
# A hiring-event invite is a synthetic persona whose email IS its takhet.com mailbox.
# Its generated résumé PDF + structured facts live in
# ``uploads/prefill/<demo_id>/<jobid>/`` ({resume.pdf, persona.json}). We map the mailbox
# to that dir, expose the state / ФИО / approximate age the operator wants on the card,
# and serve the PDF. Read-only; neutral RU everywhere (no stack names).
_PREFILL_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "prefill"))
# a 4-digit 19xx/20xx year anywhere in an education "year" / experience "dates" string.
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# assumed age at the earliest résumé anchor (graduation / first job) — turns that year
# into an approximate CURRENT age. Deliberately coarse; the UI always labels it «~N г.».
_ANCHOR_AGE = 22


def _demo_id_guess(mailbox: str) -> str:
    """Deterministic demo-id for a persona mailbox (`first.last123@…` → `demo_first_last123`),
    the fallback when the demo-registry lookup misses (no scan of every prefill dir)."""
    local = (mailbox or "").strip().lower().split("@")[0]
    return "demo_" + re.sub(r"[^a-z0-9]+", "_", local).strip("_")


def prefill_dir_for(mailbox: str, *, root: str | None = None,
                    id_resolver=None) -> str | None:
    """The newest ``uploads/prefill/<demo_id>/<jobid>`` dir that has a ``persona.json`` for
    this persona mailbox, or None. demo_id via the demo registry
    (``candidate_apps.id_for_email``), else a deterministic localpart guess. ``root`` and
    ``id_resolver`` are injectable so the resolution is unit-testable off disk."""
    email = (mailbox or "").strip().lower()
    if not email:
        return None
    root = root or _PREFILL_ROOT
    if id_resolver is None:
        try:
            from backend.tools import candidate_apps
            id_resolver = candidate_apps.id_for_email
        except Exception:
            def id_resolver(_e):
                return None
    ids: list[str] = []
    try:
        cid = id_resolver(email)
    except Exception:
        cid = None
    if cid:
        ids.append(cid)
    guess = _demo_id_guess(email)
    if guess and guess not in ids:
        ids.append(guess)
    for cid in ids:
        d = os.path.join(root, cid)
        if not os.path.isdir(d):
            continue
        best = None
        best_mt = -1.0
        try:
            subs = os.listdir(d)
        except OSError:
            continue
        for sub in subs:
            pj = os.path.join(d, sub, "persona.json")
            if os.path.isfile(pj):
                try:
                    mt = os.path.getmtime(pj)
                except OSError:
                    mt = 0.0
                if mt > best_mt:
                    best_mt = mt
                    best = os.path.join(d, sub)
        if best:
            return best
    return None


def load_persona(mailbox: str, **kw) -> dict | None:
    """The persona.json dict for a hiring-event mailbox (newest prefill dir), or None."""
    d = prefill_dir_for(mailbox, **kw)
    if not d:
        return None
    try:
        with open(os.path.join(d, "persona.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def estimate_age(resume: dict | None, *, now_year: int | None = None) -> int | None:
    """Approximate CURRENT age from a résumé dict: the EARLIEST year across education
    graduation years + experience date-ranges is treated as the applicant's ~age-22 anchor
    (``age ≈ now − anchor + 22``). Returns None when no plausible year is present or the
    result falls outside a sane 18–75 guard band — the caller then OMITS age rather than
    guess wildly. Pure; the UI always shows it as approximate («~N г.»)."""
    resume = resume or {}
    now_year = now_year or datetime.date.today().year
    years: list[int] = []
    for e in (resume.get("education") or []):
        if isinstance(e, dict):
            years += [int(y) for y in _YEAR_RE.findall(str(e.get("year") or ""))]
    for e in (resume.get("experience") or []):
        if isinstance(e, dict):
            years += [int(y) for y in _YEAR_RE.findall(str(e.get("dates") or ""))]
    years = [y for y in years if 1950 <= y <= now_year]
    if not years:
        return None
    age = (now_year - min(years)) + _ANCHOR_AGE
    return age if 18 <= age <= 75 else None


def candidate_detail(persona: dict | None, *, fallback_name: str = "") -> dict:
    """``{full_name, state, age}`` for a persona.json dict — the operator's expand panel.
    Pure. ``state`` prefers ``profile.state`` then city/location; ``age`` via
    :func:`estimate_age`; a missing field comes back as ``''`` / ``None`` so the UI omits
    it. ``fallback_name`` (e.g. the invite's display name) is used when the persona is
    unresolved / nameless."""
    persona = persona or {}
    prof = persona.get("profile") or {}
    resume = prof.get("resume") or persona.get("resume") or {}
    pi = resume.get("personal_info") or {}
    full_name = (prof.get("full_name") or prof.get("name") or pi.get("full_name")
                 or pi.get("name") or fallback_name or "").strip()
    state = (prof.get("state") or prof.get("city") or prof.get("location")
             or pi.get("location") or "").strip()
    return {"full_name": full_name, "state": state, "age": estimate_age(resume)}


def resume_pdf_path(mailbox: str, **kw) -> str | None:
    """Path to the persona's generated résumé PDF (``resume.pdf`` in its newest prefill dir),
    or None when absent (the caller then hides the button / renders on the fly)."""
    d = prefill_dir_for(mailbox, **kw)
    if not d:
        return None
    p = os.path.join(d, "resume.pdf")
    return p if os.path.isfile(p) else None


def resume_filename(mailbox: str, persona: dict | None = None) -> str:
    """A human, ASCII-safe download filename for a persona's résumé («Samuel Nash - resume.pdf»).
    Falls back to the mailbox localpart when the name is unknown."""
    if persona is None:
        persona = load_persona(mailbox)
    name = candidate_detail(persona).get("full_name") if persona else ""
    if not name:
        name = (mailbox or "").split("@")[0] or "resume"
    safe = re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip() or "resume"
    return f"{safe} - resume.pdf"


# ---- Zoom resolution (icims tracking redirect → real room), cached ---------------
_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "hiring_events_zoom.json")
_CACHE_LOCK = threading.Lock()
_CACHE: dict | None = None


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            with open(_CACHE_PATH, encoding="utf-8") as fh:
                _CACHE = json.load(fh)
        except Exception:
            _CACHE = {}
    return _CACHE


def _save_cache(cache: dict) -> None:
    tmp = f"{_CACHE_PATH}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=0)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _resolve_one(tracking_url: str, timeout: float = 12.0) -> str | None:
    """Follow the icims tracking redirect to the real destination (the Zoom room). One
    light request (no redirect-follow, no body): the 302 ``Location`` header IS the Zoom
    URL. Falls back to a redirect-following GET. Best-effort → None on any failure."""
    try:
        import httpx
    except Exception:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers) as c:
            r = c.get(tracking_url)
            loc = r.headers.get("location")
            if r.is_redirect and loc:
                return loc
            if r.status_code < 400 and str(r.url) != tracking_url:
                return str(r.url)
    except Exception:
        pass
    try:  # some tracking hops need the full follow
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as c:
            r = c.get(tracking_url)
            final = str(r.url)
            return final if final and final != tracking_url else None
    except Exception:
        return None


def resolve_join_url(tracking_url: str | None, *, use_cache: bool = True,
                     write: bool = True) -> str | None:
    """Resolve one icims tracking link to its real destination, cached by tracking URL.
    Returns the resolved URL, or None when resolution failed (the caller then keeps the
    tracking link, which still redirects to Zoom when a human clicks it)."""
    if not tracking_url:
        return None
    cache = _load_cache()
    if use_cache and tracking_url in cache:
        return (cache.get(tracking_url) or {}).get("url")
    resolved = _resolve_one(tracking_url)
    if write:
        with _CACHE_LOCK:
            cache = _load_cache()
            cache[tracking_url] = {"url": resolved, "ts": int(time.time())}
            _save_cache(cache)
    return resolved


def resolve_many(tracking_urls: list[str], *, workers: int = 8) -> dict[str, str | None]:
    """Resolve a batch of tracking links concurrently (cache-first). Returns
    {tracking_url: resolved_url|None}. Only misses hit the network."""
    urls = [u for u in dict.fromkeys(tracking_urls) if u]
    cache = _load_cache()
    out: dict[str, str | None] = {}
    misses: list[str] = []
    for u in urls:
        if u in cache:
            out[u] = (cache.get(u) or {}).get("url")
        else:
            misses.append(u)
    if misses:
        with ThreadPoolExecutor(max_workers=min(workers, len(misses))) as ex:
            results = list(ex.map(lambda u: (u, _resolve_one(u)), misses))
        with _CACHE_LOCK:
            cache = _load_cache()
            now = int(time.time())
            for u, r in results:
                cache[u] = {"url": r, "ts": now}
                out[u] = r
            _save_cache(cache)
    return out


# ---- DB scan + aggregation -------------------------------------------------------
def _event_rows() -> list[dict]:
    """The raw hiring-event invite rows from ``mail_index`` (newest first)."""
    sql = (
        "SELECT mailbox, candidate, from_name, from_email, subject, snippet, "
        "       path, path_hash, date_ts "
        f"FROM mail_index WHERE {_MATCH_SQL} "
        "ORDER BY date_ts DESC, path_hash DESC"
    )
    try:
        with mail_db._cur() as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _parse_invite(row: dict) -> dict | None:
    """Enrich one invite row with the parsed schedule, role and Zoom tracking link.
    Reads the Maildir file directly (no state change). None if the file is unreadable
    or it is not actually a hiring-event invite."""
    subject = row.get("subject") or ""
    from_email = row.get("from_email") or ""
    if not is_hiring_event(subject, from_email):
        return None
    plain = html = ""
    path = row.get("path")
    if path and os.path.isfile(path):
        try:
            from backend.tools import mailcrm
            full = mailcrm._parse_full(path, row.get("path_hash") or "")
            if full:
                plain = full.get("plain") or ""
                html = full.get("html") or ""
        except Exception:
            pass
    date_text, time_text = extract_schedule(plain)
    role = extract_role(subject, plain)
    tracking_url, _unsub = extract_join(plain, html)
    invite_ts = int(row.get("date_ts") or 0)
    # Booking DEADLINE for THIS candidate — the SAME logic as the «Собес» surface: an explicit
    # «within N days» / «by <date>» window is a real countdown, else an ESTIMATE from invite +
    # DEFAULT_DAYS. Best-effort — a parse miss must never drop the invite.
    deadline_ts = None
    deadline_estimated = True
    invite_age_days = None
    try:
        from backend.tools import interview_priority as _ip
        deadline_ts, deadline_estimated = _ip.extract_deadline(subject, plain, invite_ts)
    except Exception:
        deadline_ts, deadline_estimated = None, True
    deadline_days = None
    if deadline_ts:
        try:
            deadline_days = int((deadline_ts - time.time()) // 86400)
        except Exception:
            deadline_days = None
    if invite_ts:
        try:
            invite_age_days = max(0, int((time.time() - invite_ts) // 86400))
        except Exception:
            invite_age_days = None
    return {
        "mailbox": row.get("mailbox") or "",
        "candidate": row.get("candidate") or "",
        "from_name": row.get("from_name") or "",
        "from_email": from_email,
        "subject": subject,
        "path_hash": row.get("path_hash") or "",
        "date_ts": row.get("date_ts") or 0,
        "date_text": date_text,
        "time_text": time_text,
        "role": role,
        "tracking_url": tracking_url,
        "deadline_ts": deadline_ts,
        "deadline_days": deadline_days,
        "deadline_estimated": deadline_estimated,
        "invite_age_days": invite_age_days,
    }


def events(*, resolve: bool = True) -> list[dict]:
    """Every captured hiring-event invite (one per persona mailbox × message), newest
    first, each carrying its schedule, role, tracking link and — when ``resolve`` — the
    resolved ``join_url`` (the real Zoom room) + ``meeting_id``. Falls back to the
    tracking link as ``join_url`` when resolution fails (it still opens Zoom on click)."""
    invites = [inv for inv in (_parse_invite(r) for r in _event_rows()) if inv]
    if resolve:
        mapping = resolve_many([inv["tracking_url"] for inv in invites if inv["tracking_url"]])
    else:
        mapping = {}
    for inv in invites:
        resolved = mapping.get(inv["tracking_url"]) if inv["tracking_url"] else None
        inv["join_url"] = resolved or inv["tracking_url"]
        inv["meeting_id"] = zoom_meeting_id(resolved)
        inv["resolved"] = bool(resolved)
    return invites


def grouped_events(*, resolve: bool = True) -> list[dict]:
    """Invites grouped by the Zoom room they open (so one card = one live room + all the
    personas invited to it). Groups keyed by ``meeting_id`` when resolved, else by the
    tracking link. Each group: {key, meeting_id, join_url, role, date_text, time_text,
    latest_ts, invites:[…]} sorted by most-recent invite first."""
    groups: dict[str, dict] = {}
    for inv in events(resolve=resolve):
        key = inv.get("meeting_id") or inv.get("join_url") or inv["path_hash"]
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "key": key,
                "meeting_id": inv.get("meeting_id"),
                "join_url": inv.get("join_url"),
                "role": inv.get("role") or "Remote CSR",
                "date_text": inv.get("date_text") or "",
                "time_text": inv.get("time_text") or "",
                "latest_ts": inv.get("date_ts") or 0,
                "invites": [],
            }
        g["invites"].append(inv)
        if (inv.get("date_ts") or 0) > g["latest_ts"]:
            g["latest_ts"] = inv["date_ts"]
            # keep the freshest schedule/room representative
            g["join_url"] = inv.get("join_url") or g["join_url"]
            g["date_text"] = inv.get("date_text") or g["date_text"]
            g["time_text"] = inv.get("time_text") or g["time_text"]
    out = list(groups.values())
    out.sort(key=lambda g: g["latest_ts"], reverse=True)
    return out


def candidate_join(inv: dict, *, group_meeting_id: str | None = None) -> dict:
    """Per-candidate join info: THIS persona's OWN original tracking link + the resolved
    Zoom room it maps to (both keyed on the candidate's own invite, not the group's room).

    A mass invite blasts the SAME Zoom room to many personas, but each persona's invite
    carries a UNIQUE icims tracking link and a candidate CAN resolve to a different room.
    Returns ``{tracking_url, join_url, meeting_id, resolved, differs}`` where:
      * ``tracking_url`` — the persona's unique original invite link (redirects to Zoom);
      * ``join_url``     — the resolved ``*.zoom.us`` room, else the tracking link (still
                            opens Zoom on click) — the URL the «Ссылка» control opens;
      * ``meeting_id``   — the resolved Zoom meeting id, else None;
      * ``resolved``     — whether this invite resolved to a real Zoom room;
      * ``differs``      — True iff this persona's resolved room differs from the group's
                            shared room (BOTH must be resolved meeting ids to compare — an
                            unresolved candidate is NEVER flagged as differing).
    Pure; unit-testable off the invite dict :func:`events` produces."""
    tracking = inv.get("tracking_url") or None
    join = inv.get("join_url") or tracking or None
    mid = inv.get("meeting_id") or None
    resolved = bool(inv.get("resolved") or mid)
    differs = bool(mid and group_meeting_id and mid != group_meeting_id)
    return {
        "tracking_url": tracking,
        "join_url": join,
        "meeting_id": mid,
        "resolved": resolved,
        "differs": differs,
    }


# ---- rendering (dedicated «События найма» surface) -------------------------------
def _fmt_date(ts: int) -> str:
    if not ts:
        return ""
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        return ""


def _resume_worthy(resume: dict | None) -> bool:
    """True when a résumé dict has enough to render a PDF on the fly (the download-button
    fallback when no ``resume.pdf`` sits on disk)."""
    resume = resume or {}
    return bool(resume.get("personal_info") or resume.get("experience")
                or resume.get("education"))


# ---- per-candidate schedule window + booking deadline ----------------------------
_EN_DOW = (("monday", "Пн"), ("tuesday", "Вт"), ("wednesday", "Ср"), ("thursday", "Чт"),
           ("friday", "Пт"), ("saturday", "Сб"), ("sunday", "Вс"))
_TIME12_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?\s*[Mm]\.?")
_RU_MON = ("янв", "фев", "мар", "апр", "мая", "июн",
           "июл", "авг", "сент", "окт", "нояб", "дек")


def _to24(m: "re.Match") -> str:
    h = int(m.group(1))
    mn = m.group(2) or "00"
    ap = m.group(3).lower()
    if ap == "p" and h != 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h}:{mn}"


def _window_label(date_text: str, time_text: str) -> str:
    """A compact RU «окно работы Zoom-комнаты» from the parsed schedule, e.g. «Monday–Friday» +
    «9:30 AM – 5:00 PM ET» → «Пн–Пт 9:30 – 17:00 ET». Best-effort: English weekday names are
    localised and 12h times converted to 24h; on any hiccup the raw parsed text is used."""
    d = (date_text or "").strip()
    t = (time_text or "").strip()
    if not d and not t:
        return ""
    try:
        dd = d
        for en, ru in _EN_DOW:
            dd = re.sub(en, ru, dd, flags=re.I)
        tt = _TIME12_RE.sub(_to24, t)
        return " ".join(x for x in (dd, tt) if x)
    except Exception:
        return " ".join(x for x in (d, t) if x)


def _fmt_dl_date(ts) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)
        return f"{dt.day} {_RU_MON[dt.month - 1]}"
    except Exception:
        return ""


def _deadline_chip_parts(inv: dict) -> tuple[str, str]:
    """(label, level) for a candidate's booking deadline chip. level ∈ {ok, soon, urgent, over}.
    ONLY an EXPLICIT past deadline reads «поздно подключаться» (over/dim) — an estimated one is
    never «поздно» (owner). Near deadlines read «сегодня»/«осталось N дн»; further ones «до 30 сент»."""
    ts = inv.get("deadline_ts")
    if not ts:
        return "", ""
    days = inv.get("deadline_days")
    estimated = inv.get("deadline_estimated")
    if days is not None and days < 0 and not estimated:
        return "поздно подключаться", "over"          # explicit past deadline only
    if days is None:
        lvl = "ok"
    elif days <= 1:
        lvl = "urgent"
    elif days <= 3:
        lvl = "soon"
    else:
        lvl = "ok"
    if days is not None and 0 <= days <= 3:
        label = "сегодня" if days == 0 else f"осталось {days} дн"
    else:
        d = _fmt_dl_date(ts)
        label = f"до {d}" if d else ""
    return label, lvl


def _meta_row_html(inv: dict) -> str:
    """A per-candidate meta line under the name: the Zoom «окно» (working hours of THIS
    candidate's room) + the booking deadline chip. Both best-effort; empty → nothing rendered."""
    bits = []
    win = _window_label(inv.get("date_text") or "", inv.get("time_text") or "")
    if win:
        bits.append(f'<span class="he-win">Окно: {escape(win)}</span>')
    dl, lvl = _deadline_chip_parts(inv)
    if dl:
        bits.append(f'<span class="he-dl he-dl-{lvl}">{escape(dl)}</span>')
    return f'<div class="he-inv-meta">{"".join(bits)}</div>' if bits else ""


def _claim_color(c: dict, color_for=None) -> str:
    """A claim's display colour: `db.color_for` when the callable is threaded in (honours an
    explicit override), else the colour event_claims_for already resolved, else a safe grey."""
    if color_for:
        try:
            return color_for({"id": c.get("responsible_id"), "color": c.get("color")})
        except Exception:
            pass
    return c.get("color") or "#5f6368"


def _claim_time_label(c: dict, me: dict | None) -> str:
    """A claim's join time as HH:MM in the VIEWER's timezone, or '' when no time is set."""
    ts = c.get("join_ts")
    if not ts:
        return ""
    try:
        from backend.interviews import slots
        return slots.to_local(ts, (me or {}).get("tz")).strftime("%H:%M")
    except Exception:
        return ""


def _claim_block(mbx: str, claims: list | None, me: dict | None, color_for=None) -> str:
    """The shared claim strip for one candidate row: a coloured chip per claimer
    («@Имя · подключится 14:00» / «@Имя · подключился ✓», colour = the claimer's), plus, for the
    ACTING user, an inline «Я подключусь» time + «Подключился» control (or «Отменить» if already
    claimed). Every role sees the same chips; only the acting user gets the control."""
    claims = claims or []
    mine = None
    chips = []
    for c in claims:
        if me and c.get("responsible_id") == me.get("id"):
            mine = c
        color = _claim_color(c, color_for)
        who = escape(c.get("name") or "—")
        if c.get("joined"):
            lbl = "подключился ✓"
        else:
            t = _claim_time_label(c, me)
            lbl = f"подключится {t}" if t else "готов подключиться"
        chips.append(
            f'<span class="he-claim-chip" style="background:{color}22;color:{color};'
            f'border:1px solid {color}55">@{who} · {escape(lbl)}</span>')
    chips_html = (f'<span class="he-claim-chips">{"".join(chips)}</span>' if chips
                  else '<span class="he-claim-none">Пока никто не отметился</span>')

    control = ""
    if me and me.get("id"):
        mb_attr = escape(mbx, quote=True)
        if mine is not None:
            cur_t = _claim_time_label(mine, me)
            chk = " checked" if mine.get("joined") else ""
            control = (
                '<span class="he-claim-mine">'
                '<form method="post" action="/hiring-events/claim" class="he-claim-form">'
                f'<input type="hidden" name="mailbox" value="{mb_attr}">'
                f'<input type="time" name="join_local" value="{escape(cur_t, quote=True)}" '
                'aria-label="Во сколько подключитесь">'
                f'<label class="he-claim-chk"><input type="checkbox" name="joined" value="1"{chk}> '
                'Подключился</label>'
                '<button type="submit" class="he-claim-save">Сохранить</button></form>'
                '<form method="post" action="/hiring-events/unclaim" class="he-claim-form">'
                f'<input type="hidden" name="mailbox" value="{mb_attr}">'
                '<button type="submit" class="he-claim-cancel">Отменить</button></form>'
                '</span>')
        else:
            control = (
                '<span class="he-claim-mine">'
                '<form method="post" action="/hiring-events/claim" class="he-claim-form">'
                f'<input type="hidden" name="mailbox" value="{mb_attr}">'
                '<input type="time" name="join_local" aria-label="Во сколько подключитесь">'
                '<label class="he-claim-chk"><input type="checkbox" name="joined" value="1"> '
                'Подключился</label>'
                '<button type="submit" class="he-claim-save">Я подключусь</button></form>'
                '</span>')
    return f'<div class="he-claim">{chips_html}{control}</div>'


def _invite_row_html(inv: dict, *, group_meeting_id: str | None = None,
                     claims: list | None = None, me: dict | None = None,
                     color_for=None) -> str:
    """One persona row: name + mailbox, a per-candidate «Ссылка» (THIS persona's OWN Zoom
    room, keyed on their unique invite — flagged «др. комната» when it differs from the
    group's shared room), «Письмо» link, a «Скачать резюме» button (when a résumé resolves)
    and an expand chevron that toggles the Штат / ФИО / Возраст panel (which also shows the
    resolved room + the persona's personal invite link)."""
    mbx = inv.get("mailbox") or ""
    who_fallback = inv.get("candidate") or mbx.split("@")[0]
    # resolve the persona ONCE (its prefill dir), then read persona.json + résumé presence.
    prefill = prefill_dir_for(mbx)
    persona = None
    has_resume = False
    if prefill:
        try:
            with open(os.path.join(prefill, "persona.json"), encoding="utf-8") as fh:
                persona = json.load(fh)
        except Exception:
            persona = None
        resume = ((persona or {}).get("profile") or {}).get("resume") \
            or (persona or {}).get("resume") or {}
        has_resume = (os.path.isfile(os.path.join(prefill, "resume.pdf"))
                      or _resume_worthy(resume))
    detail = candidate_detail(persona, fallback_name=who_fallback)

    who = escape(detail["full_name"] or who_fallback)
    mb = escape(mbx)
    when = _fmt_date(inv.get("date_ts") or 0)
    open_mail = escape(f"/mail/candidates?q={mbx}", quote=True)

    # a stable, unique panel id per message (the mail path_hash, sanitized).
    pid = re.sub(r"[^A-Za-z0-9_-]", "", inv.get("path_hash") or "")
    if not pid:
        pid = hashlib.md5(mbx.encode("utf-8")).hexdigest()[:10]
    did = f"he-d-{pid}"

    res_btn = ""
    if has_resume:
        href = "/hiring-events/resume?mbx=" + urllib.parse.quote(mbx, safe="")
        res_btn = (f'<a class="he-res" href="{escape(href, quote=True)}">'
                   '<span class="he-dl" aria-hidden="true">↓</span>Скачать резюме</a>')

    exp_btn = (f'<button type="button" class="he-exp-btn" aria-expanded="false" '
               f'aria-controls="{did}" aria-label="Показать данные кандидата">'
               '<span class="he-chev" aria-hidden="true">⌄</span></button>')

    # THIS persona's OWN join link (keyed on their unique invite), + whether it opens a
    # DIFFERENT Zoom room than the group's shared one.
    cj = candidate_join(inv, group_meeting_id=group_meeting_id)
    if cj["join_url"]:
        cls = "he-inv-link" + (" he-inv-link-diff" if cj["differs"] else "")
        room_title = (f'Zoom · {cj["meeting_id"]}' if cj["meeting_id"]
                      else "Откроется в Zoom")
        link_html = (f'<a class="{cls}" href="{escape(cj["join_url"], quote=True)}" '
                     f'target="_blank" rel="noopener noreferrer" '
                     f'title="{escape(room_title, quote=True)}">Ссылка</a>')
        if cj["differs"]:
            link_html += '<span class="he-inv-diff">др. комната</span>'
    else:
        link_html = '<span class="he-inv-link he-inv-link-off">Ссылки нет</span>'

    bits = []
    if detail["state"]:
        bits.append(f'Штат: {escape(detail["state"])}')
    bits.append(f'ФИО: {escape(detail["full_name"] or who_fallback)}')
    if detail["age"]:
        bits.append(f'Возраст: ~{detail["age"]} г.')
    detail_html = " · ".join(bits) if bits else "Данные кандидата недоступны"

    # per-candidate room + personal invite link, surfaced in the expand panel
    extra = []
    if cj["meeting_id"]:
        room_line = f'Комната: Zoom · {escape(cj["meeting_id"])}'
        if cj["differs"]:
            room_line += " · отдельная комната"
        extra.append(f'<div class="he-detail-room">{room_line}</div>')
    elif cj["join_url"]:
        extra.append('<div class="he-detail-room">Комната: определится при '
                     'открытии ссылки</div>')
    if cj["tracking_url"]:
        extra.append('<div class="he-detail-link">Персональная ссылка: '
                     f'<a href="{escape(cj["tracking_url"], quote=True)}" '
                     'target="_blank" rel="noopener noreferrer">открыть приглашение</a>'
                     '</div>')
    detail_body = f'<div class="he-detail-facts">{detail_html}</div>' + "".join(extra)

    claim_html = _claim_block(mbx, claims, me, color_for)

    return (
        '<div class="he-inv-wrap">'
        '<div class="he-inv">'
        f'<div class="he-inv-main"><span class="he-inv-name">{who}</span>'
        f'<span class="he-inv-mb">{mb}</span>{_meta_row_html(inv)}</div>'
        '<div class="he-inv-actions">'
        f'<span class="he-inv-when">{escape(when)}</span>'
        f'{link_html}'
        f'<a class="he-inv-open" href="{open_mail}">Письмо</a>'
        f'{res_btn}{exp_btn}</div>'
        '</div>'
        f'{claim_html}'
        f'<div class="he-detail" id="{did}" hidden>{detail_body}</div>'
        '</div>'
    )


def _group_card_html(g: dict, *, claims: dict | None = None, me: dict | None = None,
                     color_for=None) -> str:
    join = g.get("join_url") or ""
    role = escape(g.get("role") or "Remote CSR")
    date_text = escape(g.get("date_text") or "")
    time_text = escape(g.get("time_text") or "")
    mid = g.get("meeting_id")
    n = len(g.get("invites") or [])
    room_label = f"Zoom · {escape(str(mid))}" if mid else "Zoom"
    join_btn = (
        f'<a class="he-join" href="{escape(join, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">Присоединиться</a>' if join else
        '<span class="he-join he-join-off">Ссылка недоступна</span>'
    )
    sched_bits = " · ".join(x for x in (date_text, time_text) if x)
    invites = "".join(
        _invite_row_html(inv, group_meeting_id=mid,
                         claims=(claims or {}).get(inv.get("mailbox")),
                         me=me, color_for=color_for)
        for inv in g.get("invites") or [])
    return (
        '<section class="he-card">'
        '<div class="he-head">'
        f'<div class="he-head-left"><div class="he-role">{role}</div>'
        f'<div class="he-sched">{sched_bits}</div>'
        f'<div class="he-room">{room_label}</div></div>'
        f'<div class="he-head-right">{join_btn}'
        f'<span class="he-count">{n} канд.</span></div>'
        '</div>'
        f'<div class="he-invites">{invites}</div>'
        '</section>'
    )


_CSS = """
.he-wrap{max-width:860px;margin:0 auto;padding:4px 0 40px;}
.he-lead{color:var(--ink-soft);font-size:13.5px;line-height:1.6;margin:2px 0 16px;}
.he-empty{color:var(--ink-mute);font-size:14px;padding:28px 4px;text-align:center;}
.he-card{border:1px solid var(--line);border-radius:14px;background:var(--panel);
  margin:0 0 14px;overflow:hidden;}
.he-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;
  padding:14px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap;}
.he-role{font-weight:700;font-size:15px;color:var(--ink);}
.he-sched{color:var(--ink-soft);font-size:13px;margin-top:3px;}
.he-room{color:var(--ink-mute);font-size:11.5px;font-family:var(--ff-mono);margin-top:3px;}
.he-head-right{display:flex;flex-direction:column;align-items:flex-end;gap:6px;}
.he-join{display:inline-flex;align-items:center;height:var(--ctl-h);padding:0 var(--ctl-px);
  border-radius:var(--r-full);background:#188038;color:#fff;font-weight:700;
  font-size:var(--ctl-fs);text-decoration:none;white-space:nowrap;}
.he-join:hover{background:#137333;text-decoration:none;}
.he-join-off{background:var(--panel-2);color:var(--ink-mute);}
.he-count{color:var(--ink-mute);font-size:11.5px;}
.he-invites{padding:4px 6px 8px;}
.he-inv-wrap{border-radius:8px;}
.he-inv{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;flex-wrap:wrap;}
.he-inv:hover{background:var(--panel-2);}
.he-inv-main{display:flex;flex-direction:column;min-width:120px;flex:1 1 150px;}
.he-inv-name{font-weight:600;font-size:13.5px;color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
.he-inv-mb{font-size:11.5px;color:var(--ink-mute);font-family:var(--ff-mono);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
/* per-candidate meta: Zoom working-hours window + booking deadline chip */
.he-inv-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:3px;}
.he-win{font-size:11px;font-weight:600;color:var(--ink-soft);background:var(--panel-2);
  border-radius:var(--r-full);padding:2px 8px;white-space:nowrap;}
.he-dl{font-size:11px;font-weight:700;border-radius:var(--r-full);padding:2px 8px;white-space:nowrap;}
.he-dl-ok{color:var(--ink-soft);background:var(--panel-2);}
.he-dl-soon{color:var(--warn,#b45309);background:var(--warn-soft,#fef3c7);}
.he-dl-urgent{color:var(--danger,#a50e0e);background:#fce8e6;}
.he-dl-over{color:var(--ink-mute);background:var(--panel-2);opacity:.7;}
.he-inv-actions{display:flex;align-items:center;gap:8px;margin-left:auto;flex:0 0 auto;}
.he-inv-when{font-size:12px;color:var(--ink-mute);flex:0 0 auto;}
.he-inv-open{font-size:12.5px;color:var(--accent);flex:0 0 auto;white-space:nowrap;}
.he-inv-link{display:inline-flex;align-items:center;height:var(--chip-h);padding:0 12px;
  border-radius:var(--r-full);background:#188038;color:#fff;font-size:12.5px;font-weight:600;
  text-decoration:none;white-space:nowrap;flex:0 0 auto;}
.he-inv-link:hover{background:#137333;text-decoration:none;}
.he-inv-link-off{background:var(--panel-2);color:var(--ink-mute);}
.he-inv-link-diff{background:#b8860b;}
.he-inv-link-diff:hover{background:#9c7209;}
.he-inv-diff{font-size:11.5px;font-weight:700;color:#b8860b;flex:0 0 auto;white-space:nowrap;}
.he-detail-room{margin-top:5px;font-family:var(--ff-mono);font-size:11.5px;}
.he-detail-link{margin-top:5px;}
.he-detail a{color:var(--accent);text-decoration:none;}
.he-detail a:hover{text-decoration:underline;}
.he-res{display:inline-flex;align-items:center;gap:5px;height:var(--chip-h);padding:0 12px;
  border:1px solid var(--line);border-radius:var(--r-full);background:var(--panel);
  color:var(--ink);font-size:12.5px;font-weight:600;text-decoration:none;white-space:nowrap;}
.he-res:hover{border-color:var(--accent);color:var(--accent);text-decoration:none;}
.he-res .he-dl{font-size:14px;line-height:1;}
.he-exp-btn{display:inline-flex;align-items:center;justify-content:center;width:var(--chip-h);
  height:var(--chip-h);border:1px solid var(--line);border-radius:50%;background:var(--panel);
  color:var(--ink-soft);cursor:pointer;padding:0;flex:0 0 auto;
  -webkit-appearance:none;appearance:none;}
.he-exp-btn:hover{border-color:var(--accent);color:var(--accent);}
.he-chev{display:inline-block;font-size:15px;line-height:1;transition:transform .18s ease;}
.he-exp-btn[aria-expanded="true"] .he-chev{transform:rotate(180deg);}
.he-detail{margin:2px 10px 8px;padding:9px 12px;border-radius:8px;background:var(--panel-2);
  color:var(--ink-soft);font-size:12.5px;line-height:1.55;}
.he-detail[hidden]{display:none;}
/* shared claims: who (any role) will join / has joined this candidate's Zoom room + when */
.he-claim{display:flex;align-items:center;gap:8px 10px;flex-wrap:wrap;margin:0 10px 8px;
  padding:8px 10px;border-top:1px dashed var(--line);}
.he-claim-chips{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
.he-claim-chip{display:inline-flex;align-items:center;height:var(--chip-sm-h,22px);padding:0 9px;
  border-radius:var(--r-full);font-size:11.5px;font-weight:700;white-space:nowrap;}
.he-claim-none{color:var(--ink-mute);font-size:11.5px;}
.he-claim-form{display:inline-flex;align-items:center;gap:7px;margin:0;flex-wrap:wrap;}
.he-claim-form input[type=time]{padding:6px 8px;border:1px solid var(--line-strong);border-radius:7px;
  font-size:13px;background:var(--panel);color:var(--ink);}
.he-claim-chk{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;
  color:var(--ink-soft);white-space:nowrap;cursor:pointer;}
.he-claim-chk input{width:16px;height:16px;flex:0 0 auto;}
.he-claim-save,.he-claim-cancel{display:inline-flex;align-items:center;height:var(--chip-h);
  padding:0 12px;border-radius:var(--r-full);font-size:12.5px;font-weight:700;cursor:pointer;
  border:1px solid var(--line-strong);}
.he-claim-save{background:var(--accent);color:#fff;border-color:var(--accent);}
.he-claim-save:hover{filter:brightness(.96);}
.he-claim-cancel{background:var(--panel);color:var(--ink-soft);}
.he-claim-cancel:hover{border-color:var(--danger);color:var(--danger);}
.he-claim-mine{margin-left:auto;display:inline-flex;align-items:center;gap:7px;flex-wrap:wrap;}
"""


# The expand toggle: ONE delegated click listener (idempotent across the jfSwap in-place
# tab switch — a swap aborts the prior page's listeners via window.jfPage.signal, this one
# re-registers). Keyboard-accessible: the control is a real <button> with aria-expanded /
# aria-controls, so Enter/Space fire the same click. No external libs.
_TOGGLE_JS = """<script>
(function(){
  var sig=(window.jfPage||{}).signal;
  function onClick(e){
    var t=e.target;
    var btn=(t&&t.closest)?t.closest('.he-exp-btn'):null;
    if(!btn)return;
    e.preventDefault();
    var panel=document.getElementById(btn.getAttribute('aria-controls'));
    if(!panel)return;
    var willOpen=panel.hasAttribute('hidden');
    if(willOpen){panel.removeAttribute('hidden');}else{panel.setAttribute('hidden','');}
    btn.setAttribute('aria-expanded',willOpen?'true':'false');
  }
  document.addEventListener('click',onClick,sig?{signal:sig}:false);
})();
</script>"""


def render_page(groups: list[dict] | None = None, *, claims: dict | None = None,
                me: dict | None = None, color_for=None) -> str:
    """The «События найма» SHARED surface: live Zoom hiring events, each with its
    schedule + a «Присоединиться» button + the personas invited to it. Distinct from the
    «Собес» interview pool. Neutral RU; no stack names.

    `claims` ({mailbox: [claim,…]} from db.event_claims_for), `me` (the acting responsible) and
    `color_for` (db.color_for) drive the shared claim strips — who (any role) will join / has
    joined each candidate. All default to None so the CLI `--refresh`/`_main` path renders
    unchanged (no per-user control, no claims)."""
    from backend.tools import mailcrm_ui
    if groups is None:
        groups = grouped_events()
    n_rooms = len(groups)
    n_inv = sum(len(g.get("invites") or []) for g in groups)
    lead = (
        "Живые события найма Teleperformance: подключитесь к Zoom-комнате под нужным "
        "кандидатом и получите оффер на месте — без теста. Отметьте галочкой, во сколько "
        "подключитесь к кандидату — это видят все. Список формируется из входящих приглашений."
    )
    if groups:
        cards = "".join(_group_card_html(g, claims=claims, me=me, color_for=color_for)
                        for g in groups)
        body_inner = f'<p class="he-lead">{escape(lead)}</p>{cards}'
    else:
        body_inner = ('<p class="he-lead">' + escape(lead) + '</p>'
                      '<div class="he-empty">Пока нет приглашений на события найма.</div>')
    meta = f"комнат: {n_rooms} · приглашений: {n_inv}"
    head = mailcrm_ui._page_head("События найма", count=n_rooms, meta=meta)
    body = (f'<style>{_CSS}</style><div class="he-wrap">{head}{body_inner}</div>'
            f'{_TOGGLE_JS}')
    return mailcrm_ui._page("hiring", body)


# ---- CLI -------------------------------------------------------------------------
def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Teleperformance Virtual Hiring Event capture")
    ap.add_argument("--refresh", action="store_true",
                    help="resolve every invite's Zoom link + refresh the cache")
    ap.add_argument("--list", action="store_true", help="print captured events")
    args = ap.parse_args(argv)
    if args.refresh:
        rows = [inv for inv in (_parse_invite(r) for r in _event_rows()) if inv]
        urls = [inv["tracking_url"] for inv in rows if inv["tracking_url"]]
        mapping = resolve_many(urls)
        ok = sum(1 for v in mapping.values() if v)
        print(f"resolved {ok}/{len(mapping)} tracking links (cache: {_CACHE_PATH})")
    if args.list or not args.refresh:
        for g in grouped_events():
            gmid = g.get("meeting_id")
            print(f"[{gmid or '?'}] {g['role']} | "
                  f"{g['date_text']} {g['time_text']} | {len(g['invites'])} invited")
            print(f"    join: {g['join_url']}")
            for inv in g["invites"]:
                cj = candidate_join(inv, group_meeting_id=gmid)
                room = cj["meeting_id"] or "unresolved"
                flag = " [DIFFERENT ROOM]" if cj["differs"] else ""
                print(f"      - {inv['mailbox']}  ({_fmt_date(inv['date_ts'])})  "
                      f"room={room}{flag}")
                print(f"          own link: {cj['join_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
