"""Co-pilot daemon: a persistent HEADFUL Chromium on DISPLAY=:98 that the bot pre-fills
on command; the human watches it via noVNC on the phone and does any video/test. After
the fill the bot now also clicks Submit automatically (see _click_submit_after_fill) —
enabled by explicit user request, reversing the original human-submit-only design.

Reuses the apply strategies (fill + LLM-drafted answers). Runs on 127.0.0.1:8102, exposed
only via nginx + basic-auth alongside the dashboard. The résumé PDF is the one the batch
already rendered (uploads/prefill/<profile>/<job>/resume.pdf).

Own display/ports (:98 / vnc 5901 / novnc 6090) so it never collides with the lowercase
jobfinder co-pilot (:99 / 5900 / 6080) or lalafo-vnc (6081) on the same host.

    DISPLAY=:98 uvicorn backend.copilot:app --host 127.0.0.1 --port 8102
"""
import asyncio
import json
import logging
import os
import re
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright

from backend import status_store
from backend.applier.batch import _TERMINAL_STATUSES
from backend.applier.runner import _pick_strategy
from backend.dashboard_app import _load_jobs, _safe_id
from backend.profiles.facts import load_facts
from backend.profiles.store import get_profile
from backend.services.tailor.render import render_text
from backend.services.tailor.tailor import tailor_resume
from backend.services.tailor.variants import variant_for

logger = logging.getLogger(__name__)

os.environ.setdefault("DISPLAY", ":98")
# COPILOT_HEADLESS=1 launches Chromium headless (no noVNC watch) — used by the parallel
# bulk worker pool (backend/tools/bulk_pool.py) which runs N of these on their own ports.
# The default (unset) keeps the headful DISPLAY=:98 browser the human watches in noVNC.
HEADLESS = os.environ.get("COPILOT_HEADLESS") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFILL_ROOT = PROJECT_ROOT / "uploads" / "prefill"

# One shared headful browser = one reviewer at a time. A second profile loading a job
# would clobber the first person's mid-review form, so /load is owner-gated.
BUSY_TTL = 15 * 60  # seconds before an abandoned session stops blocking others

NOVNC_ADDR = ("127.0.0.1", int(os.environ.get("COPILOT_NOVNC_PORT", "6090")))  # websockify port the noVNC iframe talks to

# COPIED from extension/content.js CONFIRM_RE — the two MUST stay in sync.
CONFIRM_RE = re.compile(
    r"(thank you for applying|application (has been )?submitted|successfully submitted"
    r"|we have received your application|application received)", re.I)
_URL_RE = re.compile(r"confirm|thank", re.I)

WATCH_INTERVAL = 2.0        # seconds between confirmation polls
WATCH_MAX = 10 * 60         # give up after 10 minutes
# Parallel workers await the confirmation/email-code step INLINE (so a worker finishes one
# job fully before the next /load cancels its watch). The ATS confirmation is an EMAILED
# code that takes MINUTES to arrive, so this must be generous (the single co-pilot uses the
# full 10-min WATCH_MAX); we cap it at 5 min so throughput still moves (adaptive scaling adds
# workers to compensate for the longer per-job hold) and a genuinely stuck job falls to
# «Незавершённые», where «Докрутить» finishes it on the single co-pilot's 10-min watch.
WAIT_SUBMIT_MAX = 300

app = FastAPI(title="JobFinder co-pilot")
_S: dict = {"pw": None, "browser": None, "page": None, "ctx": None,
            "proxy_server": "", "lock": asyncio.Lock(),
            "current": None, "owner": None, "loaded_at": 0.0, "watch": None}


def looks_submitted(text: str, url: str) -> bool:
    """Pure confirmation matcher: page text against CONFIRM_RE, plus the same
    URL heuristic the extension uses (thank-you/confirmation pages)."""
    if CONFIRM_RE.search(text or ""):
        return True
    return bool(_URL_RE.search(url or ""))


def can_load(owner: str | None, loaded_at: float, requester: str, now: float) -> bool:
    """May `requester` take the shared page? Yes when it's free, theirs, or the
    current owner has been idle past BUSY_TTL (abandoned review)."""
    if not owner or owner == requester:
        return True
    return (now - (loaded_at or 0.0)) >= BUSY_TTL


# Image-upload controls (photo/avatar/headshot) must NEVER get the résumé PDF — mirrors
# base.attach_resume's résumé-field heuristic (but keeps "autofill", a résumé parser).
_PHOTO_FILE_RE = re.compile(r"photo|avatar|picture|headshot|selfie|logo")


def _is_photo_input(info: dict) -> bool:
    """True if a file input is an image/photo/avatar control (the résumé must not go here).
    `info` = {"acc": <accept attr>, "blob": <id+name+surrounding text>}, lower-cased."""
    return ("image/" in (info.get("acc") or "")) or bool(_PHOTO_FILE_RE.search(info.get("blob") or ""))


def _on_filechooser(fc):
    """Playwright intercepts the file chooser so no native OS dialog pops up on the
    shared headful page. Attach the current résumé (résumé-upload buttons) — but NOT to a
    photo/avatar/image field; if none applies, feed an empty list so the chooser still closes."""
    import asyncio as _a
    path = _S.get("resume_pdf")

    async def _handle():
        try:
            attach = bool(path)
            if attach:
                try:
                    info = await fc.element.evaluate(
                        '(el)=>{const c=el.closest("div,section,fieldset,form");'
                        'return {acc:(el.accept||"").toLowerCase(),'
                        ' blob:((el.id||"")+" "+(el.name||"")+" "+(c?c.innerText:""))'
                        '.toLowerCase()};}')
                    if _is_photo_input(info):
                        attach = False  # a headshot/avatar upload — leave it for the human
                except Exception:
                    pass
            await fc.set_files(path if attach else [])
        except Exception:
            pass
    _a.ensure_future(_handle())


async def _ensure_browser():
    """Launch (or relaunch) the persistent headful browser on the virtual display."""
    if _S["page"] is not None and not _S["page"].is_closed():
        return _S["page"]
    try:
        if _S["pw"] is None:
            _S["pw"] = await async_playwright().start()
        # Pass DISPLAY explicitly — playwright doesn't reliably forward it, and without it
        # headful Chromium renders to no display (invisible to noVNC).
        # Do NOT launch with proxy={"server":"per-context"}: in Playwright 1.49 that sentinel
        # makes EVERY context that doesn't set its own proxy fail with
        # net::ERR_PROXY_CONNECTION_FAILED (Chromium tries to reach a proxy literally named
        # "per-context") — i.e. an empty proxy pool → "no internet" in noVNC on every fill.
        # A plain launch still honors a per-CONTEXT proxy (verified: a context created with
        # proxy=… routes through it, one without goes DIRECT), so _use_proxy_context's
        # rotation keeps working while the empty-pool/direct case has real internet.
        _launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if not HEADLESS:
            _launch_args.insert(0, "--start-maximized")
        _S["browser"] = await _S["pw"].chromium.launch(
            headless=HEADLESS, args=_launch_args,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":98")})
        ctx = await _S["browser"].new_context(no_viewport=True)
        _S["ctx"], _S["proxy_server"] = ctx, ""
        _S["page"] = await ctx.new_page()
        # Intercept any button-triggered file picker so the NATIVE OS "Open File" dialog
        # never appears (it would block the shared page + cover the form in noVNC).
        # Attaching the current résumé here is the right action for résumé-upload buttons.
        _S["page"].on("filechooser", _on_filechooser)
        await _S["page"].goto("about:blank")
        return _S["page"]
    except Exception:
        logger.error("headful browser launch failed", exc_info=True)
        # Non-sticky: drop the half-initialized browser/page so the next /load
        # retries a clean launch instead of reusing broken state.
        browser, _S["browser"], _S["page"] = _S["browser"], None, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        raise


async def _use_proxy_context(server: str, username: str | None, password: str | None):
    """Route the active page through `server` by (re)building the browser context with
    that proxy — a fresh IP per application. Reuses the current context when the proxy
    is unchanged; server="" means a plain DIRECT context. Returns the page to fill.

    The old context (and its noVNC window) is closed after the new one is ready, so the
    watch-screen briefly flickers to the new page — expected. On any failure the caller
    falls back to the direct browser."""
    await _ensure_browser()  # guarantees _S["browser"] is alive
    want = server or ""
    if (_S.get("page") is not None and not _S["page"].is_closed()
            and _S.get("proxy_server", "") == want):
        return _S["page"]
    proxy_cfg = None
    if server:
        proxy_cfg = {"server": server}
        if username:
            proxy_cfg["username"] = username
            proxy_cfg["password"] = password or ""
    old_ctx = _S.get("ctx")
    ctx = (await _S["browser"].new_context(no_viewport=True, proxy=proxy_cfg)
           if proxy_cfg else await _S["browser"].new_context(no_viewport=True))
    page = await ctx.new_page()
    page.on("filechooser", _on_filechooser)
    await page.goto("about:blank")
    _S["ctx"], _S["page"], _S["proxy_server"] = ctx, page, want
    if old_ctx is not None:
        try:
            await old_ctx.close()
        except Exception:
            pass
    return page


@app.on_event("startup")
async def _startup():
    try:
        await _ensure_browser()
    except Exception:
        pass  # already logged at ERROR in _ensure_browser; /load will retry


async def _novnc_up() -> bool:
    """TCP probe of the noVNC websockify port — is the watch-screen reachable?"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(*NOVNC_ADDR), timeout=1.0)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


def _cancel_watch() -> None:
    """Stop the current submit-detection poller (a new /load supersedes it)."""
    task = _S.get("watch")
    if task is not None and not task.done():
        task.cancel()
    _S["watch"] = None


async def _watch_submit(page, profile: str, jid: str,
                        applicant_email: str = "", load_ts: float = 0.0) -> None:
    """Submit DETECTION + email-code AUTO-FILL + code-step confirm. Polls the live page:
      (1) when a Greenhouse-style 'enter the emailed security code' step appears (after the
          auto Submit), read that code from the candidate's OWN mailbox, fill it, and click
          the step's confirm/submit button (_click_code_confirm) to finalize. It never
          touches a captcha — if the step is captcha-gated the submit just waits for the human.
      (2) when the confirmation text/URL appears, mark the job submitted.
    Never navigates."""
    deadline = time.time() + WATCH_MAX
    code_done = False
    while time.time() < deadline:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            if page.is_closed():
                return
            # Shared single browser: if a concurrent job took over the co-pilot, this
            # session is stale — STOP so we never mark the wrong job submitted, nor fill a
            # code / click confirm on someone else's page. (_cancel_watch already fires on a
            # new /load or /goto; this guards the in-flight iteration.)
            if _S.get("current") != jid or _S.get("owner") not in (None, profile):
                return
            text = await page.inner_text("body", timeout=5000)
            if looks_submitted(text, page.url):
                status_store.mark(profile, jid, "submitted")
                logger.info("submit detected for %s/%s — marked submitted", profile, jid)
                return True
            if (applicant_email and not code_done
                    and re.search(r"(?i)verification code|security code|enter the .{0,20}code", text)):
                state = await page.evaluate(
                    "() => { const i = document.querySelector("
                    "\"input[aria-label*='code' i],input[placeholder*='code' i],"
                    "input[name*='code' i],input[id*='security' i],input[id*='code' i]\");"
                    " return i ? ((i.value||'').trim() ? 'filled':'empty') : 'nofield'; }")
                if state == "empty":
                    from backend.tools.verify_code import read_code
                    code = read_code(applicant_email, load_ts)
                    if code:
                        for sel in ("input[aria-label*='code' i]", "input[placeholder*='code' i]",
                                    "input[name*='code' i]", "input[id*='security' i]",
                                    "input[id*='code' i]"):
                            try:
                                el = page.locator(sel).first
                                if await el.count() and await el.is_visible(timeout=1000):
                                    # The code field is often a SEGMENTED OTP (one box per
                                    # char), so TYPE the code with real keystrokes — the widget
                                    # auto-advances box-to-box; .fill() would drop all but the
                                    # first char. Works for a single input too.
                                    await el.click()
                                    await page.keyboard.press("Control+A")
                                    await page.keyboard.press("Backspace")
                                    await page.keyboard.type(code, delay=60)
                                    code_done = True
                                    logger.info("auto-filled email security code for %s", applicant_email)
                                    break
                            except Exception:
                                continue
                    if code_done:
                        # Some OTP widgets auto-submit on the last digit; give that a beat,
                        # then click the step's confirm/submit button to finalize the apply.
                        await page.wait_for_timeout(1200)
                        try:
                            await _click_code_confirm(page)
                        except Exception:
                            logger.warning("code-step confirm click failed", exc_info=True)
        except Exception:
            continue  # transient (mid-navigation, detached body) — keep polling


def _apply_identity(u: str) -> tuple:
    """Company/job identity of an apply URL, host-normalized. Greenhouse embed
    (?for=cresta) and board (/cresta/jobs/..) collapse to ('greenhouse','cresta');
    ashby/lever/workable identify by host + first path segment (the company slug)."""
    try:
        p = urlparse(u or "")
    except Exception:
        return ("", "")
    host = (p.netloc or "").lower()
    seg = [s for s in (p.path or "").strip("/").split("/") if s]
    seg0 = seg[0].lower() if seg else ""
    if "greenhouse.io" in host:
        forq = parse_qs(p.query or "").get("for", [""])[0].lower()
        return ("greenhouse", forq or seg0)
    if "workable.com" in host:
        # Workable redirects /j/<shortcode>/apply -> /<company>/j/<shortcode>/apply on load,
        # so the first path segment flips from 'j' to the company slug. Identify by the STABLE
        # shortcode after '/j/' instead — else that legit same-form redirect reads as a page
        # drift and the race guard falsely aborts EVERY Workable submit.
        if "j" in seg and seg.index("j") + 1 < len(seg):
            return ("workable", seg[seg.index("j") + 1].lower())
    return (host, seg0)


def _same_apply_page(actual: str, expected: str) -> bool:
    """True when the live page is still the SAME company/job we filled — the race guard
    for the shared single browser."""
    return bool(actual) and bool(expected) and _apply_identity(actual) == _apply_identity(expected)


# Post-submit block/validation/captcha signals — turns a silent 'no confirmation' into a
# diagnosable outcome (a datacenter-IP submit is often gated by anti-bot / a required field).
_SUBMIT_BLOCK_RE = re.compile(
    r"(verify (you|that you).{0,20}human|are you (a )?robot|recaptcha|hcaptcha|captcha|"
    r"press (and hold|&)|we're updating your forms|please try again|something went wrong|"
    # NOTE: "please enter" was REMOVED — it false-matched the benign FILLED-form hint
    # "If you do not have a preferred name, please enter your legal name." (Samsara/Greenhouse),
    # which falsely flagged a fully-filled form as blocked → skipped the email-code watch →
    # killed an application that was completing fine. Keep only error-specific "please" wordings.
    r"this field is required|please (?:fill|complete|correct) (?:in |out |this|the|all|your)|is required|"
    # Real ATS rejection wordings observed on GH/Ashby (these were MISSED, so a rejected
    # submit was mislabeled "awaiting confirmation" and burned the full 300s watch):
    r"flagged as possible spam|turn off your (vpn|proxy)|couldn.?t submit(?: your)?|"
    r"missing entry|needs? correction|items? for (?:a )?required section|"
    r"please accept|accept the terms)", re.I)


async def _submit_evidence(page, shot_dir) -> dict:
    """Snapshot the page right after the submit click: url, a full-page screenshot, whether
    a confirmation OR a block/validation banner is visible. Best-effort, never raises."""
    ev = {"post_url": None, "confirmed": None, "blocked": None, "screenshot": None}
    try:
        ev["post_url"] = page.url
    except Exception:
        pass
    try:
        txt = await page.inner_text("body", timeout=4000)
        ev["confirmed"] = bool(looks_submitted(txt, ev.get("post_url") or ""))
        m = _SUBMIT_BLOCK_RE.search(txt or "")
        ev["blocked"] = m.group(0)[:60] if m else None
        # A page that already CONFIRMED or reached the emailed-security-code step has PROGRESSED
        # — the submit was accepted. Never let a stray block-phrase match on the filled form
        # (a field hint / privacy "please" text) flag it as blocked, which would SKIP the
        # code-fill watch (gated on `not blocked`) and kill an application that was completing.
        if ev["blocked"] and (ev["confirmed"] or re.search(
                r"(?i)verification code|security code|enter the .{0,25}code|"
                r"confirm you'?re a human|code (?:was |has been )?sent", txt or "")):
            ev["blocked"] = None
    except Exception:
        pass
    try:
        if shot_dir is not None:
            path = str(Path(shot_dir) / "after_submit.png")
            await page.screenshot(path=path, full_page=True)
            ev["screenshot"] = path
    except Exception:
        pass
    return ev


async def _click_submit_after_fill(page, result: dict, *, expected_url: str = "",
                                   profile: str = "", shot_dir=None, dry_run: bool = False) -> dict:
    """Press the ATS Submit button after the fill — but ONLY when it is safe to. Enabled by
    explicit user request (reverses the human-submit-only design, commit a8ab56e), yet it
    refuses to submit when doing so would be wrong:
      - INCOMPLETE: any unfilled required field (e.g. Lever 'Current location' the datacenter
        geocode can't set) -> leave it for the human.
      - NEEDS REVIEW: any answer flagged for human review ([review] safety contract — a
        synthetic persona's behavioral/unbacked answers must be seen before they go live).
      - RACE: the shared co-pilot page drifted to a different company/job, or another run
        took ownership -> abort (never submit the wrong form).
    On a real click it captures post-submit evidence (screenshot + block/confirm detection).
    `dry_run=True` runs every gate and locates the Submit button but does NOT click it
    (returns `would_click`) — for verifying coverage across many forms WITHOUT submitting.
    Returns a dict describing the outcome; never raises."""
    unfilled = (result.get("unfilled") or []) if isinstance(result, dict) else []
    review = (result.get("review_items") or []) if isinstance(result, dict) else []
    if unfilled:
        logger.info("auto-submit skipped: %d unfilled required field(s): %s", len(unfilled), unfilled[:5])
        return {"clicked": False, "reason": "incomplete", "unfilled": unfilled[:8]}
    if review:
        logger.info("auto-submit skipped: %d answer(s) need human review", len(review))
        return {"clicked": False, "reason": "needs_review",
                "review": [str(r)[:60] for r in review[:8]]}
    if _S.get("owner") not in (None, profile):
        return {"clicked": False, "reason": "preempted", "owner": _S.get("owner")}
    try:
        cur_url = page.url
    except Exception:
        cur_url = ""
    if expected_url and not _same_apply_page(cur_url, expected_url):
        logger.warning("auto-submit ABORTED (page drift): live=%s expected=%s", cur_url, expected_url)
        return {"clicked": False, "reason": "page_drift", "actual": cur_url, "expected": expected_url}
    try:
        from backend.applier import analyzer, filler
        sel = (result.get("submit_selector") if isinstance(result, dict) else None) \
            or await analyzer.find_submit_button(page)
        if not sel:
            logger.warning("auto-submit: no submit button found on the page")
            return {"clicked": False, "reason": "no_button"}
        if dry_run:
            # All gates passed and the Submit button is present — but do NOT click.
            # Still snapshot the FILLED form (no click) so coverage can be eyeballed.
            ev = await _submit_evidence(page, shot_dir)
            return {"clicked": False, "would_click": True, "reason": "would_click",
                    "selector": sel, "dry_run": True, **ev}
        # Let async validation / a just-started résumé upload settle (Ashby rejects a
        # same-instant Submit), then re-check the page hasn't drifted before the click.
        await page.wait_for_timeout(1500)
        if expected_url and not _same_apply_page(page.url, expected_url):
            return {"clicked": False, "reason": "page_drift", "actual": page.url, "expected": expected_url}
        clicked = await filler.click_submit(page, {"submit_selector": sel})
        await page.wait_for_timeout(1500)
        ev = await _submit_evidence(page, shot_dir)
        logger.info("auto-submit: clicked=%s sel=%s post=%s confirmed=%s blocked=%s",
                    clicked, sel, ev.get("post_url"), ev.get("confirmed"), ev.get("blocked"))
        return {"clicked": bool(clicked), "reason": "clicked" if clicked else "click_failed",
                "selector": sel, **ev}
    except Exception:
        logger.warning("auto-submit failed", exc_info=True)
        return {"clicked": False, "reason": "error"}


# Confirm/submit button on the emailed-security-code step (Greenhouse & co). Verify/Confirm
# come first (that step's primary action); Submit/Continue are the Greenhouse-reuse fallbacks.
_CODE_CONFIRM_SELECTORS = (
    'button:has-text("Verify")',
    'button:has-text("Confirm")',
    'button:has-text("Submit Application")',
    'button:has-text("Submit")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button[type="submit"]',
    'input[type="submit"]',
)


async def _click_code_confirm(page) -> bool:
    """After the emailed security code is typed, click the step's confirm/submit button to
    finalize (the second Submit on a Greenhouse-style email-verification step). Best-effort:
    many OTP widgets auto-submit on the last digit, so if no button is visible this is a
    no-op; a captcha on the step will just block the submit for the human to finish."""
    for sel in _CODE_CONFIRM_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(1500)
                logger.info("code-step confirm clicked: %s", sel)
                return True
        except Exception:
            continue
    return False


@app.get("/health")
async def health():
    ok = _S["page"] is not None and not _S["page"].is_closed()
    return {"ok": True, "browser": ok, "novnc": await _novnc_up(),
            "current": _S["current"], "owner": _S["owner"]}


@app.post("/release")
async def release(profile: str = Form("michael")):
    """Free the shared page: the owner is done, or anyone after the owner went idle."""
    profile = _safe_id(profile) or "michael"
    owner = _S["owner"]
    if owner and (owner == profile or time.time() - _S["loaded_at"] >= BUSY_TTL):
        _S["owner"] = None
        _S["loaded_at"] = 0.0
        return {"released": True}
    return {"released": owner is None, "owner": owner}


@app.post("/goto")
async def goto(url: str = Form(...)):
    """Fast navigation ONLY — point the shared headful browser at a job's apply URL so
    noVNC shows the RIGHT job the instant "Заполнить" is clicked, BEFORE the (slower)
    draft-gen + /load fill. Without this the browser stays on the PREVIOUS job during
    draft generation and noVNC shows a stale page. No fill; ownership is claimed later by
    /load. Preempts a demo owner (every catalog click is a fresh demo persona) but leaves
    a real human's mid-review page alone."""
    url = (url or "").strip()
    if not url.startswith("http"):
        return JSONResponse({"error": "bad url"}, status_code=400)
    if str(_S.get("owner") or "").startswith("demo_"):
        _S["owner"] = None
    if not can_load(_S["owner"], _S["loaded_at"], "__preview__", time.time()):
        return JSONResponse({"busy": _S.get("owner")}, status_code=423)
    async with _S["lock"]:
        _cancel_watch()
        page = await _ensure_browser()
        try:
            await page.evaluate("1")
        except Exception:
            browser, _S["browser"], _S["page"] = _S.get("browser"), None, None
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            page = await _ensure_browser()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _S["current"] = None  # not owned yet — the following /load fill claims it
        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)
    return JSONResponse({"navigated": url})


@app.post("/load")
async def load(jobid: str = Form(...), profile: str = Form("michael"), dry_run: str = Form(""),
               proxy_server: str = Form(""), proxy_username: str = Form(""),
               proxy_password: str = Form(""), wait_submit: str = Form("")):
    profile = _safe_id(profile) or "michael"
    jobid = _safe_id(jobid)
    is_dry = str(dry_run).strip().lower() in ("1", "true", "yes", "on")
    is_wait = str(wait_submit).strip().lower() in ("1", "true", "yes", "on")
    # A demo persona (synth_persona, id demo_*) is ephemeral — never a human mid-review.
    # Every demo click is a NEW persona, so the per-owner busy gate would leave the previous
    # demo's page stuck ("shows my old requests"). Preempt a demo owner so the new load wins.
    if str(_S.get("owner") or "").startswith("demo_"):
        _S["owner"] = None
    if not can_load(_S["owner"], _S["loaded_at"], profile, time.time()):
        return JSONResponse(
            {"error": f"co-pilot busy: {_S['owner']} is reviewing — try later or POST /release"},
            status_code=423)
    d = PREFILL_ROOT / profile / jobid
    rep_file = d / "report.json"
    if not rep_file.exists():
        return JSONResponse({"error": "job not found"}, status_code=404)
    rep = json.loads(rep_file.read_text(encoding="utf-8"))
    url = rep.get("apply_url", "")
    title, company = rep.get("job_title", ""), rep.get("company", "")

    async with _S["lock"]:
        _cancel_watch()  # the previous job's page is going away with this goto
        page = await _ensure_browser()
        # The persistent headful browser can die between loads (Xvfb/CDP hiccup, EPIPE,
        # a crashed tab) while the page reference still reports open — the old frame then
        # stays frozen in noVNC and the next goto throws 'session closed', so a NEW job
        # opens onto a STALE page. Ping the page; on failure tear the browser down and
        # relaunch a clean one so a fresh load never lands on a dead/stale page.
        try:
            await page.evaluate("1")
        except Exception:
            logger.warning("co-pilot page unresponsive — relaunching browser for a clean load")
            browser, _S["browser"], _S["page"] = _S.get("browser"), None, None
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            page = await _ensure_browser()
        # Give THIS application its assigned egress IP (fresh context per proxy). Empty
        # proxy_server -> stay on the current (direct) context. Best-effort: a proxy that
        # fails to build must not sink the fill — fall back to the direct browser.
        if proxy_server.strip():
            try:
                page = await _use_proxy_context(proxy_server.strip(),
                                                proxy_username.strip() or None,
                                                proxy_password)
            except Exception:
                logger.warning("proxy context setup failed (%s) — continuing direct",
                               proxy_server, exc_info=True)
                page = await _ensure_browser()
        try:
            prof = get_profile(profile)
            facts = load_facts(profile)
        except KeyError:
            # a synthetic demo persona (synth_persona) isn't in the roster store — load it
            # from the prefill dir instead of crashing with "profile not found".
            pj = d / "persona.json"
            if not pj.exists():
                return JSONResponse(
                    {"error": "demo persona not saved — re-generate the fill"}, status_code=404)
            persona = json.loads(pj.read_text(encoding="utf-8"))
            from backend.profiles.store import Profile
            prof = Profile.from_dict(persona.get("profile") or {})
            facts = persona.get("facts") or {}
        niche, variant = variant_for({"title": title, "description": ""}, prof)
        form = prof.to_form_dict()
        if variant and variant.get("years_experience"):
            form["years_experience"] = variant["years_experience"]
        base = variant or prof.resume
        tailored = tailor_resume(base, title, company, "", use_ai=False)
        resume_pdf = str(d / "resume.pdf")
        _S["resume_pdf"] = resume_pdf  # used by the filechooser interceptor
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            strat = _pick_strategy(url)
            known = rep.get("drafted_answers") or {}  # backed picks + drafts replay instantly
            # draft=True always: the per-person answer cache makes re-drafting open
            # questions a no-op cost-wise, and unbacked choice questions SHOULD be
            # re-evaluated on reload (they're never replayed via known_answers).
            result = await strat.prefill(page, form, resume_pdf,
                                         job={"title": title, "company": company},
                                         draft=True,
                                         resume_summary=render_text(tailored),
                                         known_answers=known,
                                         facts=facts,
                                         profile_id=profile, niche=niche or "")
            _S["current"] = jobid
            _S["owner"] = profile
            _S["loaded_at"] = time.time()
            # AUTO-SUBMIT: press the ATS Submit button right after the fill (explicit user
            # request — see _click_submit_after_fill), but ONLY when the form is complete,
            # nothing needs review, and the shared page is still ours. The watch below then
            # records the confirmation page into status.json.
            submit_result = await _click_submit_after_fill(
                page, result, expected_url=url, profile=profile, shot_dir=d, dry_run=is_dry)
            # Watch for the resulting confirmation (also auto-fills an emailed security-code
            # step if the ATS shows one). Skip in dry_run — nothing was submitted.
            _email = (form.get("email") or "").strip()
            # If the ATS already REJECTED the submit at validation (blocked — a required field
            # our fill missed), there is no confirmation coming: don't waste the worker on a
            # WAIT_SUBMIT_MAX watch (that's what turned blocked jobs into ReadTimeout `error`
            # rows). Return immediately; the job lands in «Незавершённые» for the human.
            if not is_dry and not submit_result.get("blocked"):
                if is_wait and submit_result.get("clicked"):
                    # Parallel worker: FINISH the email-code + confirmation step INLINE, so
                    # this worker doesn't grab the next job (whose /load would _cancel_watch)
                    # before Ashby's code step completes. Bounded by WAIT_SUBMIT_MAX; if it
                    # doesn't confirm in time the job falls to «Незавершённые».
                    try:
                        ok = await asyncio.wait_for(
                            _watch_submit(page, profile, jobid, _email, time.time()),
                            timeout=WAIT_SUBMIT_MAX)
                        submit_result["confirmed"] = bool(ok)
                    except asyncio.TimeoutError:
                        submit_result.setdefault("confirmed", False)
                    except Exception:
                        submit_result.setdefault("confirmed", False)
                else:
                    _S["watch"] = asyncio.create_task(_watch_submit(
                        page, profile, jobid, _email, time.time()))
            return JSONResponse({"loaded": jobid, "company": company, "title": title,
                                 "submitted_click": submit_result.get("clicked"),
                                 "submit_result": submit_result,
                                 "filled": result.get("filled"),
                                 "unfilled": len(result.get("unfilled") or []),
                                 "unfilled_list": result.get("unfilled") or [],
                                 "choice_picks": result.get("choice_picks") or {},
                                 "review_items": result.get("review_items") or [],
                                 "answer_sources": result.get("answer_sources") or {},
                                 "page_type": result.get("page_type")})
        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.post("/mark_submitted")
async def mark_submitted(profile: str = Form("michael"), jid: str = Form(...)):
    """Manual fallback for the auto-detector: the human confirms they submitted."""
    profile = _safe_id(profile) or "michael"
    jid = _safe_id(jid)
    if not jid:
        return JSONResponse({"error": "bad jid"}, status_code=400)
    status_store.mark(profile, jid, "submitted")
    if _S.get("current") == jid:
        _cancel_watch()  # already marked by hand — nothing left to detect
    return {"marked": jid, "status": "submitted"}


_CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#0f1216;color:#e7ebf0}
.bar{position:sticky;top:0;background:#171c23;border-bottom:1px solid #232a33;padding:10px;z-index:5}
.hint{font-size:12px;color:#8a94a3;padding:8px 10px}
iframe{width:100%;height:62vh;border:0;background:#000;display:block}
.jobs{padding:8px}
.job{background:#171c23;border:1px solid #232a33;border-radius:10px;padding:10px;margin:8px 0;display:flex;justify-content:space-between;align-items:center;gap:8px}
.j b{font-size:14px}.j span{font-size:11px;color:#8a94a3;display:block}
button{font-size:13px;font-weight:600;padding:10px 12px;border-radius:8px;border:0;cursor:pointer}
.load{background:#2563eb;color:#fff}.done{background:#10391f;color:#46d17f;border:1px solid #1c5e35}
.mark{background:#171c23;color:#46d17f;border:1px solid #1c5e35}
.btns{display:flex;gap:6px;flex-shrink:0}
.badge{font-size:10px;font-weight:700;padding:3px 7px;border-radius:20px}
.ready{background:#10391f;color:#46d17f}.warn{background:#3a2f10;color:#e0b341}.sub{background:#10293a;color:#49a8e0}
.intv{background:#2a103a;color:#c061e0}.rej{background:#3a1010;color:#e05a5a}
#status{font-size:12px;color:#46d17f;min-height:14px;padding:4px 10px}
"""

_JS = """
async function loadJob(jid){
  const s=document.getElementById('status'); s.textContent='Filling '+jid+' ...';
  try{
    const r=await fetch('/copilot/load',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'jobid='+encodeURIComponent(jid)+'&profile='+encodeURIComponent(PROFILE)});
    const j=await r.json();
    if(j.error){if(r.status===423){alert(j.error);}s.textContent='Error: '+j.error;return;}
    var sr=j.submit_result||{}; var msg=sr.clicked?('Submit pressed'+(sr.confirmed?' — confirmed':(sr.blocked?' — blocked: '+sr.blocked:' — awaiting confirmation'))):('not submitted ('+(sr.reason||'?')+') — review above');
    s.textContent='✓ Filled '+(j.company||'')+' — '+msg+'. (filled '+j.filled+', left '+j.unfilled+')';
    document.getElementById('vnc').contentWindow.location.reload();
  }catch(e){s.textContent='Error: '+e;}
}
async function markSubmitted(jid,btn){
  const s=document.getElementById('status');
  try{
    const r=await fetch('/copilot/mark_submitted',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'jid='+encodeURIComponent(jid)+'&profile='+encodeURIComponent(PROFILE)});
    const j=await r.json();
    if(j.error){s.textContent='Error: '+j.error;return;}
    if(btn){btn.textContent='✓ submitted';btn.className='done';btn.disabled=true;}
    s.textContent='✓ Marked submitted — it will leave the queue on reload.';
  }catch(e){s.textContent='Error: '+e;}
}
"""


@app.get("/", response_class=HTMLResponse)
async def home(profile: str = "michael"):
    profile = _safe_id(profile) or "michael"
    jobs = _load_jobs(profile)
    rows = []
    for j in jobs:
        if j["_status"] in _TERMINAL_STATUSES:
            continue  # submitted/rejected/interview — nothing left to fill
        rows.append(
            "<div class='job'><div class='j'>"
            f"<b>{escape(j.get('company',''))}</b>"
            f"<span>{escape(j.get('job_title','')[:48])} · <span class='badge {j['_cls']}'>{escape(j['_badge'])}</span></span>"
            "</div>"
            "<div class='btns'>"
            f"<button class='load' onclick=\"loadJob('{escape(j['_id'])}')\">Fill →</button>"
            f"<button class='mark' onclick=\"markSubmitted('{escape(j['_id'])}',this)\">✓ mark submitted</button>"
            "</div>"
            "</div>")
    body = "".join(rows) or "<div class='hint'>No open jobs in the queue.</div>"
    novnc = "/vnc/vnc_lite.html?path=vnc/websockify&autoconnect=1&resize=scale&reconnect=1"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Co-pilot — {escape(profile)}</title><style>{_CSS}</style></head><body>"
        f"<div class='bar'><b>Co-pilot</b> — tap <b>Fill →</b>; the form is filled and Submit is pressed automatically (watch above).</div>"
        f"<iframe id='vnc' src='{novnc}'></iframe>"
        "<div id='status'></div>"
        f"<div class='jobs'>{body}</div>"
        f"<script>const PROFILE={json.dumps(profile)};{_JS}</script>"
        "</body></html>")


@app.get("/state")
async def state():
    page = _S["page"]
    if page is None or page.is_closed():
        return {"error": "no page"}
    vals = await page.evaluate('''()=>[...document.querySelectorAll(".select__container")].map(c=>{const l=c.querySelector("label,.select__label");const v=c.querySelector(".select__single-value");return {label:(l?l.textContent.trim().slice(0,46):"?"),value:(v?v.textContent.trim():"EMPTY")};})''')
    return {"url": page.url, "selects": vals}
