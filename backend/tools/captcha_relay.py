"""Phone relay for solving captchas on the server's real browser → captcha.systeam.kz.

Why REAL input, not Playwright clicks: hCaptcha's behavioural check REJECTS synthetic
(CDP/Playwright) mouse events — a bot-driven click just parks the challenge off-screen, so no
solvable puzzle ever appears. Only genuine X mouse events (what noVNC forwards) are accepted. So
this relay screenshots the headful browser's X display with `scrot` and forwards the phone's taps
as REAL clicks with `xdotool` — a lightweight, mobile-clean noVNC scoped to the one browser. The
human drives navigation (tap Next/Submit → a stable captcha appears) and solves the captcha, all
from a phone; the bot only fills the form fields (over CDP, which doesn't move the real mouse, so
the two never fight). The captcha token is minted on the real Teleperformance origin — valid even
for Enterprise hCaptcha, which off-page token farms can't do.

The screenshot is the WHOLE browser window (address bar + page), so a tap normalised to the image
maps 1:1 to display coordinates — no fragile nested-iframe box maths. The reliable Playwright
detection (visible_popup) is kept only to LABEL the status ("captcha — solve it" vs "form").

Runs inside the automation's asyncio loop (shares the Playwright `page` for detection + bring-to-
front). Bind 127.0.0.1; exposed via nginx captcha.systeam.kz (SSL + basic-auth).

    from backend.tools import captcha_relay
    captcha_relay.set_page(page, display=":98", label="Teleperformance")
    await captcha_relay.serve(9003)
"""
from __future__ import annotations

import asyncio
import base64
import os
import subprocess

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

_PAGE = None
_LABEL = "captcha"
_DISPLAY = ":98"
_DISP = (1280, 900)              # display size, refreshed in serve()
_SCR_TMP = "/tmp/jf_captcha_relay.png"
_LOCK = asyncio.Lock()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def set_page(page, display: str = ":98", label: str = "captcha") -> None:
    global _PAGE, _DISPLAY, _LABEL
    _PAGE = page
    _DISPLAY = display or ":98"
    _LABEL = label or "captcha"


def clear_page() -> None:
    global _PAGE
    _PAGE = None


# ---- real X display grab + input (scrot / xdotool) ----------------------------------------------
def _env():
    return {**os.environ, "DISPLAY": _DISPLAY}


def _display_size() -> tuple:
    try:
        out = subprocess.run(["xdotool", "getdisplaygeometry"], env=_env(),
                             capture_output=True, text=True, timeout=4).stdout.split()
        return int(out[0]), int(out[1])
    except Exception:
        return 1280, 900


def _grab_png() -> bytes:
    subprocess.run(["scrot", "-o", "-p", _SCR_TMP], env=_env(), timeout=8,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    with open(_SCR_TMP, "rb") as f:
        return f.read()


def _click_real(x: int, y: int) -> None:
    subprocess.run(["xdotool", "mousemove", "--sync", str(int(x)), str(int(y)), "click", "1"],
                   env=_env(), timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False)


async def _bring_to_front():
    """Raise the automation browser so scrot/xdotool hit IT, not another window on the shared :98."""
    if _PAGE is None:
        return
    try:
        await _PAGE.bring_to_front()
    except Exception:
        pass


# ---- reliable hCaptcha visible-challenge detection (for the status label only) -------------------
_CHALLENGE_HOST = "https://newassets.hcaptcha.com/captcha/v1/"


def _all_frames(frame):
    yield frame
    for c in frame.child_frames:
        yield from _all_frames(c)


async def _challenge_el(page):
    """The challenge iframe element when a real VISIBLE hCaptcha challenge is up, else None.
    frame=challenge URL excludes the checkbox widget + invisible badge; is_visible() excludes the
    parked/hidden full-size leftover (hCaptcha parks it at left/top:-10000px, opacity:0)."""
    try:
        frames = list(_all_frames(page.main_frame))
    except Exception:
        return None
    for fr in frames:
        u = getattr(fr, "url", "") or ""
        if _CHALLENGE_HOST not in u or "frame=challenge" not in u:
            continue
        try:
            el = await fr.frame_element()
            if not await el.is_visible():
                continue
            box = await el.bounding_box()
        except Exception:
            continue
        if box and box["width"] > 200 and box["height"] > 200:
            return el
    return None


async def visible_popup(page):
    """Truthy when a VISIBLE hCaptcha challenge is up — the is-a-captcha-up gate shared with the bot
    (backend.tools.icims_recon._has_captcha)."""
    return await _challenge_el(page)


# ---- mobile page --------------------------------------------------------------------------------
_HTML = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=6,user-scalable=yes">
<title>Капча · пульт</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;background:#0c1116;color:#e6edf3;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;flex-direction:column;min-height:100vh}
  header{padding:9px 12px;background:#0c47c2;color:#fff;display:flex;align-items:center;gap:9px;
         position:sticky;top:0;z-index:5}
  header b{font-weight:700}
  #st{margin-left:auto;font-size:12px;opacity:.95}
  #badge{font-size:12px;padding:2px 8px;border-radius:20px;background:#1f6feb}
  #badge.cap{background:#d1242f}
  .wrap{flex:1;display:flex;flex-direction:column;align-items:center;padding:6px;gap:6px}
  #v{width:100%;max-width:1280px;border:1px solid #223;border-radius:8px;background:#000;
     touch-action:manipulation;display:block}
  .hint{font-size:12.5px;color:#9fb0c0;text-align:center;padding:2px 12px 10px;max-width:680px}
</style></head><body>
<header><b>JF</b> <span id="lab">пульт</span> <span id="badge">…</span><span id="st">…</span></header>
<div class="wrap">
  <img id="v" alt="browser">
  <div class="hint">Тапай прямо по экрану — это реальные клики в браузере на сервере.
     Сам жми «Next»/«Submit», решай капчу как обычно. Пальцами можно зумить.</div>
</div>
<script>
const v=document.getElementById('v'), st=document.getElementById('st'),
      lab=document.getElementById('lab'), badge=document.getElementById('badge');
let busy=false;
function refresh(){ v.src='/frame?t='+Date.now(); }
v.addEventListener('load', ()=> setTimeout(refresh, 500));
v.addEventListener('error', ()=> setTimeout(refresh, 1200));
async function poll(){
  try{
    const j = await (await fetch('/size',{cache:'no-store'})).json();
    lab.textContent = j.label || 'пульт';
    if(j.captcha){ badge.textContent='КАПЧА'; badge.className='cap'; }
    else { badge.textContent='форма'; badge.className=''; }
    st.textContent = j.w ? (j.w+'×'+j.h) : 'офлайн';
  }catch(e){ st.textContent='офлайн'; }
}
v.addEventListener('click', async (e)=>{
  if(busy) return; busy=true;
  const r=v.getBoundingClientRect();
  const x=(e.clientX-r.left)/r.width, y=(e.clientY-r.top)/r.height;
  try{ await fetch('/click',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({x,y})}); }catch(err){}
  busy=false; setTimeout(refresh, 180);
});
poll(); setInterval(poll, 1500); refresh();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/size")
async def size():
    dw, dh = _DISP
    cap = False
    if _PAGE is not None:
        try:
            async with _LOCK:
                cap = bool(await visible_popup(_PAGE))
        except Exception:
            cap = False
    return JSONResponse({"w": dw, "h": dh, "captcha": cap,
                         "label": (_LABEL + " · КАПЧА — реши её") if cap
                                  else (_LABEL + " · форма — жми Next")})


@app.get("/frame")
async def frame():
    """Live screenshot of the whole browser on the X display (real pixels via scrot)."""
    try:
        await _bring_to_front()
        png = await asyncio.get_event_loop().run_in_executor(None, _grab_png)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
    except Exception:
        return Response(_BLANK_PNG, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/click")
async def click(req: Request):
    """Forward a tap as a REAL X click (xdotool) at the same display coordinate — the only input
    hCaptcha accepts. Normalised (x,y) → display px (the screenshot IS the display)."""
    try:
        d = await req.json()
        x = max(0.0, min(1.0, float(d.get("x", 0))))
        y = max(0.0, min(1.0, float(d.get("y", 0))))
    except Exception:
        return JSONResponse({"ok": False, "err": "bad json"})
    dw, dh = _DISP
    try:
        await _bring_to_front()
        await asyncio.get_event_loop().run_in_executor(None, _click_real, round(x * dw), round(y * dh))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "err": f"{type(e).__name__}: {e}"[:80]})


async def serve(port: int = 9003):
    """Start the relay on 127.0.0.1:port inside the CURRENT event loop; returns the server task.
    Signal handlers DISABLED (an embedded uvicorn would hijack the automation's SIGINT/SIGTERM); the
    task is wrapped so a bind failure can't kill the caller's loop."""
    global _DISP
    try:
        _DISP = _display_size()
    except Exception:
        pass
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    async def _guarded():
        try:
            await server.serve()
        except BaseException as e:
            print(f"[captcha relay server stopped: {type(e).__name__}: {e}]"[:140], flush=True)

    return asyncio.create_task(_guarded())
