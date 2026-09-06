"""Virtual microphone via PulseAudio, so we can feed DYNAMIC per-item audio into Chromium's
getUserMedia (Chromium reads `--use-file-for-fake-audio-capture` only once at launch, so a static file
can't answer per-item). Used to PASS the AMCAT SVAR listen-repeat / read-aloud items (speak the
required sentence) and thereby advance through SVAR into the Typing / Personality / cognitive modules.

Setup (idempotent): a null-sink `virtmic` + a real `module-remap-source` `virtmic_src` from its
monitor (Chromium enumerates a remap-source as a mic, but NOT a bare .monitor — verified). Launch
Chromium WITHOUT `--use-file-for-fake-audio-capture` and WITH `PULSE_SERVER` in its env + the pulse
default-source set to `virtmic_src`; then `speak(wav)` plays audio into the sink so the mic "hears" it.

Verified live: Chromium captured paplay'd speech at RMS ~0.47. No PII (local audio only).
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger("assessment_harvester")

PULSE_SERVER = f"/run/user/{os.getuid()}/pulse/native"
SINK = "virtmic"
SOURCE = "virtmic_src"


def _env() -> dict:
    return dict(os.environ, PULSE_SERVER=PULSE_SERVER, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")


def _pactl(*args, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["pactl", *args], env=_env(), capture_output=True, text=True, timeout=timeout)


def available() -> bool:
    from shutil import which
    return bool(which("pactl") and which("paplay"))


def ensure() -> bool:
    """Ensure pulse is running with the null-sink + remap-source + default-source. Idempotent.
    Returns True if the virtual mic is ready."""
    if not available():
        return False
    try:
        # start a user pulse daemon if none
        if subprocess.run(["pulseaudio", "--check"], env=_env()).returncode != 0:
            subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1"], env=_env(), timeout=20)
        srcs = _pactl("list", "short", "sources").stdout
        if SINK not in srcs:
            _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
                   f"sink_properties=device.description={SINK}")
        srcs = _pactl("list", "short", "sources").stdout
        if SOURCE not in srcs:
            _pactl("load-module", "module-remap-source", f"source_name={SOURCE}",
                   f"master={SINK}.monitor", f"source_properties=device.description={SOURCE}")
        _pactl("set-default-source", SOURCE)
        ok = SOURCE in _pactl("list", "short", "sources").stdout
        logger.info("[mic] virtual mic ready=%s (source=%s)", ok, SOURCE)
        return ok
    except Exception as exc:
        logger.info("[mic] ensure failed: %s", exc)
        return False


def launch_args() -> list[str]:
    """Chromium args for using the pulse virtual mic (NO --use-file-for-fake-audio-capture; real audio).
    Keep the fake CAMERA (video) file — only the mic goes virtual."""
    return ["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


def launch_env() -> dict:
    return _env()


def speak(wav_path: str, *, blocking: bool = False, timeout: int = 30):
    """Play `wav_path` into the virtual mic (so Chromium's getUserMedia captures it). Returns the Popen
    (non-blocking) so the caller can stop it, or waits when blocking."""
    if not wav_path or not os.path.exists(wav_path):
        return None
    try:
        if blocking:
            subprocess.run(["paplay", f"--device={SINK}", wav_path], env=_env(), timeout=timeout)
            return None
        return subprocess.Popen(["paplay", f"--device={SINK}", wav_path], env=_env())
    except Exception as exc:
        logger.info("[mic] speak failed: %s", exc)
        return None


def silence():
    """Play nothing — placeholder for stopping any current playback (handled by the caller's Popen)."""
    return None
