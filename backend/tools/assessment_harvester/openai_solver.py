"""Live VISION/TEXT solver via the OpenAI API — used by the harvester for items the local text model
can't answer (figural odd-one-out, image/diagram cognitive, hard knowledge MCQs). The answer is banked
keyed to the question+media so a recurrence REPLAYS it (the harvester's normal replay path).

Key is read from OPENAI_API_KEY (env, else parsed from backend/.env). NEVER logged. This module is only
imported when a live solve is needed, so the harvester runs unchanged when no key is present.
"""
from __future__ import annotations

import base64
import os
import pathlib
import re

import httpx

_API = "https://api.openai.com/v1/chat/completions"
_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")
_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")


def _key() -> str | None:
    k = os.getenv("OPENAI_API_KEY")
    if k:
        return k.strip()
    # fall back to parsing backend/.env (the harvester process may not export it)
    try:
        env = pathlib.Path(__file__).resolve().parents[2] / ".env"
        for line in env.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def available() -> bool:
    return bool(_key())


def _post(model: str, content, max_tokens: int = 12, timeout: float = 60.0) -> str | None:
    key = _key()
    if not key:
        return None
    try:
        r = httpx.post(_API, timeout=timeout,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                             "messages": [{"role": "user", "content": content}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        # surface the reason once (status/text) without the key
        import logging
        logging.getLogger("assessment_harvester").info("[openai] solve failed: %s", str(exc)[:160])
        return None


def _img_data_url(image_path: str) -> str | None:
    try:
        raw = pathlib.Path(image_path).read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode()
    except Exception:
        return None


def solve_vision_mcq(image_path: str, n: int, question: str = "", odd_one_out: bool = False) -> int | None:
    """A screenshot of an N-option multiple-choice question (figural OR text/knowledge). Return the 0-based
    index of the answer the model picks. `odd_one_out` frames a non-verbal figural item; otherwise a normal
    'pick the correct answer' knowledge/reasoning item."""
    du = _img_data_url(image_path)
    if not du:
        return None
    if odd_one_out:
        task = (f"This is a non-verbal reasoning question with {n} figure options (labelled Option 1..{n}). "
                "Pick the odd-one-out / the figure that does not fit the rule, per the question shown. ")
    else:
        task = (f"This is a job-assessment multiple-choice question with {n} options. Read the question and "
                "all options carefully and pick the CORRECT answer. ")
    q = task + (f"Extra context: {question}. " if question else "") + \
        f"Reply with ONLY the option NUMBER (1 to {n}), nothing else."
    out = _post(_VISION_MODEL, [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": du, "detail": "high"}}], max_tokens=6)
    if not out:
        return None
    m = re.search(r"[1-9]\d?", out)
    if not m:
        return None
    idx = int(m.group()) - 1
    return idx if 0 <= idx < n else None


def solve_figural(image_path: str, n: int, prompt: str = "") -> int | None:
    """Back-compat wrapper: figural odd-one-out."""
    return solve_vision_mcq(image_path, n, prompt, odd_one_out=True)


def solve_text(question: str, options: list[str]) -> int | None:
    """A text MCQ (knowledge / verbal / numerical). Return the 0-based index of the correct option."""
    if not options:
        return None
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    q = ("Answer this job-assessment multiple-choice question correctly. "
         f"Question: {question}\n\nOptions:\n{numbered}\n\nReply with ONLY the number of the best option.")
    out = _post(_TEXT_MODEL, q, max_tokens=6)
    if not out:
        return None
    m = re.search(r"[1-9]\d?", out)
    if not m:
        return None
    idx = int(m.group()) - 1
    return idx if 0 <= idx < len(options) else None
