"""Offline speech-to-text for harvested assessment audio (AMCAT listen-repeat items etc.).

The question audio is a plain MP3 on S3 (`qbdata-amcat.s3.amazonaws.com/SpeechAssessmentBank/...`);
we capture it during the walk and transcribe it here so the spoken sentence becomes a bankable
question. Whisper lives in a dedicated venv (`~/.venvs/asr`, `faster-whisper`) to keep it off the
system Python (PEP 668); we call it as a subprocess so the harvester stays on system Python.

No network, no PII: purely local transcription of a captured audio file.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger("assessment_harvester")

_VENV_PY = os.path.expanduser("~/.venvs/asr/bin/python")
_MODEL = os.environ.get("ASR_MODEL", "base")   # base is accurate enough + fast on CPU


def available() -> bool:
    return os.path.exists(_VENV_PY)


# One-shot transcription in the whisper venv. Prints a JSON line so we parse only our own output.
_SNIPPET = r"""
import sys, json
from faster_whisper import WhisperModel
path, model = sys.argv[1], sys.argv[2]
m = WhisperModel(model, device="cpu", compute_type="int8")
segs, info = m.transcribe(path, language="en", vad_filter=True)
txt = " ".join(s.text for s in segs).strip()
print("ASR_JSON:" + json.dumps({"text": txt, "lang": info.language}))
"""


def transcribe(audio_path: str, *, timeout: int = 180) -> str | None:
    """Return the transcript of `audio_path` (mp3/wav), or None on failure. Best-effort, never raises."""
    if not available() or not audio_path or not os.path.exists(audio_path):
        return None
    try:
        r = subprocess.run([_VENV_PY, "-c", _SNIPPET, audio_path, _MODEL],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        logger.info("[asr] subprocess failed: %s", exc)
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith("ASR_JSON:"):
            try:
                d = json.loads(line[len("ASR_JSON:"):])
                return (d.get("text") or "").strip() or None
            except Exception:
                return None
    if r.returncode != 0:
        logger.info("[asr] rc=%s err=%s", r.returncode, (r.stderr or "")[-200:])
    return None
