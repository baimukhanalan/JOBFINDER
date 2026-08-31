"""Captcha-solving via a third-party service (CapSolver / 2Captcha).

Removes the "a human must solve a live captcha" barrier for the needs_laptop ATS families
(Workday reCAPTCHA, Kelly/Akamai, iCIMS/Taleo, SmartRecruiters) so their SUBMIT can run
unattended from the server — combined with a residential proxy for the IP-reputation part.
It does NOT and CANNOT solve a human video/voice assessment (HireVue/Versant/Amazon VJT).

Config (env, all optional — absent key => disabled, every call is a graceful no-op):
  CAPTCHA_SOLVER_PROVIDER  capsolver (default) | twocaptcha
  CAPTCHA_SOLVER_KEY       the provider API key
Supported: reCAPTCHA v2, reCAPTCHA v3, hCaptcha, Cloudflare Turnstile. NOT DataDome/PerimeterX
(those need a full residential/browser-fingerprint path, out of scope here).

The module is import-safe and side-effect-free until `solve_on_page`/`solve_*` is called with
a key present. All network calls are best-effort and never raise into the caller.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_CAPSOLVER_BASE = "https://api.capsolver.com"
_TWOCAPTCHA_BASE = "https://api.2captcha.com"
_POLL_INTERVAL = 3.0
_POLL_MAX = 120.0        # captchas usually resolve in 10-40s


def _provider() -> str:
    return (os.getenv("CAPTCHA_SOLVER_PROVIDER") or "capsolver").strip().lower()


def _key() -> str:
    return (os.getenv("CAPTCHA_SOLVER_KEY") or "").strip()


def is_enabled() -> bool:
    """True only when a provider API key is configured — callers no-op otherwise."""
    return bool(_key())


# ---- CapSolver task types ------------------------------------------------------
# ProxyLess variants: the solver uses its own IP for the token. That is correct here — the
# token is bound to the site key + page URL, not to the browser IP, and our page traffic
# already goes through our own (residential) proxy.
_CAPSOLVER_TASK = {
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
}
# 2Captcha "method" per kind (in/out API).
_TWOCAPTCHA_METHOD = {
    "recaptcha_v2": "userrecaptcha",
    "recaptcha_v3": "userrecaptcha",
    "hcaptcha": "hcaptcha",
    "turnstile": "turnstile",
}


async def _capsolver_solve(kind: str, site_key: str, page_url: str,
                           action: str | None = None) -> str | None:
    task = {"type": _CAPSOLVER_TASK[kind], "websiteURL": page_url, "websiteKey": site_key}
    if kind == "recaptcha_v3":
        task["pageAction"] = action or "verify"
        task["minScore"] = 0.7
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(f"{_CAPSOLVER_BASE}/createTask",
                          json={"clientKey": _key(), "task": task})
        j = r.json()
        if j.get("errorId"):
            logger.warning("capsolver createTask error: %s", j.get("errorDescription"))
            return None
        task_id = j.get("taskId")
        if not task_id:
            return None
        import asyncio
        waited = 0.0
        while waited < _POLL_MAX:
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            rr = await cx.post(f"{_CAPSOLVER_BASE}/getTaskResult",
                               json={"clientKey": _key(), "taskId": task_id})
            jr = rr.json()
            if jr.get("errorId"):
                logger.warning("capsolver getTaskResult error: %s", jr.get("errorDescription"))
                return None
            if jr.get("status") == "ready":
                sol = jr.get("solution") or {}
                return sol.get("gRecaptchaResponse") or sol.get("token")
    logger.warning("capsolver timed out after %ss", _POLL_MAX)
    return None


async def _twocaptcha_solve(kind: str, site_key: str, page_url: str,
                            action: str | None = None) -> str | None:
    params = {"key": _key(), "method": _TWOCAPTCHA_METHOD[kind], "json": 1,
              "pageurl": page_url}
    if kind in ("recaptcha_v2", "recaptcha_v3"):
        params["googlekey"] = site_key
        if kind == "recaptcha_v3":
            params["version"] = "v3"
            params["action"] = action or "verify"
    elif kind == "hcaptcha":
        params["sitekey"] = site_key
    elif kind == "turnstile":
        params["sitekey"] = site_key
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.get(f"{_TWOCAPTCHA_BASE}/in.php", params=params)
        j = r.json()
        if str(j.get("status")) != "1":
            logger.warning("2captcha in.php error: %s", j.get("request"))
            return None
        cap_id = j.get("request")
        import asyncio
        waited = 0.0
        while waited < _POLL_MAX:
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            rr = await cx.get(f"{_TWOCAPTCHA_BASE}/res.php",
                              params={"key": _key(), "action": "get", "id": cap_id, "json": 1})
            jr = rr.json()
            if str(jr.get("status")) == "1":
                return jr.get("request")
            if jr.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                logger.warning("2captcha res.php error: %s", jr.get("request"))
                return None
    return None


async def solve(kind: str, site_key: str, page_url: str, action: str | None = None) -> str | None:
    """Solve one captcha of `kind` ∈ {recaptcha_v2,recaptcha_v3,hcaptcha,turnstile}. Returns the
    token, or None (disabled / error / timeout). Never raises."""
    if not is_enabled() or kind not in _CAPSOLVER_TASK or not site_key or not page_url:
        return None
    try:
        if _provider() == "twocaptcha":
            return await _twocaptcha_solve(kind, site_key, page_url, action)
        return await _capsolver_solve(kind, site_key, page_url, action)
    except Exception as exc:
        logger.warning("captcha solve failed (%s): %s", kind, exc)
        return None


# ---- on-page detection + token injection (Playwright) --------------------------
# Pure DOM probe: which captcha (if any) is rendered, and its site key. Returns (kind, key).
_DETECT_JS = r"""() => {
  const q = s => document.querySelector(s);
  // Cloudflare Turnstile
  let el = q('.cf-turnstile[data-sitekey], [data-sitekey].cf-turnstile') ||
           q('iframe[src*="challenges.cloudflare.com"]')?.closest('[data-sitekey]');
  if (el && el.getAttribute('data-sitekey')) return {kind:'turnstile', key:el.getAttribute('data-sitekey')};
  // hCaptcha
  el = q('.h-captcha[data-sitekey], [data-hcaptcha-sitekey]');
  if (el) return {kind:'hcaptcha', key: el.getAttribute('data-sitekey')||el.getAttribute('data-hcaptcha-sitekey')};
  // reCAPTCHA v2 (visible checkbox)
  el = q('.g-recaptcha[data-sitekey], [data-sitekey].g-recaptcha');
  if (el && el.getAttribute('data-sitekey')) return {kind:'recaptcha_v2', key:el.getAttribute('data-sitekey')};
  // reCAPTCHA v3 (invisible) — key is in the api.js script ?render=<key>
  const s = [...document.querySelectorAll('script[src*="recaptcha/api.js"]')]
    .map(x => (x.src.match(/[?&]render=([^&]+)/)||[])[1]).find(Boolean);
  if (s && s !== 'explicit') return {kind:'recaptcha_v3', key: s};
  return null;
}"""

_INJECT_JS = r"""([kind, token]) => {
  const set = (sel) => { const t = document.querySelector(sel);
    if (t) { t.value = token; t.dispatchEvent(new Event('input',{bubbles:true}));
             t.dispatchEvent(new Event('change',{bubbles:true})); return true; } return false; };
  let ok = false;
  if (kind === 'turnstile') {
    ok = set('input[name="cf-turnstile-response"]') | set('[name="cf-turnstile-response"]');
  } else if (kind === 'hcaptcha') {
    ok = set('textarea[name="h-captcha-response"]') | set('textarea[name="g-recaptcha-response"]');
  } else {
    // reCAPTCHA v2/v3: the response textarea (v2 hidden) — create it if missing.
    let t = document.querySelector('textarea#g-recaptcha-response, textarea[name="g-recaptcha-response"]');
    if (!t) { t = document.createElement('textarea'); t.id='g-recaptcha-response';
              t.name='g-recaptcha-response'; t.style.display='none'; document.body.appendChild(t); }
    t.value = token; t.dispatchEvent(new Event('change',{bubbles:true})); ok = true;
  }
  return !!ok;
}"""


async def detect_captcha(page):
    """(kind, site_key) of the captcha rendered on `page`, or None. Never raises."""
    try:
        info = await page.evaluate(_DETECT_JS)
    except Exception:
        return None
    if info and info.get("kind") and info.get("key"):
        return info["kind"], info["key"]
    return None


async def solve_on_page(page, action: str | None = None) -> bool:
    """Detect a captcha on `page`, solve it via the service, and inject the token. Returns True
    only if a captcha was found AND solved AND injected. A graceful no-op (False) when the
    solver is disabled, no captcha is present, or anything fails — so it is always safe to call
    right before/after a submit click. Never raises."""
    if not is_enabled():
        return False
    det = await detect_captcha(page)
    if not det:
        return False
    kind, site_key = det
    try:
        page_url = page.url
    except Exception:
        page_url = ""
    token = await solve(kind, site_key, page_url, action=action)
    if not token:
        logger.info("captcha (%s) present but not solved", kind)
        return False
    try:
        await page.evaluate(_INJECT_JS, [kind, token])
        logger.info("captcha (%s) solved + injected", kind)
        return True
    except Exception as exc:
        logger.warning("captcha token injection failed: %s", exc)
        return False


# ---- AWS WAF CAPTCHA (Amazon Passport / account.amazon.jobs) -------------------
# Amazon's corporate ATS gates account-creation (and sometimes the apply submit) with an
# AWS WAF CAPTCHA (captcha-sdk.awswaf.com jsapi.js; window.AwsWafCaptcha.renderCaptcha).
# It is Amazon-proprietary — NOT reCAPTCHA/hCaptcha/Turnstile — so it is a SEPARATE task
# type from _CAPSOLVER_TASK above (kept out of that dict on purpose; a token here is a
# cookie, not a g-recaptcha-response). CapSolver mints it with AntiAwsWafTask; the token is
# short-lived, so solve it right before the register/submit request.
_CAPSOLVER_AWS_WAF_TASK = "AntiAwsWafTaskProxyLess"

# Which AWS WAF challenge is on the page + its challenge props. AWS WAF exposes the
# key/iv/context the solver needs via window.gokuProps (present on the interactive shell);
# an interactive puzzle rendered by the SDK may expose none — CapSolver can still mint a
# token from the websiteURL alone, so a null props set is not a hard failure.
_AWS_WAF_DETECT_JS = r"""() => {
  const sdk = [...document.querySelectorAll('script[src*="captcha-sdk.awswaf.com"]')][0];
  const box = document.querySelector('.captcha-container, [class*="captcha-container"], '
              + '#captcha-container, [id*="awswaf" i]');
  const shell = /(verify you are human|are you a human|solve this puzzle)/i
                .test((document.body && document.body.innerText || '').slice(0, 4000));
  if (!sdk && !box && !shell) return null;
  const g = window.gokuProps
        || (window.AwsWafIntegration && window.AwsWafIntegration.gokuProps)
        || (window.awsWafCookieDomainList ? {} : null) || {};
  return { present: true, key: g.key || null, iv: g.iv || null, context: g.context || null };
}"""


async def _capsolver_solution(task: dict) -> dict | None:
    """Run one CapSolver task and return its full `solution` dict (or None on
    disabled/error/timeout). Separate from _capsolver_solve — the AWS WAF solution is a
    cookie, not a gRecaptchaResponse token, so the caller extracts the right field."""
    if not is_enabled():
        return None
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.post(f"{_CAPSOLVER_BASE}/createTask",
                          json={"clientKey": _key(), "task": task})
        j = r.json()
        if j.get("errorId"):
            logger.warning("capsolver createTask error: %s", j.get("errorDescription"))
            return None
        task_id = j.get("taskId")
        if not task_id:
            return None
        import asyncio
        waited = 0.0
        while waited < _POLL_MAX:
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            rr = await cx.post(f"{_CAPSOLVER_BASE}/getTaskResult",
                               json={"clientKey": _key(), "taskId": task_id})
            jr = rr.json()
            if jr.get("errorId"):
                logger.warning("capsolver getTaskResult error: %s", jr.get("errorDescription"))
                return None
            if jr.get("status") == "ready":
                return jr.get("solution") or {}
    logger.warning("capsolver aws-waf timed out after %ss", _POLL_MAX)
    return None


async def solve_aws_waf(page) -> bool:
    """Detect an AWS WAF CAPTCHA on `page`, solve it via CapSolver's AntiAwsWafTask, and inject
    the resulting `aws-waf-token` cookie onto the browser context. Returns True only if a
    challenge was found AND solved AND the token injected. A graceful no-op (False) when the
    solver is disabled, no AWS WAF challenge is present, the provider is 2captcha (its Amazon-WAF
    method is not wired here), or anything fails — so it is always safe to call before a register
    or submit click. Never raises.

    The token is IP-bound: solve it through the SAME (residential) session the page uses, and do
    it immediately before the gated request — it expires fast."""
    if not is_enabled():
        return False
    if _provider() == "twocaptcha":
        logger.info("aws-waf: 2captcha provider not wired for AWS WAF; skipping")
        return False
    try:
        info = await page.evaluate(_AWS_WAF_DETECT_JS)
    except Exception:
        return False
    if not info or not info.get("present"):
        return False
    try:
        page_url = page.url
    except Exception:
        page_url = ""
    if not page_url:
        return False
    task = {"type": _CAPSOLVER_AWS_WAF_TASK, "websiteURL": page_url}
    if info.get("key"):
        task["awsKey"] = info["key"]
    if info.get("iv"):
        task["awsIv"] = info["iv"]
    if info.get("context"):
        task["awsContext"] = info["context"]
    try:
        sol = await _capsolver_solution(task)
    except Exception as exc:
        logger.warning("aws-waf solve failed: %s", exc)
        return False
    token = (sol or {}).get("cookie") or (sol or {}).get("token") if sol else None
    if isinstance(token, str) and token.startswith("aws-waf-token="):
        token = token.split("=", 1)[1]
    if not token:
        logger.info("aws-waf captcha present but not solved")
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(page_url).hostname or "").lower()
        # Scope the cookie domain-wide when on *.amazon.jobs so the token also satisfies the
        # account.amazon.jobs / passport.amazon.jobs XHRs the SPA fires.
        domain = ".amazon.jobs" if host.endswith("amazon.jobs") else host
        await page.context.add_cookies(
            [{"name": "aws-waf-token", "value": token, "domain": domain, "path": "/"}])
        logger.info("aws-waf captcha solved + token injected")
        return True
    except Exception as exc:
        logger.warning("aws-waf token injection failed: %s", exc)
        return False
