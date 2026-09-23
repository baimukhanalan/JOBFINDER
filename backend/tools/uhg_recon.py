"""RECON + self-verifying probe — UnitedHealth Group / Optum apply flow (source key: unitedhealth).

OUTCOME 2026-09-23: **BLOCKED — workforce Azure AD SSO, NO candidate self-registration anywhere.**
Re-verified fresh (curl_cffi redirect trace + a LIGHT headless DOM walk, NO heavy :98 headful). The
"newRegister.jsf returns 200 without bouncing to login.microsoftonline" hint did NOT survive: that URL
serves an EMPTY octet-stream attachment (a download, 0 bytes), NOT a registration form — exactly the
"Download is starting" quirk the task flagged. Every real account/apply path 302s to Azure. So this is
NOT a strategy — there is no self-service flow to drive. This module is the resolver + the pure
classifiers + a light re-verification probe (`--probe`) that WATCHES for the SSO wall to ever drop
(promotes only if a genuine native Taleo self-register form appears). A sibling strategy file
(`applier/strategies/uhg_taleo.py`) was deliberately NOT created — a lane only exists once a
self-register path exists. If Azure SSO is ever removed, the probe flips `data/uhg_verified.json` and a
human then builds the lane by composing TaleoStrategy (proven on TTEC) — the wall was never Taleo.

================================================================================================
THE REAL HUMAN APPLY FLOW (traced end-to-end 2026-09-23 from a live job apply_url)
================================================================================================
  job row apply_url (Radancy TalentBrew job-detail page), e.g.
    https://careers.unitedhealthgroup.com/job/<city>/<slug>/34088/<radancy_id>          [200]
    -> the page embeds the EXTERNAL Taleo apply URL (careersection 10020, "Apply" button):
       https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=<taleo_id>            [200]
       (careersection 10000 = INTERNAL employee mobility "Internal apply" — never use)
    -> jobapply.ftl renders a Taleo "Privacy Agreement" / Statement-Before-Authentication page
       (akira JSF, ViewState; title "Privacy Agreement"). NO captcha, NO WAF.
    -> click "I Accept" (#dialogTemplate-dialogForm-StatementBeforeAuthentificationContent-ContinueButton)
    -> the account step: a full client-side redirect to the corporate IdP, NOT a native Taleo Sign-In:
         hop A  PingFederate  https://authgateway3.entiam.uhg.com/ext/microsoft-authn
         hop B  Azure AD      https://login.microsoftonline.com/db05faca-c82a-4b9d-b9c5-0f64b6755421/
                              oauth2/v2.0/authorize?client_id=7e95aaf6-...&scope=openid+User.Read
                              (title "Sign in to your account")
       There is NO "New User" / "Create account" on the Taleo side — the privacy accept jumps straight
       to Azure. A human can only proceed with a credential UHG's IT issued (an internal/referral login).

DECISIVE EVIDENCE THAT NO CANDIDATE SELF-REGISTRATION EXISTS (fresh, 2026-09-23):
  * The Azure tenant db05faca-c82a-4b9d-b9c5-0f64b6755421 is a WORKFORCE Entra ID tenant, NOT B2C:
      - the authorize host is login.microsoftonline.com (B2C would be *.b2clogin.com / a `/tfp/`
        policy / a `p=B2C_1_*` policy param — NONE present),
      - scope=`openid User.Read`, response_type=code (a workforce OIDC app),
      - the only "sign up" strings on the page are the generic MSA boilerplate
        `urlMsaSignup=https://login.live.com/oauth20_authorize` present on EVERY Azure login page
        (a PERSONAL Microsoft-account link, not this tenant's candidate registration), and there are
        ZERO rendered "Create account / Sign up / Register" links.
    Workforce Entra ID has no public self-service sign-up: a candidate cannot mint a credential.
  * Every Taleo account/browse endpoint on careersection 10020 redirects to Azure (client-side SAML
    "Redirecting" interstitial → login.microsoftonline.com, RelayState=referrals.unitedhealthgroup.com):
        accessmanagement.ftl  -> login.microsoftonline.com   (title "Redirecting")
        register.ftl          -> login.microsoftonline.com
        createprofile.ftl     -> login.microsoftonline.com
        moresearch.ftl        -> login.microsoftonline.com   (even plain job-browsing is SSO-gated)
  * The native Taleo IAM register/login endpoints are NOT usable self-service forms:
        iam/accessmanagement/newRegister.jsf?portal=10020  -> HTTP 200 but content-type
            application/x-octet-stream, 0 bytes, an empty DOWNLOAD (the attachment quirk) — NOT a form.
        careersection/10020/login.jsf                      -> HTTP 200, 0 bytes (needs the JSF flow).
        iam/accessmanagement/login.jsf?portal=10020        -> bounces to the Radancy job-alert page
            (a marketing EmailAddress/EmailConfirm subscribe form, NOT a Taleo account register).
  * NO captcha / NO WAF anywhere in the flow (consistent with the 2026-09-01 probe). The wall is purely
    IDENTITY: a workforce Azure AD login with no candidate self-registration. A captcha key or a US IP
    does NOT help. This matches (and re-proves with fresh evidence) the prior BLOCKED verdict.

CONTRAST: TTEC (ttec.taleo.net) is the SAME vendor (Oracle Taleo) but has a NATIVE candidate register
(username+password → emailed code) with NO SSO redirect — which is why TaleoStrategy is PROVEN there.
UHG differs only in the account step (Azure SSO), and that is the whole blocker.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

# ---- Redirect / apply-URL resolution (pure, network-free) ------------------------------------

_TALEO_APPLYURL_RE = re.compile(
    r"https://[a-z0-9.\-]*taleo\.net/careersection/(\d+)/jobapply\.ftl\?job=(\d+)", re.I)

# Hosts that mean "we were handed off to the corporate identity provider" (the SSO wall).
_SSO_HOST_RE = re.compile(
    r"(?:^|//|\.)(?:login\.microsoftonline\.com|login\.microsoft\.com|"
    r"[a-z0-9\-]*\.entiam\.uhg\.com|sts\.windows\.net|login\.windows\.net|"
    r"[a-z0-9\-]*\.b2clogin\.com|login\.live\.com/oauth)", re.I)


def resolve_apply_url(radancy_job_page_html: str, prefer_section: str = "10020") -> str | None:
    """Extract the EXTERNAL Taleo apply URL from a Radancy job-detail page's HTML.

    The mass_hiring_jobs.apply_url is the Radancy job page; the real Taleo apply form lives at a
    DIFFERENT (careersection, taleo_job_id). Prefer careersection 10020 (external) over 10000
    (internal). Returns the taleo.net jobapply.ftl URL, or None if not present.
    """
    hits = _TALEO_APPLYURL_RE.findall(radancy_job_page_html or "")
    if not hits:
        return None
    for section, job in hits:
        if section == prefer_section:
            return f"https://uhg.taleo.net/careersection/{section}/jobapply.ftl?job={job}"
    section, job = hits[0]
    return f"https://uhg.taleo.net/careersection/{section}/jobapply.ftl?job={job}"


def is_sso_url(url: str) -> bool:
    """True iff `url` is a corporate-IdP handoff (Azure AD / PingFederate / MSA) — the SSO wall."""
    return bool(_SSO_HOST_RE.search(url or ""))


def classify_landing(url: str) -> str:
    """Where a redirect/nav landed: one of
        'azure_sso'    — login.microsoftonline / entiam.uhg PingFederate / any IdP handoff (BLOCKED)
        'taleo_native' — a real Taleo careersection page (would be reachable only if SSO were removed)
        'radancy'      — the careers.unitedhealthgroup.com marketing/job-alert front (not an account)
        'unknown'      — anything else.
    PURE (network-free)."""
    u = (url or "").lower()
    if is_sso_url(u):
        return "azure_sso"
    if "taleo.net/careersection" in u:
        return "taleo_native"
    if "careers.unitedhealthgroup.com" in u or "referrals.unitedhealthgroup.com" in u:
        return "radancy"
    return "unknown"


def _has_input(html: str, *, type_: str | None = None, name_re: str | None = None) -> bool:
    for tag in re.findall(r"<input\b[^>]*>", html or "", re.I):
        if type_ is not None:
            m = re.search(r'type=["\']([^"\']+)', tag, re.I)
            if not m or m.group(1).lower() != type_.lower():
                continue
        if name_re is not None:
            m = re.search(r'(?:name|id)=["\']([^"\']+)', tag, re.I)
            if not m or not re.search(name_re, m.group(1), re.I):
                continue
        return True
    return False


def is_native_register_form(html: str, url: str = "") -> bool:
    """True iff `html` is a GENUINE native (non-SSO) self-registration form: it must carry BOTH a
    password input AND an email/username input, AND must NOT be an Azure/MSA sign-in page (which also
    has password+email fields but is the SSO wall, not a candidate self-register). This is the single
    signal the probe watches for — the day it returns True, UHG has opened a self-service path.
    PURE (network-free)."""
    if not html:
        return False
    low = html.lower()
    if is_sso_url(url) or "microsoftonline" in low or "login.live.com" in low or "b2clogin" in low:
        return False  # an Azure/MSA sign-in page is NOT a candidate self-register
    has_pw = _has_input(html, type_="password")
    has_ident = (_has_input(html, type_="email")
                 or _has_input(html, name_re=r"email|user(name)?|login"))
    # a native Taleo register also self-identifies via these akira ids / "create account" wording
    looks_register = bool(re.search(
        r"createprofile|newregister|new user|create (?:an )?account|register", low))
    return bool(has_pw and has_ident and looks_register)


def probe_verdict(landing_url: str, landing_html: str = "") -> tuple[str, str]:
    """(verdict, detail) for a probed apply flow's final landing. PURE (network-free).
        'blocked_sso'         — landed on the Azure/IdP wall (the current, expected reality).
        'self_register_open'  — a native Taleo self-register form appeared (SSO removed! build a lane).
        'unknown'             — landed somewhere unclassified (log + retry; never promote).
    """
    if is_native_register_form(landing_html, landing_url):
        return "self_register_open", "a native Taleo self-registration form is now reachable"
    if classify_landing(landing_url) == "azure_sso":
        return "blocked_sso", "workforce Azure AD SSO, no candidate self-registration"
    return "unknown", f"landed on {classify_landing(landing_url)}: {landing_url[:80]}"


# ---- Live re-verification probe (light headless; import-safe without playwright) --------------

PRIVACY_ACCEPT_SEL = (
    "#dialogTemplate-dialogForm-StatementBeforeAuthentificationContent-ContinueButton")


async def probe_apply_flow(taleo_apply_url: str, *, timeout_ms: int = 45000) -> dict:
    """LIGHT headless walk: jobapply.ftl (Privacy) -> click "I Accept" -> read the landing. Returns
    {chain, final_url, landing_kind, native_register, verdict, detail}. Read-only: it NEVER registers,
    NEVER submits (there is nothing to submit — the wall is before any form). Best-effort; on any error
    returns a verdict of 'unknown'. Requires playwright; caller runs it only when the box is quiet."""
    from playwright.async_api import async_playwright  # lazy import — keeps the module import-safe

    chain: list[str] = []
    out = {"chain": chain, "final_url": "", "landing_kind": "unknown",
           "native_register": False, "verdict": "unknown", "detail": ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="en-US", accept_downloads=True)
        pg = await ctx.new_page()
        pg.on("framenavigated", lambda fr: chain.append(fr.url) if fr is pg.main_frame else None)
        try:
            await pg.goto(taleo_apply_url, wait_until="domcontentloaded", timeout=timeout_ms)
            btn = await pg.query_selector(PRIVACY_ACCEPT_SEL)
            if btn:
                try:
                    await btn.click(timeout=8000)
                except Exception:
                    pass
            await pg.wait_for_timeout(6000)
            out["final_url"] = pg.url
            try:
                html = await pg.content()
            except Exception:
                html = ""
            out["landing_kind"] = classify_landing(pg.url)
            out["native_register"] = is_native_register_form(html, pg.url)
            out["verdict"], out["detail"] = probe_verdict(pg.url, html)
        except Exception as e:  # noqa: BLE001
            out["detail"] = f"probe error: {type(e).__name__}: {e}"
        finally:
            await browser.close()
    return out


def _fetch_apply_url_for(job_page_url: str) -> str | None:
    """Fetch a Radancy job page and resolve its external Taleo apply URL (curl_cffi, best-effort)."""
    try:
        from curl_cffi import requests as cq  # lazy — keeps import-safe
        r = cq.get(job_page_url, impersonate="chrome", timeout=30)
        if r.status_code == 200:
            return resolve_apply_url(r.text)
    except Exception:
        pass
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="UHG apply-flow re-verification probe (read-only).")
    ap.add_argument("--apply-url", default=None,
                    help="a uhg.taleo.net/careersection/10020/jobapply.ftl?job=<id> URL to probe")
    ap.add_argument("--job-page", default=None,
                    help="a careers.unitedhealthgroup.com job page to resolve the apply URL from first")
    args = ap.parse_args()
    apply_url = args.apply_url
    if not apply_url and args.job_page:
        apply_url = _fetch_apply_url_for(args.job_page)
        print(f"resolved apply URL: {apply_url}")
    if not apply_url:
        print("no apply URL — pass --apply-url or --job-page")
        sys.exit(2)
    res = asyncio.run(probe_apply_flow(apply_url))
    print(f"landing_kind : {res['landing_kind']}")
    print(f"final_url    : {res['final_url'][:100]}")
    print(f"native_reg   : {res['native_register']}")
    print(f"VERDICT      : {res['verdict']} — {res['detail']}")


if __name__ == "__main__":
    main()
