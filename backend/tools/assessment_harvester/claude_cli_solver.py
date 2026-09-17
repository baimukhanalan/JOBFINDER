"""Solve cognitive MCQs by shelling out to the LOCALLY INSTALLED `claude` CLI in headless print mode
(`claude -p ... --allowedTools Read`), instead of a paid vision API. This runs on the machine's Claude
subscription (on-disk credentials), so it needs NO OPENAI/OPENROUTER/GEMINI credit — the reason it's the
top of the solver cascade when enabled.

Cost/quality knobs (env): HARVEST_CLAUDE_SOLVER=1 enables it; HARVEST_CLAUDE_MODEL picks the model
(default the fast/cheap haiku; set to a sonnet id for harder figural reasoning at higher subscription
usage). Each call spawns a fresh `claude -p` agent (~10-15s), so this is slower than an API but free.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

def _env(name: str, default: str = "") -> str:
    """Read a setting from the environment, else from backend/.env — so the mass run can be toggled live
    (a fresh candidate subprocess re-reads .env at import) without restarting the orchestrator."""
    v = os.getenv(name)
    if v is not None and v != "":
        return v
    try:
        for line in (pathlib.Path(__file__).resolve().parents[2] / ".env").read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def _model() -> str:
    return _env("HARVEST_CLAUDE_MODEL", "claude-haiku-4-5-20251001")


def _timeout() -> float:
    try:
        return float(_env("HARVEST_CLAUDE_TIMEOUT", "90"))
    except ValueError:
        return 90.0


def _bin() -> str | None:
    b = shutil.which("claude")
    if b:
        return b
    for c in (os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude"):
        if os.path.exists(c):
            return c
    return None


def available() -> bool:
    # OPT-IN: the mass run only uses this when explicitly enabled (it draws on the Claude subscription's
    # usage quota, thousands of calls over a full run), and only if the CLI is actually installed.
    return _env("HARVEST_CLAUDE_SOLVER") == "1" and bool(_bin())


def _run(prompt: str, timeout: float | None = None) -> str | None:
    b = _bin()
    if not b:
        return None
    import logging
    log = logging.getLogger("assessment_harvester")
    env = dict(os.environ)
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    try:
        p = subprocess.run(
            [b, "-p", prompt, "--allowedTools", "Read", "--model", _model()],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=timeout or _timeout(), text=True)
        out = (p.stdout or "").strip()
        if not out:
            log.info("[claude-cli] empty output (rc=%s): %s", p.returncode, (p.stderr or "")[:120])
        return out or None
    except subprocess.TimeoutExpired:
        log.info("[claude-cli] timeout after %.0fs", timeout)
        return None
    except Exception as exc:
        log.info("[claude-cli] failed: %s", str(exc)[:140])
        return None


def _pick_num(out: str | None, n: int) -> int | None:
    if not out:
        return None
    # the model is asked to reply with ONLY the number; take the first 1-2 digit token as the answer
    m = re.search(r"[1-9]\d?", out)
    if not m:
        return None
    idx = int(m.group()) - 1
    return idx if 0 <= idx < n else None


def solve_vision_mcq(image_path: str, n: int, question: str = "", odd_one_out: bool = False) -> int | None:
    if not pathlib.Path(image_path).exists():
        return None
    if odd_one_out:
        task = (f"a non-verbal reasoning question with {n} figure options (labelled Option 1..{n}); pick "
                "the odd-one-out / the figure that does not fit the rule, per the question shown")
    else:
        task = (f"a multiple-choice question with {n} options; read the question and all options and pick "
                "the CORRECT answer")
    prompt = (f"Read the image file {image_path}. It shows {task}. "
              + (f"Extra context: {question}. " if question else "")
              + f"Reply with ONLY the option NUMBER (1 to {n}), nothing else.")
    return _pick_num(_run(prompt), n)


def solve_figural(image_path: str, n: int, prompt: str = "") -> int | None:
    return solve_vision_mcq(image_path, n, prompt, odd_one_out=True)


def solve_text(question: str, options: list[str]) -> int | None:
    if not options:
        return None
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    prompt = ("Answer this job-assessment multiple-choice question correctly. "
              f"Question: {question}\n\nOptions:\n{numbered}\n\n"
              "Reply with ONLY the number of the best option, nothing else.")
    return _pick_num(_run(prompt), len(options))
