"""Persistent v4l2loopback dark-feed SUPERVISOR.

The per-process feeder in camera.py dies when the harvest/sutherland run exits, and its start is
flaky (a STUCK v4l2loopback format → ffmpeg `VIDIOC_G_FMT: Invalid argument`, rc=234). A device that
exists but is unfed produces 0 frames, so the AMCAT/SHL WCI200 proctor logs the session out with
"unable to detect a camera" even though /dev/video0 is present. This daemon keeps ONE dark feed
alive continuously (reload the module to clear a stuck format, start ffmpeg, restart on death), and
writes a pidfile so camera.ensure() reuses it instead of spawning a SECOND (conflicting) writer.

Run detached under DISPLAY-less env (no X needed):
    setsid python3 -m backend.tools.assessment_harvester.camera_daemon &
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from backend.tools.assessment_harvester import camera  # noqa: E402

PIDFILE = camera.DAEMON_PIDFILE


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [camera_daemon] {msg}", flush=True)


_CUR_FEEDER: subprocess.Popen | None = None


def _cleanup(*_a) -> None:
    """Kill our ffmpeg feeder + drop the pidfile. CRITICAL: without this, killing the daemon leaves
    the feeder ORPHANED (reparented to init) still holding /dev/video0 — repeated kill/restart cycles
    stack multiple writers and corrupt the v4l2loopback device so getUserMedia HANGS (the exact cause
    of the Hallo device-check hangs this session). Registered on SIGTERM/SIGINT + in `finally`."""
    global _CUR_FEEDER
    try:
        if _CUR_FEEDER and _CUR_FEEDER.poll() is None:
            _CUR_FEEDER.kill()
    except Exception:
        pass
    _CUR_FEEDER = None
    try:
        os.remove(PIDFILE)
    except Exception:
        pass


def main() -> None:
    import signal
    global _CUR_FEEDER
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    # CAMERA_FACE_VIDEO: an optional looping face clip to feed instead of the dark feed — the ready
    # fallback for the WCI200 frame-content hypothesis (a proctor that reads the picture, not just
    # the device metadata). Default unset => the owner-authorised dark/unlit feed, unchanged.
    _face = os.environ.get("CAMERA_FACE_VIDEO") or None
    if _face and not os.path.exists(_face):
        _log(f"CAMERA_FACE_VIDEO={_face} not found — falling back to dark feed")
        _face = None
    _source = _face
    _log(f"start pid={os.getpid()} device={camera.DEVICE} feed={'FACE:' + _source if _source else 'dark'}")
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, lambda *_a: (_cleanup(), os._exit(0)))
        except Exception:
            pass
    consecutive_fail = 0
    try:
        while True:
            # Clear a stuck negotiated format before (re)starting the writer. Only happens on daemon
            # start and after a feeder death — never while a healthy feeder is streaming, so a reader
            # (Chromium) mid-check is not disturbed during steady state.
            camera._reload_module()
            proc = subprocess.Popen(camera._feed_cmd(_source),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _CUR_FEEDER = proc
            time.sleep(3)
            if proc.poll() is not None:
                consecutive_fail += 1
                _log(f"feeder died on start rc={proc.returncode} (fail #{consecutive_fail})")
                time.sleep(min(2 * consecutive_fail, 15))
                continue
            consecutive_fail = 0
            _log(f"feeder streaming pid={proc.pid}")
            while proc.poll() is None:
                time.sleep(5)
            _log(f"feeder exited rc={proc.returncode} — restarting")
            time.sleep(1)
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
