"""Live VISION/TEXT solver via the OpenRouter API — the PREFERRED cognitive solver for the harvester
(the direct OpenAI account is low on credits). OpenRouter is OpenAI-compatible (chat/completions with
`image_url` blocks), so this mirrors openai_solver but points at OpenRouter and reads a DEDICATED key.

Key from HARVEST_OPENROUTER_KEY (env, else parsed from backend/.env). NEVER logged. Imported only when a
live solve is needed; the adapter falls back to Anthropic/OpenAI/local when no key is present.
"""
from __future__ import annotations

import base64
import os
import pathlib
import re

import httpx

_API = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter model IDs are "<vendor>/<model>". gpt-4o-mini keeps parity with the proven OpenAI path
# (cheap + vision); override with OPENROUTER_VISION_MODEL / OPENROUTER_TEXT_MODEL for stronger models
# (e.g. openai/gpt-4o, google/gemini-2.0-flash-001, anthropic/claude-sonnet-4).
_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "openai/gpt-4o-mini")
_TEXT_MODEL = os.getenv("OPENROUTER_TEXT_MODEL", "openai/gpt-4o-mini")

# Set once a 402/401 proves the account is unfunded/invalid — after that `available()` is False so the
# adapter's solver cascade skips OpenRouter (and falls through to the next funded solver) instead of
# burning a doomed call on every item.
_DEAD = False


def _key() -> str | None:
    k = os.getenv("HARVEST_OPENROUTER_KEY")
    if k and k.strip():
        return k.strip()
    # fall back to parsing backend/.env (the harvester process may not export it)
    try:
        env = pathlib.Path(__file__).resolve().parents[2] / ".env"
        for line in env.read_text().splitlines():
            if line.startswith("HARVEST_OPENROUTER_KEY="):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v
    except Exception:
        pass
    return None


def available() -> bool:
    return bool(_key()) and not _DEAD


def _post(model: str, content, max_tokens: int = 12, timeout: float = 60.0) -> str | None:
    key = _key()
    if not key:
        return None
    import logging
    import random
    import time
    log = logging.getLogger("assessment_harvester")
    for attempt in range(6):
        try:
            r = httpx.post(_API, timeout=timeout,
                           headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json",
                                    # optional OpenRouter attribution headers (neutral, no stack disclosure)
                                    "HTTP-Referer": "https://jobs.systeam.kz",
                                    "X-Title": "assessment-harvester"},
                           json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                                 "messages": [{"role": "user", "content": content}]})
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("retry-after", "") or 0)
                except ValueError:
                    wait = 0.0
                wait = wait or min(2 ** attempt, 30)
                time.sleep(min(wait, 40) + random.uniform(0, 1.5))
                continue
            if r.status_code in (401, 402):
                # unfunded (402 Payment Required) or invalid key (401) — retrying can't help. Disable
                # OpenRouter for the rest of this process so the cascade falls through to another solver.
                global _DEAD
                if not _DEAD:
                    log.info("[openrouter] disabled: HTTP %s (account unfunded / key invalid)", r.status_code)
                _DEAD = True
                return None
            r.raise_for_status()
            data = r.json()
            # OpenRouter mirrors the OpenAI shape; guard against a provider error body
            ch = (data.get("choices") or [])
            if not ch:
                log.info("[openrouter] no choices: %s", str(data)[:160])
                return None
            return ch[0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            sc = getattr(e.response, "status_code", None)
            if sc == 429 and attempt < 5:
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1.5))
                continue
            log.info("[openrouter] solve failed: %s", str(e)[:160])
            return None
        except Exception as exc:
            log.info("[openrouter] solve failed: %s", str(exc)[:160])
            return None
    log.info("[openrouter] gave up after 429 retries")
    return None


def _img_data_url(image_path: str) -> str | None:
    try:
        raw = pathlib.Path(image_path).read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode()
    except Exception:
        return None


def solve_vision_mcq(image_path: str, n: int, question: str = "", odd_one_out: bool = False) -> int | None:
    """A screenshot of an N-option MCQ (figural OR text/knowledge). Return the 0-based index picked."""
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
