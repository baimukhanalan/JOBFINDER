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
# pm2 runs uvicorn with --log-level warning, which silences every app-level INFO line (auto-submit
# verdicts, session warm-up, the submit-mutation capture). Give this logger its own INFO handler so
# those diagnostics reach the pm2 error log regardless of uvicorn's level (idempotent on reload).
if not any(getattr(h, "_copilot_info", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s copilot %(levelname)s %(message)s"))
    _h._copilot_info = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

os.environ.setdefault("DISPLAY", ":98")
# COPILOT_HEADLESS=1 launches Chromium headless (no noVNC watch) — used by the parallel
# bulk worker pool (backend/tools/bulk_pool.py) which runs N of these on their own ports.
# The default (unset) keeps the headful DISPLAY=:98 browser the human watches in noVNC.
HEADLESS = os.environ.get("COPILOT_HEADLESS") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFILL_ROOT = PROJECT_ROOT / "uploads" / "prefill"

# COPILOT_NOPECHA=1 loads the vendored NopeCHA captcha-solver extension (auto-solves
# hCaptcha / reCAPTCHA / Cloudflare Turnstile IN-PAGE). Loading an extension REQUIRES a
# persistent context, so this flag also switches the launch to launch_persistent_context.
# Default off => the live co-pilot launch is byte-identical. COPILOT_PROXY sets a
# launch-time egress (Ashby/Salmon need a residential/mobile IP; captcha ATSes are direct).
NOPECHA_ON = os.environ.get("COPILOT_NOPECHA") == "1"
NOPECHA_EXT = str(PROJECT_ROOT / "backend" / "vendor" / "nopecha_ext")
NOPECHA_PROFILE = os.environ.get("COPILOT_NOPECHA_PROFILE", "/tmp/copilot_nopecha_profile")
LAUNCH_PROXY = os.environ.get("COPILOT_PROXY", "").strip()

# One shared headful browser = one reviewer at a time. A second profile loading a job
# would clobber the first person's mid-review form, so /load is owner-gated.
BUSY_TTL = 15 * 60  # seconds before an abandoned session stops blocking others

NOVNC_ADDR = ("127.0.0.1", int(os.environ.get("COPILOT_NOVNC_PORT", "6090")))  # websockify port the noVNC iframe talks to

# COPIED from extension/content.js CONFIRM_RE — the two MUST stay in sync.
CONFIRM_RE = re.compile(
    r"(thank you for applying|application (has been )?submitted|successfully submitted"
    r"|we have received your application|application received"
    # Avature/Maximus completion page: "…no assessment is required and you have completed
    # the application process." (a variant with no "thank you for applying" on the page).
    r"|completed the application process|you have completed the application"
    r"|your application is complete)", re.I)
_URL_RE = re.compile(r"confirm|thank", re.I)

WATCH_INTERVAL = 2.0        # seconds between confirmation polls
WATCH_MAX = 10 * 60         # give up after 10 minutes
# Parallel workers await the confirmation/email-code step INLINE (so a worker finishes one
# job fully before the next /load cancels its watch). The ATS confirmation is an EMAILED
# code that takes MINUTES to arrive, so this must be generous (the single co-pilot uses the
# full 10-min WATCH_MAX); we cap it at 5 min so throughput still moves (adaptive scaling adds
# workers to compensate for the longer per-job hold) and a genuinely stuck job falls to
# «Незавершённые», where «Докрутить» finishes it on the single co-pilot's 10-min watch.
# 120s not 300s: measured the emailed security code → ATS ack gap is a MEDIAN 33s (max ~67s),
# so a job that hasn't confirmed in 120s is almost certainly dead (spam/blocked), not slow. The
# old 300s held a worker on a dead job for 5 min for nothing; 120s frees it ~2.5× faster (big
# throughput win — batches are dominated by the failing jobs' wait). A rare slow code is still
# caught later by the ground-truth reconciler via the ATS receipt, so nothing is lost.
WAIT_SUBMIT_MAX = 120

# The emailed-code step that gates the final submit on GH/Ashby ("security code"), and on
# Oracle ORC / Candidate Experience ("verification code" / "verify your email" PIN). ONE prompt
# regex + ONE code-field selector list recognize the step across all three ATSes. The Oracle
# selectors are APPENDED after the GH/Ashby ones (which stay first), so the existing GH/Ashby
# behaviour is unchanged; the extra prompt wordings only make the trigger fire MORE often, and
# the code-fill only proceeds when a matching EMPTY field is found AND a code is read from the
# candidate's own mailbox — so a false trigger is a harmless no-op.
_CODE_STEP_RE = re.compile(
    r"(?i)verification code|security code|enter the .{0,20}code|verify your email"
    r"|enter (?:the )?(?:pin|code)|one.?time (?:code|password|pin)")
_CODE_FIELD_SELECTORS = (
    "input[aria-label*='code' i]", "input[placeholder*='code' i]",
    "input[name*='code' i]", "input[id*='security' i]", "input[id*='code' i]",
    # Oracle ORC (JET) — the PIN input's id/aria-label carry 'pin'/'verification',
    # not always 'code'. Appended, so GH/Ashby match their existing selectors first.
    "input[aria-label*='verification' i]", "input[aria-label*='pin' i]",
    "input[id*='pin' i]", "input[name*='pin' i]")

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
        if NOPECHA_ON:
            # An extension can only load through a persistent context. Launch one with the
            # NopeCHA ext, optionally through a fixed launch-time proxy (COPILOT_PROXY), then
            # arm NopeCHA via its setup URL. The persistent CONTEXT doubles as the "browser"
            # handle here (it has .close()); _use_proxy_context is not used in this mode
            # (the launch-time proxy is the egress), so /load must not pass proxy_server.
            ext_args = ([f"--disable-extensions-except={NOPECHA_EXT}",
                         f"--load-extension={NOPECHA_EXT}"] if os.path.isdir(NOPECHA_EXT) else [])
            lk = dict(headless=False, no_viewport=True, args=_launch_args + ext_args,
                      ignore_https_errors=True,
                      env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":98")})
            if LAUNCH_PROXY:
                pc = {"server": LAUNCH_PROXY}
                _pu = os.environ.get("COPILOT_PROXY_USER", "").strip()
                if _pu:
                    pc["username"] = _pu
                    pc["password"] = os.environ.get("COPILOT_PROXY_PASS", "")
                lk["proxy"] = pc
            os.makedirs(NOPECHA_PROFILE, exist_ok=True)
            ctx = await _S["pw"].chromium.launch_persistent_context(NOPECHA_PROFILE, **lk)
            _S["browser"], _S["ctx"], _S["proxy_server"] = ctx, ctx, LAUNCH_PROXY
            _S["page"] = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _S["page"].on("filechooser", _on_filechooser)
            if ext_args:
                try:
                    _key = os.environ.get("NOPECHA_KEY", "").strip()
                    _cfg = ("input_method=javascript|hcaptcha_auto_open=true|hcaptcha_auto_solve=true|"
                            "recaptcha_auto_solve=true|turnstile_auto_solve=true|"
                            "hcaptcha_solve_delay_time=200|enabled=true"
                            + (f"|key={_key}" if _key else ""))
                    sp = await ctx.new_page()
                    await sp.goto("https://nopecha.com/setup#" + _cfg,
                                  wait_until="domcontentloaded", timeout=45000)
                    await sp.wait_for_timeout(3500)
                    await sp.close()
                    logger.info("NopeCHA armed (%s, proxy=%s)",
                                "key" if _key else "free tier", LAUNCH_PROXY or "direct")
                except Exception:
                    logger.warning("NopeCHA arm failed", exc_info=True)
            await _S["page"].goto("about:blank")
            return _S["page"]
        _lk = dict(headless=HEADLESS, env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":98")},
                   **_stealth_launch_kwargs(_launch_args))
        try:
            # Real Google Chrome = authentic TLS/UA fingerprint (bundled Chromium is a bot tell
            # reCAPTCHA v3 scores down); falls back to the bundled build if the channel is missing.
            _S["browser"] = (await _S["pw"].chromium.launch(channel="chrome", **_lk) if STEALTH_ON
                             else await _S["pw"].chromium.launch(**_lk))
        except Exception:
            logger.warning("real Chrome launch failed — using bundled Chromium", exc_info=True)
            _S["browser"] = await _S["pw"].chromium.launch(**_lk)
        ctx = await _new_ctx(None)
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
    ctx = await _new_ctx(proxy_cfg)
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


# ---- reCAPTCHA-v3-friendly browser posture (2026-09-13) ------------------------------------
# Ashby's application form loads reCAPTCHA v3 (`api.js?render=<site key>`) and sends a
# `recaptchaToken` with the submit mutation; a LOW score = "flagged as possible spam". A bare
# Playwright browser scores as a bot (navigator.webdriver, --enable-automation, bundled-Chromium
# TLS) — PROVEN: the same complete Dana form submitted through a real phone's CELLULAR IP was
# still flagged (E1), so the network was not the tell. The fill browser therefore takes the
# project's stealth posture (applier/browser.py: real Chrome channel, no automation switch,
# AutomationControlled off, the _STEALTH init script, en-US / Asia/Almaty) and pauses like a
# human before pressing Submit (v3 scores interaction too). COPILOT_STEALTH=0 restores the plain
# launch.
STEALTH_ON = os.environ.get("COPILOT_STEALTH", "1") != "0"


def _stealth_launch_kwargs(base_args: list[str]) -> dict:
    if not STEALTH_ON:
        return dict(args=base_args)
    return dict(ignore_default_args=["--enable-automation"],
                args=base_args + ["--disable-blink-features=AutomationControlled",
                                  "--disable-features=IsolateOrigins,site-per-process"])


# COPILOT_FP_DIVERSIFY=1: make each fill context look like a DIFFERENT device to Ashby's own
# `deviceFingerprint` (sent with the submit mutation next to the reCAPTCHA token). Every one of
# our submissions ever came from the one Xvfb :98 machine (same screen, fonts, canvas, WebGL), so
# after a burst of flagged submits the whole device+IP cluster is penalised regardless of IP,
# browser flags or email domain (proven 2026-09-13: five complete fills through three IPs, two
# domains, stealth on/off, a never-touched tenant — all flagged). Per-context: a common laptop
# screen size, a plausible cores/memory pair, and a faint deterministic canvas/audio perturbation
# (a NEW hash per context, stable within it — a fingerprint that changes mid-session is itself a
# tell). ON by default since 2026-09-13: with this + the session warm-up, a Dana Erlan application
# to Salmon was ACCEPTED ("Your application was successfully submitted") on the very WiFi IP that had
# been flagged minutes earlier — the network was never the decisive signal. COPILOT_FP_DIVERSIFY=0
# disables.
FP_DIVERSIFY = os.environ.get("COPILOT_FP_DIVERSIFY", "1") != "0"
_FP_SCREENS = ((1366, 768), (1440, 900), (1536, 864), (1600, 900), (1680, 1050), (1920, 1080))

_FP_DIVERSIFY_JS = """(() => {
  const seed = %(seed)d;
  let s = seed >>> 0;
  const rnd = () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => %(cores)d });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => %(mem)d });
  // canvas: perturb a handful of pixels deterministically per context (invisible, hash-changing)
  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
  const tweak = (ctx, w, h) => {
    try {
      if (!w || !h) return;
      const img = _getImageData.call(ctx, 0, 0, w, h);
      const d = img.data;
      for (let i = 0; i < 12; i++) {
        const p = (Math.floor(rnd() * (d.length / 4))) * 4;
        d[p] = (d[p] + 1) & 255;
      }
      ctx.putImageData(img, 0, 0);
    } catch (e) {}
  };
  HTMLCanvasElement.prototype.toDataURL = function (...a) {
    const c = this.getContext && this.getContext('2d');
    if (c) tweak(c, this.width, this.height);
    return _toDataURL.apply(this, a);
  };
  CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h, ...r) {
    const img = _getImageData.call(this, x, y, w, h, ...r);
    const d = img.data;
    for (let i = 0; i < 6 && d.length > 4; i++) {
      const p = (Math.floor(rnd() * (d.length / 4))) * 4;
      d[p] = (d[p] + 1) & 255;
    }
    return img;
  };
  // audio: a tiny deterministic offset in the analyser output
  if (window.AudioBuffer) {
    const _gcd = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function (ch) {
      const data = _gcd.call(this, ch);
      const off = (seed %% 97) * 1e-7;
      for (let i = 0; i < data.length; i += 1000) data[i] = data[i] + off;
      return data;
    };
  }
})();"""


def _fp_profile() -> dict:
    """One plausible device profile per context (seeded per call)."""
    import random
    w, h = random.choice(_FP_SCREENS)
    return {"seed": random.randrange(1, 2**31 - 1), "cores": random.choice((4, 8, 12)),
            "mem": random.choice((4, 8, 16)), "w": w, "h": h}


async def _new_ctx(proxy_cfg: dict | None):
    """A fill context: per-proxy egress + (stealth) a real locale/timezone and the anti-automation
    init script, so the reCAPTCHA v3 token minted at Submit carries a human-looking browser."""
    kw: dict = {"no_viewport": True}
    if STEALTH_ON:
        kw.update(locale="en-US", timezone_id="Asia/Almaty", color_scheme="light")
    fp = None
    if FP_DIVERSIFY:
        fp = _fp_profile()
        kw.pop("no_viewport", None)
        kw.update(viewport={"width": fp["w"], "height": fp["h"] - 120},
                  screen={"width": fp["w"], "height": fp["h"]})
    if proxy_cfg:
        kw["proxy"] = proxy_cfg
    ctx = await _S["browser"].new_context(**kw)
    if fp:
        try:
            await ctx.add_init_script(_FP_DIVERSIFY_JS % fp)
            logger.info("fp-diversify: screen %dx%d cores=%d mem=%d seed=%d",
                        fp["w"], fp["h"], fp["cores"], fp["mem"], fp["seed"])
        except Exception:
            logger.warning("fp-diversify script not applied", exc_info=True)
    if STEALTH_ON:
        try:
            from backend.applier.browser import _STEALTH
            await ctx.add_init_script(_STEALTH)
            # _STEALTH pins navigator.platform to MacIntel (for the spoofed macOS UA of the bundled
            # build); with REAL Linux Chrome the UA says X11/Linux, so keep platform coherent —
            # a UA/platform mismatch is itself a fingerprint tell.
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });")
        except Exception:
            logger.warning("stealth init script not applied", exc_info=True)
    return ctx


async def _human_dwell(page, submit_selector: str) -> None:
    """A few seconds of human-looking activity (mouse travel, a pause) before Submit — reCAPTCHA
    v3 scores interaction, and an instant post-fill click is a bot tell. Never raises."""
    if not STEALTH_ON:
        return
    try:
        import random
        btn = page.locator(submit_selector).first
        try:
            await btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        for _ in range(random.randint(4, 7)):
            await page.mouse.move(random.randint(200, 1000), random.randint(150, 800),
                                  steps=random.randint(8, 20))
            await page.wait_for_timeout(random.randint(350, 900))
        try:
            box = await btn.bounding_box()
            if box:
                await page.mouse.move(box["x"] + box["width"] / 2 + random.randint(-15, 15),
                                      box["y"] + box["height"] / 2 + random.randint(-5, 5), steps=15)
        except Exception:
            pass
        await page.wait_for_timeout(random.randint(5000, 9000))
    except Exception:
        logger.debug("human dwell skipped", exc_info=True)


async def _warm_session(page, company_key: str = "") -> None:
    """Warm the browser session like a person arriving at the form: Google (consent + a search
    typed at human speed) and the employer's careers root BEFORE the application page. reCAPTCHA v3
    scores a cookie-less, history-less context low no matter the IP; a warmed session carries
    Google cookies + a navigation trail. Best-effort, bounded, never raises."""
    if not STEALTH_ON:
        return
    try:
        import random
        await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(random.randint(1200, 2200))
        for sel in ('button:has-text("Accept all")', 'button:has-text("I agree")',
                    'button#L2AGLb', 'button:has-text("Reject all")'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=800):
                    await b.click(timeout=2000)
                    break
            except Exception:
                continue
        q = f"{company_key or 'ashby'} careers".replace("-", " ")
        try:
            box = page.locator('textarea[name="q"], input[name="q"]').first
            await box.click(timeout=3000)
            await page.keyboard.type(q, delay=random.randint(70, 140))
            await page.wait_for_timeout(random.randint(600, 1200))
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(random.randint(2500, 4000))
            await page.mouse.wheel(0, random.randint(300, 900))
            await page.wait_for_timeout(random.randint(800, 1600))
        except Exception:
            pass
        if company_key:
            try:
                await page.goto(f"https://jobs.ashbyhq.com/{company_key}", wait_until="domcontentloaded",
                                timeout=30000)
                await page.wait_for_timeout(random.randint(2500, 4500))
                await page.mouse.wheel(0, random.randint(300, 800))
                await page.wait_for_timeout(random.randint(900, 1800))
            except Exception:
                pass
        logger.info("session warmed (google + %s careers root)", company_key or "-")
    except Exception:
        logger.debug("session warm skipped", exc_info=True)


def _attach_submit_capture(page, shot_dir) -> None:
    """Record the server's answer to the application submit mutation (Ashby GraphQL
    `ApiSubmitSingleApplicationFormAction`) into <shot_dir>/submit_response.json — the UI collapses
    every rejection into one 'flagged as possible spam' banner, but the payload carries the real
    code (e.g. RECAPTCHA_SCORE_BELOW_THRESHOLD)."""
    import json as _json

    async def _on_response(resp):
        try:
            u = resp.url
            # Ashby posts the application through ApiSubmitSingleApplicationFormAction OR
            # ApiSubmitMultipleFormsAction (multi-form postings) — match both.
            if "non-user-graphql" not in u or "op=ApiSubmit" not in u or "Form" not in u:
                return
            body = await resp.text()
            data = {"url": u, "status": resp.status, "body": body[:20000]}
            codes = sorted(set(re.findall(r'"(?:code|errorCode|__typename)"\s*:\s*"([A-Z_]{6,})"', body)))
            data["codes"] = codes
            try:
                os.makedirs(shot_dir, exist_ok=True)
                with open(os.path.join(str(shot_dir), "submit_response.json"), "w", encoding="utf-8") as f:
                    _json.dump(data, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            logger.info("submit mutation response: http=%s codes=%s", resp.status, codes)
        except Exception:
            pass

    try:
        page.on("response", lambda r: asyncio.create_task(_on_response(r)))
    except Exception:
        logger.debug("submit capture not attached", exc_info=True)


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
            # A block / anti-spam banner can render SECONDS after the click — Ashby's "flagged as
            # possible spam" / "we couldn't submit your application" appears ~10-15s LATER, so the
            # +1.5s _submit_evidence check misses it and the job wrongly reads confirmed=false.
            # Detect it here during the watch so a spam-flagged submit is reported BLOCKED. Guard
            # against a false-positive on a code/confirm page (same carve-out as _submit_evidence).
            if not code_done and not _CODE_STEP_RE.search(text or ""):
                _bm = _SUBMIT_BLOCK_RE.search(text or "")
                if _bm:
                    reason = _bm.group(0)[:60]
                    status_store.mark(profile, jid, "blocked")
                    logger.info("submit BLOCKED for %s/%s — %s", profile, jid, reason)
                    return ("blocked", reason)
            if (applicant_email and not code_done and _CODE_STEP_RE.search(text)):
                sel_str = ",".join(_CODE_FIELD_SELECTORS)
                state = await page.evaluate(
                    "(sel) => { const i = document.querySelector(sel);"
                    " return i ? ((i.value||'').trim() ? 'filled':'empty') : 'nofield'; }",
                    sel_str)
                if state == "empty":
                    from backend.tools.verify_code import read_code
                    code = read_code(applicant_email, load_ts)
                    if code:
                        for sel in _CODE_FIELD_SELECTORS:
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
    r"this field is required|please (?:fill|complete|correct) (?:in |out |this|the|all|your)|"
    # field-scoped "is required" ONLY — a bare "is required" false-matched the Avature/Maximus
    # SUCCESS page "…no assessment is required and you have completed the application process",
    # flagging a completed application as blocked (incident 2026-08-30).
    r"(?:field|entry|section|question|response|answer) is required|"
    # Real ATS rejection wordings observed on GH/Ashby (these were MISSED, so a rejected
    # submit was mislabeled "awaiting confirmation" and burned the full 300s watch):
    r"flagged as possible spam|turn off your (vpn|proxy)|couldn.?t submit(?: your)?|"
    r"missing entry|needs? correction|items? for (?:a )?required section|"
    r"please accept|accept the terms)", re.I)


async def _submit_evidence(page, shot_dir, *, poll_secs: float = 20.0) -> dict:
    """Snapshot the page after the submit click: url, a full-page screenshot, whether a confirmation
    OR a block/validation banner is visible. Best-effort, never raises. POLLS for up to `poll_secs`:
    Ashby's anti-spam banner ("flagged as possible spam") AND its Thank-you page both render ASYNC
    ~10-15s AFTER the click — a single read at +1.5s missed them and reported a MISLEADING
    blocked=None/confirmed=False on a submit that was actually spam-REJECTED (proven live 2026-09-12:
    Salmon + a fresh masabi both showed the spam banner ~14s post-click the early read never saw)."""
    ev = {"post_url": None, "confirmed": None, "blocked": None, "screenshot": None}
    try:
        ev["post_url"] = page.url
    except Exception:
        pass
    deadline = time.time() + poll_secs
    while True:
        try:
            txt = await page.inner_text("body", timeout=4000)
            try:
                ev["post_url"] = page.url
            except Exception:
                pass
            conf = bool(looks_submitted(txt, ev.get("post_url") or ""))
            m = _SUBMIT_BLOCK_RE.search(txt or "")
            blk = m.group(0)[:60] if m else None
            # A CONFIRMED / emailed-code page has PROGRESSED — never let a stray block-phrase on it
            # (a field hint / privacy "please") flag it blocked (that would skip the code-fill watch).
            if blk and (conf or _CODE_STEP_RE.search(txt or "") or re.search(
                    r"(?i)confirm you'?re a human|code (?:was |has been )?sent", txt or "")):
                blk = None
            ev["confirmed"], ev["blocked"] = conf, blk
            if conf or blk or time.time() >= deadline:
                break
        except Exception:
            if time.time() >= deadline:
                break
        try:
            await page.wait_for_timeout(2000)
        except Exception:
            break
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
        # A dead/expired posting (GH embed 404 "can't find that page", Ashby "Job not found",
        # a board redirect to the company listing) renders NO form: 0 fields → unfilled=[]
        # vacuously "complete", so we land here with no button. That is NOT a missing-button
        # bug — the posting is GONE. Report it distinctly ("no_form") so the worker marks the
        # catalog row dead and the ledger drops it, instead of re-running «Докрутить» forever
        # or mislabeling a churned posting as a detection failure.
        pt = result.get("page_type") if isinstance(result, dict) else None
        no_form = not (result.get("filled") or 0) and not (result.get("unfilled") or [])
        if pt in ("expired", "login_required", "captcha") or no_form:
            logger.info("auto-submit skipped: no fillable form (page_type=%s)", pt)
            return {"clicked": False, "reason": "no_form", "page_type": pt}
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
        await _human_dwell(page, sel)          # look human before the click (reCAPTCHA v3)
        clicked = await filler.click_submit(page, {"submit_selector": sel})
        await page.wait_for_timeout(1500)
        # _submit_evidence now POLLS ~20s for Ashby's async spam-banner / confirmation.
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
            "current": _S["current"], "owner": _S["owner"],
            "stealth": STEALTH_ON, "fp_diversify": FP_DIVERSIFY}


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
            {"error": f"занято: {_S['owner']} сейчас смотрит — попробуйте позже"},
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
                    {"error": "анкета не сохранена — заполните заново"}, status_code=404)
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
            # Record the server's verdict on the submit mutation (real error code, not just the
            # banner) and arrive at the form like a person (Google cookies + the careers root).
            _attach_submit_capture(page, d)
            _ck = re.search(r"ashbyhq\.com/([^/?#]+)", url or "")
            await _warm_session(page, _ck.group(1) if _ck else "")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # A React ATS form (Ashby/Greenhouse) can render SECONDS after domcontentloaded —
            # much slower through a residential/phone proxy. A fixed 2s wait intermittently saw
            # 0 fields → a false `no_form` on a posting whose form was actually present (proven
            # via live page inspection on Salmon/Ashby). POLL for a real form control (bounded);
            # a truly dead/expired posting just times out and falls through to the genuine
            # no_form path. Fast forms proceed the instant a field attaches, so nothing regresses.
            try:
                await page.wait_for_selector(
                    "form input:not([type=hidden]), form textarea, form select, "
                    "input[type=file], [role=combobox]",
                    timeout=15000, state="attached")
            except Exception:
                pass
            await page.wait_for_timeout(1500)
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
                        if isinstance(ok, tuple) and ok and ok[0] == "blocked":
                            # a delayed anti-spam / "couldn't submit" banner — report it BLOCKED
                            submit_result["blocked"] = ok[1]
                            submit_result["confirmed"] = False
                        else:
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
    var sr=j.submit_result||{}; var msg=sr.clicked?('Отправлено'+(sr.confirmed?' — подтверждено':(sr.blocked?' — ошибка: '+sr.blocked:' — в процессе'))):('не отправлено ('+(sr.reason||'?')+') — проверьте');
    s.textContent='✓ Заполнено '+(j.company||'')+' — '+msg+'. (заполнено '+j.filled+', осталось '+j.unfilled+')';
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
    s.textContent='✓ Отмечено — исчезнет из очереди.';
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
            f"<button class='mark' onclick=\"markSubmitted('{escape(j['_id'])}',this)\">✓ Подано</button>"
            "</div>"
            "</div>")
    body = "".join(rows) or "<div class='hint'>Очередь пуста.</div>"
    novnc = "/vnc/vnc_lite.html?path=vnc/websockify&autoconnect=1&resize=scale&reconnect=1"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Co-pilot — {escape(profile)}</title><style>{_CSS}</style></head><body>"
        f"<div class='bar'><b>Co-pilot</b> — форма и отправка происходят автоматически (смотрите выше).</div>"
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
