"""Parallel bulk-apply worker pool.

Spawns N **headless** co-pilot processes (`backend.copilot` with COPILOT_HEADLESS=1),
each on its own 127.0.0.1 port with its own browser, so greenhouse/ashby fills run
concurrently instead of one-at-a-time through the single noVNC co-pilot. Lever/Workable
stay OFF this lane (they need the human to solve the captcha in noVNC) — the dashboard
routes those straight to the «Незавершённые» ledger.

Workers are spawned on demand when a parallel run starts and torn down when it ends. They
inherit the dashboard's `mail` group (it runs under `sg mail`), so each worker can still
read the candidate Maildir for the Ashby emailed-security-code step. No Xvfb/DISPLAY is
needed — headless Chromium renders offscreen.
"""
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
_BASE_PORT = 8110          # workers get 8110, 8111, … (main headful co-pilot stays 8102)
_MAX_WORKERS = 16

# module-global live pool: parallel Popen handles + their ports
_state: dict = {"procs": [], "ports": []}


def worker_ports() -> list[int]:
    return list(_state["ports"])


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


def _spawn_worker(port: int, extra_env: dict | None = None):
    """Launch one headless co-pilot on `port`. Inherits the parent's env+groups (so the
    dashboard's `mail` group carries over for Maildir reads). `extra_env` overlays extra
    variables (e.g. AVATURE_ADVANCE=1 for the mass-hiring lane). Returns the Popen or None."""
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env["COPILOT_HEADLESS"] = "1"
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    cmd = (f"cd {_REPO} && exec /usr/bin/python3 -m uvicorn backend.copilot:app "
           f"--host 127.0.0.1 --port {port} --log-level warning")
    try:
        return subprocess.Popen(["/bin/bash", "-c", cmd], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception:
        logger.warning("failed to spawn worker on :%s", port, exc_info=True)
        return None


def _wait_healthy(ports: list[int], timeout: float = 90.0) -> list[int]:
    deadline = time.monotonic() + timeout
    pending = list(ports)
    ready: list[int] = []
    while pending and time.monotonic() < deadline:
        still = []
        for p in pending:
            (ready if _health_ok(p) else still).append(p)
        pending = still
        if pending:
            time.sleep(1.0)
    return ready


def start_workers(n: int, wait: float = 90.0, extra_env: dict | None = None) -> list[int]:
    """Tear down any existing pool, spawn `n` fresh headless workers, and return the ports
    that came up healthy within `wait` seconds (may be fewer than n if some failed).
    `extra_env` overlays env vars on every worker (e.g. AVATURE_ADVANCE for mass-hiring)."""
    stop_workers()
    n = max(1, min(int(n), _MAX_WORKERS))
    procs, ports = [], []
    for i in range(n):
        port = _BASE_PORT + i
        p = _spawn_worker(port, extra_env=extra_env)
        if p is not None:
            procs.append(p)
            ports.append(port)
    _state["procs"], _state["ports"] = procs, ports
    ready = _wait_healthy(ports, timeout=wait)
    # drop dead ones from the tracked pool (their procs get reaped on stop_workers)
    _state["ports"] = ready
    logger.info("bulk pool: %s/%s workers healthy on ports %s", len(ready), n, ready)
    return ready


def add_worker(wait: float = 90.0) -> int | None:
    """Spawn ONE more worker on the next free port (for the adaptive lane) without
    disturbing the existing ones. Returns the port, or None if it didn't come up."""
    used = set(_state["ports"])
    port = _BASE_PORT
    while port in used or _health_ok(port, timeout=0.4):
        port += 1
        if port > _BASE_PORT + 60:
            return None
    p = _spawn_worker(port)
    if p is None:
        return None
    _state["procs"].append(p)
    if _wait_healthy([port], timeout=wait):
        _state["ports"].append(port)
        return port
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:
        pass
    return None


def stop_workers() -> None:
    """SIGTERM every worker's process group (kills uvicorn + its Chromium), then clear."""
    for p in _state.get("procs", []):
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
    # brief grace, then SIGKILL any stragglers
    time.sleep(0.5)
    for p in _state.get("procs", []):
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    _state["procs"], _state["ports"] = [], []
