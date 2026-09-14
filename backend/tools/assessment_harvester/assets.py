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

import hashlib
import os
import shutil
import subprocess

_DIR = os.path.join(os.path.dirname(__file__), "assets")
SPEECH_WAV = os.path.join(_DIR, "speech.wav")
FACE_Y4M = os.path.join(_DIR, "face.y4m")

# TTS voice STACK (best-first): ElevenLabs (cloud, most natural — only when a key is set) -> piper
# (local neural, no key) -> espeak-ng (robotic, always available). A synthesized clip is CACHED on
# disk content-addressed by (engine+voice, text): a repeated/identical question replays the SAME
# stored file with NO regeneration and NO latency (the owner's "prepared mp3, no extra generation"
# requirement — collect all questions, synthesize each answer ONCE, replay per matching question).
_ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
_ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # a default preset voice
_ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

# piper — a local NEURAL TTS (natural voice, no API key), the fallback under ElevenLabs. Env-overridable.
_PIPER_BIN = os.environ.get("PIPER_BIN", os.path.expanduser("~/.venvs/piper/bin/piper"))
_PIPER_MODEL = os.environ.get(
    "PIPER_MODEL", os.path.expanduser("~/.local/share/piper-voices/en_US-amy-medium.onnx"))

# content-addressed TTS cache (gitignored data dir); one file per unique (engine, text)
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TTS_CACHE_DIR = os.environ.get(
    "TTS_CACHE_DIR", os.path.join(_BACKEND_DIR, "data", "assessment_media", "tts_cache"))


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _eleven_wav(text: str, path: str) -> bool:
    """Most-natural voice via the ElevenLabs API — used ONLY when ELEVENLABS_API_KEY is set, and (via
    speech_wav_for) only ONCE per unique question: a repeated question replays the cached file with no
    API call and no latency. Returns False (=> fall back to piper/espeak) when the key is absent or the
    request fails, so the pipeline never blocks on the network."""
    if not _ELEVEN_KEY:
        return False
    ff = _ffmpeg()
    if not ff:
        return False
    raw = path + ".eleven.mp3"
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{_ELEVEN_VOICE}",
            data=_json.dumps({"text": text, "model_id": _ELEVEN_MODEL,
                              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}).encode(),
            headers={"xi-api-key": _ELEVEN_KEY, "Content-Type": "application/json",
                     "Accept": "audio/mpeg"}, method="POST")
        with urllib.request.urlopen(req, timeout=45) as resp, open(raw, "wb") as fh:
            fh.write(resp.read())
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", raw,
                        "-af", "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono",
                        "-y", path], check=True, timeout=60)
        try:
            os.remove(raw)
        except OSError:
            pass
        return os.path.exists(path) and os.path.getsize(path) > 1000
    except Exception:
        try:
            os.remove(raw)
        except OSError:
            pass
        return False


def _tts_engine_tag() -> str:
    """Identifies the active voice so a cache entry made by one engine isn't replayed after a swap."""
    if _ELEVEN_KEY:
        return "eleven:" + _ELEVEN_VOICE
    if os.path.exists(_PIPER_BIN) and os.path.exists(_PIPER_MODEL):
        return "piper:" + os.path.basename(_PIPER_MODEL)
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak"
    return "tone"


def speech_wav_for(text: str) -> str | None:
    """Content-addressed TTS cache. Synthesize `text` to a mono 48kHz WAV ONCE and return the path to a
    reusable file; a repeated/identical `text` (a recurring question / prepared answer) replays the SAME
    cached file with NO regeneration and no latency. Returns None only if synthesis fails and no cache
    entry exists. This is the owner's "готовую mp3 без лишней генерации" mechanic applied to speaking/
    video answers, exactly like the MCQ answer-bank replays a stored option."""
    text = (text or "").strip()
    if not text:
        return None
    key = hashlib.sha1((_tts_engine_tag() + "\x00" + text).encode("utf-8")).hexdigest()
    path = os.path.join(_TTS_CACHE_DIR, key + ".wav")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path                                     # HIT — replay ready audio, no synthesis
    try:
        os.makedirs(_TTS_CACHE_DIR, exist_ok=True)
    except OSError:
        return None
    tmp = path + ".tmp.wav"
    if speak_text_wav(text, tmp):
        try:
            os.replace(tmp, path)                       # atomic publish so a reader never sees a partial
            return path
        except OSError:
            return tmp if os.path.exists(tmp) else None
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None


def _piper_wav(text: str, path: str) -> bool:
    """Natural neural TTS via piper -> mono 48kHz s16 WAV. Returns False (so speak_text_wav falls back
    to espeak-ng) if the piper binary/model are absent or synthesis fails."""
    ff = _ffmpeg()
    if not (ff and os.path.exists(_PIPER_BIN) and os.path.exists(_PIPER_MODEL)):
        return False
    raw = path + ".piper.wav"
    try:
        subprocess.run([_PIPER_BIN, "-m", _PIPER_MODEL, "-f", raw], input=text, text=True,
                       check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", raw,
                        "-af", "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono,volume=2.0",
                        "-y", path], check=True, timeout=60)
        try:
            os.remove(raw)
        except OSError:
            pass
        return os.path.exists(path) and os.path.getsize(path) > 1000
    except Exception:
        return False


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
    """Synthesize `text` to a mono 48kHz s16 WAV — a NATURAL neural voice via piper when available,
    else robotic-but-intelligible espeak-ng. Used for the default fake-mic asset and for per-item
    dynamic TTS of a captured prompt / a prepared spoken answer."""
    if _eleven_wav(text, path):         # most-natural cloud voice (only when ELEVENLABS_API_KEY is set)
        return True
    if _piper_wav(text, path):          # natural neural voice (no API key), the default
        return True
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
