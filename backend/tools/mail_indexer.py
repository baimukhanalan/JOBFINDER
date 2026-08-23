#!/usr/bin/python3
"""inotify-driven index of the candidate Maildirs -> Postgres `mail_index`.

Keeps the `mail_index` table (backend/tools/mail_db.py) in sync with the on-disk
Maildirs of every provisioned candidate (backend/tools/mailcrm.candidates()), so
the CRM queries a fast index instead of scanning + parsing .eml files on disk.

Only our own domain is watched: /var/mail/vhosts/takhet.com/<local>/{new,cur}.
A full reconcile runs at startup and every 300s (safety sweep); between sweeps an
inotify watcher (ctypes, zero idle CPU) indexes/prunes single files as mail lands
or is moved/deleted. The Maildir new/ -> cur/ rename fires MOVED_FROM(old) +
MOVED_TO(new), which prune-old + index-new handle naturally.

Row parsing is delegated entirely to mailcrm.build_index_row (which returns None
for any file outside a candidate mailbox) — this module never re-parses mail.

Run with /usr/bin/python3 from /home/projects/JOBFINDER (absolute backend.* imports):
    python3 -m backend.tools.mail_indexer
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import time

from backend.tools import mail_db, mail_health, mailcrm

_pid = mailcrm._pid
MAILDIR_ROOT = mailcrm.MAILDIR_ROOT
DOMAIN = "takhet.com"
DOMAIN_ROOT = os.path.join(MAILDIR_ROOT, DOMAIN)
SWEEP_SECONDS = 300


# ---- file enumeration (candidate mailboxes only) ---------------------------
def _iter_candidate_files():
    """Yield (abs_path, seen) for every message file in each candidate's new/ + cur/.
    seen = 0 for new/, 1 for cur/."""
    for c in mailcrm.candidates():
        maildir = c.get("maildir")
        if not maildir:
            continue
        for sub, seen in (("new", 0), ("cur", 1)):
            d = os.path.join(maildir, sub)
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for fn in names:
                if fn.startswith("."):
                    continue
                yield os.path.join(d, fn), seen


# ---- full reconcile --------------------------------------------------------
def run_once():
    """Reconcile the whole index against disk. Insert files not yet indexed, prune
    rows whose files disappeared. When the classifier version changes, refresh all
    rows once so old false positives are corrected too. Returns (updated, pruned)."""
    known = mail_db.all_path_hashes()
    try:
        refresh_kinds = mail_db.get_meta("classifier_version") != mailcrm.classifier_version()
    except Exception:
        refresh_kinds = True
    on_disk: set[str] = set()
    updated = 0
    refresh_failed = False
    for path, seen in _iter_candidate_files():
        h = _pid(path)
        on_disk.add(h)
        if h in known and not refresh_kinds:
            continue
        try:
            row = mailcrm.build_index_row(path, seen)
        except Exception as e:
            print(f"index parse error {path}: {e}", flush=True)
            refresh_failed = refresh_failed or refresh_kinds
            continue
        if not row:
            refresh_failed = refresh_failed or (refresh_kinds and h in known)
            continue
        try:
            mail_db.upsert_message(**row)
            updated += 1
        except Exception as e:
            print(f"upsert error {path}: {e}", flush=True)
            refresh_failed = refresh_failed or refresh_kinds
    pruned = mail_db.delete_paths(known - on_disk)
    if refresh_kinds and not refresh_failed:
        mail_db.set_meta("classifier_version", mailcrm.classifier_version())
    mail_health.heartbeat()   # a full reconcile completed -> the backstop is alive
    return updated, pruned


# ---- single-file index / prune (used by the watcher) -----------------------
def index_file(path):
    """Index one Maildir file. seen from whether the path is under new/ (0) or cur/ (1);
    build_index_row returns None for anything outside a candidate mailbox (skipped)."""
    seen = 0 if (os.sep + "new" + os.sep) in path else 1
    try:
        row = mailcrm.build_index_row(path, seen)
    except Exception as e:
        print(f"index_file error {path}: {e}", flush=True)
        return
    if not row:
        return
    try:
        mail_db.upsert_message(**row)
    except Exception as e:
        print(f"upsert error {path}: {e}", flush=True)


def prune_file(path):
    """Drop one file's row from the index (harmless if it was never indexed)."""
    try:
        mail_db.delete_paths([_pid(path)])
    except Exception as e:
        print(f"prune error {path}: {e}", flush=True)


# ---- safety sweep ----------------------------------------------------------
def _safety_sweep():
    while True:
        time.sleep(SWEEP_SECONDS)
        try:
            run_once()
        except Exception as e:
            print("safety sweep error:", e, flush=True)


# ---- inotify watcher (ctypes, zero idle load) ------------------------------
IN_CREATE = 0x100
IN_MOVED_TO = 0x80
IN_DELETE = 0x200
IN_MOVED_FROM = 0x40
IN_ISDIR = 0x40000000
_MASK = IN_CREATE | IN_MOVED_TO | IN_DELETE | IN_MOVED_FROM


def watch():
    """Block on inotify events under our domain root, indexing/pruning single files
    as they land, and extending the watch tree when new mailbox dirs appear."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.inotify_init()
    if fd < 0:
        raise OSError("inotify_init failed")
    wd_path: dict[int, str] = {}

    def add(path):
        wd = libc.inotify_add_watch(fd, path.encode(), _MASK)
        if wd >= 0:
            wd_path[wd] = path

    def add_tree(root):
        if not os.path.isdir(root):
            return
        for dirpath, _dirs, _files in os.walk(root):
            add(dirpath)

    add_tree(DOMAIN_ROOT)
    print(f"watching {len(wd_path)} dirs under {DOMAIN_ROOT}", flush=True)

    while True:
        buf = os.read(fd, 8192)                 # blocks at 0% CPU until an event
        i = 0
        while i < len(buf):
            wd, mask, _cookie, nlen = struct.unpack_from("iIII", buf, i)
            i += 16
            name = buf[i:i + nlen].split(b"\0", 1)[0].decode("utf-8", "replace")
            i += nlen
            base = wd_path.get(wd)
            if not base:
                continue
            full = os.path.join(base, name)
            is_dir = bool(mask & IN_ISDIR)
            if is_dir and (mask & (IN_CREATE | IN_MOVED_TO)):
                add_tree(full)                  # new mailbox / new|cur dir -> watch it
                continue
            if is_dir:
                continue
            # file event: only act inside a new/ or cur/ leaf dir
            if os.path.basename(base) not in ("new", "cur"):
                continue
            if mask & (IN_CREATE | IN_MOVED_TO):
                index_file(full)                # new mail file -> index instantly
            elif mask & (IN_DELETE | IN_MOVED_FROM):
                prune_file(full)                # mail removed/moved out -> drop it


# ---- entrypoint ------------------------------------------------------------
def main():
    mail_db.ensure_schema()
    try:
        run_once()                              # initial reconcile
    except Exception as e:
        print("initial sweep error:", e, flush=True)
    threading.Thread(target=_safety_sweep, daemon=True).start()
    while True:
        try:
            watch()
        except Exception as e:
            print("watch error, retrying in 5s:", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
