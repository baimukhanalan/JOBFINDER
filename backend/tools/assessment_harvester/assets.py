"""Fake media assets for driving speaking/listening/webcam-gated assessment sections.

Chromium is launched with `--use-file-for-fake-audio-capture=speech.wav` and
`--use-file-for-fake-video-capture=face.y4m` (see core.py) so getUserMedia() returns a synthetic
mic + camera — no permission dialog, and SVAR/speaking recorders capture *audible* input so the
record -> stop -> submit cycle can ADVANCE (we harvest the prompt; the score is irrelevant).

The assets are generated on demand with ffmpeg (kept out of git — big + regenerable):
  * speech.wav  — mono 48kHz s16, ~22s of speech-BAND audio (formant-ish tones + pink noise,
    syllable-rate tremolo). NB: no local TTS engine is installed, so this is speech-LIKE energy,
    not intelligible words — enough to register as a non-silent recording. A section that gates on
    speech-RECOGNITION content (not just "audio present") is the documented ceiling.
  * face.y4m    — 320x240 test pattern. Satisfies a basic camera-present check; a webcam LIVENESS
    check that needs a real face is the documented ceiling.
"""
from __future__ import annotations

import os
import shutil
import subprocess

_DIR = os.path.join(os.path.dirname(__file__), "assets")
SPEECH_WAV = os.path.join(_DIR, "speech.wav")
FACE_Y4M = os.path.join(_DIR, "face.y4m")


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _gen_speech(path: str) -> bool:
    ff = _ffmpeg()
    if not ff:
        return False
    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "sine=frequency=180:duration=22",
           "-f", "lavfi", "-i", "sine=frequency=550:duration=22",
           "-f", "lavfi", "-i", "sine=frequency=1800:duration=22",
           "-f", "lavfi", "-i", "anoisesrc=d=22:c=pink:a=0.12",
           "-filter_complex",
           "[0][1][2][3]amix=inputs=4:normalize=0,tremolo=f=4.5:d=0.85,"
           "highpass=f=90,lowpass=f=3800,volume=2.2,"
           "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono",
           "-y", path]
    try:
        subprocess.run(cmd, check=True, timeout=120)
        return os.path.exists(path)
    except Exception:
        return False


def _gen_face(path: str) -> bool:
    ff = _ffmpeg()
    if not ff:
        return False
    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=12:duration=4",
           "-pix_fmt", "yuv420p", "-y", path]
    try:
        subprocess.run(cmd, check=True, timeout=120)
        return os.path.exists(path)
    except Exception:
        return False


def ensure_assets() -> dict:
    """Generate the fake media files if missing. Returns {'audio': path|None, 'video': path|None}."""
    os.makedirs(_DIR, exist_ok=True)
    audio = SPEECH_WAV if (os.path.exists(SPEECH_WAV) or _gen_speech(SPEECH_WAV)) else None
    video = FACE_Y4M if (os.path.exists(FACE_Y4M) or _gen_face(FACE_Y4M)) else None
    return {"audio": audio, "video": video}
