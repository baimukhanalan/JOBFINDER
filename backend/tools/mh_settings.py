"""Persisted operator settings for the Mass Hiring board + on-demand apply engine.

Stored in backend/data/mh_settings.json (gitignored). Reversible toggles only — e.g.
`hide_spanish` hides the Spanish-required jobs from BOTH the board display and the apply
engine until Spanish-speaking staff are onboarded (flip it back, nothing is hardcoded)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "mh_settings.json"
_LOCK = threading.RLock()
_DEFAULTS = {"hide_spanish": True}


def get() -> dict:
    with _LOCK:
        d = dict(_DEFAULTS)
        try:
            d.update(json.loads(_PATH.read_text()))
        except Exception:
            pass
        return d


def set(**kw) -> dict:  # noqa: A003 - deliberate tiny settings API
    with _LOCK:
        d = get()
        d.update(kw)
        _PATH.parent.mkdir(exist_ok=True)
        tmp = _PATH.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, _PATH)
        return d


def hide_spanish() -> bool:
    return bool(get().get("hide_spanish", True))


def drop_spanish(ids) -> list[int]:
    """Remove Spanish-required job ids from a lane's apply set when hide_spanish is on.
    One query, reversible. Called by every mass-hiring lane's job-id selector so the engine
    honours the same toggle the board display uses."""
    ids = [int(i) for i in (ids or [])]
    if not ids or not hide_spanish():
        return ids
    from backend.tools import mail_db
    try:
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id FROM mass_hiring_jobs WHERE id = ANY(%s) AND title ILIKE %s",
                        (ids, "%spanish%"))
            sp = {r[0] for r in cur.fetchall()}
    except Exception:
        return ids
    return [i for i in ids if i not in sp]
