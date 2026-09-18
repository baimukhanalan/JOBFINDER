"""Durable, no-browser REAL-STATE checker for assessment invites.

Born from the 2026-09-18 full-sweep AMCAT audit. `amcatglobal.aspiringminds.com` is a static Angular
SPA shell: a bare `httpx.get(invite_url)` returns byte-identical HTML whether the token is fresh,
already completed, or long expired — the real verdict only appears once the SPA's own JS fires an XHR.
Reverse-engineered by capturing that XHR from a real Chromium session: the SPA POSTs the JWT to

    https://amcatglobalapi.aspiringminds.com/api/v1/auth/login
    (form-urlencoded: authJwt=<token>&fingerPrint=<anything>&cookies=&loginUrl=<invite_url>)

and renders the JSON `message` verbatim. Replaying that exact POST via plain httpx reproduces the
IDENTICAL server verdict — no browser, ~100ms/invite, safe at moderate concurrency — and unlike a
bare page load it actually DISCRIMINATES the three states a stale "evaluating..." guess used to
conflate:
  - `lex100_expired`    — "Error Code LEX100: Your test login credentials have expired...".
  - `completed_report`  — "...completed or submitted... Message code TC100." (single-use token
                            already consumed, typically by an earlier harvester attempt now burned
                            in harvest_state.json).
  - `fresh_answerable`  — `loginSuccess: true` (a genuinely live, unopened token).
  - `unknown`           — a response shape we haven't seen before (reported, never guessed silent).

Verified 2026-09-18 against all 436 live AMCAT invites: 269 lex100_expired + 167 completed_report,
zero fresh_answerable — consistent with the standing "AMCAT never completes" lane finding. A REAL
completion still requires driving the browser (`harvest_runner --platform amcat --url ... --mailbox
...`) — this module only answers "is this token worth driving," cheaply, for all invites at once.

CLI:
    PYTHONPATH=. python3 -m backend.tools.assessment_state_check --platform amcat [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import urllib.parse
from collections import Counter

import httpx

from backend.tools.assessment_harvester import discover

AMCAT_LOGIN_API = "https://amcatglobalapi.aspiringminds.com/api/v1/auth/login"
# Matches the header shape the real SPA sends; the exact fingerprint value doesn't matter to the
# server for a login-status probe (verified against the live API).
_AMCAT_HEADERS = {
    "x-api-client": "eyJwbGF0Zm9ybSI6IldlYkNsaWVudFYyIn0=",
    "x-api-signature": "B",
    "x-api-authtoken": "A",
    "content-type": "application/x-www-form-urlencoded",
    "accept": "application/json, text/plain, */*",
    "referer": "https://amcatglobal.aspiringminds.com/",
    "origin": "https://amcatglobal.aspiringminds.com",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/143.0.0.0 Safari/537.36"),
}
_FINGERPRINT = "assessment_state_check-probe"

_LEX100_RE = re.compile(r"\bLEX100\b", re.I)
_TC100_RE = re.compile(r"completed or submitted|message code tc\d*", re.I)
_CAMERA_RE = re.compile(r"unable to detect a camera|camera is mandatory|camera not detected", re.I)


def decode_jwt_payload(token: str) -> dict:
    """Best-effort decode of a JWT's middle (payload) segment. Never raises — a malformed or
    foreign token just yields {}, so the checker degrades to 'unknown' instead of crashing."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


def extract_token(url: str) -> str | None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (q.get("token") or [None])[0]


def classify_amcat_message(message: str | None, login_success: bool) -> str:
    if login_success:
        return "fresh_answerable"
    msg = message or ""
    if _LEX100_RE.search(msg):
        return "lex100_expired"
    if _TC100_RE.search(msg):
        return "completed_report"
    if _CAMERA_RE.search(msg):
        return "camera_walled"
    return "unknown"


async def check_amcat_invite(client: httpx.AsyncClient, url: str) -> dict:
    """Probe ONE AMCAT invite URL and return its real state. Never raises."""
    result: dict = {"url": url, "state": "unknown"}
    token = extract_token(url)
    if not token:
        result["state"] = "no_token"
        return result
    result["token_payload"] = decode_jwt_payload(token)
    body = {"authJwt": token, "fingerPrint": _FINGERPRINT, "cookies": "", "loginUrl": url}
    try:
        r = await client.post(AMCAT_LOGIN_API, data=body, headers=_AMCAT_HEADERS, timeout=20)
    except Exception as exc:
        result["state"] = "probe_error"
        result["error"] = str(exc)[:200]
        return result
    result["http_status"] = r.status_code
    try:
        j = r.json()
    except Exception:
        result["state"] = "probe_error"
        result["error"] = f"non-json response, http {r.status_code}"
        return result
    data = j.get("data") or {}
    message = j.get("message")
    login_success = bool(data.get("loginSuccess"))
    ud = data.get("userDetails") or {}
    result.update({
        "message": message,
        "login_success": login_success,
        "full_name": ud.get("fullName"),
        "expiry_date": ud.get("expiryDate"),
        "start_date_time": ud.get("startDateTime"),
        "has_login_expired": ud.get("hasLoginExpired"),
        "num_used_test": ud.get("numUsedTest"),
        "num_allowed_test": ud.get("numAllowedTest"),
    })
    result["state"] = classify_amcat_message(message, login_success)
    return result


# platform -> async(client, url) -> dict. Only 'amcat' is reverse-engineered so far; add a platform
# here once its own no-browser probe (if one exists) is captured the same way.
CHECKERS = {"amcat": check_amcat_invite}


async def check_platform(platform: str, *, limit: int | None = None, include_done: bool = True,
                          concurrency: int = 15) -> list[dict]:
    """Probe every discovered invite for `platform` and return [{'mailbox':, 'url':, 'state':, ...}].

    Cheap + gentle: bounded concurrency, one HTTP POST per invite, no browser. `include_done=True`
    (the default here, unlike `discover.discover`'s own default) because a STATE check should cover
    already-attempted tokens too — that's exactly how you catch a stale 'stuck'/'evaluating' label in
    harvest_state.json that is actually long dead."""
    checker = CHECKERS.get(platform)
    if not checker:
        raise ValueError(f"no state-checker for platform {platform!r} (have {list(CHECKERS)})")
    invites = discover.discover(platform, limit=limit, include_done=include_done)
    results: list[dict] = []
    sem = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient() as client:
        async def _one(mbx: str, url: str) -> dict:
            async with sem:
                r = await checker(client, url)
            r["mailbox"] = mbx
            return r
        for coro in asyncio.as_completed([_one(mbx, url) for mbx, url in invites]):
            results.append(await coro)
    return results


def summarize(results: list[dict]) -> dict[str, int]:
    return dict(Counter(r.get("state", "unknown") for r in results))


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", default="amcat", choices=list(CHECKERS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=15)
    ap.add_argument("--samples", type=int, default=3, help="sample invite links to print per state")
    ap.add_argument("--exclude-done", action="store_true",
                     help="only never-attempted invites (default: check ALL, incl. already-attempted)")
    args = ap.parse_args()

    results = asyncio.run(check_platform(args.platform, limit=args.limit,
                                          include_done=not args.exclude_done,
                                          concurrency=args.concurrency))
    counts = summarize(results)
    print(f"{args.platform}: {len(results)} invite(s) probed")
    for state, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {state:20} {n}")
    by_state: dict[str, list[dict]] = {}
    for r in results:
        by_state.setdefault(r.get("state", "unknown"), []).append(r)
    for state, rows in by_state.items():
        print(f"\n--- {state} (sample of {min(args.samples, len(rows))}) ---")
        for r in rows[: args.samples]:
            print(f"  {r.get('mailbox', '?'):28} {r['url']}")


if __name__ == "__main__":
    _cli()
