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

import json
import os
import re
import threading
import time
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


# ---- rendering (dedicated «События найма» surface) -------------------------------
def _fmt_date(ts: int) -> str:
    if not ts:
        return ""
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        return ""


def _invite_row_html(inv: dict) -> str:
    mb = escape(inv.get("mailbox") or "")
    who = escape(inv.get("candidate") or (inv.get("mailbox") or "").split("@")[0])
    when = _fmt_date(inv.get("date_ts") or 0)
    open_mail = escape(f"/mail/candidates?q={inv.get('mailbox') or ''}", quote=True)
    return (
        '<div class="he-inv">'
        f'<div class="he-inv-main"><span class="he-inv-name">{who}</span>'
        f'<span class="he-inv-mb">{mb}</span></div>'
        f'<span class="he-inv-when">{escape(when)}</span>'
        f'<a class="he-inv-open" href="{open_mail}">Письмо</a>'
        '</div>'
    )


def _group_card_html(g: dict) -> str:
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
    invites = "".join(_invite_row_html(inv) for inv in g.get("invites") or [])
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
.he-inv{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;}
.he-inv:hover{background:var(--panel-2);}
.he-inv-main{display:flex;flex-direction:column;min-width:0;flex:1 1 auto;}
.he-inv-name{font-weight:600;font-size:13.5px;color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
.he-inv-mb{font-size:11.5px;color:var(--ink-mute);font-family:var(--ff-mono);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
.he-inv-when{font-size:12px;color:var(--ink-mute);flex:0 0 auto;}
.he-inv-open{font-size:12.5px;color:var(--accent);flex:0 0 auto;white-space:nowrap;}
"""


def render_page(groups: list[dict] | None = None) -> str:
    """The «События найма» operator surface: live Zoom hiring events, each with its
    schedule + a «Присоединиться» button + the personas invited to it. Distinct from the
    «Собес» interview pool. Neutral RU; no stack names."""
    from backend.tools import mailcrm_ui
    if groups is None:
        groups = grouped_events()
    n_rooms = len(groups)
    n_inv = sum(len(g.get("invites") or []) for g in groups)
    lead = (
        "Живые события найма Teleperformance: подключитесь к Zoom-комнате под нужным "
        "кандидатом и получите оффер на месте — без теста. Собеседование обычно идёт "
        "будни, время указано по ET. Список формируется из входящих приглашений."
    )
    if groups:
        cards = "".join(_group_card_html(g) for g in groups)
        body_inner = f'<p class="he-lead">{escape(lead)}</p>{cards}'
    else:
        body_inner = ('<p class="he-lead">' + escape(lead) + '</p>'
                      '<div class="he-empty">Пока нет приглашений на события найма.</div>')
    meta = f"комнат: {n_rooms} · приглашений: {n_inv}"
    head = mailcrm_ui._page_head("События найма", count=n_rooms, meta=meta)
    body = f'<style>{_CSS}</style><div class="he-wrap">{head}{body_inner}</div>'
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
            print(f"[{g.get('meeting_id') or '?'}] {g['role']} | "
                  f"{g['date_text']} {g['time_text']} | {len(g['invites'])} invited")
            print(f"    join: {g['join_url']}")
            for inv in g["invites"]:
                print(f"      - {inv['mailbox']}  ({_fmt_date(inv['date_ts'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
