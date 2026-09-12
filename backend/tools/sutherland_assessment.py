"""Sutherland (SHL TalentCentral, us1) assessment front-door + WCI200 wall reporter.

Sutherland's SHL invite (`talentcentral@shl.com`, subject "Your Sutherland assessment invitation")
autologins into `talentcentral.us1.shl.com/experience/#/link/<token>`, walks a short SHL intro
(cookies -> Welcome -> "About you"[Submit] -> an SHL webcam check), then REDIRECTS to the AMCAT /
Aspiring Minds player (`amcatglobal.aspiringminds.com`) which runs the ACTUAL assessment behind a
CONTINUOUS webcam proctor (SHL/AMCAT "WCI200").

**LIVE FINDING — 2026-09-12 (do NOT re-conclude the opposite):** the WCI200 proctor REJECTS a
synthetic/fake camera outright ("Error Code WCI200: We are unable to detect a camera on your
device"), regardless of feed brightness (pure-black, dim, or lit), egress (direct / phone slot), or
correctly driving AMCAT's own diagnostic. Proof it is not merely a flow bug: the harvester's
`AmcatAdapter` PASSES the TP-AMCAT webcam check with the very same fake camera (852 banked items),
yet driving the Sutherland->AMCAT handoff with that same `AmcatAdapter.enter()` STILL hits the WCI200
camera wall. The independent harvester conclusion agrees (`adapters/shl.py::ShlAdapter.wall`). It is
NOT a face requirement (no face is ever asked for) — it hard-requires a REAL camera DEVICE. This
datacenter host has NO real camera and CANNOT synthesize one: v4l2loopback's dependency `videodev` is
absent from this kernel (`modprobe` fails with "Unknown symbol v4l2_device_register"), so Chromium can
only offer its fake device. => a Sutherland assessment is currently UN-completable on this server.
The owner's "a dark camera passes" holds only for a REAL physical (unlit) webcam, which we do not have.

What this driver DOES: drive the SHL front-door (incl. the About-You **Submit** gate that
`shl_assessment._forward_control` alone can't click, + a reload-retry for SHL's OWN webcam-race that a
dim feed passes), reach the AMCAT handoff, and REPORT the WCI200 real-camera wall
(`blocked_proctor_camera`). If a real camera device ever exists here, the AMCAT battery is COGNITIVE
(numerical/verbal/logical + AMPI personality + SVAR) — the harvester's `AmcatAdapter` is the answering
engine, and per the etalon boundary any ability/knowledge item must go to a human (never auto-solved);
`answer_scored` (below) only applies to the minority SHL-OPQ `label.question-answer-label` markup.

Headful is mandatory (SHL/AMCAT reject headless): run under DISPLAY=:98 + `sg mail`. The fake camera is
served via Chromium's fake-device flags (see `camera_launch_args`).
"""
from __future__ import annotations

import logging
import os
import re

from backend.tools import shl_assessment as sa

logger = logging.getLogger("sutherland_assessment")

_ASSETS = os.path.join(os.path.dirname(__file__), "assessment_harvester", "assets")
DARK_CAMERA = os.path.join(_ASSETS, "dark_camera.y4m")
SPEECH_WAV = os.path.join(_ASSETS, "speech.wav")


def ensure_dark_camera() -> str | None:
    """Generate the DARK/BLANK (unlit) fake-camera y4m if missing, and return its path (or None).

    A very-dark GREY base (0x141414) + faint per-frame noise (a live, non-uniform, face-less feed).
    NB: this dim feed passes SHL's OWN front-door webcam check (the "Webcam not detected" modal), but
    is STILL rejected by the downstream AMCAT WCI200 proctor ("unable to detect a camera") — WCI200
    wants a REAL camera device, not any fake feed (see the module docstring). Kept because it is the
    farthest a fake camera gets (through the SHL intro, up to the AMCAT wall). NOT a fabricated face."""
    import shutil
    import subprocess
    if os.path.exists(DARK_CAMERA):
        return DARK_CAMERA
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    try:
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=0x141414:s=640x480:r=15:d=5,noise=alls=14:allf=t",
             "-pix_fmt", "yuv420p", "-y", DARK_CAMERA], check=True, timeout=120)
        return DARK_CAMERA if os.path.exists(DARK_CAMERA) else None
    except Exception:
        return None

# Sutherland link host + the autologin/error routes.
LINK_RE = re.compile(r"https?://talentcentral\.us1\.shl\.com/experience/#/link/[A-Za-z0-9+/=_-]+")

# A device / webcam / system check page (WCI200). Detected by TEXT or a live <video> preview.
_DEVICE_RE = re.compile(
    r"system check|device check|check your (device|camera|microphone|system|setup)|"
    r"webcam|we need access|allow access|test your|record(ing)? (your|will)|"
    r"proctor|equipment|browser check|take (a )?(photo|picture)|capture (your )?(photo|image)|"
    r"enable your (camera|webcam)|turn on your (camera|webcam)|"
    r"your (camera|microphone) (is|will|needs)|grant (access|permission)", re.I)
# A HARD face / liveness requirement — we NEVER fabricate a face, so this is a real terminal stop.
# NB: deliberately does NOT match "camera on you" / a bare camera-not-detected (that is a webcam
# TIMING failure, matched by _WEBCAM_UNDETECTED_RE below and RETRIED, not a face demand).
_FACE_REQUIRED_RE = re.compile(
    r"(no|can'?t|cannot|unable to|couldn'?t) (detect|find|see|recognize) (your |a )?(face|person)\b|"
    r"face (not|isn'?t) (detected|found|visible)|ensure your face|position your face|"
    r"make sure your face|center your face|no face detected|face is required|face the camera|"
    r"we can'?t see you|hold up (your|a) (id|identification|photo id)|show your face", re.I)
# A webcam PRESENCE/timing failure (the fake feed wasn't ready when SHL polled). RETRYABLE — reload
# the invite link (progress saved) so the next load finds the camera warm. Live-observed wordings:
# "Webcam not detected. You will be logged out", "unable to detect a camera on you".
_WEBCAM_UNDETECTED_RE = re.compile(
    r"webcam (not|isn'?t|could not be|couldn'?t be) (detected|found)|"
    r"(unable to|can'?t|cannot|couldn'?t|could not) (detect|find|access) (a |your )?(web)?cam(era)?|"
    r"(web)?cam(era)? (not|isn'?t) (detected|found|working|available)|no (web)?cam(era)? (detected|found)", re.I)
_NOT_FOUND_RE = re.compile(r"something went wrong|it looks like an error|/not-found", re.I)

# The AMCAT/Aspiring Minds continuous-proctor camera wall reached AFTER the SHL intro redirects to
# amcatglobal.aspiringminds.com. TERMINAL on this server (no real camera; fake device rejected).
_AMCAT_WALL_RE = re.compile(
    r"wci200|error code wci|camera is mandatory|unable to detect a camera|"
    r"logged out.*camera|camera.*mandatory for online proctoring", re.I)

# Buttons that move a device / overview / interstitial page forward (guarded, non-destructive).
_PROCEED_NAMES = ("Continue", "Next", "Proceed", "Start", "Begin", "Get Started", "Confirm",
                  "I'm ready", "Ready", "Allow", "Enable", "Submit", "Done", "OK", "Launch")


def camera_launch_args() -> list[str]:
    """Chromium args that feed a DARK/BLANK (unlit) camera + a non-silent mic through the fake device,
    auto-granting getUserMedia (no permission dialog). The dark camera is what the owner authorized for
    the WCI200 check — an unlit camera, NOT a fabricated face. Override the video file with the
    `SUTHERLAND_CAM` env (a path to a .y4m/.mjpeg) for tuning the feed."""
    from backend.tools.assessment_harvester import assets
    assets.ensure_assets()  # make sure speech.wav exists (mic present)
    ensure_dark_camera()    # generate the dim unlit-webcam feed if missing
    video = os.environ.get("SUTHERLAND_CAM") or DARK_CAMERA
    args = ["--no-sandbox", "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            # hide the automation fingerprint (navigator.webdriver) — some proctor SDKs refuse a
            # webdriver-flagged browser and report a bogus "webcam not detected".
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required"]
    if os.path.exists(video):
        args.append(f"--use-file-for-fake-video-capture={video}")
    if os.path.exists(SPEECH_WAV):
        args.append(f"--use-file-for-fake-audio-capture={SPEECH_WAV}")
    return args


async def _click_named(page, names=_PROCEED_NAMES) -> str | None:
    """Click the first visible+enabled button/link whose accessible name is exactly one of `names`."""
    for nm in names:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=re.compile(rf"^\s*{re.escape(nm)}\s*$", re.I))
            el = await sa._actionable(loc)
            if el:
                try:
                    await el.click(timeout=4000)
                    return nm
                except Exception:
                    continue
    return None


async def _video_present(page) -> bool:
    """True if a live camera <video> preview is on the page (a device-check signal)."""
    try:
        return await page.evaluate("""() => [...document.querySelectorAll('video')].some(v =>
            !!v.srcObject || (v.videoWidth||0) > 0)""")
    except Exception:
        return False


async def _body(page) -> str:
    try:
        return await page.inner_text("body", timeout=4000)
    except Exception:
        return ""



async def run_sutherland(link: str, persona: dict | None = None, *, page,
                         complete_scored: bool = True, verify_only: bool = False,
                         max_steps: int = 80, on_shot=None) -> dict:
    """Drive a HEADFUL page through the Sutherland SHL intro + (optionally) the scored section.

    Returns {status, note, steps, device_check, items_answered?, last_progress?}. status ∈
      * verify_only=True: 'device_passed' | 'device_requires_face' | 'reached_scored' |
        'completed' | 'stuck' | 'error'
      * complete_scored=True: whatever `answer_scored` returns ('completed'|'needs_human'|'stuck'|
        'error') plus the intro outcomes above.
    `on_shot(tag)` (optional) is awaited after key pages so a caller can screenshot.
    """
    persona = persona or {"country": "United States", "education_level": "Bachelor"}
    res = {"status": "error", "note": "", "steps": 0, "device_check": None}

    async def shot(tag):
        if on_shot:
            try:
                await on_shot(tag)
            except Exception:
                pass

    try:
        await page.goto(link, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        await sa._dismiss_cookies(page)
        await page.wait_for_timeout(1500)

        not_found_retries = 0
        device_retries = 0
        device_passed = False
        for step in range(1, max_steps + 1):
            res["steps"] = step
            txt = await _body(page)
            tl = txt.lower()
            url = page.url or ""

            # ---- completion (the ONLY reliable done-signal) ----
            if (re.search(r"\b0\s*assessment", tl) and "left" in tl) or sa._COMPLETE_RE.search(txt):
                res["status"] = "completed"
                res["note"] = "overview shows 0 assessments left / completion confirmed"
                await shot(f"{step:02d}_complete")
                return res

            # ---- AMCAT WCI200 continuous-proctor camera wall (TERMINAL on this server) ----
            # After the SHL intro redirects to amcatglobal.aspiringminds.com, the proctor rejects our
            # synthetic camera. Not a face demand and not a flow bug — a REAL camera device is required
            # (unavailable here). Report it plainly rather than looping.
            if ("aspiringminds.com" in url and _AMCAT_WALL_RE.search(txt)) or _AMCAT_WALL_RE.search(txt):
                res["status"] = "blocked_proctor_camera"
                res["device_check"] = "amcat_wci200_rejects_fake_camera"
                res["note"] = ("AMCAT WCI200 proctor rejects the fake camera ('unable to detect a "
                               "camera') — hard-requires a REAL camera device (no face needed); "
                               "un-completable on this server (no real/virtual camera available)")
                await shot(f"{step:02d}_wci200_wall")
                return res

            # ---- SHL not-found / error: reload the invite link (progress is saved) ----
            if _NOT_FOUND_RE.search(tl) or "/not-found" in url:
                if not_found_retries >= 3:
                    res["status"] = "stuck"
                    res["note"] = "SHL 'something went wrong' persisted after 3 reloads (token likely dead/expired)"
                    await shot(f"{step:02d}_notfound")
                    return res
                not_found_retries += 1
                logger.info("[sutherland] not-found — reloading link (retry %d)", not_found_retries)
                await shot(f"{step:02d}_notfound")
                await page.goto(link, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(6000)
                await sa._dismiss_cookies(page)
                continue

            # ---- already inside the OPQ item player? ----
            try:
                on_item = await page.locator("label.question-answer-label").count()
            except Exception:
                on_item = 0
            if on_item:
                res["device_check"] = res["device_check"] or "not_encountered_before_items"
                if verify_only:
                    res["status"] = "reached_scored"
                    res["note"] = f"reached OPQ item player ({on_item} labels) without a face wall"
                    await shot(f"{step:02d}_items")
                    return res
                sc = await sa.answer_scored(page, persona)
                sc["steps"] = step
                sc["device_check"] = res["device_check"]
                await shot(f"{step:02d}_scored_{sc.get('status')}")
                return sc

            # ---- device / webcam check (WCI200) ----
            # A 'webcam not detected' warning is handled ANY time (the proctor can re-check mid-flow).
            # The generic device page (system-check text OR a live proctor <video>) is handled ONLY
            # until we've passed once — after that the proctor <video> stays on EVERY page, so keying
            # off it again would loop forever instead of letting the assessment content load.
            webcam_warn = bool(_WEBCAM_UNDETECTED_RE.search(txt))
            on_device = (not device_passed) and (_DEVICE_RE.search(txt) or await _video_present(page))
            if webcam_warn or on_device:
                # Do NOT pre-acquire getUserMedia ourselves — that races/breaks SHL's own camera probe
                # ("webcam not detected"). Just let the page settle so its probe finds the fake device.
                await page.wait_for_timeout(3500)
                txt = await _body(page)
                await shot(f"{step:02d}_device")

                # (1) a TRUE face/liveness demand -> we never fabricate a face -> terminal stop.
                if _FACE_REQUIRED_RE.search(txt):
                    res["status"] = "device_requires_face"
                    res["device_check"] = "requires_face"
                    res["note"] = f"WCI200 hard-requires a face: {_FACE_REQUIRED_RE.search(txt).group(0)!r}"
                    await shot(f"{step:02d}_face_required")
                    return res

                # (2) 'Webcam not detected' = a PRESENCE/timing miss (the fake feed wasn't ready when
                # SHL polled). Dismiss the warning + reload the invite link so the next load finds the
                # camera warm. Bounded; a hard camera wall would exhaust the retries -> stuck (not a
                # false 'passed').
                if _WEBCAM_UNDETECTED_RE.search(txt):
                    if device_retries >= 4:
                        res["status"] = "stuck"
                        res["device_check"] = "webcam_undetected"
                        res["note"] = "WCI200 'webcam not detected' persisted after 4 warm-reload retries"
                        return res
                    device_retries += 1
                    logger.info("[sutherland] webcam-not-detected -> warm-reload retry %d", device_retries)
                    await _click_named(page, ("OK", "Okay", "Close", "Dismiss", "Retry", "Try again"))
                    await page.wait_for_timeout(1500)
                    await page.goto(link, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(7000)  # let SHL's own probe find the camera on reload
                    await sa._dismiss_cookies(page)
                    continue

                # (3) camera present + no warning -> click a proceed control (if any) and advance.
                clicked = await _click_named(page)
                logger.info("[sutherland] device page: clicked %r", clicked)
                await page.wait_for_timeout(5000)
                txt2 = await _body(page)
                await shot(f"{step:02d}_after_device")
                if _FACE_REQUIRED_RE.search(txt2):
                    res["status"] = "device_requires_face"
                    res["device_check"] = "requires_face"
                    res["note"] = f"WCI200 hard-requires a face after proceed: {_FACE_REQUIRED_RE.search(txt2).group(0)!r}"
                    return res
                if _WEBCAM_UNDETECTED_RE.search(txt2):
                    # the warning came up AFTER our click -> treat as the retryable timing miss.
                    if device_retries < 4:
                        device_retries += 1
                        logger.info("[sutherland] webcam-not-detected (post-click) -> warm-reload %d", device_retries)
                        await _click_named(page, ("OK", "Okay", "Close", "Dismiss"))
                        await page.wait_for_timeout(1500)
                        await page.goto(link, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(7000)
                        await sa._dismiss_cookies(page)
                        continue
                    res["status"] = "stuck"
                    res["device_check"] = "webcam_undetected"
                    res["note"] = "WCI200 'webcam not detected' after proceed, retries exhausted"
                    return res
                if (page.url or "") != url or not _DEVICE_RE.search(txt2):
                    device_passed = True
                    res["device_check"] = "dark_camera_passed"
                    logger.info("[sutherland] device check PASSED with a dark camera")
                    if verify_only:
                        res["status"] = "device_passed"
                        res["note"] = "WCI200 device check passed with a dark/blank camera"
                        return res
                    continue
                # still on device page — a control we didn't press; loop and retry
                await page.wait_for_timeout(2000)
                continue

            # ---- About You (country of residence) -> fill + Submit ----
            if "about you" in tl or "/about-you" in url or "country of residence" in tl:
                try:
                    await sa._fill_background_page(page, persona)
                except Exception as e:
                    logger.info("[sutherland] fill about-you err: %s", e)
                c = await _click_named(page, ("Submit", "Continue", "Next"))
                logger.info("[sutherland] about-you: clicked %r", c)
                await shot(f"{step:02d}_aboutyou")
                await page.wait_for_timeout(4500)
                continue

            # ---- consent gate (mandatory toggle + Next) ----
            if await page.get_by_role("checkbox").count() and await sa._next_button(page):
                if await sa._toggle_consent_and_next(page):
                    await page.wait_for_timeout(4000)
                    continue

            # ---- generic forward (Welcome / instructions / overview / Start) ----
            fwd = await sa._forward_control(page)
            if fwd:
                try:
                    await fwd.click(timeout=4000)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
                continue
            c = await _click_named(page)
            if c:
                await page.wait_for_timeout(4500)
                continue
            await page.wait_for_timeout(3000)

        res["status"] = "stuck"
        res["note"] = f"max_steps reached (device_passed={device_passed})"
        return res
    except Exception as exc:
        res["note"] = f"{type(exc).__name__}: {exc}"[:200]
        return res
