"""Anthropic (Claude) VISION/TEXT solver — the PREFERRED cognitive solver for the harvester (OpenAI hit
persistent 429 rate limits on the mass Harver run). Uses the Messages API with vision. Key from
ANTHROPIC_API_KEY (env, else parsed from backend/.env). NEVER logged. Imported only when a live solve is
needed; the adapter falls back to OpenAI/local when no key is present.
"""
from __future__ import annotations

import base64
import os
import pathlib
import re

import httpx

_API = "https://api.anthropic.com/v1/messages"
_MODEL = os.getenv("ANTHROPIC_VISION_MODEL", "claude-sonnet-5")


def _key() -> str | None:
    # DEDICATED var so the harvester's Claude usage never flips the résumé-tailoring / answer-drafting
    # paths (which read the project's ANTHROPIC_API_KEY and would leave the local Sumrak model if it were
    # set). Prefer HARVEST_ANTHROPIC_KEY; fall back to ANTHROPIC_API_KEY only if explicitly present.
    for var in ("HARVEST_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"):
        k = os.getenv(var)
        if k and k.strip():
            return k.strip()
    try:
        env = pathlib.Path(__file__).resolve().parents[2] / ".env"
        lines = env.read_text().splitlines()
        for var in ("HARVEST_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"):
            for line in lines:
                if line.startswith(var + "="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
    except Exception:
        pass
    return None


def available() -> bool:
    return bool(_key())


def _post(content, max_tokens: int = 12, timeout: float = 60.0) -> str | None:
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
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"},
                           json={"model": _MODEL, "max_tokens": max_tokens, "temperature": 0,
                                 "messages": [{"role": "user", "content": content}]})
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("retry-after", "") or 0)
                except ValueError:
                    wait = 0.0
                time.sleep(min((wait or min(2 ** attempt, 30)), 40) + random.uniform(0, 1.5))
                continue
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        except httpx.HTTPStatusError as e:
            sc = getattr(e.response, "status_code", None)
            if sc == 429 and attempt < 5:
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1.5))
                continue
            log.info("[anthropic] solve failed: %s", str(e)[:160])
            return None
        except Exception as exc:
            log.info("[anthropic] solve failed: %s", str(exc)[:160])
            return None
    log.info("[anthropic] gave up after 429 retries")
    return None


def _img_block(image_path: str):
    try:
        raw = pathlib.Path(image_path).read_bytes()
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(raw).decode()}}
    except Exception:
        return None


def solve_vision_mcq(image_path: str, n: int, question: str = "", odd_one_out: bool = False) -> int | None:
    """A screenshot of an N-option MCQ (figural OR text/knowledge). Return the 0-based index Claude picks."""
    img = _img_block(image_path)
    if not img:
        return None
    if odd_one_out:
        task = (f"a non-verbal reasoning question with {n} figure options (labelled Option 1..{n}); pick the "
                "odd-one-out / the figure that does not fit the rule, per the question shown.")
    else:
        task = (f"a multiple-choice question with {n} options; read the question and all options carefully "
                "and pick the CORRECT answer.")
    txt = (f"This screenshot shows {task} " + (f"Extra context: {question}. " if question else "")
           + f"Reply with ONLY the option NUMBER (1 to {n}), nothing else.")
    out = _post([img, {"type": "text", "text": txt}], max_tokens=8)
    if not out:
        return None
    m = re.search(r"[1-9]\d?", out)
    if not m:
        return None
    idx = int(m.group()) - 1
    return idx if 0 <= idx < n else None


def solve_text(question: str, options: list[str]) -> int | None:
    if not options:
        return None
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    txt = ("Answer this job-assessment multiple-choice question correctly. "
           f"Question: {question}\n\nOptions:\n{numbered}\n\nReply with ONLY the number of the best option.")
    out = _post([{"type": "text", "text": txt}], max_tokens=8)
    if not out:
        return None
    m = re.search(r"[1-9]\d?", out)
    if not m:
        return None
    idx = int(m.group()) - 1
    return idx if 0 <= idx < len(options) else None
