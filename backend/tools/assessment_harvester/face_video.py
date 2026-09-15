"""Free, local talking-head generation for the Hallo video/speaking face-feed — the $0 replacement for
the paid higgsfield credit path (which is plan-gated on this account; Runway/Kling are the same
credit/trial model, watermarked + length-capped, so unusable for a webcam feed).

Uses Wav2Lip (open-source) to LIP-SYNC the synthetic face still to a prepared TTS answer WAV, so the
face's mouth matches the exact words Hallo transcribes and scores. Output has NO audio (the virtmic
plays the answer separately) and is normalized to the camera-feed spec (1280x720 yuv420p 15fps) so it
drops straight into camera.py's /dev/video0 feed.

CPU-only host (no GPU): generation is slow (~2-3 min per short clip), so results are CONTENT-ADDRESSED
CACHED by the audio bytes and meant to be PRE-GENERATED for the banked prepared answers (see the CLI
`--prebank` below) — at run time the camera just feeds the ready clip; nothing is generated inline.

All heavy deps live in a separate venv (~/.venvs/lipsync); this module only shells out to it, so the
harvester runtime never imports torch. Everything is optional: lipsync_wav() returns None when the venv
or checkpoint is absent, and callers fall back to the idle face loop (camera._default_source).

Weights note: Wav2Lip checkpoints are research/non-commercial-licensed — this is one-time internal
synthetic-asset generation; a conscious owner decision before any commercial productization.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from shutil import which

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MEDIA = os.path.join(_BACKEND_DIR, "data", "assessment_media")
_FACE_STILL = os.environ.get("FACE_STILL", os.path.join(_MEDIA, "face_720.png"))
_LIPSYNC_PY = os.environ.get("LIPSYNC_PY", os.path.expanduser("~/.venvs/lipsync/bin/python"))
_WAV2LIP_CKPT = os.environ.get("WAV2LIP_CKPT", os.path.join(_MEDIA, "models", "wav2lip_gan.state.pth"))
_CACHE = os.environ.get("FACE_LIPSYNC_CACHE", os.path.join(_MEDIA, "face_lipsync_cache"))
_W, _H, _FPS = 1280, 720, 15


def available() -> bool:
    """True when the free lip-sync toolchain is present (venv python + converted checkpoint + a face
    still + ffmpeg). Callers use this to decide whether to lip-sync or fall back to the idle loop."""
    return (os.path.exists(_LIPSYNC_PY) and os.path.exists(_WAV2LIP_CKPT)
            and os.path.exists(_FACE_STILL) and bool(which("ffmpeg")))


def _audio_key(wav_path: str) -> str:
    """SHA-1 of the audio bytes so identical answer audio (same cached TTS clip) shares one video."""
    h = hashlib.sha1()
    with open(wav_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def lipsync_wav(wav_path: str, face: str | None = None) -> str | None:
    """Return a cached 1280x720/yuv420p/15fps mp4 of the face lip-synced to `wav_path` (no audio track).
    Generated ONCE per unique audio and cached; a repeated answer returns the stored clip instantly.
    Returns None if the toolchain is unavailable or synthesis fails (caller falls back to the idle loop).
    Slow on CPU (~2-3 min) — call ahead of time (prebank), not inline during a live record window."""
    if not (wav_path and os.path.exists(wav_path) and available()):
        return None
    out = os.path.join(_CACHE, _audio_key(wav_path) + ".mp4")
    if os.path.exists(out) and os.path.getsize(out) > 10000:
        return out                                       # HIT — ready clip, no synthesis
    try:
        os.makedirs(_CACHE, exist_ok=True)
    except OSError:
        return None
    face = face or _FACE_STILL
    raw = out + ".raw.mp4"
    # Wav2Lip runs in its own venv (keeps torch out of the harvester); a tiny inline program drives it.
    code = ("from lipsync import LipSync\n"
            f"LipSync(model='wav2lip', checkpoint_path={_WAV2LIP_CKPT!r}, device='cpu', nosmooth=True)"
            f".sync({face!r}, {wav_path!r}, {raw!r})\n")
    try:
        subprocess.run([_LIPSYNC_PY, "-c", code], check=True, timeout=1200,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # normalize to the camera-feed spec; drop the audio track (the virtmic plays the answer)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", raw, "-an",
             "-vf", f"scale={_W}:{_H}:force_original_aspect_ratio=increase,crop={_W}:{_H},"
                    f"fps={_FPS},format=yuv420p",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", out],
            check=True, timeout=180)
        return out if (os.path.exists(out) and os.path.getsize(out) > 10000) else None
    except Exception:
        return None
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


def _prebank(limit: int = 0) -> None:
    """Pre-generate lip-synced clips for every cached prepared-answer TTS WAV so they are ready to feed
    instantly at run time. Walks the TTS cache (assets.speech_wav_for output). Idempotent (cached)."""
    tts_dir = os.path.join(_MEDIA, "tts_cache")
    wavs = sorted(os.path.join(tts_dir, f) for f in os.listdir(tts_dir)) if os.path.isdir(tts_dir) else []
    wavs = [w for w in wavs if w.endswith(".wav")]
    if limit:
        wavs = wavs[:limit]
    print(f"[face_video] prebank: {len(wavs)} TTS clips, available={available()}")
    done = 0
    for w in wavs:
        out = lipsync_wav(w)
        done += bool(out)
        print(f"[face_video] {'OK ' if out else 'FAIL'} {os.path.basename(w)} -> {out}")
    print(f"[face_video] prebank done: {done}/{len(wavs)} lip-synced clips cached in {_CACHE}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Free local Wav2Lip face-video generation")
    ap.add_argument("--wav", help="lip-sync one WAV and print the cached mp4 path")
    ap.add_argument("--prebank", action="store_true", help="pre-generate clips for all cached TTS WAVs")
    ap.add_argument("--limit", type=int, default=0, help="cap --prebank count (0 = all)")
    a = ap.parse_args()
    if a.wav:
        print(lipsync_wav(a.wav))
    elif a.prebank:
        _prebank(a.limit)
    else:
        print(f"available={available()} face={_FACE_STILL} ckpt={_WAV2LIP_CKPT} cache={_CACHE}")
