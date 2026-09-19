#!/usr/bin/env python3
"""SSH ``ProxyCommand`` for the ``macalan`` host — reach the Mac (100.86.135.112) by
``tailscale nc`` over a LIVE userspace-egress slot chosen dynamically.

WHY: the ``*/10 tailscale_egress --sync`` cron CHURNS which
``backend/data/ts-egress/<slot>/`` exists, so ``~/.ssh/config`` must NOT hardcode
``ts-egress/0/tailscaled.sock`` — when slot 0 is momentarily gone, ``ssh macalan`` dies
with ``dial unix .../ts-egress/0/tailscaled.sock: no such file or directory``, which
breaks the Sutherland ``caffeinate`` + drives (``assessment_supervisor`` with
``MAC_SSH=macalan``). This wrapper scans every slot socket, prefers one whose tailnet
status actually sees the Mac, falls back to the first existing socket, and execs
``tailscale --socket=<live> nc <host> <port>`` so stdin/stdout stream straight through.

It mirrors ``assessment_supervisor._egress_sock`` and REUSES it directly when importable
(single source of truth) — otherwise the inline scan below is byte-equivalent.

Usage (ProxyCommand): ``macalan_proxy.py <host> <port>``
  * env ``TS_EGRESS_DIR``   — override the slot directory to scan (used to simulate
                              a missing slot 0 in tests); forces the deterministic
                              inline scan of that dir.
  * env ``MAC_IP``          — the Mac's tailnet IP to prefer (default 100.86.135.112).
  * env ``MACALAN_PROXY_PRINT=1`` — print the chosen socket + scanned dir and exit
                              WITHOUT connecting (for testing/diagnostics).
  * env ``MACALAN_PROXY_DEBUG=1`` — also log the chosen socket to stderr before exec.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

_MAC_IP = os.environ.get("MAC_IP", "100.86.135.112")
# The LIVE deploy dir where the egress-sync cron writes the slots (CLAUDE.md: the
# lowercase /home/projects/jobfinder is THE live code). Used as a fallback when this
# wrapper is a copy deployed OUTSIDE the repo (e.g. ~/.ssh/macalan_proxy.py) so its
# __file__-relative root has no ts-egress dir of its own.
_LIVE_ROOT = "/home/projects/jobfinder"


def _ts() -> str:
    """Absolute path to the tailscale binary (cron PATH may be minimal)."""
    return shutil.which("tailscale") or "/usr/bin/tailscale"


def _egress_dir() -> str:
    """Resolve the ts-egress slot directory to scan."""
    override = os.environ.get("TS_EGRESS_DIR")
    if override:
        return override
    # repo root relative to THIS file (correct when the wrapper lives inside the repo)
    here_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for root in (here_root, _LIVE_ROOT):
        cand = os.path.join(root, "backend", "data", "ts-egress")
        if os.path.isdir(cand):
            return cand
    return os.path.join(here_root, "backend", "data", "ts-egress")


def _scan(egress_dir: str) -> str | None:
    """Mirror of assessment_supervisor._egress_sock over an explicit dir: prefer a slot
    whose tailnet status sees the Mac; else the first existing socket; else None."""
    socks = sorted(glob.glob(os.path.join(egress_dir, "*", "tailscaled.sock")))
    fallback = None
    for s in socks:
        fallback = fallback or s
        try:
            r = subprocess.run([_ts(), f"--socket={s}", "status"],
                               capture_output=True, text=True, timeout=8)
            out = (r.stdout or "").lower()
            if _MAC_IP in (r.stdout or "") or "macbook-air-alan" in out:
                return s
        except Exception:
            continue
    return fallback


def _pick_sock() -> str | None:
    # An explicit TS_EGRESS_DIR override forces the deterministic inline scan (so tests
    # can point at a fabricated slot set, e.g. one missing slot 0).
    if os.environ.get("TS_EGRESS_DIR"):
        return _scan(os.environ["TS_EGRESS_DIR"])
    egress = _egress_dir()
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(egress)))
    # Reuse the supervisor's selection logic when importable (single source of truth).
    try:
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from backend.tools.assessment_supervisor import _egress_sock  # type: ignore
        s = _egress_sock()
        if s:
            return s
    except Exception:
        pass
    return _scan(egress)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: macalan_proxy.py <host> <port>\n")
        return 2
    host, port = argv[1], argv[2]
    sock = _pick_sock()
    if os.environ.get("MACALAN_PROXY_PRINT"):
        print(f"egress_dir={_egress_dir()}")
        print(f"socket={sock or ''}")
        return 0 if sock else 3
    if not sock:
        sys.stderr.write(
            "macalan_proxy: no live ts-egress tailscaled socket under "
            f"{_egress_dir()} — cannot reach the Mac\n")
        return 3
    if os.environ.get("MACALAN_PROXY_DEBUG"):
        sys.stderr.write(f"macalan_proxy: using {sock}\n")
    ts = _ts()
    # exec: replace this process so the nc pipe is wired directly to ssh's stdio.
    os.execv(ts, [ts, f"--socket={sock}", "nc", host, port])
    return 1  # unreachable if execv succeeds


if __name__ == "__main__":
    sys.exit(main(sys.argv))
