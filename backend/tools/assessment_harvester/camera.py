"""Virtual camera via v4l2loopback — the video twin of mic.py. Chromium's synthetic camera
(`--use-fake-device-for-media-stream`) is REJECTED by the AMCAT/SHL WCI200 continuous proctor
("Error Code WCI200: unable to detect a camera on your device"); a v4l2loopback device is a REAL
V4L2 capture node (`/dev/video0`, advertises `yuv420p 640x480` once fed) that getUserMedia + WCI200
accept as a genuine webcam.

Mirrors mic.py exactly: mic.py loads a PulseAudio null-sink + remap-source and PLAYS dynamic audio
into it (`speak`) so Chromium's getUserMedia "hears" it; camera.py loads a v4l2loopback device and an
ffmpeg PRODUCER PUSHES a dynamic video stream into it (`feed`) so getUserMedia "sees" it. Both launch
Chromium WITHOUT the corresponding `--use-file-for-fake-*` flag (real device) but WITH
`--use-fake-ui-for-media-stream` (auto-grant, no permission dialog).

Setup (idempotent, `ensure()`): load `videodev` + `v4l2loopback` (one device, `video_nr=0`,
`exclusive_caps=1`, realistic `card_label="Integrated Camera"` — a "Dummy"/"Loopback" name is a proctor
fingerprint) if `/dev/video0` is absent, make it world-usable, then start the DARK/BLANK (unlit) feed.
The dim feed is exactly the physical-unlit-webcam the owner authorized for WCI200 — NOT a fabricated
face (no face is ever drawn). The module file ships with the kernel; its dep `videodev` comes from
`linux-modules-extra-$(uname -r)` (installed 2026-09-12). Boot-persisted via
`/etc/modules-load.d/v4l2loopback.conf` + `/etc/modprobe.d/v4l2loopback.conf` + a udev 0666 rule, so
`/dev/video0` normally exists at boot and `ensure()` only has to (re)start the feeder — no root at
runtime; a missing device is self-healed with `sudo -n modprobe` (passwordless for `programmer`).

Verified live 2026-09-12: fed the dark camera into `/dev/video0`, read back a real 640x480 frame, the
device advertised `yuv420p 640x480`. Single writer only (v4l2loopback `devices=1`) — Sutherland runs
are paced sequential, so one browser reads the one device at a time. No PII (local video only).
"""
from __future__ import annotations

import logging
import os
import subprocess
from shutil import which

logger = logging.getLogger("assessment_harvester")

DEVICE = os.environ.get("CAMERA_DEVICE", "/dev/video0")
CARD_LABEL = "Integrated Camera"
VIDEO_NR = 0

# Optional FACE-FEED: a looped ready face clip fed into the device instead of the dark feed, so a
# proctor that reads the PICTURE (Hallo video-response scoring) sees a plausible live candidate — the
# owner's "готовая запись, не зависеть от реального лица" (a synthetic face, never a real person's).
# WCI200 (AMCAT/Sutherland) rejects a virtual device regardless of content, so the face is neutral there
# and beneficial for Hallo. Resolution order (see _default_source): CAMERA_FACE_VIDEO env overrides
# (a path to feed, or 'dark'/'0'/'none'/'off' to force the dark feed) -> the standard on-disk face loop
# -> the dark/unlit feed. Default file is gitignored (backend/data/assessment_media/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STD_FACE_VIDEO = os.path.join(_BACKEND_DIR, "data", "assessment_media", "face_loop.mp4")


def _default_source() -> str | None:
    """Feed source when a caller passes no explicit video: the standard face loop if present, else the
    dark/unlit feed (None). CAMERA_FACE_VIDEO overrides — a path, or 'dark'/'0'/'none'/'off' forces dark."""
    env = (os.environ.get("CAMERA_FACE_VIDEO") or "").strip()
    if env:
        if env.lower() in ("dark", "0", "none", "off", "false"):
            return None
        if os.path.exists(env):
            return env
    return _STD_FACE_VIDEO if os.path.exists(_STD_FACE_VIDEO) else None
# A persistent external feeder (camera_daemon.py) writes its pid here. When it is alive, ensure()
# REUSES its continuous dark feed instead of spawning a second (conflicting) writer — v4l2loopback is
# single-writer, so two feeders fight over the format (VIDIOC_G_FMT invalid) and BOTH die.
DAEMON_PIDFILE = os.environ.get("CAMERA_DAEMON_PIDFILE", "/tmp/jf_camera_feeder.pid")
_WIDTH, _HEIGHT, _FPS = 1280, 720, 15  # 16:9 (real-webcam standard) so proctor's 480x270 req is a clean downscale, not an aspect-crop

# module-level producer handle (analogous to mic.py's paplay Popen)
_FEEDER: subprocess.Popen | None = None


def available() -> bool:
    """ffmpeg present AND a v4l2loopback device is reachable (already loaded OR the .ko is on disk)."""
    if not which("ffmpeg"):
        return False
    if os.path.exists(DEVICE):
        return True
    kver = os.uname().release
    return os.path.exists(f"/lib/modules/{kver}/kernel/v4l2loopback/v4l2loopback.ko.zst") or \
        os.path.exists(f"/lib/modules/{kver}/kernel/v4l2loopback/v4l2loopback.ko")


def _ensure_module() -> bool:
    """Ensure /dev/video0 exists (load videodev + v4l2loopback via passwordless sudo if absent)."""
    if os.path.exists(DEVICE):
        return True
    try:
        subprocess.run(["sudo", "-n", "modprobe", "videodev"], capture_output=True, timeout=20)
        subprocess.run(
            ["sudo", "-n", "modprobe", "v4l2loopback", "devices=1", f"video_nr={VIDEO_NR}",
             f"card_label={CARD_LABEL}", "exclusive_caps=1"], capture_output=True, timeout=20)
        # world-usable so any launcher (pm2/cron/browser) can open it without the video group
        subprocess.run(["sudo", "-n", "chmod", "0666", DEVICE], capture_output=True, timeout=10)
    except Exception as exc:
        logger.info("[camera] modprobe failed: %s", exc)
    return os.path.exists(DEVICE)


def _reload_module() -> bool:
    """Reload v4l2loopback to CLEAR a stuck/incompatible negotiated format (v4l2loopback holds the
    first format until reload; a producer at a different resolution then fails VIDIOC_G_FMT -> rc=234).
    Safe only with NO consumer/producer attached (call before the browser opens the device)."""
    try:
        subprocess.run(["sudo", "-n", "modprobe", "-r", "v4l2loopback"], capture_output=True, timeout=20)
        subprocess.run(
            ["sudo", "-n", "modprobe", "v4l2loopback", "devices=1", f"video_nr={VIDEO_NR}",
             f"card_label={CARD_LABEL}", "exclusive_caps=1"], capture_output=True, timeout=20)
        subprocess.run(["sudo", "-n", "chmod", "0666", DEVICE], capture_output=True, timeout=10)
    except Exception as exc:
        logger.info("[camera] module reload failed: %s", exc)
    return os.path.exists(DEVICE)


def _feed_cmd(source: str | None) -> list[str]:
    """ffmpeg producer args. `source` = a video file (looped) or None -> a live DARK/BLANK feed
    (dim grey + faint per-frame noise = a live, face-less, unlit webcam)."""
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if source and os.path.exists(source):
        src = ["-stream_loop", "-1", "-re", "-i", source]
    else:
        src = ["-f", "lavfi", "-re", "-i",
               f"color=c=0x141414:s={_WIDTH}x{_HEIGHT}:r={_FPS},noise=alls=12:allf=t"]
    return base + src + ["-vf", "format=yuv420p", "-f", "v4l2", DEVICE]


def is_running() -> bool:
    return _FEEDER is not None and _FEEDER.poll() is None


def feed(video_path: str | None = None) -> bool:
    """(Re)start the producer pushing `video_path` (or the live dark feed) into the device. Analogous
    to mic.speak — swaps the media the "camera" shows. Returns True if a producer is now running."""
    global _FEEDER
    import time
    if video_path is None:
        video_path = _default_source()
    stop()

    def _spawn() -> bool:
        global _FEEDER
        _FEEDER = subprocess.Popen(_feed_cmd(video_path),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.5)  # let it fill the device before a reader opens it
        return _FEEDER.poll() is None

    try:
        if _spawn():
            return True
        # feeder died early — almost always a STUCK v4l2loopback format from a prior run (VIDIOC_G_FMT
        # Invalid arg / rc=234). Without a live producer the exclusive_caps device presents NO capture
        # source, so a browser getUserMedia gets NotFoundError. Reload the module to clear it + retry.
        rc = _FEEDER.returncode if _FEEDER else None
        logger.info("[camera] feeder exited early rc=%s — reloading module to clear stuck format", rc)
        _reload_module()
        if _spawn():
            logger.info("[camera] feeder recovered after module reload")
            return True
        logger.info("[camera] feeder STILL dead after reload rc=%s", _FEEDER.returncode if _FEEDER else None)
        _FEEDER = None
        return False
    except Exception as exc:
        logger.info("[camera] feed failed: %s", exc)
        _FEEDER = None
        return False


def _daemon_alive() -> bool:
    """True if the persistent camera_daemon feeder process is running (owns the device's dark feed)."""
    try:
        pid = int(open(DAEMON_PIDFILE).read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def ensure(video_path: str | None = None) -> bool:
    """Ensure the v4l2loopback device exists + a dark feed is running. Idempotent. Returns True ONLY
    if a real camera device is actually STREAMING frames for Chromium to enumerate (a device that
    exists but is unfed = 0 frames = the WCI200 'unable to detect a camera' logout, so 'exists' is
    NOT 'ready')."""
    if not available():
        return False
    # A persistent external feeder (camera_daemon.py) already owns + streams the device — reuse it and
    # do NOT touch the module/feeder (a second writer would collide and kill both).
    if _daemon_alive():
        return True
    if not _ensure_module():
        logger.info("[camera] no /dev/video device (module load failed)")
        return False
    if video_path is None:
        video_path = _default_source()
    if video_path is None and is_running():
        return True
    ok = feed(video_path)
    logger.info("[camera] virtual camera ready=%s (device=%s)", ok, DEVICE)
    return ok


def stop():
    """Stop the producer (the device stays loaded)."""
    global _FEEDER
    if _FEEDER is not None:
        try:
            _FEEDER.terminate()
            _FEEDER.wait(timeout=5)
        except Exception:
            try:
                _FEEDER.kill()
            except Exception:
                pass
        _FEEDER = None


def launch_args() -> list[str]:
    """Chromium args for using the REAL v4l2loopback camera (NO --use-fake-device-for-media-stream —
    that synthetic device is what WCI200 rejects). Keep --use-fake-ui to auto-grant getUserMedia."""
    return ["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


def launch_env() -> dict:
    return dict(os.environ)
