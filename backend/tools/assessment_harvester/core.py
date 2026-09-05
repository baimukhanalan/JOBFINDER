"""The shared harvest loop.

Mirrors `shl_assessment.answer_scored`, but the etalon's integrity hard-stop becomes a RANDOM-answer
seam: every MCQ (including cognitive ones the etalon refuses) is screenshotted, banked, then answered
with a RANDOM option to advance.

The old "free-response = dead end" ceiling is removed with a FAKE MEDIA DEVICE (owner-authorized):
Chromium is launched with a fake mic (speech.wav) + fake camera (face.y4m) + auto-granted
getUserMedia, so speaking/listening/video sections DRIVE PAST the device check:
  * LISTENING: audio autoplays; capture prompt + options + the audio asset URL, then random-answer.
  * SPEAKING / SVAR: mic auto-granted -> the recorder captures the fake speech -> run
    record -> stop -> submit so the item ADVANCES (score is garbage; we harvest, not pass).
  * TYPING: fill the field with text and advance.
  * VIDEO: fake camera; run the record cycle.
Only a genuine wall (hard captcha / webcam LIVENESS rejecting the fake face) is a real stop —
screenshot it and report it.

Key ordering rule: bank BEFORE advancing (invites are single-use — if the click burns the session we
still keep the captured item).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

from backend.tools.assessment_harvester import assets, bank, media

logger = logging.getLogger("assessment_harvester")

_FAKE_SPEECH_TEXT = (
    "Hello, my name is Alex. I have several years of customer service experience. "
    "I enjoy helping people solve their problems and I stay calm and professional under pressure. "
    "Thank you for the opportunity to speak with you today.")

# ---- item-type classification (broad; for the bank label + is_ability meta) ----------------------
_NUM_Q_RE = re.compile(
    r"\bcalculate\b|what is the (value|result|total|percentage|sum|difference|average|ratio|product)|"
    r"how (many|much) (is|would|does|will)|which (number|is (larger|smaller|greater|less))|"
    r"\bpercentage\b|\bratio\b|solve for|next in the (series|sequence)", re.I)
_PIC_Q_RE = re.compile(
    r"which (figure|shape|image|pattern|comes next)|complete the (series|sequence|pattern)|"
    r"odd one out|which of these (figures|shapes|images)|the missing (figure|piece|shape)", re.I)
_VERB_Q_RE = re.compile(
    r"based on the (passage|information|text|paragraph)|read the (passage|following)|"
    r"synonym|antonym|closest in meaning|opposite in meaning|correct(ly)? spell|grammatic|"
    r"choose the (correct|right) (word|sentence|option)|fill in the blank|complete the sentence|"
    r"error in the (sentence|following)", re.I)
_SJT_Q_RE = re.compile(
    r"most likely to do|least likely to do|what would you (do|be)|which (response|action)|"
    r"best describes what you would do|how would you (respond|handle|deal)", re.I)
_PERS_Q_RE = re.compile(
    r"describes you (the )?best|which statement|strongly agree|strongly disagree|\bi (am|prefer|enjoy|like|tend)\b|"
    r"how (often|much) do you|rate (yourself|how)|to what extent", re.I)
_TF_RE = re.compile(r"^(true|false|cannot say|can'?t say|not enough information)$", re.I)


def classify(item: dict) -> tuple[str, bool]:
    """Return (item_type, is_ability). is_ability = a cognitive/knowledge item (right answer)."""
    opts = [o.get("text", "") for o in (item.get("options") or [])]
    opt_txt = [o for o in opts if o.strip()]
    # free-response families need a REAL response control (a record button / a big text box), not a
    # bare decorative <video>/<audio> on an instructions page. A record control (has_mic) means a
    # spoken/video answer; if a <video> element is also present, label it video (same fake-device
    # handler). A big textarea with no options is a typing item.
    if item.get("has_mic") and not opt_txt:
        return ("video" if item.get("has_video") else "speaking"), False
    if item.get("has_textarea") and not opt_txt:
        return "typing", False
    # a listening item carries audio AND MCQ options
    if item.get("has_audio") and opt_txt:
        return "listening", False
    q = item.get("question", "") or ""
    imgs = item.get("qimgs") or []
    if _PIC_Q_RE.search(q) or (imgs and (not opt_txt or all(len(o) <= 3 for o in opt_txt))):
        return "picture", True
    if item.get("has_table"):
        return "numerical", True
    numeric = sum(1 for o in opt_txt if re.fullmatch(r"[\d.,%$£€+\-*/ ]{1,14}", o.strip()))
    if opt_txt and numeric >= max(2, len(opt_txt) - 1):
        return "numerical", True
    if _NUM_Q_RE.search(q):
        return "numerical", True
    if any(_TF_RE.match(o.strip()) for o in opt_txt) and 0 < len(opt_txt) <= 4:
        return "verbal", True
    if _VERB_Q_RE.search(q):
        return "verbal", True
    if _SJT_Q_RE.search(q):
        return "sjt", False
    if _PERS_Q_RE.search(q) or any(re.search(r"agree|disagree", o, re.I) for o in opt_txt):
        return "personality", False
    return "unknown", False


def _msig(item: dict) -> str:
    imgs = list(item.get("qimgs") or [])
    for o in item.get("options") or []:
        if o.get("image"):
            imgs.append(o["image"])
    return bank.media_sig(imgs)


async def _signature(page, adapter) -> tuple:
    it = await adapter.read_item(page)
    return (it.get("question", ""), tuple(o.get("text", "") for o in it.get("options") or []),
            it.get("progress"))


def _launch_args() -> tuple[list[str], dict]:
    """Chromium args + the fake-media asset paths. Fake mic/camera let speaking/listening/video
    sections drive past the device check."""
    a = assets.ensure_assets()
    args = ["--no-sandbox",
            "--use-fake-ui-for-media-stream",       # auto-grant getUserMedia (no dialog)
            "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required"]
    if a.get("audio"):
        args.append(f"--use-file-for-fake-audio-capture={a['audio']}")
    if a.get("video"):
        args.append(f"--use-file-for-fake-video-capture={a['video']}")
    return args, a


async def harvest_one(url: str, mailbox: str, adapter, *, max_items: int = 140,
                      min_delay: float = 0.8, max_delay: float = 2.2) -> dict:
    """Drive ONE assessment session end-to-end. Returns a result dict with per-type counts."""
    from playwright.async_api import async_playwright
    res = {"status": "error", "banked": 0, "by_type": {}, "shots": [], "note": "", "mailbox": mailbox,
           "walls": []}
    args, asset_paths = _launch_args()
    res["fake_media"] = asset_paths

    def _bank(item, item_type, is_ability, chosen, shot, free=None, audio_url=None):
        bank.record(
            platform=adapter.platform, item_type=item_type, question=item.get("question", ""),
            options=item.get("options") or [],
            media={"image": shot, "audio": audio_url,
                   "prompt_text": item.get("question", "") if free in ("speaking", "typing", "video") else None},
            chosen_answer=chosen,
            source={"mailbox": mailbox, "invite_url": url},
            kind_meta={"is_scored": True, "is_ability": is_ability},
            msig=_msig(item))
        res["banked"] += 1
        res["by_type"][item_type] = res["by_type"].get(item_type, 0) + 1

    async def _shot(item):
        s = await media.capture(page, adapter.platform, item.get("question", ""),
                                [o.get("text", "") for o in item.get("options") or []],
                                item.get("url", url))
        return s

    async def _run():
        nonlocal page
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=False, args=args, timeout=60000)
            ctx = await b.new_context(viewport={"width": 1280, "height": 850},
                                      permissions=["microphone", "camera"])
            page = await ctx.new_page()
            try:
                await adapter.enter(page, url)
                stale = 0
                for step in range(max_items):
                    if await adapter.is_done(page):
                        res["status"] = "completed"
                        res["note"] = f"completed ({res['banked']} banked)"
                        return
                    wall = await adapter.wall(page)
                    if wall:
                        shot = await media.capture(page, adapter.platform, f"WALL:{wall}", [], page.url)
                        if shot:
                            res["shots"].append((f"wall:{wall}", shot, ""))
                            res["walls"].append((wall, shot, ""))
                        res["status"] = f"wall_{wall}"
                        res["note"] = f"genuine wall '{wall}' — cannot breach ({res['banked']} banked before it)"
                        return
                    if await adapter.dismiss_noise(page):
                        await page.wait_for_timeout(500)
                        continue
                    item = await adapter.read_item(page)
                    opts = item.get("options") or []
                    opt_txt = [o.get("text", "") for o in opts if (o.get("text") or "").strip()]
                    item_type, is_ability = classify(item)

                    # ---- landing / transition (nothing to answer, no media widget) ----
                    if not opts and item_type not in ("speaking", "video", "typing"):
                        if await adapter.advance(page):
                            await page.wait_for_timeout(1200)
                            stale = 0
                            continue
                        if await adapter.is_done(page):
                            res["status"] = "completed"; res["note"] = "completed (no forward)"
                            return
                        stale += 1
                        if stale >= 3:
                            res["status"] = "stuck"
                            res["note"] = f"no options/forward at step {step}: {item.get('body','')[:120]}"
                            return
                        await page.wait_for_timeout(1500)
                        continue
                    stale = 0

                    q = item.get("question", "")
                    need_shot = (item_type in ("speaking", "video", "typing", "listening", "picture")
                                 or is_ability or bool(item.get("qimgs"))
                                 or item.get("has_table") or any(o.get("image") for o in opts)
                                 or not opt_txt)
                    shot = await _shot(item) if need_shot else None
                    if shot:
                        res["shots"].append((item_type, shot, q[:80]))

                    # ---- SPEAKING (SVAR) — fake mic, record->stop->submit ----
                    if item_type == "speaking":
                        _bank(item, "speaking", False,
                              {"text": None, "index": None, "value": "fake_audio_submitted", "source": "fake_device"},
                              shot, free="speaking")
                        logger.info("[%s] #%d speaking q=%r (fake mic)", mailbox, res["banked"], q[:60])
                        if await adapter.handle_speaking(page):
                            await page.wait_for_timeout(1500)
                            continue
                        res["walls"].append(("speaking", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"speaking item did not advance even with fake mic (item {res['banked']})"
                        return

                    # ---- VIDEO — fake camera, record cycle ----
                    if item_type == "video":
                        _bank(item, "video", False,
                              {"text": None, "index": None, "value": "fake_video_submitted", "source": "fake_device"},
                              shot, free="video")
                        logger.info("[%s] #%d video q=%r (fake cam)", mailbox, res["banked"], q[:60])
                        if await adapter.handle_speaking(page, record_secs=4.0):
                            await page.wait_for_timeout(1500)
                            continue
                        res["walls"].append(("video", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"video item did not advance (item {res['banked']})"
                        return

                    # ---- TYPING — fill and advance ----
                    if item_type == "typing":
                        _bank(item, "typing", False,
                              {"text": None, "index": None, "value": "fake_text_typed", "source": "fake_device"},
                              shot, free="typing")
                        logger.info("[%s] #%d typing q=%r", mailbox, res["banked"], q[:60])
                        if await adapter.handle_typing(page, _FAKE_SPEECH_TEXT):
                            await page.wait_for_timeout(1200)
                            continue
                        res["walls"].append(("typing", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"typing item did not advance (item {res['banked']})"
                        return

                    # ---- LISTENING — capture audio, then it's an MCQ ----
                    audio_url = None
                    if item_type == "listening":
                        audio_url = await adapter.handle_listening(page)

                    # ---- MCQ (incl. listening/cognitive/personality) — random answer ----
                    if not opts:
                        # a media item with no pickable option we could detect -> record + advance
                        _bank(item, item_type, is_ability,
                              {"text": None, "index": None, "value": None, "source": "capture_only"},
                              shot, audio_url=audio_url)
                        if not await adapter.advance(page):
                            res["walls"].append((item_type, shot, q[:80]))
                            res["status"] = "stuck"
                            res["note"] = f"{item_type} item, no options, could not advance"
                            return
                        await page.wait_for_timeout(1200)
                        continue

                    idx = random.randint(0, len(opts) - 1)
                    chosen = {"text": opt_txt[idx] if idx < len(opt_txt) else opts[idx].get("text"),
                              "index": idx, "value": None, "source": "random"}
                    _bank(item, item_type, is_ability, chosen, shot, audio_url=audio_url)
                    logger.info("[%s] #%d %s%s q=%r opts=%d pick=%d",
                                mailbox, res["banked"], item_type,
                                " IMG" if item.get("qimgs") else "", q[:60], len(opts), idx)

                    prev = (q, tuple(o.get("text", "") for o in opts), item.get("progress"))
                    await asyncio.sleep(random.uniform(min_delay, max_delay))
                    if not await adapter.answer_mcq(page, item, idx):
                        await adapter.advance(page)
                    advanced = False
                    for _ in range(10):
                        await page.wait_for_timeout(600)
                        cur = await _signature(page, adapter)
                        if cur != prev and (cur[0] or cur[1]):
                            advanced = True
                            break
                        if await adapter.advance(page):
                            if await _signature(page, adapter) != prev:
                                advanced = True
                                break
                        if await adapter.is_done(page):
                            res["status"] = "completed"; res["note"] = "completed after answer"
                            return
                    if not advanced:
                        if await adapter.is_done(page):
                            res["status"] = "completed"; res["note"] = "completed (final item)"
                            return
                        res["status"] = "stuck"
                        res["note"] = f"item did not advance after answer ({res['banked']} banked)"
                        return
                res["status"] = "stuck"
                res["note"] = f"max_items reached ({res['banked']} banked)"
            finally:
                try:
                    await ctx.close()
                    await b.close()
                except Exception:
                    pass

    page = None
    try:
        await asyncio.wait_for(_run(), timeout=1200)
    except asyncio.TimeoutError:
        res["note"] = f"timeout 1200s ({res['banked']} banked before hang)"
        if res["banked"]:
            res["status"] = "partial_timeout"
    except Exception as exc:
        res["note"] = f"{type(exc).__name__}: {exc}"[:200]
    return res
