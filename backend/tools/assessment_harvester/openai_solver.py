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
_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")  # higher rate limits + cheaper; ok for these MCQs
_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")

# Set once a 429 proves the account is out of quota/credit (insufficient_quota) — NOT a transient rate
# limit. After that available()=False so the solver cascade skips OpenAI instantly instead of burning the
# full 6-retry ~65s backoff per item on a quota error that retrying can never clear (that backoff was
# slowing the whole lane ~10x once credits ran out).
_DEAD = False


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
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                           json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                                 "messages": [{"role": "user", "content": content}]})
            if r.status_code == 429:
                # Distinguish a real rate limit (retry helps) from an out-of-credit quota error (retry is
                # useless). On insufficient_quota / credit_balance_exhausted, disable OpenAI for the rest
                # of the process so the cascade returns a placeholder INSTANTLY instead of after ~65s.
                body = ""
                try:
                    err = (r.json() or {}).get("error", {})
                    body = f"{err.get('type', '')} {err.get('code', '')} {err.get('message', '')}".lower()
                except Exception:
                    body = (r.text or "").lower()
                if ("insufficient_quota" in body or "credit_balance_exhausted" in body
                        or "no credits" in body or "exceeded your current quota" in body):
                    global _DEAD
                    if not _DEAD:
                        log.info("[openai] disabled: out of credit (insufficient_quota)")
                    _DEAD = True
                    return None
                # rate-limited (many parallel workers) — back off (honor Retry-After) and retry so the
                # answer is the CORRECT vision pick, not a placeholder. Self-throttles the whole lane.
                try:
                    wait = float(r.headers.get("retry-after", "") or 0)
                except ValueError:
                    wait = 0.0
                wait = wait or min(2 ** attempt, 30)
                time.sleep(min(wait, 40) + random.uniform(0, 1.5))
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            sc = getattr(e.response, "status_code", None)
            if sc == 429 and attempt < 5:
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1.5))
                continue
            log.info("[openai] solve failed: %s", str(e)[:160])
            return None
        except Exception as exc:
            log.info("[openai] solve failed: %s", str(exc)[:160])
            return None
    log.info("[openai] gave up after 429 retries")
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
