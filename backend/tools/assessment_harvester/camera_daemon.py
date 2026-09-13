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


def main() -> None:
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    _log(f"start pid={os.getpid()} device={camera.DEVICE}")
    consecutive_fail = 0
    try:
        while True:
            # Clear a stuck negotiated format before (re)starting the writer. Only happens on daemon
            # start and after a feeder death — never while a healthy feeder is streaming, so a reader
            # (Chromium) mid-check is not disturbed during steady state.
            camera._reload_module()
            proc = subprocess.Popen(camera._feed_cmd(None),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        try:
            os.remove(PIDFILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()
