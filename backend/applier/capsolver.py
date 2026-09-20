"""CapSolver API client — the PAID escalation tier for the HARD captcha variants.

`createTask` + `getTaskResult` against https://api.capsolver.com, with a BOUNDED
exponential-backoff poll (never a tight retry loop — see the external-API rule in
CLAUDE.md). Every failure path returns None and never raises, so a caller can wrap it
unconditionally.

**No-op without a key.** The whole module is inert unless `CAPTCHA_SOLVER_KEY` is set:
`solve(...)`/`run_task(...)` return None immediately when the key is unset, BEFORE any
network client is constructed — nothing is ever spent unless a key is present AND a
challenge was actually detected + a task built. This is the paid tier that sits BEHIND
the free paths (the in-browser NopeCHA extension for reCAPTCHA/hCaptcha/Turnstile, and
`captcha_solver.solve_aws_waf`'s free `AwsWafIntegration.getToken()` for AWS WAF).

Supported CapSolver task types (all *ProxyLess* — the token binds to sitekey+URL, not to
our browser IP, and our page traffic already egresses through our own proxy):

  challenge                 task type                             solution field
  --------------------------------------------------------------------------------------
  turnstile                 AntiTurnstileTaskProxyLess            solution.token
  hcaptcha (+enterprise)    HCaptchaTaskProxyLess                 solution.gRecaptchaResponse
  recaptcha_v2              ReCaptchaV2TaskProxyLess              solution.gRecaptchaResponse
  recaptcha_v2 enterprise   ReCaptchaV2EnterpriseTaskProxyLess    solution.gRecaptchaResponse
  recaptcha_v3              ReCaptchaV3TaskProxyLess              solution.gRecaptchaResponse
  recaptcha_v3 enterprise   ReCaptchaV3EnterpriseTaskProxyLess    solution.gRecaptchaResponse
  aws_waf                   AntiAwsWafTaskProxyLess               solution.cookie

hCaptcha enterprise is the base task type PLUS an `enterprisePayload` (e.g. `{"rqdata": …}`);
reCAPTCHA enterprise has its own task type. Casing is CapSolver-documented (`ProxyLess`,
capital L) — verified against docs.capsolver.com.

Cost (CapSolver, 2026-09): AWS WAF $2.0/1k · Turnstile $1.2/1k · reCAPTCHA v2 $0.8/1k ·
v3 $1.0/1k · hCaptcha ~$1.5/1k. Owner funds the CapSolver balance in the dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.capsolver.com"
_HTTP_TIMEOUT = 30.0
# Bounded exponential-backoff poll: sleep grows _POLL_START -> ×2 -> capped at _POLL_CEIL,
# with a hard wall-clock deadline _POLL_MAX. CapSolver resolves most tasks in ~5-40s.
_POLL_START = 2.0
_POLL_CEIL = 10.0
_POLL_MAX = 120.0

_AWS_WAF_TYPE = "AntiAwsWafTaskProxyLess"

# challenge kind -> CapSolver task type. Enterprise reCAPTCHA maps to its own suffix via
# resolve_task_type(); hCaptcha enterprise stays this type + an enterprisePayload.
_TASK_TYPE = {
    "turnstile": "AntiTurnstileTaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "recaptcha_v2_enterprise": "ReCaptchaV2EnterpriseTaskProxyLess",
    "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
    "recaptcha_v3_enterprise": "ReCaptchaV3EnterpriseTaskProxyLess",
    "aws_waf": _AWS_WAF_TYPE,
}


def _key() -> str:
    return (os.getenv("CAPTCHA_SOLVER_KEY") or "").strip()


def is_enabled() -> bool:
    """True only when a CapSolver API key is configured — every call no-ops otherwise."""
    return bool(_key())


def _client() -> httpx.AsyncClient:
    """The HTTP client factory (a seam tests monkeypatch to run network-free)."""
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)


async def _sleep(secs: float) -> None:
    """Indirection so tests can capture the backoff schedule without real waiting."""
    await asyncio.sleep(secs)


def resolve_task_type(challenge: str, enterprise: bool = False) -> str | None:
    """CapSolver task-type string for a challenge kind (None if unsupported)."""
    if enterprise and challenge in ("recaptcha_v2", "recaptcha_v3"):
        return _TASK_TYPE.get(f"{challenge}_enterprise")
    return _TASK_TYPE.get(challenge)


def build_task(challenge: str, *, page_url: str, site_key: str | None = None,
               enterprise: bool = False, action: str | None = None,
               enterprise_payload: dict | None = None, is_invisible: bool = False,
               min_score: float | None = None, aws_key: str | None = None,
               aws_iv: str | None = None, aws_context: str | None = None,
               aws_challenge_js: str | None = None) -> dict | None:
    """Build the CapSolver `task` dict for one challenge. Returns None when the challenge
    is unsupported or a required field (page URL / site key) is missing. Pure — unit-tested
    for task-type selection + per-type field shape."""
    if not page_url:
        return None
    if challenge == "aws_waf":
        task: dict = {"type": _AWS_WAF_TYPE, "websiteURL": page_url}
        if aws_key:
            task["awsKey"] = aws_key
        if aws_iv:
            task["awsIv"] = aws_iv
        if aws_context:
            task["awsContext"] = aws_context
        if aws_challenge_js:
            task["awsChallengeJS"] = aws_challenge_js
        return task
    ttype = resolve_task_type(challenge, enterprise)
    if not ttype or not site_key:
        return None
    task = {"type": ttype, "websiteURL": page_url, "websiteKey": site_key}
    if challenge == "recaptcha_v3":
        task["pageAction"] = action or "verify"
        task["minScore"] = 0.7 if min_score is None else min_score
    if challenge == "hcaptcha":
        if is_invisible:
            task["isInvisible"] = True
        if enterprise_payload:
            task["enterprisePayload"] = enterprise_payload
    elif challenge == "recaptcha_v2" and enterprise and enterprise_payload:
        task["enterprisePayload"] = enterprise_payload
    return task


def _extract_token(challenge: str, solution: dict | None) -> str | None:
    """Pull the right token out of a CapSolver `solution` dict for `challenge`."""
    sol = solution or {}
    if challenge == "aws_waf":
        return sol.get("cookie") or sol.get("token")
    if challenge == "turnstile":
        return sol.get("token") or sol.get("gRecaptchaResponse")
    return sol.get("gRecaptchaResponse") or sol.get("token")


async def run_task(task: dict | None, *, poll_max: float | None = None) -> dict | None:
    """createTask then poll getTaskResult until ready, with bounded exponential backoff.
    Returns the full `solution` dict, or None (disabled / no task / API error / timeout).
    Never raises."""
    key = _key()
    if not key or not task:
        return None
    deadline = _POLL_MAX if poll_max is None else poll_max
    try:
        async with _client() as cx:
            resp = await cx.post(f"{_BASE}/createTask",
                                 json={"clientKey": key, "task": task})
            data = resp.json()
            if data.get("errorId"):
                logger.warning("capsolver createTask error: %s / %s",
                               data.get("errorCode"), data.get("errorDescription"))
                return None
            task_id = data.get("taskId")
            if not task_id:
                # A rare synchronous resolution returns the solution inline.
                sol = data.get("solution")
                return sol if sol else None
            delay = _POLL_START
            waited = 0.0
            while waited < deadline:
                await _sleep(delay)
                waited += delay
                rr = await cx.post(f"{_BASE}/getTaskResult",
                                   json={"clientKey": key, "taskId": task_id})
                jr = rr.json()
                if jr.get("errorId"):
                    logger.warning("capsolver getTaskResult error: %s / %s",
                                   jr.get("errorCode"), jr.get("errorDescription"))
                    return None
                if jr.get("status") == "ready":
                    return jr.get("solution") or {}
                # 'idle' / 'processing' -> keep polling; grow the delay, capped at the ceiling.
                delay = min(delay * 2, _POLL_CEIL)
    except Exception as exc:
        logger.warning("capsolver task failed: %s", exc)
        return None
    logger.warning("capsolver task timed out after ~%ss", deadline)
    return None


async def solve(challenge: str, *, page_url: str, site_key: str | None = None,
                enterprise: bool = False, action: str | None = None,
                enterprise_payload: dict | None = None, is_invisible: bool = False,
                min_score: float | None = None, aws_key: str | None = None,
                aws_iv: str | None = None, aws_context: str | None = None,
                aws_challenge_js: str | None = None) -> str | None:
    """Solve one captcha of `challenge` and return the token string (the cookie value for
    `aws_waf`), or None. A pure no-op (None, no network) when the key is unset, the
    challenge is unsupported, or a required field is missing. Never raises."""
    if not is_enabled():
        return None
    task = build_task(challenge, page_url=page_url, site_key=site_key, enterprise=enterprise,
                      action=action, enterprise_payload=enterprise_payload,
                      is_invisible=is_invisible, min_score=min_score, aws_key=aws_key,
                      aws_iv=aws_iv, aws_context=aws_context, aws_challenge_js=aws_challenge_js)
    if not task:
        return None
    sol = await run_task(task)
    if not sol:
        return None
    return _extract_token(challenge, sol)
