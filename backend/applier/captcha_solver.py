"""Captcha-solving via a third-party service (CapSolver / 2Captcha).

Removes the "a human must solve a live captcha" barrier for the needs_laptop ATS families
(Workday reCAPTCHA, Kelly/Akamai, iCIMS/Taleo, SmartRecruiters, Amazon AWS-WAF) so their
SUBMIT can run unattended from the server — combined with a residential proxy for the
IP-reputation part. It does NOT and CANNOT solve a human video/voice assessment
(HireVue/Versant/Amazon VJT).

**Escalation order — FREE path first, PAID CapSolver tier behind it:**
  * reCAPTCHA / hCaptcha / Turnstile: the in-browser **NopeCHA extension** runs in the page
    FIRST where it is armed (co-pilot `COPILOT_NOPECHA`, iCIMS `ICIMS_NOPECHA`) — this module
    is NOT that path. `solve_on_page` is the PAID escalation for when a challenge is still
    present + `CAPTCHA_SOLVER_KEY` is set (e.g. Cloudflare *Managed* Turnstile / *invisible
    enterprise* hCaptcha, which NopeCHA has proven unable to clear).
  * AWS WAF: `solve_aws_waf` tries the FREE `AwsWafIntegration.getToken()` browser path first
    (AWSWAF_BROWSER=1) and only falls through to the PAID `AntiAwsWafTask` for a hard visual
    puzzle.
  * **FriendlyCaptcha** (a proof-of-work widget, `.frc-captcha`/`bluex-friendly-captcha`, on e.g.
    the Randstad résumé drop): **2captcha-ONLY** — CapSolver has no FriendlyCaptcha task type and
    NopeCHA can't touch it. `solve('friendlycaptcha', …)` / `solve_on_page` route it to 2captcha
    (in.php `method=friendlycaptcha`, sitekey+pageurl → the solution token) REGARDLESS of
    `CAPTCHA_SOLVER_PROVIDER`, funded by `_twocaptcha_key()` (`TWOCAPTCHA_KEY` else
    `CAPTCHA_SOLVER_KEY`). The token is injected into the hidden `frc-captcha-solution` field.

Config (env, all optional — absent key => the PAID tier is disabled, a graceful no-op):
  CAPTCHA_SOLVER_PROVIDER  capsolver (default) | twocaptcha
  CAPTCHA_SOLVER_KEY       the provider (CapSolver) API key — funds the paid solves
  AWSWAF_BROWSER           1/true => the FREE, no-key AWS WAF path (solve_aws_waf lets the page's
                           OWN AWS WAF SDK mint the token — no external service). Default off.
Supported (CapSolver): reCAPTCHA v2 (+enterprise), reCAPTCHA v3 (+enterprise), hCaptcha
(+enterprise), Cloudflare Turnstile, AWS WAF. NOT DataDome/PerimeterX (those need a full
residential/browser-fingerprint path, out of scope here).

Cost note (CapSolver, 2026-09): AWS WAF $2.0/1k · Turnstile $1.2/1k · reCAPTCHA v2 $0.8/1k ·
v3 $1.0/1k · hCaptcha ~$1.5/1k. There is NO free/open solver for Cloudflare *Managed*
Turnstile or an *invisible enterprise* hCaptcha — those gate on Cloudflare/hCaptcha
server-side browser-integrity + IP reputation, so a paid API (its own residential browser
farm) is the cheapest working path. The AWS WAF *challenge* (silent JS proof-of-work) is the
exception: a real browser mints the `aws-waf-token` itself for FREE (AWSWAF_BROWSER) — only a
hard visual WAF puzzle needs the paid AntiAwsWafTask.

The module is import-safe and side-effect-free until `solve_on_page`/`solve_*` is called with
a key present. All network calls are best-effort and never raise into the caller. The
CapSolver HTTP client (createTask/getTaskResult + bounded exponential-backoff poll) lives in
`backend.applier.capsolver`, which this module delegates to.
"""
from __future__ import annotations

import logging
import os

import httpx

from backend.applier import capsolver as _capsolver

logger = logging.getLogger(__name__)

_TWOCAPTCHA_BASE = "https://api.2captcha.com"
_POLL_INTERVAL = 3.0
_POLL_MAX = 120.0        # captchas usually resolve in 10-40s (2captcha poll only)


def _provider() -> str:
    return (os.getenv("CAPTCHA_SOLVER_PROVIDER") or "capsolver").strip().lower()


def _key() -> str:
    return (os.getenv("CAPTCHA_SOLVER_KEY") or "").strip()


def _twocaptcha_key() -> str:
    """The 2captcha API key — a dedicated `TWOCAPTCHA_KEY` if set, else `CAPTCHA_SOLVER_KEY`.

    FriendlyCaptcha is **2captcha-ONLY** (CapSolver has no FriendlyCaptcha task type and NopeCHA
    only does reCAPTCHA/hCaptcha/Turnstile), so its solve ALWAYS funds through this key regardless of
    `CAPTCHA_SOLVER_PROVIDER`. When the owner runs CapSolver as the default provider for the other
    kinds, they set a separate `TWOCAPTCHA_KEY` here for FriendlyCaptcha; when the default provider IS
    2captcha, `CAPTCHA_SOLVER_KEY` doubles as the 2captcha key and nothing extra is needed."""
    return (os.getenv("TWOCAPTCHA_KEY") or _key()).strip()


def is_enabled() -> bool:
    """True only when a provider API key is configured — callers no-op otherwise."""
    return bool(_key())


def friendlycaptcha_available() -> bool:
    """True when a FriendlyCaptcha solve can run — a 2captcha-compatible key is configured.

    FriendlyCaptcha is 2captcha-only, so a lane gates its solve on THIS (not `is_enabled()`): with
    only `TWOCAPTCHA_KEY` set (CapSolver as the default provider, or no key at all for the other
    kinds) FriendlyCaptcha still solves."""
    return bool(_twocaptcha_key())


def _awswaf_browser_enabled() -> bool:
    """FREE, no-key AWS WAF path: let the page's OWN AWS WAF SDK mint the `aws-waf-token`
    (the documented `window.AwsWafIntegration.getToken()` + the cookie the SDK auto-sets). It
    clears the common AWS WAF *challenge* (a silent JS proof-of-work) with NO external service —
    a real browser solves AWS's own PoW. A hard visual WAF puzzle still falls through to the paid
    AntiAwsWafTask. OFF by default; enable with AWSWAF_BROWSER=1 (or provider `awswaf_browser`)."""
    if (os.getenv("AWSWAF_BROWSER") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return (os.getenv("CAPTCHA_SOLVER_PROVIDER") or "").strip().lower() == "awswaf_browser"


def aws_waf_available() -> bool:
    """True when solve_aws_waf can do SOMETHING for an AWS WAF gate — either a paid provider key
    (visual puzzle) OR the free in-browser token path (challenge). Lanes gate account bootstrap on
    THIS, not is_enabled(), so `AWSWAF_BROWSER=1` alone (no key) can attempt an AWS WAF challenge."""
    return is_enabled() or _awswaf_browser_enabled()


# ---- CapSolver task types ------------------------------------------------------
# The on-page-DETECTABLE captcha kinds (what `detect_captcha`/`solve_on_page` map from the
# DOM). The full CapSolver task-type table — incl. the enterprise reCAPTCHA variants + AWS
# WAF — lives in `backend.applier.capsolver`; this dict is the subset a DOM probe can classify.
# ProxyLess variants: the solver uses its own IP for the token. That is correct here — the
# token is bound to the site key + page URL, not to the browser IP, and our page traffic
# already goes through our own (residential) proxy.
_CAPSOLVER_TASK = {
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
}
# 2Captcha "method" per kind (in/out API). FriendlyCaptcha is 2captcha-ONLY (no CapSolver task type),
# so it lives here but NOT in _CAPSOLVER_TASK.
_TWOCAPTCHA_METHOD = {
    "recaptcha_v2": "userrecaptcha",
    "recaptcha_v3": "userrecaptcha",
    "hcaptcha": "hcaptcha",
    "turnstile": "turnstile",
    "friendlycaptcha": "friendlycaptcha",
}


async def _twocaptcha_solve(kind: str, site_key: str, page_url: str,
                            action: str | None = None) -> str | None:
    params = {"key": _twocaptcha_key(), "method": _TWOCAPTCHA_METHOD[kind], "json": 1,
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
    elif kind == "friendlycaptcha":
        # 2captcha `method=friendlycaptcha`: sitekey + pageurl → the FriendlyCaptcha solution token.
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


async def solve(kind: str, site_key: str, page_url: str, action: str | None = None,
                *, enterprise: bool = False, enterprise_payload: dict | None = None) -> str | None:
    """Solve one captcha of `kind` ∈ {recaptcha_v2,recaptcha_v3,hcaptcha,turnstile,friendlycaptcha}.
    Returns the token, or None (disabled / error / timeout). Never raises. The CapSolver path (default
    provider) runs through `backend.applier.capsolver` (createTask/getTaskResult + bounded
    exponential-backoff poll); `enterprise`/`enterprise_payload` escalate to the enterprise task
    types. 2captcha keeps the legacy in/out flow.

    **FriendlyCaptcha (`kind='friendlycaptcha'`) routes to 2captcha REGARDLESS of
    `CAPTCHA_SOLVER_PROVIDER`** — CapSolver has no FriendlyCaptcha task type, so it always funds
    through `_twocaptcha_key()`. Every other kind's routing is byte-identical to before."""
    if not site_key or not page_url:
        return None
    if kind == "friendlycaptcha":
        # 2captcha-ONLY (CapSolver can't do it). Uses TWOCAPTCHA_KEY else CAPTCHA_SOLVER_KEY.
        if not _twocaptcha_key():
            return None
        try:
            return await _twocaptcha_solve(kind, site_key, page_url)
        except Exception as exc:
            logger.warning("captcha solve failed (friendlycaptcha): %s", exc)
            return None
    if not is_enabled() or kind not in _CAPSOLVER_TASK:
        return None
    try:
        if _provider() == "twocaptcha":
            return await _twocaptcha_solve(kind, site_key, page_url, action)
        return await _capsolver.solve(kind, page_url=page_url, site_key=site_key, action=action,
                                      enterprise=enterprise, enterprise_payload=enterprise_payload)
    except Exception as exc:
        logger.warning("captcha solve failed (%s): %s", kind, exc)
        return None


# ---- on-page detection + token injection (Playwright) --------------------------
# Pure DOM probe: which captcha (if any) is rendered, its site key, and (for reCAPTCHA) whether
# the Enterprise API is loaded. Returns {kind, key, enterprise}.
_DETECT_JS = r"""() => {
  const q = s => document.querySelector(s);
  // FriendlyCaptcha — a proof-of-work widget (.frc-captcha / .bluex-friendly-captcha / the
  // friendly-challenge script). 2captcha solves it; CapSolver/NopeCHA cannot. The sitekey is on the
  // widget's data-sitekey (FriendlyCaptcha's documented attribute); Randstad renders it via captcha.js.
  {
    const frcScript = document.querySelector(
      'script[src*="friendly-challenge"], script[src*="friendlycaptcha"], script[src*="friendly.js"]');
    let f = q('.frc-captcha[data-sitekey], .bluex-friendly-captcha[data-sitekey], '
             + '[data-sitekey].frc-captcha, [data-friendly-captcha][data-sitekey]');
    const host = q('.frc-captcha, .bluex-friendly-captcha, [data-friendly-captcha]');
    if (!f && host && host.closest) f = host.closest('[data-sitekey]');
    if (f && f.getAttribute && f.getAttribute('data-sitekey'))
      return {kind:'friendlycaptcha', key:f.getAttribute('data-sitekey'), enterprise:false};
    if (host || frcScript) {
      const sk = q('[data-sitekey]');
      return {kind:'friendlycaptcha',
              key:(sk && sk.getAttribute && sk.getAttribute('data-sitekey')) || '', enterprise:false};
    }
  }
  // Cloudflare Turnstile — explicit widget, or the challenge iframe next to a data-sitekey host.
  let el = q('.cf-turnstile[data-sitekey], [data-sitekey].cf-turnstile') ||
           (q('iframe[src*="challenges.cloudflare.com"]') || {}).closest &&
             q('iframe[src*="challenges.cloudflare.com"]').closest('[data-sitekey]');
  if (el && el.getAttribute && el.getAttribute('data-sitekey'))
    return {kind:'turnstile', key:el.getAttribute('data-sitekey'), enterprise:false};
  // hCaptcha — explicit widget, or the challenge iframe (invisible variants keep the widget div).
  el = q('.h-captcha[data-sitekey], [data-hcaptcha-sitekey], [data-sitekey][data-callback][data-size]');
  if (!el && q('iframe[src*="hcaptcha.com"]'))
    el = q('[data-sitekey], [data-hcaptcha-sitekey]');
  if (el) {
    const k = el.getAttribute('data-sitekey') || el.getAttribute('data-hcaptcha-sitekey');
    if (k) return {kind:'hcaptcha', key:k, enterprise:false};
  }
  const rcEnt = !!(window.grecaptcha && window.grecaptcha.enterprise);
  // reCAPTCHA v2 (visible checkbox / invisible badge with an explicit widget).
  el = q('.g-recaptcha[data-sitekey], [data-sitekey].g-recaptcha');
  if (el && el.getAttribute('data-sitekey'))
    return {kind:'recaptcha_v2', key:el.getAttribute('data-sitekey'), enterprise:rcEnt};
  // reCAPTCHA v3 (invisible) — key is in the api.js script ?render=<key>.
  const s = [...document.querySelectorAll('script[src*="recaptcha/api.js"], script[src*="recaptcha/enterprise.js"]')]
    .map(x => (x.src.match(/[?&]render=([^&]+)/)||[])[1]).find(Boolean);
  if (s && s !== 'explicit')
    return {kind:'recaptcha_v3', key:s,
            enterprise: rcEnt || [...document.querySelectorAll('script[src*="recaptcha/enterprise.js"]')].length>0};
  return null;
}"""

# Inject the solved token into the right field + fire framework events, then best-effort invoke
# any registered callback so an SPA that gates Submit on a JS callback (not just the hidden field)
# actually advances. Everything is wrapped so a missing element / exotic page never throws.
_INJECT_JS = r"""([kind, token]) => {
  const setField = (sel, create) => {
    let t = document.querySelector(sel);
    if (!t && create) {
      t = document.createElement('textarea'); t.id = create.id; t.name = create.name;
      t.style.display = 'none'; (document.body||document.documentElement).appendChild(t);
    }
    if (!t) return false;
    try {
      t.value = token;
      t.dispatchEvent(new Event('input', {bubbles:true}));
      t.dispatchEvent(new Event('change', {bubbles:true}));
    } catch (e) {}
    return true;
  };
  const callGrecaptchaCb = (api) => {
    // Walk ___grecaptcha_cfg.clients for the registered callback fn + invoke with the token.
    try {
      const cfg = window.___grecaptcha_cfg;
      if (!cfg || !cfg.clients) return;
      for (const id in cfg.clients) {
        const client = cfg.clients[id];
        for (const a in client) {
          const o = client[a];
          if (o && typeof o === 'object') {
            for (const b in o) {
              const inner = o[b];
              if (inner && typeof inner === 'object' && typeof inner.callback === 'function') {
                try { inner.callback(token); } catch (e) {}
              }
            }
          }
        }
      }
    } catch (e) {}
  };
  let ok = false;
  if (kind === 'turnstile') {
    ok = setField('[name="cf-turnstile-response"]',
                  {id:'cf-turnstile-response', name:'cf-turnstile-response'});
    try { if (window.turnstile && typeof window.turnstile.reset !== 'function') {} } catch (e) {}
  } else if (kind === 'hcaptcha') {
    const a = setField('textarea[name="h-captcha-response"], [name="h-captcha-response"]',
                       {id:'h-captcha-response', name:'h-captcha-response'});
    const b = setField('textarea[name="g-recaptcha-response"], [name="g-recaptcha-response"]',
                       {id:'g-recaptcha-response', name:'g-recaptcha-response'});
    ok = a || b;
  } else if (kind === 'friendlycaptcha') {
    // FriendlyCaptcha's solution goes into the hidden `frc-captcha-solution` field; the widget then
    // reads it. Set it, fire events, mark the widget done + invoke any registered callback so an SPA
    // gate that watches the widget state (not just the hidden field) advances.
    ok = setField('input[name="frc-captcha-solution"], textarea[name="frc-captcha-solution"], '
                  + '.frc-captcha-solution', {id:'frc-captcha-solution', name:'frc-captcha-solution'});
    try {
      document.querySelectorAll('.frc-captcha, .bluex-friendly-captcha, [data-friendly-captcha]')
        .forEach(w => {
          try { w.setAttribute('data-attached', 'true'); if (w.dataset) w.dataset.solution = token; }
          catch (e) {}
          const cb = w.getAttribute && w.getAttribute('data-callback');
          if (cb && typeof window[cb] === 'function') { try { window[cb](token); } catch (e) {} }
        });
    } catch (e) {}
  } else {
    // reCAPTCHA v2/v3: the (usually hidden) response textarea — create it if missing.
    ok = setField('textarea#g-recaptcha-response, textarea[name="g-recaptcha-response"]',
                  {id:'g-recaptcha-response', name:'g-recaptcha-response'});
    callGrecaptchaCb();
  }
  return !!ok;
}"""


async def _detect_full(page):
    """{kind, key, enterprise} of the captcha rendered on `page`, or None. Never raises."""
    try:
        info = await page.evaluate(_DETECT_JS)
    except Exception:
        return None
    if info and info.get("kind") and info.get("key"):
        return {"kind": info["kind"], "key": info["key"],
                "enterprise": bool(info.get("enterprise"))}
    return None


async def detect_captcha(page):
    """(kind, site_key) of the captcha rendered on `page`, or None. Never raises. (The richer
    `_detect_full` also carries the reCAPTCHA `enterprise` flag `solve_on_page` uses.)"""
    info = await _detect_full(page)
    if info:
        return info["kind"], info["key"]
    return None


async def solve_on_page(page, action: str | None = None) -> bool:
    """Detect a captcha on `page`, escalate to the PAID CapSolver tier, and inject the token.
    Returns True only if a challenge was found AND solved AND injected.

    ESCALATION ORDER: the FREE path runs FIRST and elsewhere — the in-browser NopeCHA extension
    for reCAPTCHA/hCaptcha/Turnstile (where armed) and, for AWS WAF, `solve_aws_waf`'s
    `AwsWafIntegration.getToken()`. This function is the PAID tier that fires only when a token
    captcha is STILL present AND `CAPTCHA_SOLVER_KEY` is set — so it is a pure no-op (False)
    without a key, when no captcha is present, or on any error, and is always safe to call right
    before/after a submit click. If no token captcha is found it falls through to `solve_aws_waf`
    (itself free-first) for an AWS WAF gate. Never raises."""
    det = await _detect_full(page)
    if det:
        kind, site_key, enterprise = det["kind"], det["key"], det["enterprise"]
        # FriendlyCaptcha is 2captcha-only — gate on a 2captcha key, not is_enabled(). Every other
        # token captcha needs the paid CapSolver/2captcha key (the free NopeCHA path, when armed,
        # already ran in the browser); no key => no-op here.
        if kind == "friendlycaptcha":
            if not friendlycaptcha_available() or not site_key:
                if not site_key:
                    logger.info("friendlycaptcha present but no sitekey resolved on the page")
                return False
        elif not is_enabled():
            return False
        try:
            page_url = page.url
        except Exception:
            page_url = ""
        token = await solve(kind, site_key, page_url, action=action, enterprise=enterprise)
        if not token:
            logger.info("captcha (%s%s) present but not solved", kind,
                        " enterprise" if enterprise else "")
            return False
        try:
            await page.evaluate(_INJECT_JS, [kind, token])
            logger.info("captcha (%s%s) solved + injected", kind,
                        " enterprise" if enterprise else "")
            return True
        except Exception as exc:
            logger.warning("captcha token injection failed: %s", exc)
            return False
    # No reCAPTCHA/hCaptcha/Turnstile — an AWS WAF gate may still be present (free-first + paid).
    try:
        return await solve_aws_waf(page)
    except Exception:
        return False


# ---- AWS WAF CAPTCHA (Amazon Passport / account.amazon.jobs) -------------------
# Amazon's corporate ATS gates account-creation (and sometimes the apply submit) with an
# AWS WAF CAPTCHA (captcha-sdk.awswaf.com jsapi.js; window.AwsWafCaptcha.renderCaptcha).
# It is Amazon-proprietary — NOT reCAPTCHA/hCaptcha/Turnstile — so it is a SEPARATE task
# type from _CAPSOLVER_TASK above (kept out of that dict on purpose; a token here is a
# cookie, not a g-recaptcha-response). The paid solve now runs through `capsolver.solve("aws_waf", …)`
# (which builds this exact task type); this constant is the canonical name for reference. The
# token is short-lived, so solve it right before the register/submit request.
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
  const challengeJs = [...document.querySelectorAll(
        'script[src*="captcha-sdk.awswaf.com"], script[src*="challenge.js"]')]
        .map(x => x.src).find(Boolean) || null;
  return { present: true, key: g.key || null, iv: g.iv || null, context: g.context || null,
           challengeJs: challengeJs };
}"""


async def _inject_aws_waf_cookie(page, page_url: str, token) -> bool:
    """Set the `aws-waf-token` cookie on the browser context. Scopes it domain-wide on
    *.amazon.jobs so it also satisfies the account/passport XHRs the SPA fires. Never raises."""
    if isinstance(token, str) and token.startswith("aws-waf-token="):
        token = token.split("=", 1)[1]
    if not token:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(page_url).hostname or "").lower()
        domain = ".amazon.jobs" if host.endswith("amazon.jobs") else host
        if not domain:
            return False
        await page.context.add_cookies(
            [{"name": "aws-waf-token", "value": token, "domain": domain, "path": "/"}])
        return True
    except Exception as exc:
        logger.warning("aws-waf token injection failed: %s", exc)
        return False


# The documented AWS WAF SPA integration API: window.AwsWafIntegration.getToken() returns a Promise
# resolving to the aws-waf-token (AWS runs the challenge JS proof-of-work in-page). It is the
# INTENDED way an SPA obtains a token to attach to API calls — free, no external service. A hard
# visual puzzle exposes only window.AwsWafCaptcha (renderCaptcha) and getToken won't silently
# resolve; we detect that and fall through to the paid solver.
_AWS_WAF_GETTOKEN_JS = r"""
async () => {
  const I = window.AwsWafIntegration;
  if (!I || typeof I.getToken !== 'function') return null;
  try {
    const t = await Promise.race([
      I.getToken(),
      new Promise(r => setTimeout(() => r(null), 20000)),
    ]);
    return (typeof t === 'string' && t) ? t : null;
  } catch (e) { return null; }
}"""


async def _awswaf_browser_token(page) -> bool:
    """FREE: mint the `aws-waf-token` via the page's own AWS WAF SDK (no external solver, no key).
    Returns True only when a token was obtained AND set as the cookie. A cheap no-op (False) when
    the SDK / getToken API is absent (e.g. a hard visual puzzle exposes only AwsWafCaptcha, or the
    page isn't AWS-WAF-gated). Never raises. LIVE-PROVEN 2026-09-20: on a real AWS-WAF page,
    getToken() returned a 390-char aws-waf-token headless from a datacenter IP."""
    try:
        token = await page.evaluate(_AWS_WAF_GETTOKEN_JS)
    except Exception:
        token = None
    try:
        page_url = page.url
    except Exception:
        page_url = ""
    if token:
        ok = await _inject_aws_waf_cookie(page, page_url, token)
        if ok:
            logger.info("aws-waf token minted via AwsWafIntegration.getToken() (free path)")
        return ok
    # getToken() unavailable/None, but challenge.js may have set the cookie itself already.
    try:
        for c in await page.context.cookies():
            if c.get("name") == "aws-waf-token" and c.get("value"):
                logger.info("aws-waf token present from the page SDK cookie (free path)")
                return True
    except Exception:
        pass
    return False


async def solve_aws_waf(page) -> bool:
    """Make an `aws-waf-token` available for `page` (Amazon Passport register / apply). Two paths:

    1. FREE (AWSWAF_BROWSER=1, no key): the page's own AWS WAF SDK mints the token via
       `AwsWafIntegration.getToken()` — solves the silent WAF *challenge* with no external service.
    2. PAID (CapSolver AntiAwsWafTask, CAPTCHA_SOLVER_KEY): solves a hard visual WAF *puzzle*.

    The free path is tried first (when enabled) and, if it yields nothing (a visual puzzle), falls
    through to the paid solver. Returns True only if a token was obtained AND injected. A graceful
    no-op (False) when neither path is armed, no AWS WAF challenge is present, the provider is
    2captcha (its Amazon-WAF method is not wired here), or anything fails — so it is always safe to
    call before a register or submit click. Never raises.

    The token is IP-bound: solve it through the SAME (residential) session the page uses, and do
    it immediately before the gated request — it expires fast."""
    # 1) FREE in-browser path first (no key needed).
    if _awswaf_browser_enabled():
        try:
            if await _awswaf_browser_token(page):
                return True
        except Exception as exc:
            logger.warning("aws-waf browser token failed: %s", exc)
        # else fall through to the paid solver (if keyed) for a hard visual puzzle
    # 2) PAID CapSolver path.
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
    # PAID escalation: CapSolver AntiAwsWafTask (client in backend.applier.capsolver, bounded
    # exponential-backoff poll). It returns the aws-waf-token COOKIE value (solution.cookie).
    try:
        token = await _capsolver.solve("aws_waf", page_url=page_url,
                                       aws_key=info.get("key"), aws_iv=info.get("iv"),
                                       aws_context=info.get("context"),
                                       aws_challenge_js=info.get("challengeJs"))
    except Exception as exc:
        logger.warning("aws-waf solve failed: %s", exc)
        return False
    if not token:
        logger.info("aws-waf captcha present but not solved")
        return False
    ok = await _inject_aws_waf_cookie(page, page_url, token)
    if ok:
        logger.info("aws-waf captcha solved + token injected")
    return ok
