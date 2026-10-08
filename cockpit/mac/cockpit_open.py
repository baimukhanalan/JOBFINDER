#!/usr/bin/env python3
"""Interview Cockpit — Mac-side opener (Phase 2). Installed to ~/Library/NativelyCockpit/ and run by
the LaunchAgent com.jobfinder.cockpit every 5 min + at login/wake.

Reads ~/NativelyInbox/next.json (staged by the server `cockpit_prep.py`). When we are within 20 minutes
of the собес (and up to 90 min after), it OPENS the Cockpit schedule (JobFinder /cabinet) in an app
window, ensures Natively is running, reveals/attaches the staged résumé, and posts a notification — once
per (mailbox,start) via a handled-marker, so the 5-min poll + a wake never re-open. `--now` forces it
for a manual open.

SAFETY: only opens apps/windows + posts a notification. It NEVER touches OBS / the virtual camera / the
CDP tunnel used by the Sutherland assessment lane, and never kills anything. Fully reversible (uninstall
removes this file + the LaunchAgent). Never raises — all failures are logged and swallowed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

INBOX = os.path.expanduser("~/NativelyInbox")
MANIFEST = os.path.join(INBOX, "next.json")
HANDLED = os.path.join(INBOX, ".handled")
LOG = os.path.join(INBOX, "cockpit.log")
LEAD_MIN = 20          # open this many minutes before the собес
GRACE_MIN = 90         # still open up to this many minutes after the start


def log(msg: str) -> None:
    try:
        os.makedirs(INBOX, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, timeout=30, capture_output=True)
    except Exception as exc:  # noqa: BLE001
        log(f"cmd failed {cmd[:2]}: {exc}")


def notify(title: str, text: str) -> None:
    safe = text.replace('"', "'")[:230]
    _run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'])


def open_cockpit(url: str) -> None:
    # a dedicated Chrome app-window feels like a native app; fall back to the default browser.
    chrome = "/Applications/Google Chrome.app"
    if os.path.isdir(chrome):
        _run(["open", "-na", "Google Chrome", "--args", f"--app={url}"])
    else:
        _run(["open", url])


def ensure_natively(resume_mac_path: str | None) -> None:
    _run(["open", "-a", "Natively"])       # launches or focuses; no-op if already open
    if resume_mac_path:
        p = os.path.expanduser(resume_mac_path)
        if os.path.exists(p):
            _run(["open", "-R", p])        # reveal the résumé in Finder for a one-click attach
            # best-effort: bring Natively forward so the résumé can be dropped into settings
            _run(["osascript", "-e", 'tell application "Natively" to activate'])


def already_handled(key: str) -> bool:
    try:
        return os.path.exists(HANDLED) and open(HANDLED).read().strip() == key
    except Exception:
        return False


def mark_handled(key: str) -> None:
    try:
        with open(HANDLED, "w") as f:
            f.write(key)
    except Exception:
        pass


def main() -> int:
    force = "--now" in sys.argv
    if not os.path.exists(MANIFEST):
        return 0
    try:
        m = json.load(open(MANIFEST))
    except Exception as exc:
        log(f"manifest unreadable: {exc}"); return 0

    start_iso = m.get("start_ts")
    key = f"{m.get('mailbox')}|{start_iso}"
    now = datetime.now(timezone.utc)

    if not force:
        if not start_iso:
            return 0                                   # no time set yet — auto_reserve will set it
        try:
            start = datetime.fromisoformat(start_iso)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except Exception:
            return 0
        if now < start - timedelta(minutes=LEAD_MIN):
            return 0                                   # too early
        if now > start + timedelta(minutes=GRACE_MIN):
            return 0                                   # long past
        if already_handled(key):
            return 0                                   # already opened for this собес

    log(f"OPEN cockpit for {m.get('candidate_name')} @ {m.get('company')} ({key})")
    open_cockpit(m.get("cockpit_url") or "https://jobs.systeam.kz/cabinet")
    ensure_natively(m.get("resume_mac_path"))
    who = f"{m.get('candidate_name','?')} · {m.get('company','?')}"
    res = "резюме готово — прикрепи в Natively" if m.get("has_resume") else "резюме недоступно"
    notify("Собес через 20 мин", f"{who}. {res}.")
    mark_handled(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
