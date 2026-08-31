"""In-process captcha-solving relay → captcha.systeam.kz (solve from your phone).

Why a mirror-and-forward relay (not an embedded widget): an hCaptcha token is bound to the origin
of the page that rendered it (Teleperformance / iCIMS). A widget re-hosted on our own domain would
mint a token for the WRONG origin and be rejected — and off-page token farms have DROPPED hCaptcha
because its 2024-2026 AI-resistant visual set broke them. So the challenge MUST be solved in OUR
server browser, where it renders against TP's origin and mints a valid (even Enterprise) token.

This relay runs INSIDE the automation's asyncio loop, sharing the live Playwright `page`. It streams
that browser's viewport to a mobile web page and forwards each tap back as a REAL mouse click, so a
human solves the captcha from a phone while the automation stays on the server. The bot's own
`_has_captcha` poll then sees the challenge clear and continues the wizard. Free, ours, Enterprise-proof.

Bind 127.0.0.1 only; exposed via nginx captcha.systeam.kz (SSL + basic-auth). One active page at a time.

    from backend.tools import captcha_relay
    captcha_relay.set_page(page, label="Teleperformance hCaptcha")
    await captcha_relay.serve(9003)      # returns the uvicorn server task; keep it alive
"""
from __future__ import annotations

import asyncio
import base64

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# 1x1 transparent PNG — served instead of a page screenshot when NO captcha is up, so the phone can
# never show the underlying form (the whole point: mirror ONLY a real, visible challenge).
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

_PAGE = None
_LABEL = "captcha"
_LOCK = asyncio.Lock()          # serialise page ops (screenshot / click) on the one event loop

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def set_page(page, label: str = "captcha") -> None:
    """Point the relay at the current Playwright page (call whenever the automation (re)creates it)."""
    global _PAGE, _LABEL
    _PAGE = page
    _LABEL = label or "captcha"


def clear_page() -> None:
    global _PAGE
    _PAGE = None


async def _viewport() -> dict:
    """The live CSS pixel size of the visible viewport (patchright uses no_viewport → the real
    window size, so page.viewport_size is None; read innerWidth/innerHeight instead)."""
    try:
        vp = await _PAGE.evaluate("()=>({w:window.innerWidth,h:window.innerHeight})")
        if vp and vp.get("w"):
            return {"w": int(vp["w"]), "h": int(vp["h"])}
    except Exception:
        pass
    return {"w": 1280, "h": 850}


_CHALLENGE_HOST = "https://newassets.hcaptcha.com/captcha/v1/"


def _all_frames(frame):
    yield frame
    for c in frame.child_frames:
        yield from _all_frames(c)


# Runs INSIDE the frame that OWNS the hCaptcha widget (same-origin — the iCIMS content frame, or the
# main frame for a top-level widget). Returns the challenge iframe's rect RELATIVE TO THAT FRAME's
# viewport when a real, VISIBLE puzzle is up, else null. Visibility is the wrapper's own toggle
# (opacity/visibility/aria-hidden + on-screen), never size — the hidden/leftover challenge iframe
# stays full-size but its wrapper is parked at left/top:-10000px. Verbatim signals from a captured
# hCaptcha DOM (rawandahmad698/pyCFSolver) + QIN2DIM/hcaptcha-challenger.
_POPUP_JS = r"""() => {
  const ifrs = [...document.querySelectorAll('iframe')];
  const ifr = ifrs.find(f => (f.title || '').includes('hCaptcha challenge'))
           || ifrs.find(f => (f.src || '').includes('frame=challenge'));
  if (!ifr) return null;
  // climb to the toggling wrapper (the absolutely/fixed-positioned ancestor hCaptcha parks off-screen)
  let wrap = ifr.parentElement;
  for (let i = 0; i < 4 && wrap && wrap.parentElement; i++) {
    const p = getComputedStyle(wrap).position;
    if (p === 'absolute' || p === 'fixed') break;
    wrap = wrap.parentElement;
  }
  const cs = getComputedStyle(wrap || ifr);
  const r = ifr.getBoundingClientRect();          // popup content, this-frame-viewport-relative
  const shown = cs.visibility !== 'hidden'
    && parseFloat(cs.opacity || '1') > 0.1
    && (!wrap || wrap.getAttribute('aria-hidden') !== 'true')
    && r.top > -1000 && r.left > -1000 && r.bottom > 0 && r.right > 0
    && r.width > 200 && r.height > 200;
  return shown ? {x: r.x, y: r.y, width: r.width, height: r.height} : null;
}"""


_DBG_JS = r"""() => ({
  url: location.href.slice(0, 55),
  iframes: [...document.querySelectorAll('iframe')].map(f => {
    const r = f.getBoundingClientRect();
    return {title: (f.title || '').slice(0, 45), src: (f.src || '').slice(0, 70),
            w: Math.round(r.width), h: Math.round(r.height),
            top: Math.round(r.top), left: Math.round(r.left)};
  })
})"""


async def _frame_offset(frame) -> tuple:
    """Top-viewport (x,y) offset of `frame` = sum of its ancestor iframe elements' boxes. The iCIMS
    content frame is a DIRECT child of main, so this is one reliable hop (frame_element on a direct
    child is unambiguous, unlike a deeply-nested frame's own bounding_box)."""
    ox = oy = 0.0
    f = frame
    while f is not None and f.parent_frame is not None:
        try:
            fe = await f.frame_element()
            b = await fe.bounding_box()
            if b:
                ox += b["x"]
                oy += b["y"]
        except Exception:
            pass
        f = f.parent_frame
    return ox, oy


async def _challenge_el(page):
    """(element_handle, box) of the challenge iframe when a real VISIBLE hCaptcha challenge is up,
    else (None, None). The `frame=challenge` URL excludes the checkbox widget AND the invisible
    badge; Playwright is_visible() excludes the parked/hidden wrapper (hCaptcha hides the still
    full-size iframe with opacity:0 / visibility:hidden + off-screen). Returning the element lets the
    handlers use element.screenshot() / element.click(position=) — Playwright's OWN box maths, so no
    fragile manual nested-iframe offset."""
    try:
        frames = list(_all_frames(page.main_frame))
    except Exception:
        return None, None
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
            return el, box
    return None, None


async def visible_popup(page) -> dict | None:
    """{width,height} of a VISIBLE hCaptcha challenge, else None — the is-a-captcha-up gate shared by
    the relay and the bot (backend.tools.icims_recon._has_captcha)."""
    el, box = await _challenge_el(page)
    return box if el else None


_HTML = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5,user-scalable=yes">
<title>Решить капчу</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;background:#0c1116;color:#e6edf3;font:15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;flex-direction:column;min-height:100vh}
  header{padding:10px 14px;background:#0c47c2;color:#fff;display:flex;align-items:center;gap:10px;
         position:sticky;top:0;z-index:5}
  header b{font-weight:700}
  #status{margin-left:auto;font-size:12px;opacity:.9}
  .wrap{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:8px;gap:10px}
  #cap{width:100%;max-width:900px;border:1px solid #223;border-radius:10px;background:#000;
       touch-action:manipulation;display:block}
  #waitBox{display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;
           color:#9fb0c0;padding:40px 20px;max-width:520px}
  #waitBox .big{font-size:44px}
  #waitBox .t{font-size:17px;color:#e6edf3;font-weight:600}
  .pulse{width:14px;height:14px;border-radius:50%;background:#3fb950;animation:p 1.2s infinite}
  @keyframes p{0%,100%{opacity:.3}50%{opacity:1}}
  .hint{font-size:13px;color:#9fb0c0;text-align:center;padding:0 14px 6px;max-width:640px}
</style></head><body>
<header><b>JF</b> <span id="label">Капча</span><span id="status">…</span></header>
<div class="wrap">
  <div id="waitBox">
    <div class="big">🟢</div>
    <div class="t">Капчи сейчас нет — жду</div>
    <div>Держи вкладку открытой. Как только на форме всплывёт капча, она появится здесь сама,
         и по ней можно будет тапать. Ничего нажимать не нужно.</div>
    <div class="pulse"></div>
  </div>
  <img id="cap" alt="captcha" hidden>
  <div class="hint" id="hint" hidden>Тапай прямо по картинкам. Реши капчу (нужные тайлы → Verify) —
     сервер продолжит сам. Можно зумить пальцами.</div>
</div>
<script>
const img=document.getElementById('cap'), st=document.getElementById('status'),
      lab=document.getElementById('label'), waitBox=document.getElementById('waitBox'),
      hint=document.getElementById('hint');
let hasCap=false, busy=false;
function refresh(){ if(hasCap) img.src='/frame?t='+Date.now(); }
async function poll(){
  try{
    const j=await (await fetch('/size',{cache:'no-store'})).json();
    lab.textContent=j.label||'Капча';
    if(j.captcha){
      st.textContent='капча '+j.w+'×'+j.h;
      if(!hasCap){ hasCap=true; img.hidden=false; hint.hidden=false; waitBox.style.display='none'; refresh(); }
    }else{
      st.textContent=j.w?'жду капчу…':'нет страницы';
      if(hasCap||waitBox.style.display==='none'){ hasCap=false; img.hidden=true; hint.hidden=true; waitBox.style.display='flex'; }
    }
  }catch(e){ st.textContent='офлайн'; }
}
img.addEventListener('load',()=>{ if(hasCap) setTimeout(refresh, 550); });
img.addEventListener('error',()=>{ if(hasCap) setTimeout(refresh, 1200); });
img.addEventListener('click', async (e)=>{
  if(busy||!hasCap) return; busy=true;
  const r=img.getBoundingClientRect();
  const x=(e.clientX-r.left)/r.width, y=(e.clientY-r.top)/r.height;
  st.textContent='тап…';
  try{ await fetch('/click',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({x,y})}); }catch(err){}
  busy=false; setTimeout(refresh, 220);
});
poll(); setInterval(poll, 1000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/size")
async def size():
    """captcha:true ONLY when a real, visible hCaptcha challenge is up (reliable frame=challenge +
    is_visible gate). We serve the FULL viewport (not a crop): the nested iCIMS iframe makes
    Playwright's challenge-iframe box paint ~49px off, so a crop mis-frames it — but a full-viewport
    shot + full-viewport tap mapping is exact, and the gate still guarantees the phone shows the page
    ONLY while a captcha is actually there (else the waiting state)."""
    if _PAGE is None:
        return JSONResponse({"w": 0, "h": 0, "label": "нет страницы", "captcha": False})
    async with _LOCK:
        el, _ = await _challenge_el(_PAGE)
    vp = await _viewport()
    if el:
        return JSONResponse({"w": vp["w"], "h": vp["h"], "label": _LABEL, "captcha": True})
    return JSONResponse({"w": vp["w"], "h": vp["h"], "label": _LABEL, "captcha": False})


@app.get("/dbg")
async def dbg():
    """DEBUG: per same-origin frame, the iframes it holds (title/src/rect) + what visible_popup picks."""
    if _PAGE is None:
        return JSONResponse({"err": "no page"})
    out = []
    try:
        for fr in _all_frames(_PAGE.main_frame):
            try:
                out.append(await fr.evaluate(_DBG_JS))
            except Exception as e:
                out.append({"xorigin": f"{type(e).__name__}"[:30], "url": (getattr(fr, "url", "") or "")[:55]})
    except Exception as e:
        out.append({"err": str(e)[:60]})
    box = await visible_popup(_PAGE)
    return JSONResponse({"picked": box, "frames": out})


@app.get("/full")
async def full():
    """DEBUG: uncropped viewport screenshot (to verify where the popup actually is vs the box)."""
    if _PAGE is None:
        return Response(_BLANK_PNG, media_type="image/png")
    try:
        async with _LOCK:
            png = await _PAGE.screenshot(timeout=8000)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
    except Exception:
        return Response(_BLANK_PNG, media_type="image/png")


@app.get("/frame")
async def frame():
    """Cropped screenshot of the LIVE captcha popup — or a 1x1 blank when no challenge is up, so the
    phone never shows the underlying page."""
    if _PAGE is None:
        return Response(_BLANK_PNG, media_type="image/png", headers={"Cache-Control": "no-store"})
    try:
        async with _LOCK:
            el, _ = await _challenge_el(_PAGE)
            if not el:
                return Response(_BLANK_PNG, media_type="image/png",
                                headers={"Cache-Control": "no-store"})
            png = await _PAGE.screenshot(timeout=8000)   # full viewport (only served while a captcha is up)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
    except Exception:
        return Response(_BLANK_PNG, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/click")
async def click(req: Request):
    """Forward a tap (normalised to the shown popup) as a real click — but ONLY while a challenge is
    up, so a stray tap can never click the underlying form."""
    if _PAGE is None:
        return JSONResponse({"ok": False, "err": "no page"})
    try:
        d = await req.json()
        x = max(0.0, min(1.0, float(d.get("x", 0))))
        y = max(0.0, min(1.0, float(d.get("y", 0))))
    except Exception:
        return JSONResponse({"ok": False, "err": "bad json"})
    try:
        async with _LOCK:
            el, _ = await _challenge_el(_PAGE)
            if not el:
                return JSONResponse({"ok": False, "err": "no captcha"})
            # full-viewport tap → page coordinate (exact; no nested-iframe offset in this path)
            vp = await _viewport()
            await _PAGE.mouse.click(x * vp["w"], y * vp["h"])
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "err": f"{type(e).__name__}: {e}"[:80]})


async def serve(port: int = 9003):
    """Start the relay on 127.0.0.1:port inside the CURRENT event loop; returns the server task.
    Signal handlers are DISABLED — an embedded uvicorn that installs them hijacks the automation
    process's SIGINT/SIGTERM and tears the whole run down. The task never propagates: it's wrapped
    so a bind failure or crash can't kill the caller's loop."""
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None      # embedded: do NOT touch process signals

    async def _guarded():
        try:
            await server.serve()
        except BaseException as e:      # bind failure / cancellation — never kill the automation
            print(f"[captcha relay server stopped: {type(e).__name__}: {e}]"[:140], flush=True)

    return asyncio.create_task(_guarded())
