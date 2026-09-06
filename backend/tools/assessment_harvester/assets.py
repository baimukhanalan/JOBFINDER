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


# Generic intelligible English — enough words for SVAR's speech RECOGNITION to register real speech
# (a bare tone was rejected as "we are unable to hear you"). Content need not match the shown sentence;
# we only need each SVAR item to detect speech and ADVANCE (score is irrelevant — we harvest).
_SPEECH_TEXT = (
    "Hello, my name is Alex and I am very glad to be here today. "
    "I have several years of customer service experience and I really enjoy helping people. "
    "I stay calm and professional under pressure, and I always listen carefully to every customer. "
    "I communicate clearly and I work very well as part of a team. "
    "Thank you very much for this opportunity, I am confident I would be a great fit for this role.")


def speak_text_wav(text: str, path: str) -> bool:
    """Synthesize `text` to a mono 48kHz s16 WAV via espeak-ng (real intelligible speech) + ffmpeg.
    Used both for the default fake-mic asset and for per-item dynamic TTS of a captured prompt."""
    esp = shutil.which("espeak-ng") or shutil.which("espeak")
    ff = _ffmpeg()
    if not esp or not ff:
        return False
    raw = path + ".raw.wav"
    try:
        # -s 150 wpm (clear), -g small word gap; espeak writes a 22050Hz WAV
        subprocess.run([esp, "-s", "150", "-g", "3", "-w", raw, text], check=True, timeout=60)
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", raw,
                        "-af", "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono,volume=2.0",
                        "-y", path], check=True, timeout=60)
        try:
            os.remove(raw)
        except OSError:
            pass
        return os.path.exists(path)
    except Exception:
        return False


def _gen_tone(path: str) -> bool:
    """Fallback when no TTS engine: ~28s of continuous speech-BAND energy (not words)."""
    ff = _ffmpeg()
    if not ff:
        return False
    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "sine=frequency=180:duration=28",
           "-f", "lavfi", "-i", "sine=frequency=550:duration=28",
           "-f", "lavfi", "-i", "sine=frequency=1800:duration=28",
           "-f", "lavfi", "-i", "anoisesrc=d=28:c=pink:a=0.12",
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


def _gen_speech(path: str) -> bool:
    """Real intelligible speech (espeak-ng) so SVAR speech-recognition registers words and ADVANCES;
    falls back to a speech-band tone if no TTS engine is present."""
    return speak_text_wav(_SPEECH_TEXT, path) or _gen_tone(path)


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
