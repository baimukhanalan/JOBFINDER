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
_SHL_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shl_assess_runner.py")
# repo-root logs/ (tools -> backend -> jobfinder): three dirnames. A missing third dirname sent the
# hook's SHL runner output to backend/logs/shl_assess.log while the retired pm2 daemon (and all
# monitoring / the Health tab) watched repo-root logs/shl_assess.log — which then looked "frozen"
# since 2026-08-31 even though the hook kept completing invites (2026-09-12 diagnosis).
_SHL_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "shl_assess.log")


def _maybe_trigger_shl(row, seen):
    """A FRESH Maximus SHL assessment invite just landed → spawn the completion drainer (detached),
    so the OPQ is done within seconds of the email rather than on a schedule. The runner's file lock
    collapses concurrent triggers to one instance that drains ALL pending invites. `seen==0` gates
    it to new/ arrivals (a cur/ re-index doesn't re-fire). Fully isolated — the caller try/excepts
    it so it can NEVER affect indexing."""
    if seen != 0:
        return
    fe = (row.get("from_email") or "").lower()
    subj = (row.get("subject") or "").lower()
    if "maximus" not in fe or "complete your assessment" not in subj:
        return
    import subprocess
    # WATCHDOG: kill any SHL runner stuck > 45 min before spawning, so a hung run (display
    # contention has hung one for ~56 min) can't hold the drain lock and block the queue. The
    # in-runner per-assessment (900s) + drain (40-min) caps make this rare; this is belt-and-braces.
    try:
        out = subprocess.run(["ps", "-eo", "pid,etimes,cmd"], capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            if "shl_assess_runner" not in line or "ps -eo" in line:
                continue
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 2700:
                subprocess.run(["kill", "-9", parts[0]], timeout=5)
    except Exception:
        pass
    # SHL_SOLVE_ABILITY: owner opt-in ("try the cognitive ones, see how it goes") — the scored etalon
    # attempts cognitive/knowledge items (banked strong answer, else model) instead of hard-stopping.
    # Synthetic-persona etalon only; a diagram/table item the model can't parse still stops for a human.
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":98",
               SHL_SOLVE_ABILITY=os.environ.get("SHL_SOLVE_ABILITY") or "1")
    try:
        log = open(_SHL_LOG, "a")
    except Exception:
        log = subprocess.DEVNULL
    subprocess.Popen(["/usr/bin/python3", _SHL_RUNNER, "--drain", "--concurrency", "3"],
                     env=env, stdout=log, stderr=log, start_new_session=True)
    if hasattr(log, "close"):
        log.close()  # the child keeps its own dup; don't leak the parent fd


_HARVEST_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvest_runner.py")
_HARVEST_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "harvest_amcat_event.log")


_HALLO_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "harvest_hallo_event.log")

_LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
_SUTHERLAND_PROBE_FLAG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", ".sutherland_probe_armed")
_SUTHERLAND_LOG = os.path.join(_LOGS_DIR, "harvest_sutherland_probe.log")


def _maybe_trigger_sutherland(row, seen):
    """ONE-SHOT camera investigation (armed via the `.sutherland_probe_armed` flag file): on a FRESH,
    ORIGINAL "Your Sutherland assessment invitation", immediately drive THAT exact token — extracted
    from the just-arrived mail (`row['path']`), so it is inside its short (~1-2h) validity window; a
    paced/discovery run only ever reaches already-EXPIRED Sutherland tokens (link-expired) — with the
    camera-capability SPOOF (`CAM_SPOOF`, incl. groupId pairing) + full `CAM_TRACE`, to capture what the
    WCI200 proctor reads before "unable to detect a camera". Disarms after ONE fire (deletes the flag),
    so it is not a permanent lane — armed deliberately for this investigation. Fully try/excepted by the
    caller so it can NEVER affect indexing."""
    if seen != 0 or not os.path.exists(_SUTHERLAND_PROBE_FLAG):
        return
    fe = (row.get("from_email") or "").lower()
    subj = (row.get("subject") or "").lower()
    if "talentcentral@shl.com" not in fe or not subj.startswith("your sutherland assessment"):
        return
    path = row.get("path")
    if not path:
        return
    try:
        from backend.tools.assessment_harvester import discover
        m = discover.MATCHERS["shl_sutherland"]
        url = discover.link_from_path(path, m["link_re"], unquote=m.get("unquote", False))
    except Exception:
        url = None
    if not url:
        return
    try:                                    # disarm FIRST (one-shot) so an invite burst fires once
        os.remove(_SUTHERLAND_PROBE_FLAG)
    except Exception:
        pass
    import subprocess
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":98", HARVEST_PROXY="phone",
               CAM_SPOOF="1", CAM_TRACE="1", CAM_TRACE_DIR=_LOGS_DIR, HARVEST_SESSION_SECS="600")
    try:
        log = open(_SUTHERLAND_LOG, "a")
    except Exception:
        log = subprocess.DEVNULL
    subprocess.Popen(["/usr/bin/python3", _HARVEST_RUNNER, "--platform", "shl_sutherland",
                      "--url", url, "--mailbox", row.get("mailbox") or "sutherland_probe"],
                     env=env, stdout=log, stderr=log, start_new_session=True)
    if hasattr(log, "close"):
        log.close()


def _kill_stuck_harvest(match_token: str, max_secs: int) -> None:
    """Belt-and-braces: SIGKILL a harvest_runner stuck longer than max_secs so a hung run can't hold
    the fcntl lock and starve later invites. SCOPED by `match_token` (the --platform arg in the
    cmdline) so an amcat watchdog never kills a legitimately-long hallo battery, and vice versa."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-eo", "pid,etimes,cmd"], capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            if "harvest_runner" not in line or match_token not in line or "ps -eo" in line:
                continue
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > max_secs:
                subprocess.run(["kill", "-9", parts[0]], timeout=5)
    except Exception:
        pass


def _mem_available_kb() -> int:
    """MemAvailable from /proc/meminfo (kB); a large sentinel on any read error so a parse failure
    never blocks a trigger."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 1 << 30


def _maybe_trigger_amcat(row, seen):
    """A FRESH AMCAT (Teleperformance) assessment invite just landed → drive it IMMEDIATELY through
    the harvester on a PHONE-egress slot. Two reasons this must be event-driven, not the */20 cron:
    (1) the login token is a SINGLE-USE ES256-JWT that EXPIRES — the cron reaches a deep backlog only
    after tokens are dead; (2) the AMCAT proctor's NE500 logout is a per-IP rate-limit on our
    DATACENTER IP, so a mobile-carrier slot (HARVEST_PROXY=phone) is what gets a session through the
    entry gate / Section C-D. harvest_runner's file lock collapses concurrent triggers. `seen==0`
    gates it to new/ arrivals. Fully isolated — the caller try/excepts so it can NEVER affect indexing.
    Only the TP "Test Login Details" subject fires it (Sutherland's shl.com invite is camera-walled)."""
    if seen != 0:
        return
    fe = (row.get("from_email") or "").lower()
    subj = (row.get("subject") or "").lower()
    if "shl.com" not in fe or "test login" not in subj:
        return
    import subprocess
    _kill_stuck_harvest("amcat", 2700)   # 45 min watchdog, scoped to amcat runs
    # session_secs under the 45-min watchdog (default 20 min truncates the battery).
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":98", HARVEST_PROXY="phone",
               HARVEST_SESSION_SECS=os.environ.get("HARVEST_SESSION_SECS") or "2400")
    try:
        log = open(_HARVEST_LOG, "a")
    except Exception:
        log = subprocess.DEVNULL
    subprocess.Popen(["/usr/bin/python3", _HARVEST_RUNNER, "--platform", "amcat", "--limit", "1"],
                     env=env, stdout=log, stderr=log, start_new_session=True)
    if hasattr(log, "close"):
        log.close()


def _maybe_trigger_hallo(row, seen):
    """A FRESH Hallo.ai assessment invite (TP's CURRENT post-apply assessment, from support@hallo.ai,
    subject "Complete your TP hiring assessment") just landed → drive it through the harvester at once.
    Event-driven for the same reason as _maybe_trigger_amcat: the app.hallo.ai/.../ai-assessment/<token>
    link is SINGLE-USE (a burned token is dead) so a token is best walked the moment it arrives. Nothing
    else triggers on hallo.ai mail — before this hook, Hallo invites (TP migrated off AMCAT) sat
    unharvested. harvest_runner's fcntl lock serializes it against any other harvest (there is ONE
    virtual mic + ONE /dev/video0 — NEVER fan out). Fully try/excepted by the caller so it can NEVER
    affect indexing.

    OOM GUARD: a Hallo run is a headful Chromium; the TP apply lane (icims_recon) fires headful browsers
    in clustered rounds — exactly when Hallo invites arrive — and stacking them OOM'd the box before. So
    SKIP when MemAvailable is tight; the invite stays in mail_index (UNBURNED, so nothing is lost) and a
    later invite from the same TP round (they cluster) or a manual `harvest_runner --platform hallo`
    sweep picks it up. Direct egress (no HARVEST_PROXY): Hallo has no NE500-style per-IP rate limit and
    the proven runs ran direct."""
    if seen != 0:
        return
    fe = (row.get("from_email") or "").lower()
    # Fire on ANY hallo.ai inbound (seen==0), NOT only subjects containing "assessment": TP/Hallo send
    # the invite under several subjects ("Complete your TP hiring assessment", "Your career is just a
    # few steps away…") and the subject gate dropped ~4 of 6 variants. discover.py's ai-assessment
    # link_re is the real authority — harvest_runner exits without launching a browser when there is no
    # fresh token, so a non-invite hallo.ai mail is a cheap no-op.
    if "hallo.ai" not in fe:
        return
    if _mem_available_kb() < 6 * 1024 * 1024:   # < 6 GiB available → too tight to add a headful browser
        return
    import subprocess
    _kill_stuck_harvest("hallo", 5400)   # 90 min: a full 6-module battery runs long; kill only a hang
    # session_secs: the 20-min default killed a deep battery mid-walk (0 completions, 17 partial_timeout)
    # — give the full 6-module battery time, kept just under the 90-min watchdog.
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":98",
               HARVEST_SESSION_SECS=os.environ.get("HARVEST_SESSION_SECS") or "5100")
    try:
        log = open(_HALLO_LOG, "a")
    except Exception:
        log = subprocess.DEVNULL
    subprocess.Popen(["/usr/bin/python3", _HARVEST_RUNNER, "--platform", "hallo", "--limit", "1"],
                     env=env, stdout=log, stderr=log, start_new_session=True)
    if hasattr(log, "close"):
        log.close()


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
    try:
        _maybe_trigger_shl(row, seen)
    except Exception as e:
        print(f"shl trigger error {path}: {e}", flush=True)
    try:
        _maybe_trigger_amcat(row, seen)
    except Exception as e:
        print(f"amcat trigger error {path}: {e}", flush=True)
    try:
        _maybe_trigger_hallo(row, seen)
    except Exception as e:
        print(f"hallo trigger error {path}: {e}", flush=True)
    try:
        _maybe_trigger_sutherland(row, seen)
    except Exception as e:
        print(f"sutherland trigger error {path}: {e}", flush=True)


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
        for dirpath, dirs, _files in os.walk(root):
            # Never watch hidden Maildir subfolders (.Trash/.Sent/.Drafts/.Junk). Their leaf
            # dir is literally named "cur"/"new", so an event there would otherwise be treated
            # as inbox mail — and moving a thread into .Trash/cur would RE-INDEX the file we
            # just deleted. Prune hidden dirs from the walk so they're never watched.
            dirs[:] = [d for d in dirs if not d.startswith(".")]
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
                if not name.startswith("."):    # skip .Trash/.Sent/... (deleted/sent copies)
                    add_tree(full)              # new mailbox / new|cur dir -> watch it
                continue
            if is_dir:
                continue
            # file event: only act inside a REAL mailbox new/ or cur/ leaf — NOT a hidden
            # Maildir subfolder like .Trash/cur or .Sent/cur (whose basename is also "cur"),
            # which hold deleted/sent copies. Reject when the parent dir is a hidden folder.
            if os.path.basename(base) not in ("new", "cur") or \
                    os.path.basename(os.path.dirname(base)).startswith("."):
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
