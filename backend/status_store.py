"""Shared status.json read/write for profile job applications.

Single source of truth used by:
  - backend/dashboard_app.py  (_apply_mark; /mark and /mark_ext from the extension)
  - backend/inbox_index.py   (auto-advance from email)

Shape: { jid: {"status": str, "ts": ISO-8601 UTC} }
Valid statuses: pending | submitted | interview | rejected
"ts" is stamped on every write (foundation for follow-up automation); entries
written before it existed lack the key — readers must treat it as optional.
"""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _now_iso() -> str:
    """Current UTC time as ISO-8601 (seam for tests)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status_path(profile: str) -> Path:
    # Resolve OUT_ROOT at call time, not import time: tests monkeypatch
    # runner.OUT_ROOT, and an import-time `from ... import OUT_ROOT` snapshot
    # would silently keep writing to the original path.
    from backend.applier import runner
    return runner.OUT_ROOT / profile / "status.json"


def load(profile: str) -> dict:
    """Return current status dict for profile. Empty dict if missing/corrupt."""
    p = _status_path(profile)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark(profile: str, jid: str, to: str) -> None:
    """Atomically set status[jid] = {"status": to, "ts": now-UTC}.

    Empty `to` removes the entry (undo).
    The atomic lock+tmp+replace pattern shared by the dashboard mark routes and
    the inbox auto-advance — one implementation instead of several.
    """
    with _LOCK:
        data = load(profile)
        if not isinstance(data, dict):
            data = {}
        if to:
            data[jid] = {"status": to, "ts": _now_iso()}
        else:
            data.pop(jid, None)
        p = _status_path(profile)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
