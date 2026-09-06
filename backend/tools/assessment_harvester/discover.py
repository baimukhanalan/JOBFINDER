"""Discover assessment invites sitting in the synthetic-persona Maildirs, per platform.

Generalizes `shl_assess_runner.discover_invites`. Each platform declares a MATCHER (an SQL WHERE
fragment over `mail_index` + a link regex). We read the raw Maildir file at the `path` column,
un-quoted-printable it, and extract the assessment link. Invites already recorded in
`harvest_state.json` are excluded (a burned token is never re-attempted).

Discovery gotcha: the AMCAT invite is SENT FROM `talentcentral@shl.com` (subject "TP Assessment -
Test Login Details") but the LINK is AMCAT — so match on subject + link regex, NOT sender.
"""
from __future__ import annotations

import json
import os
import re

from backend.tools import mail_db

_DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data")
STATE_PATH = os.path.join(_DATA, "harvest_state.json")

# platform -> {where: SQL fragment (params-free), link_re: compiled regex over the un-QP'd body}
MATCHERS: dict[str, dict] = {
    "amcat": {
        "where": ("from_email ILIKE '%%shl.com%%' AND subject ILIKE '%%TP Assessment%%' "
                  "AND coalesce(outbound,false)=false"),
        "link_re": re.compile(r"https?://amcatglobal\.aspiringminds\.com/\?autoLoginVersion=3&token=[^\s\"'<>\\)]+"),
    },
    # SHL/Sutherland + TTEC/Taleo added when their adapters land.
    "shl_sutherland": {
        "where": ("from_email ILIKE '%%shl.com%%' AND subject ILIKE '%%sutherland assessment%%' "
                  "AND coalesce(outbound,false)=false"),
        "link_re": re.compile(r"https?://talentcentral\.us1\.shl\.com/experience/#/link/[^\s\"'<>\\)]+"),
    },
    "taleo_ttec": {
        "where": ("from_email ILIKE '%%ttec.com%%' AND subject ILIKE '%%Required Assessments%%' "
                  "AND coalesce(outbound,false)=false"),
        "link_re": re.compile(r"https?://teletech\.taleo\.net/[^\s\"'<>\\)]*sealedRequestId=[^\s\"'<>\\)]+"),
    },
}


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        os.makedirs(_DATA, exist_ok=True)
        tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=0)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def mark(url: str, status: str) -> None:
    st = load_state()
    st[url] = status
    save_state(st)


def link_from_path(path: str, link_re: re.Pattern):
    """Extract the assessment link from a raw Maildir file (un-quoted-printable first)."""
    try:
        txt = open(path, "rb").read().decode("utf-8", "ignore").replace("=\r\n", "").replace("=\n", "")
    except Exception:
        return None
    hits = sorted(set(link_re.findall(txt)))
    if not hits:
        return None
    # 'token=3D...' is quoted-printable for 'token=...'; strip a leading '3D' after '='.
    return hits[0].replace("=3D", "=")


def discover(platform: str, *, limit: int | None = None, include_done: bool = False) -> list[tuple[str, str]]:
    """Return [(mailbox_localpart, invite_url)] newest-first for one platform, excluding invites
    already in a terminal state (unless include_done)."""
    m = MATCHERS.get(platform)
    if not m:
        raise ValueError(f"unknown platform {platform!r}")
    sql = (f"SELECT DISTINCT ON (mailbox) mailbox, path, date_ts FROM mail_index "
           f"WHERE {m['where']} ORDER BY mailbox, date_ts DESC")
    rows = []
    with mail_db.conn() as c, c.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    rows.sort(key=lambda r: r[2] or 0, reverse=True)  # newest invite first across mailboxes
    state = load_state()
    out: list[tuple[str, str]] = []
    for mailbox, path, _ts in rows:
        url = link_from_path(path, m["link_re"])
        if not url:
            continue
        # Any recorded state means this token was already ATTEMPTED. Single-use assessment tokens go
        # terminal ("already completed/submitted") once opened, so a burned one is dead regardless of
        # HOW the attempt ended — never re-serve it (the runner records a compound status like
        # "stuck_free_response:banked1", so an exact-match check missed these and re-served them).
        if not include_done and url in state:
            continue
        out.append((mailbox.split("@")[0], url))
        if limit and len(out) >= limit:
            break
    return out
