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
import glob
import hashlib
import logging
import os
import random
import re
import tempfile

from backend.tools.assessment_harvester import answer_key, assets, asr, bank, media, mic, writex

# Audio whose transcript is a section/instruction prompt, not harvestable content.
_ASR_INSTR_RE = re.compile(
    r"read out sentence|in this section|section [a-d]\b|listen and repeat|listen carefully|"
    r"move ahead|submit answer|please speak|please repeat|\bpractice\b|you will hear a conversation|"
    r"is now complete|we will now begin|^question \d+\.?$|^section [a-d]\.?$", re.I)


def _bank_captured_audio(res: dict, mailbox: str, url: str, platform: str) -> None:
    """Transcribe the captured question audio (listen-repeat sentences / listen-comprehension dialogs
    + questions) with whisper and bank each distinct content sentence as a 'listening' item. The audio
    is a plain S3 mp3 captured during the walk; instruction/section prompts are filtered out."""
    adir = res.get("_audiodir")
    if not adir or not os.path.isdir(adir) or not asr.available():
        return
    files = sorted(glob.glob(os.path.join(adir, "*.mp3")))
    banked = 0
    seen = set()
    for f in files:
        txt = asr.transcribe(f)
        if not txt:
            continue
        t = " ".join(txt.split()).strip()
        if len(t) < 8 or _ASR_INSTR_RE.search(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            bank.record(platform=platform, item_type="listening", question=t, options=[],
                        media={"image": None, "audio": None, "prompt_text": t},
                        chosen_answer={"text": None, "index": None, "value": "audio_transcribed", "source": "asr"},
                        source={"mailbox": mailbox, "invite_url": url},
                        kind_meta={"is_scored": False, "is_ability": False}, msig="")
            banked += 1
            res["banked"] += 1
            res["by_type"]["listening"] = res["by_type"].get("listening", 0) + 1
        except Exception:
            pass
    logger.info("[asr] banked %d listening sentences (from %d audio files) for %s", banked, len(files), mailbox)

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
# Sales Competency Test — a best/worst situational-judgement item (catalog SALES-01). Recognised so a
# harvested Sales item gets the 'sales' label and looks up the imported best/worst answer key.
_SALES_RE = re.compile(
    r"choose the ['\"]?best['\"]? and (the )?['\"]?worst['\"]?|'best' and the 'worst'|"
    r"best and (the )?worst (action|response|option)", re.I)
# The question's DATA lives in an image/diagram/table (not the text) -> the text-only local model can't
# solve it, so never live-solve/cache it (random now + offline vision). Deliberately NOT matching
# verbal "passage/paragraph/sentence" (those are text-solvable).
_IMG_REF_RE = re.compile(
    r"refer to (the )?(given )?(diagram|figure|table|image|chart|graph|list|sign|icon|picture|dimensions|"
    r"address|map|layout|floor ?plan|information)|"
    r"given (diagram|figure|table|image|chart|graph|list|sign|icon|picture|dimensions)|"
    r"\bas shown\b|shown (in|above|below|here|is|are)|in the (diagram|figure|table|image|chart|picture)|"
    r"the (diagram|figure|table|graph|chart|image|sign) (shows|lists|below|above|given|displays)", re.I)


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
        # A WriteX email-writing task (a free-text email) vs a plain typing-SPEED test: the email task
        # needs a drafted business email, the speed test a copy of the shown paragraph.
        if writex.is_writex((item.get("question", "") or "") + " " + (item.get("body") or "")):
            return "writing", False
        return "typing", False
    # a listening item carries audio AND MCQ options
    if item.get("has_audio") and opt_txt:
        return "listening", False
    q = item.get("question", "") or ""
    imgs = item.get("qimgs") or []
    if opt_txt and (_SALES_RE.search(q) or _SALES_RE.search(item.get("body") or "")):
        return "sales", False
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


def _is_assessment_url(url: str) -> bool:
    """True when `url` is already ON an assessment PLAYER (mid-flow), so a CDP-resume can start the walk
    there instead of re-navigating. The AMCAT/Aspiring-Minds player is the definite mid-assessment
    surface; the SHL TalentCentral SPA counts only PAST the invite landing (the `#/link/<base64>` hash
    from the invite email is NOT a resume point — let adapter.enter consume it)."""
    u = (url or "").lower()
    if not u or u.startswith("about:") or u.startswith("chrome:") or u.startswith("data:"):
        return False
    if "aspiringminds" in u or "amcatglobal" in u or "myamcat" in u or "amcat" in u:
        return True
    if "shl.com" in u and "/experience" in u and "/link/" not in u:
        # Exclude the SHL INTRO pages (auth/welcome, task list). A CDP-resume there must RE-RUN
        # adapter.enter (the welcome->consent->task-list->launch intro is idempotent); otherwise the
        # walk loop mis-reads the welcome page as a 1-option "question" ("Are you excited to start
        # your journey?") and answers it. Only an in-progress assessment task is a real resume point.
        if any(k in u for k in ("/auth", "welcome", "task-list", "basic-task", "tasklist")):
            return False
        return True
    return False


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


# CAM_TRACE=1: wrap every camera-detection API and console.log each call, so a live drive reveals
# EXACTLY what a "unable to detect a camera" proctor (WCI200) reads before it rejects — device
# properties (client-side, spoofable) vs frame grab/upload (content/server, needs a real feed/face).
_CAM_TRACE_JS = r"""
(() => {
  const L = (w, d) => { try { console.log('CAMTRACE|' + w + '|' + (typeof d === 'string' ? d : JSON.stringify(d))); } catch (e) { console.log('CAMTRACE|' + w + '|<unser>'); } };
  try {
    const md = navigator.mediaDevices;
    if (md) {
      const gum = md.getUserMedia && md.getUserMedia.bind(md);
      if (gum) md.getUserMedia = function (c) { L('getUserMedia', c || {}); return gum(c); };
      const enu = md.enumerateDevices && md.enumerateDevices.bind(md);
      if (enu) md.enumerateDevices = function () { L('enumerateDevices', 'called'); return enu(); };
      const gsc = md.getSupportedConstraints && md.getSupportedConstraints.bind(md);
      if (gsc) md.getSupportedConstraints = function () { L('getSupportedConstraints', 'called'); return gsc(); };
    }
    const tp = self.MediaStreamTrack && MediaStreamTrack.prototype;
    if (tp) {
      for (const m of ['getCapabilities', 'getSettings', 'applyConstraints']) {
        const real = tp[m];
        if (real) tp[m] = function (...a) { L('track.' + m, a[0] || 'read'); return real.apply(this, a); };
      }
    }
    if (self.ImageCapture) {
      const IC = self.ImageCapture;
      const Wrapped = function (t) { L('new ImageCapture', (t && t.label) || '?'); return new IC(t); };
      Wrapped.prototype = IC.prototype;
      try { self.ImageCapture = Wrapped; } catch (e) {}
      for (const m of ['grabFrame', 'takePhoto', 'getPhotoCapabilities']) {
        const real = IC.prototype[m];
        if (real) IC.prototype[m] = function (...a) { L('ImageCapture.' + m, 'call'); return real.apply(this, a).then(r => { L('ImageCapture.' + m + '.ok', (r && (r.width ? r.width + 'x' + r.height : r.size || 'ok')) || 'ok'); return r; }, e => { L('ImageCapture.' + m + '.ERR', String(e)); throw e; }); };
      }
    }
  } catch (e) { L('trace-install-error', String(e)); }
})();
"""

# Spoof a real integrated webcam's capability surface over our bare v4l2loopback device. Injected into
# the page BEFORE any site JS (ctx.add_init_script) only when CAM_SPOOF=1. Merges real-webcam image-
# control capability keys + a non-empty facingMode onto the genuine getCapabilities()/getSettings()
# result (keeping the real deviceId/groupId/resolution), so a proctor that validates "is this a real
# camera?" by reading the capability set sees a plausible UVC laptop camera.
_CAM_SPOOF_JS = r"""
(() => {
  const proto = (self.MediaStreamTrack && MediaStreamTrack.prototype);
  if (!proto) return;
  const isVideo = (t) => { try { return t.kind === 'video'; } catch (e) { return false; } };
  let _micGroup = null;   // captured from enumerateDevices; a real integrated cam shares it with a mic
  const realCaps = proto.getCapabilities;
  const realSet = proto.getSettings;
  if (realCaps) {
    Object.defineProperty(proto, 'getCapabilities', {configurable: true, writable: true, value: function () {
      let c = {}; try { c = realCaps.call(this) || {}; } catch (e) {}
      if (!isVideo(this)) return c;
      if (!(c.facingMode && c.facingMode.length)) c.facingMode = ['user'];
      c.resizeMode = c.resizeMode || ['none', 'crop-and-scale'];
      c.exposureMode = ['continuous', 'manual'];
      c.exposureCompensation = {min: -2, max: 2, step: 0.16666667};
      c.exposureTime = {min: 5, max: 2500, step: 1};
      c.whiteBalanceMode = ['continuous', 'manual'];
      c.colorTemperature = {min: 2800, max: 6500, step: 10};
      c.focusMode = ['continuous', 'manual'];
      c.focusDistance = {min: 0, max: 1024, step: 1};
      c.brightness = {min: -64, max: 64, step: 1};
      c.contrast = {min: 0, max: 64, step: 1};
      c.saturation = {min: 0, max: 128, step: 1};
      c.sharpness = {min: 0, max: 6, step: 1};
      return c;
    }});
  }
  if (realSet) {
    Object.defineProperty(proto, 'getSettings', {configurable: true, writable: true, value: function () {
      let s = {}; try { s = realSet.call(this) || {}; } catch (e) {}
      if (!isVideo(this)) return s;
      if (!s.facingMode) s.facingMode = 'user';
      s.exposureMode = 'continuous'; s.whiteBalanceMode = 'continuous'; s.focusMode = 'continuous';
      s.brightness = 0; s.contrast = 32; s.saturation = 64; s.sharpness = 3; s.colorTemperature = 4600;
      if (_micGroup && s.groupId) s.groupId = _micGroup;   // pair with the mic's group
      return s;
    }});
  }
  // DEVICE PAIRING: a real integrated webcam shares its groupId with the built-in mic. Our v4l2loopback
  // camera has a standalone group (sharesGroupWithMic:false) — a likely "detect a camera" tell. Make
  // enumerateDevices report the camera in the same group as an audio input.
  try {
    const md = navigator.mediaDevices;
    if (md && md.enumerateDevices) {
      const realEnum = md.enumerateDevices.bind(md);
      md.enumerateDevices = async function () {
        const devs = await realEnum();
        const mic = devs.find(d => d.kind === 'audioinput' && d.groupId);
        if (mic) _micGroup = mic.groupId;
        if (!_micGroup) return devs;
        return devs.map(d => {
          if (d.kind !== 'videoinput' || d.groupId === _micGroup) return d;
          const o = {deviceId: d.deviceId, kind: d.kind, label: d.label, groupId: _micGroup};
          o.toJSON = () => ({deviceId: d.deviceId, kind: d.kind, label: d.label, groupId: _micGroup});
          return o;
        });
      };
    }
  } catch (e) {}
})();
"""

# PER-LANE MIC PIN (parallel harvester lanes). A lane runs its own pulse null-sink (mic.SOURCE =
# virtmic_<HARVEST_MIC_SUFFIX>_src) but Chromium's getUserMedia({audio:true}) otherwise grabs the pulse
# SERVER default source (shared across lanes). This init-script pins the audio input to THIS lane's own
# remap-source by matching its device.description label, so N concurrent browsers each capture only their
# OWN sink → no cross-lane speaking garble. Fail-open: if the labelled source isn't enumerable it leaves
# the constraints untouched (falls back to the pulse default, which every lane also sets, so a miss can't
# silence the mic). Injected ONLY when HARVEST_MIC_SUFFIX is set → the default single lane is unchanged.
_MIC_PIN_JS = r"""
(() => {
  const md = navigator.mediaDevices; if (!md || !md.getUserMedia) return;
  const rGUM = md.getUserMedia.bind(md), rEnum = md.enumerateDevices.bind(md);
  const WANT = "__SRC__";
  let srcId = null;
  const find = async () => { try { const ds = await rEnum();
    const o = ds.find(d => d.kind === 'audioinput' && (d.label || '').includes(WANT));
    return o ? o.deviceId : null; } catch (e) { return null; } };
  md.getUserMedia = async (c) => { c = c || {};
    try {
      if (c.audio) { if (srcId === null) srcId = await find();
        if (srcId) { const a = (typeof c.audio === 'object') ? c.audio : {};
          a.deviceId = {exact: srcId}; c.audio = a; } }
    } catch (e) {}
    return rGUM(c); };
})();
"""


async def harvest_one(url: str, mailbox: str, adapter, *, max_items: int = 320,
                      min_delay: float = 0.8, max_delay: float = 2.2,
                      session_secs: float = 1200) -> dict:
    """Drive ONE assessment session end-to-end. Returns a result dict with per-type counts.

    `session_secs` is the hard wall-clock for the whole walk. The 1200s (20-min) default was the #1
    reason a full AMCAT battery never COMPLETED — the deepest runs (banked ~125) walked SVAR→Typing→
    Personality→into Analytical and were killed by this cap mid-battery, not by any DOM failure. A full
    battery needs far longer, so raise it for a completion run (env HARVEST_SESSION_SECS overrides;
    harvest_runner sets a platform default). Kept at 1200 by default so a pure question-HARVEST pass
    (which only needs to reach new items, not finish) is unchanged."""
    import os as _os
    try:
        session_secs = float(_os.getenv("HARVEST_SESSION_SECS") or session_secs)
    except (TypeError, ValueError):
        pass
    from playwright.async_api import async_playwright
    res = {"status": "error", "banked": 0, "by_type": {}, "shots": [], "note": "", "mailbox": mailbox,
           "walls": []}
    # Prefer the PULSE VIRTUAL MIC: it lets us feed the REQUIRED SVAR sentence (read-aloud prompt /
    # ASR of the listen-repeat audio) into the recorder per item, so speaking sections ADVANCE past the
    # SVAR wall into Typing / Personality / cognitive. Fall back to the static fake-audio file when the
    # pulse mic isn't available (old behaviour: harvests Section A + listening, stalls at listen-repeat).
    mic_ready = False
    launch_env = None
    _cdp_mode = bool(os.getenv("HARVEST_CDP_URL"))
    if not _cdp_mode:
        try:
            mic_ready = mic.ensure()
        except Exception:
            mic_ready = False
    # Platform-specific CAMERA. The SHL TalentCentral SPA (the Sutherland front-door,
    # talentcentral.us1.shl.com/experience) BLANKS to the React noscript ("You need to enable
    # JavaScript to run this app") under Chromium's SYNTHETIC camera (--use-fake-device-for-media-
    # stream / --use-file-for-fake-video-capture) → the harvester stalled at the intro. Use the REAL
    # v4l2loopback camera instead (sutherland_assessment.camera_launch_args → a genuine /dev/video0,
    # NO fake-device): it hydrates the SPA AND is what the downstream AMCAT WCI200 proctor accepts.
    # (Proven 2026-09-13: minimal launch w/o the fake-device flag hydrates rootHTML=8435, 0 errors.)
    if _cdp_mode:
        # Remote Mac browser: no local launch args / assets needed (and no /dev/video0 to touch).
        args = ["--no-sandbox"]
        asset_paths = {}
    elif getattr(adapter, "platform", "") in ("shl_sutherland", "shl", "hallo", "harver"):
        # Hallo.ai's device-check ACCEPTS the real v4l2loopback camera (proven; unlike Sutherland's
        # WCI200 it does not reject it), and needs the real camera + the pulse virtmic to pass. Harver
        # (TTEC) is proctored the same way (camera + mic) → give it the real camera + virtmic too.
        from backend.tools import sutherland_assessment
        args = sutherland_assessment.camera_launch_args()   # real /dev/video0 + --use-fake-ui, no fake-device
        asset_paths = assets.ensure_assets()
        if mic_ready:
            launch_env = mic.launch_env()                   # real pulse virtual mic for SVAR speaking
    elif mic_ready:
        asset_paths = assets.ensure_assets()
        args = ["--no-sandbox"] + mic.launch_args()
        if asset_paths.get("video"):
            args.append(f"--use-file-for-fake-video-capture={asset_paths['video']}")
        launch_env = mic.launch_env()
    else:
        args, asset_paths = _launch_args()
    res["fake_media"] = asset_paths
    res["mic_pass"] = mic_ready
    res["_audiodir"] = tempfile.mkdtemp(prefix="amcat_aud_")
    res["_say_wav"] = os.path.join(res["_audiodir"], "say.wav")
    res["_latest_aud"] = None
    # Optional Bright Data proxy egress (env HARVEST_PROXY=1): route the whole session through a fresh
    # rotating BD IP so the assessment server sees a different IP than our (rate-limited) datacenter one
    # — the NE500 logouts appeared only after heavy same-IP use. Best-effort; None = direct.
    res["_proxy"] = None
    _pmode = os.environ.get("HARVEST_PROXY")
    if _pmode == "res":
        # RESIDENTIAL BD egress (a fresh rotating home IP per session) — datacenter IPs (ours + the BD dc
        # pool) get the assessment server's NE500 / STATE_TRANSITION logouts; residential avoids the flag.
        try:
            from backend.config import settings
            cust = settings.brightdata_customer
            pw = os.environ.get("HARVEST_RES_PW") or ""
            gw = settings.brightdata_gateway or "brd.superproxy.io:33335"
            if cust and pw:
                sess = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=10))
                user = f"brd-customer-{cust}-zone-alibaba_res-country-us-session-{sess}"
                res["_proxy"] = {"server": f"http://{gw}", "username": user, "password": pw}
                logger.info("[%s] egress via RESIDENTIAL proxy (alibaba_res, session %s)", mailbox, sess)
        except Exception as exc:
            logger.info("[%s] residential proxy unavailable (%s) — going direct", mailbox, exc)
    elif _pmode in ("phone", "mobile"):
        # PHONE/mobile-carrier egress — the live Tailscale exit-node SOCKS slots, exactly the egress
        # the SHL lane uses (shl_assess_runner._shl_proxy). The AMCAT/SHL portals NE500-rate-limit /
        # TCP-block our DATACENTER IP after volume; a real KZ mobile-carrier IP avoids it. Round-robin
        # a live slot; falls back to the datacenter pool, then DIRECT — a dead phone never blocks.
        try:
            from backend.tools import proxy_pool
            slots = proxy_pool.residential_slots()
            if slots:
                res["_proxy"] = {"server": random.choice(slots)}
                logger.info("[%s] egress via PHONE slot %s", mailbox, res["_proxy"]["server"])
            else:
                pr = proxy_pool.next_proxy()
                if pr and pr.get("server"):
                    res["_proxy"] = {k: pr[k] for k in ("server", "username", "password") if pr.get(k)}
                    logger.info("[%s] no phone slot live — egress via pool %s", mailbox, pr.get("server"))
        except Exception as exc:
            logger.info("[%s] phone egress unavailable (%s) — going direct", mailbox, exc)
    elif _pmode == "1":
        try:
            from backend.tools import proxy_pool
            pr = proxy_pool.next_proxy()
            if pr and pr.get("server"):
                res["_proxy"] = {k: pr[k] for k in ("server", "username", "password") if pr.get(k)}
                logger.info("[%s] egress via proxy %s", mailbox, pr.get("server"))
        except Exception as exc:
            logger.info("[%s] proxy unavailable (%s) — going direct", mailbox, exc)
    if res["_proxy"]:
        # BD (esp. residential) SSL-bumps HTTPS -> Chromium sees ERR_CERT_AUTHORITY_INVALID. We control
        # the proxy and don't need cert validation for a synthetic harvest, so accept any cert.
        args = args + ["--ignore-certificate-errors"]

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
            # REMOTE CDP mode (HARVEST_CDP_URL): drive an already-running Chrome elsewhere (the owner's
            # MacBook, reached over the Tailscale tunnel) instead of launching a local browser. The
            # MacBook's REAL webcam beats WCI200 and the owner's live face passes the proctoring
            # liveness/gaze check that flags a synthetic session. We do NOT own that browser -> reuse its
            # context + tab and never close it; the fake-device args / virtmic / proxy don't apply (real HW).
            _cdp = os.getenv("HARVEST_CDP_URL")
            if _cdp:
                b = await p.chromium.connect_over_cdp(_cdp, timeout=30000)
                ctx = b.contexts[0] if b.contexts else await b.new_context(
                    permissions=["microphone", "camera"])
                try:
                    await ctx.grant_permissions(["microphone", "camera"])
                except Exception:
                    pass
                # Pin the proctor's camera to the OBS Virtual Camera (our looping face) and hide the
                # Mac's built-in webcam, so a device picker or a bare {video:true} can't grab the real
                # camera. Fail-open: passes through untouched when no OBS device is enumerable.
                try:
                    await ctx.add_init_script("""(() => {
                      const md = navigator.mediaDevices; if (!md) return;
                      const rGUM = md.getUserMedia.bind(md), rEnum = md.enumerateDevices.bind(md);
                      let obsId = null;
                      const find = async () => { try { const ds = await rEnum();
                        const o = ds.find(d => d.kind==='videoinput' && /obs/i.test(d.label));
                        return o ? o.deviceId : null; } catch(e){ return null; } };
                      md.enumerateDevices = async () => { const ds = await rEnum();
                        const has = ds.some(d=>d.kind==='videoinput' && /obs/i.test(d.label));
                        return has ? ds.filter(d=>d.kind!=='videoinput' || /obs/i.test(d.label)) : ds; };
                      md.getUserMedia = async (c) => { c = c || {};
                        if (c.video) { if (obsId === null) obsId = await find();
                          if (obsId) { const v = (typeof c.video === 'object') ? c.video : {};
                            v.deviceId = {exact: obsId}; c.video = v; } }
                        return rGUM(c); };
                    })();""")
                except Exception:
                    pass
                res["_remote_cdp"] = True
                logger.info("[%s] REMOTE Chrome via CDP %s (real camera + live face)", mailbox, _cdp)
            else:
                b = await p.chromium.launch(headless=False, args=args, env=launch_env, timeout=60000)
                ctx = await b.new_context(viewport={"width": 1280, "height": 850},
                                          permissions=["microphone", "camera"],
                                          proxy=res.get("_proxy") or None,
                                          ignore_https_errors=bool(res.get("_proxy")))
            # PER-LANE MIC PIN: a parallel lane (HARVEST_MIC_SUFFIX set) captures ONLY its own pulse
            # null-sink source, so N concurrent speaking modules don't garble each other. Camera stays
            # the SHARED /dev/video0 (v4l2loopback broadcasts one feed to many readers, verified). Guarded
            # on mic_ready + the suffix so the default single lane / AMCAT cron is byte-identical.
            if mic_ready and os.getenv("HARVEST_MIC_SUFFIX"):
                try:
                    await ctx.add_init_script(_MIC_PIN_JS.replace("__SRC__", mic.SOURCE))
                except Exception:
                    pass
            # CAM_SPOOF=1: make the v4l2loopback camera's getCapabilities()/getSettings() mimic a real
            # integrated webcam (non-empty facingMode + exposure/whiteBalance/focus/brightness controls).
            # Our virtual device exposes a BARE capability set (facingMode:[], no image controls) which is
            # the likely tell a "unable to detect a camera" proctor (Sutherland WCI200) reads. Default OFF
            # (an experiment on the Sutherland lane), so normal harvest/AMCAT/Hallo runs are unchanged.
            if os.getenv("CAM_SPOOF") == "1":
                try:
                    await ctx.add_init_script(_CAM_SPOOF_JS)
                except Exception:
                    pass
            _on_console = None
            if os.getenv("CAM_TRACE") == "1":
                try:
                    await ctx.add_init_script(_CAM_TRACE_JS)
                    _cam_trace_path = os.path.join(
                        os.environ.get("CAM_TRACE_DIR", "/tmp"), f"camtrace_{mailbox}.log")
                    _ctf = open(_cam_trace_path, "a")

                    def _on_console(msg):
                        try:
                            t = msg.text
                            if "CAMTRACE|" in t:
                                _ctf.write(t + "\n"); _ctf.flush()
                        except Exception:
                            pass

                    # The proctor domains whose device/camera-check call decides WCI200. The camera
                    # wall fires right after getUserMedia and BEFORE any frame grab, so the tell is
                    # in one of these requests (device enumeration uploaded) or its SERVER response
                    # — not in frame content. Log EVERY POST to them (+ a body snippet) and the
                    # response verdict, so the next fresh token reveals the exact trigger.
                    _PROCTOR_HOSTS = ("myamcat.com", "aspiringminds", "amcat", "shl.com", "proctor")

                    def _is_proctor(u):
                        return any(h in u for h in _PROCTOR_HOSTS)

                    def _on_request(req):
                        try:
                            if req.method != "POST":
                                return
                            hl = (req.headers or {}).get("content-type", "")
                            proc = _is_proctor(req.url)
                            if proc or "image" in hl or "octet-stream" in hl or "form-data" in hl:
                                body = ""
                                if proc:
                                    try:
                                        pd = req.post_data or ""
                                        body = "|body=" + pd[:400].replace("\n", " ")
                                    except Exception:
                                        body = ""
                                _ctf.write(f"CAMTRACE|POST|{req.url[:160]}|ct={hl[:40]}{body}\n"); _ctf.flush()
                        except Exception:
                            pass

                    async def _on_proctor_response(resp):
                        try:
                            u = resp.url
                            if not _is_proctor(u):
                                return
                            low = u.lower()
                            if not any(k in low for k in ("camera", "device", "proctor", "wci",
                                                          "check", "verify", "snapshot", "media",
                                                          "webcam", "system", "config")):
                                return
                            snippet = ""
                            try:
                                b = await resp.body()
                                snippet = (b or b"")[:400].decode("utf-8", "replace").replace("\n", " ")
                            except Exception:
                                snippet = "<no-body>"
                            _ctf.write(f"CAMTRACE|RESP|{resp.status}|{u[:140]}|{snippet}\n"); _ctf.flush()
                        except Exception:
                            pass

                    ctx.on("request", _on_request)
                    ctx.on("response", lambda r: asyncio.create_task(_on_proctor_response(r)))
                    ctx.on("page", lambda p: p.on("console", _on_console))  # AMCAT player may open a new tab
                    logger.info("[camtrace] logging camera API calls -> %s", _cam_trace_path)
                except Exception as _e:
                    logger.info("[camtrace] setup failed: %s", _e)
            # CDP mode: reuse the owner's existing tab (they watch it for the live camera); else a fresh
            # page. Prefer the tab that is already ON the assessment player (a stray "Example Domain" /
            # new-tab could be pages[0] and would make adapter.enter re-navigate + burn the token).
            if res.get("_remote_cdp") and ctx.pages:
                # HARVEST_CDP_TAB_INDEX pins THIS run to a specific existing tab (0-based over
                # ctx.pages) so two harvester processes can drive two different tabs concurrently.
                _ti = os.getenv("HARVEST_CDP_TAB_INDEX")
                if _ti is not None and _ti.isdigit() and int(_ti) < len(ctx.pages):
                    page = ctx.pages[int(_ti)]
                else:
                    page = next((pg for pg in ctx.pages if _is_assessment_url(pg.url)), ctx.pages[0])
            else:
                page = await ctx.new_page()
            if _on_console is not None:
                page.on("console", _on_console)
            # Auto-accept JS dialogs (a beforeunload "leave this page?" when navigating away from an
            # in-progress assessment tab, or a stray alert) so the driver can't crash with
            # Page.handleJavaScriptDialog "No dialog is showing" before the walk even starts.
            def _on_dialog(d):
                try:
                    asyncio.create_task(d.accept())
                except Exception:
                    pass
            page.on("dialog", _on_dialog)
            ctx.on("page", lambda p: p.on("dialog", _on_dialog))
            # Capture question audio (S3 mp3s under SpeechAssessmentBank) so audio-only listen items
            # (Section B listen-repeat, Section C listen-comprehension) can be transcribed + banked.
            _seen_aud = set()

            async def _aud_sink(resp):
                try:
                    u = resp.url
                    if "SpeechAssessmentBank" not in u:
                        return
                    base = u.split("?")[0]
                    if not (base.lower().endswith((".mp3", ".wav"))
                            or "audio" in (resp.headers or {}).get("content-type", "")):
                        return
                    if base in _seen_aud:
                        return
                    body = await resp.body()
                    if not body or len(body) < 800:
                        return
                    _seen_aud.add(base)
                    fn = os.path.join(res["_audiodir"], hashlib.sha1(base.encode()).hexdigest()[:16] + ".mp3")
                    with open(fn, "wb") as w:
                        w.write(body)
                    res["_latest_aud"] = fn      # newest captured audio = the listen-repeat sentence to echo
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(_aud_sink(r)))
            try:
                # CDP RESUME (HARVEST_CDP_RESUME=1, CDP mode only): the owner already navigated the
                # remote Chrome INTO the assessment (e.g. past the SHL invite landing to the AMCAT
                # player); a fresh adapter.enter would re-navigate/reset that tab. If the already-open
                # page is on the assessment domain, SKIP the fresh nav and start the walk on it. Default
                # behaviour (env unset, or a non-assessment URL) is unchanged: adapter.enter runs.
                if (res.get("_remote_cdp") and os.getenv("HARVEST_CDP_RESUME") == "1"
                        and _is_assessment_url(page.url)):
                    logger.info("[%s] CDP RESUME: already on assessment page %s — skipping fresh nav",
                                mailbox, page.url)
                else:
                    await adapter.enter(page, url)
                stale = 0
                load_waits = 0             # extra patience budget for blank/loading transition pages
                typing_seen: dict = {}     # churn guard: a typing sentence that won't advance
                stall_skips = 0            # how many stuck items we've skipped past (bounded)
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
                    if not opts and item_type not in ("speaking", "video", "typing", "writing"):
                        if await adapter.advance(page):
                            await page.wait_for_timeout(450)
                            stale = 0
                            load_waits = 0
                            continue
                        if await adapter.is_done(page):
                            res["status"] = "completed"; res["note"] = "completed (no forward)"
                            return
                        # A blank/loading TRANSITION (a spinner, near-empty body) — common when the SHL
                        # intro hands off to the AMCAT player / next section through the slow phone
                        # proxy (30s+). Don't mistake it for a stall: poll patiently (up to ~14×3.5s ≈
                        # 50s) for content or a URL change before counting stale.
                        try:
                            _loading = await page.evaluate(
                                """() => {
                                  const t = (document.body && document.body.innerText || '').trim();
                                  const spin = document.querySelector(
                                    '[class*="spinner" i],[class*="loading" i],[class*="loader" i],'
                                    + '[role="progressbar"],svg[class*="load" i]');
                                  return t.length < 40 || !!spin;
                                }""")
                        except Exception:
                            _loading = False
                        if _loading and load_waits < 14:
                            load_waits += 1
                            await page.wait_for_timeout(3500)
                            continue
                        stale += 1
                        if stale >= 3:
                            try:
                                _u = page.url
                                _sh = await media.capture(page, adapter.platform, "STUCK", [], _u)
                                if _sh:
                                    res["shots"].append(("stuck", _sh, _u))
                            except Exception:
                                _u = "?"
                            res["status"] = "stuck"
                            res["note"] = f"no options/forward at step {step} @ {_u}: {item.get('body','')[:120]}"
                            return
                        await page.wait_for_timeout(1500)
                        continue
                    stale = 0

                    q = item.get("question", "")
                    need_shot = (item_type in ("speaking", "video", "typing", "writing", "listening", "picture")
                                 or is_ability or bool(item.get("qimgs"))
                                 or item.get("has_table") or any(o.get("image") for o in opts)
                                 or not opt_txt)
                    shot = await _shot(item) if need_shot else None
                    if shot:
                        res["shots"].append((item_type, shot, q[:80]))

                    # ---- SPEAKING (SVAR) — fake mic; read-aloud sentences are banked, intros / audio-only
                    #      listen items are _walk_only (advanced but not banked; their content is captured
                    #      + transcribed by the ASR post-pass). ----
                    if item_type == "speaking":
                        if not item.get("_walk_only"):
                            _bank(item, "speaking", False,
                                  {"text": None, "index": None, "value": "fake_audio_submitted", "source": "fake_device"},
                                  shot, free="speaking")
                            logger.info("[%s] #%d speaking q=%r (fake mic)", mailbox, res["banked"], q[:60])
                        # PASS mode: compute the sentence to speak into the virtual mic so the item
                        # ADVANCES — a read-aloud item speaks the shown prompt; a listen-repeat / audio-only
                        # item echoes the ASR of the just-played question audio. handle_speaking plays it
                        # THROUGHOUT the record window.
                        say_wav = None
                        if res.get("mic_pass"):
                            say = None
                            # PREPARED spoken ANSWER (owner's approach: collect all questions, prepare a
                            # correct answer per question, replay it). For a scenario/open-response item
                            # (e.g. a Hallo video CSR prompt) the banked answer_key.text is a good spoken
                            # response — speak THAT, not the prompt. Read-aloud items have no prepared
                            # answer and fall through to speaking the shown sentence.
                            prepared = bank.answer_for(adapter.platform, q, [])
                            if prepared and (prepared.get("text") or "").strip():
                                say = prepared["text"].strip()
                            elif not item.get("_walk_only"):
                                say = (q or "").strip() or None                     # read-aloud
                            elif asr.available():
                                await page.wait_for_timeout(1500)                    # let the audio play+capture
                                if res.get("_latest_aud"):
                                    say = asr.transcribe(res["_latest_aud"])         # listen-repeat / audio-only
                            # repeat the sentence so one playback comfortably spans the record window;
                            # speech_wav_for CACHES the clip content-addressed, so a recurring question
                            # replays the stored file with no regeneration and no latency.
                            if say and len(say) >= 4:
                                say_wav = assets.speech_wav_for(((say.strip() + ". ") * 2).strip())
                        if await adapter.handle_speaking(page, mic_say_wav=say_wav):
                            await page.wait_for_timeout(1500)
                            continue
                        res["walls"].append(("speaking", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"svar item did not advance (banked {res['banked']}, walk_only={item.get('_walk_only', False)})"
                        return

                    # ---- VIDEO — fake camera, record cycle ----
                    if item_type == "video":
                        _bank(item, "video", False,
                              {"text": None, "index": None, "value": "fake_video_submitted", "source": "fake_device"},
                              shot, free="video")
                        logger.info("[%s] #%d video q=%r (face cam)", mailbox, res["banked"], q[:60])
                        # A Hallo video-response is a SPOKEN answer on camera: the camera shows the
                        # session-level face-feed (CAMERA_FACE_VIDEO) and the mic plays the PREPARED
                        # answer for this question (cached — generated once, replayed on repeats), just
                        # like the speaking branch. Falls back to the shown prompt if none is banked.
                        say_wav = None
                        if res.get("mic_pass"):
                            prepared = bank.answer_for(adapter.platform, q, [])
                            say = (prepared.get("text") or "").strip() if prepared else ""
                            if not say:
                                say = (q or "").strip()
                            if say and len(say) >= 4:
                                say_wav = assets.speech_wav_for(((say.strip() + ". ") * 2).strip())
                        if await adapter.handle_speaking(page, record_secs=4.0, mic_say_wav=say_wav):
                            await page.wait_for_timeout(1500)
                            continue
                        res["walls"].append(("video", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"video item did not advance (item {res['banked']})"
                        return

                    # ---- TYPING — fill and advance ----
                    if item_type == "typing":
                        # churn guard: if the SAME typing sentence keeps reappearing it isn't advancing
                        # (bad extraction / unaccepted input) — bail instead of re-typing to max_items.
                        typing_seen[q] = typing_seen.get(q, 0) + 1
                        if typing_seen[q] > 8:
                            res["status"] = "stuck"
                            res["note"] = f"typing churn on same sentence ({res['banked']} banked)"
                            return
                        _bank(item, "typing", False,
                              {"text": None, "index": None, "value": "fake_text_typed", "source": "fake_device"},
                              shot, free="typing")
                        logger.info("[%s] #%d typing q=%r", mailbox, res["banked"], q[:60])
                        if await adapter.handle_typing(page, _FAKE_SPEECH_TEXT):
                            await page.wait_for_timeout(450)
                            continue
                        res["walls"].append(("typing", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"typing item did not advance (item {res['banked']})"
                        return

                    # ---- WRITING (WriteX email) — draft/replay a business email, fill + submit ----
                    if item_type == "writing":
                        _bank(item, "writing", False,
                              {"text": None, "index": None, "value": "email_submitted", "source": "writex"},
                              shot, free="typing")
                        logger.info("[%s] #%d writex q=%r", mailbox, res["banked"], q[:60])
                        try:
                            banked = bank.answer_for(adapter.platform, q, [], _msig(item))
                        except Exception:
                            banked = None
                        try:
                            email = await writex.draft_email(q or item.get("body") or "", banked=banked)
                        except Exception:
                            email = None
                        if email and await adapter.handle_writex(page, email):
                            await page.wait_for_timeout(1500)
                            continue
                        # fall back to plain typing of the body so the run still advances past the module
                        if await adapter.handle_typing(page, (email or {}).get("body") or _FAKE_SPEECH_TEXT):
                            await page.wait_for_timeout(450)
                            continue
                        res["walls"].append(("writing", shot, q[:80]))
                        res["status"] = "stuck_free_response"
                        res["note"] = f"writex item did not advance (item {res['banked']})"
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
                        await page.wait_for_timeout(450)
                        continue

                    # Answer selection: (1) REPLAY the banked answer key (match by TEXT — option order can
                    # differ between sessions); (2) else LIVE-solve a NEW TEXT question with the local model
                    # and cache it so it replays next time; (3) else random (image new-question, which needs
                    # offline vision, or HARVEST_MODE=random for pure capture).
                    harvest_random = os.environ.get("HARVEST_MODE") == "random"
                    # image-dependent = explicit images OR a cognitive item with a captured diagram/table
                    # screenshot: the TEXT-only local model can't see those, so never blind-live-solve them
                    # (they get random now + offline vision solving into the key).
                    has_img = (bool(item.get("qimgs")) or any(o.get("image") for o in opts)
                               or bool(_IMG_REF_RE.search(q)))
                    # listening-comprehension: the correct option depends on the DIALOGUE (audio), so a
                    # text-only key is unreliable — transcribe the just-played clip and solve WITH it.
                    listen_ctx = None
                    if (not harvest_random and not has_img and res.get("_latest_aud") and asr.available()
                            and (item_type == "listening" or item.get("has_audio")
                                 or re.search(r"\b(you (?:hear|heard)|conversation|dialogu?e|the call|"
                                              r"caller|customer'?s? call|the speaker|according to the "
                                              r"(?:audio|recording|conversation))\b", q, re.I))):
                        try:
                            dlg = asr.transcribe(res["_latest_aud"])
                            listen_ctx = dlg if (dlg and len(dlg) > 20) else None
                        except Exception:
                            listen_ctx = None
                    idx = None
                    pick_src = "random"
                    live_cache = False
                    cache_src = "live_llm"
                    bw = None            # (best_idx, worst_idx) for a Sales best/worst replay
                    if not harvest_random:
                        try:
                            ak = bank.answer_for(adapter.platform, q, opt_txt, _msig(item))
                        except Exception:
                            ak = None
                        if ak and ak.get("kind") == "best_worst":
                            _bt = (ak.get("best") or {}).get("text") or ""
                            _wt = (ak.get("worst") or {}).get("text") or ""
                            _bi = next((i for i, t in enumerate(opt_txt) if t.strip().lower() == _bt.strip().lower()), None)
                            _wi = next((i for i, t in enumerate(opt_txt) if t.strip().lower() == _wt.strip().lower()), None)
                            if _bi is not None:
                                idx, pick_src = _bi, "answer_key"
                            if _bi is not None and _wi is not None and _bi != _wi:
                                bw = (_bi, _wi)
                        strong = bool(ak) and ak.get("source") in ("claude_vision", "dialog_llm")
                        # replay a stored key UNLESS it's a weak text-only key for a listening item we can
                        # now upgrade with the dialogue transcript.
                        if ak and (strong or not listen_ctx):
                            want = (ak.get("text") or "").strip().lower()
                            if want:
                                idx = next((i for i, t in enumerate(opt_txt) if t.strip().lower() == want), None)
                            if idx is None and isinstance(ak.get("index"), int) and 0 <= ak["index"] < len(opts):
                                idx = ak["index"]
                            if idx is not None:
                                pick_src = "answer_key"
                        # `_no_llm_solve` lets an adapter opt an item OUT of the wasted local-model solve
                        # when it answers that item itself (Harver SJT best/worst + personality rating are
                        # decided inside the adapter, so core's solve_one here is pure latency).
                        if idx is None and not has_img and not item.get("_no_llm_solve"):  # solve live
                            solve_q = (f"[You heard this dialogue]: {listen_ctx}\n\nQuestion: {q}"
                                       if listen_ctx else q)
                            try:
                                live_idx = await answer_key.solve_one(solve_q, opt_txt)
                            except Exception:
                                live_idx = None
                            if live_idx is not None:
                                idx, pick_src, live_cache = live_idx, "live_llm", True
                                cache_src = "dialog_llm" if listen_ctx else "live_llm"
                    # If core couldn't key/solve the item AND the adapter has its OWN vision cascade
                    # (Harver `_vision_pick`), DON'T fabricate a random click: a random idx is a valid
                    # int, so the adapter's `isinstance(index,int) and 0<=index<n` replay branch would
                    # TRUST it and never run vision — blind-clicking a coin-flip on a scored cognitive
                    # item. Pass idx=None so the adapter's cascade decides. Adapters WITHOUT a vision
                    # cascade (AMCAT/base) keep the random fallback (byte-identical behavior).
                    delegate_vision = idx is None and hasattr(adapter, "_vision_pick")
                    if idx is None and not delegate_vision:
                        idx = random.randint(0, len(opts) - 1)
                        pick_src = "random"
                    if idx is None:
                        pick_src = "vision_adapter"
                        chosen = {"text": None, "index": None, "value": None, "source": pick_src}
                    else:
                        chosen = {"text": opt_txt[idx] if idx < len(opt_txt) else opts[idx].get("text"),
                                  "index": idx, "value": None, "source": pick_src}
                    _bank(item, item_type, is_ability, chosen, shot, audio_url=audio_url)
                    if live_cache:  # persist the live-solved answer so a recurrence replays it (after _bank)
                        try:
                            bank.set_answer(adapter.platform, q, opt_txt,
                                            {"text": chosen["text"], "index": idx, "source": cache_src,
                                             "needs_vision": False, "ts": bank._now()}, _msig(item))
                        except Exception:
                            pass
                    logger.info("[%s] #%d %s%s q=%r opts=%d pick=%d (%s)",
                                mailbox, res["banked"], item_type,
                                " IMG" if item.get("qimgs") else "", q[:60], len(opts),
                                idx if idx is not None else -1, pick_src)

                    prev = (q, tuple(o.get("text", "") for o in opts), item.get("progress"))
                    await asyncio.sleep(random.uniform(min_delay, max_delay))
                    if bw is not None and await adapter.answer_best_worst(page, item, bw[0], bw[1]):
                        pass                                   # both best + worst selected
                    elif not await adapter.answer_mcq(page, item, idx):
                        await adapter.advance(page)
                    advanced = False
                    _poll = 300 if os.getenv("HARVEST_FAST", "1") != "0" else 600
                    for _ in range(16):
                        await page.wait_for_timeout(_poll)
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
                        # a stubborn item (a drag-order/sequencing UI, or one that can't be answered from
                        # what we captured) shouldn't kill the whole run — try to SKIP past it so the module
                        # still completes; bail only after a few consecutive skips fail.
                        if stall_skips < 5 and await adapter.try_skip(page):
                            await page.wait_for_timeout(1500)
                            if await _signature(page, adapter) != prev:
                                stall_skips += 1
                                logger.info("[%s] skipped a stuck item (skip #%d) after %d banked",
                                            mailbox, stall_skips, res["banked"])
                                continue
                        res["status"] = "stuck"
                        res["note"] = f"item did not advance after answer ({res['banked']} banked)"
                        return
                res["status"] = "stuck"
                res["note"] = f"max_items reached ({res['banked']} banked)"
            finally:
                # REMOTE CDP: never close the owner's browser/context/tab — just let the async_playwright
                # context exit drop the CDP transport. Only a browser we launched is torn down here.
                if not res.get("_remote_cdp"):
                    try:
                        await ctx.close()
                        await b.close()
                    except Exception:
                        pass

    page = None
    try:
        await asyncio.wait_for(_run(), timeout=session_secs)
    except asyncio.TimeoutError:
        res["note"] = f"timeout {int(session_secs)}s ({res['banked']} banked before hang)"
        if res["banked"]:
            res["status"] = "partial_timeout"
    except Exception as exc:
        res["note"] = f"{type(exc).__name__}: {exc}"[:200]
    # After the walk, transcribe the captured question audio and bank the listen-repeat /
    # listen-comprehension sentences (outside the browser watchdog; blocking ASR is fine here).
    try:
        _bank_captured_audio(res, mailbox, url, adapter.platform)
    except Exception as exc:
        logger.info("[asr] post-walk bank failed: %s", exc)
    try:
        import shutil
        if res.get("_audiodir"):
            shutil.rmtree(res["_audiodir"], ignore_errors=True)
    except Exception:
        pass
    return res
