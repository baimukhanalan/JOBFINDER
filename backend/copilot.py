"""Co-pilot daemon: a persistent HEADFUL Chromium on DISPLAY=:99 that the bot pre-fills
on command; the human watches it via noVNC on the phone, does any video/test, and clicks
Submit himself — a real browser + human click, so no re-typing and not spam-flagged.

Reuses the apply strategies (fill + LLM-drafted answers). Runs on 127.0.0.1:8096, exposed
only via nginx + basic-auth alongside the dashboard. The résumé PDF is the one the batch
already rendered (uploads/prefill/<profile>/<job>/resume.pdf).

    DISPLAY=:99 uvicorn backend.copilot:app --host 127.0.0.1 --port 8096
"""
import asyncio
import json
import logging
import os
import re
import time
from html import escape
from pathlib import Path

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

os.environ.setdefault("DISPLAY", ":99")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFILL_ROOT = PROJECT_ROOT / "uploads" / "prefill"

# One shared headful browser = one reviewer at a time. A second profile loading a job
# would clobber the first person's mid-review form, so /load is owner-gated.
BUSY_TTL = 15 * 60  # seconds before an abandoned session stops blocking others

NOVNC_ADDR = ("127.0.0.1", 6080)  # websockify port the noVNC iframe talks to

# COPIED from extension/content.js CONFIRM_RE — the two MUST stay in sync.
CONFIRM_RE = re.compile(
    r"(thank you for applying|application (has been )?submitted|successfully submitted"
    r"|we have received your application|application received)", re.I)
_URL_RE = re.compile(r"confirm|thank", re.I)

WATCH_INTERVAL = 2.0        # seconds between confirmation polls
WATCH_MAX = 10 * 60         # give up after 10 minutes

app = FastAPI(title="JobFinder co-pilot")
_S: dict = {"pw": None, "browser": None, "page": None, "lock": asyncio.Lock(),
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


async def _ensure_browser():
    """Launch (or relaunch) the persistent headful browser on the virtual display."""
    if _S["page"] is not None and not _S["page"].is_closed():
        return _S["page"]
    try:
        if _S["pw"] is None:
            _S["pw"] = await async_playwright().start()
        # Pass DISPLAY explicitly — playwright doesn't reliably forward it, and without it
        # headful Chromium renders to no display (invisible to noVNC).
        _S["browser"] = await _S["pw"].chromium.launch(
            headless=False, args=["--start-maximized", "--no-sandbox", "--disable-dev-shm-usage"],
            env={**os.environ, "DISPLAY": ":99"})
        ctx = await _S["browser"].new_context(no_viewport=True)
        _S["page"] = await ctx.new_page()
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


async def _watch_submit(page, profile: str, jid: str) -> None:
    """Submit DETECTION only — never submission. After a prefill, poll the live
    page for the confirmation text/URL that appears once the HUMAN clicks Submit,
    and auto-mark the job. Read-only: never clicks, types, or navigates."""
    deadline = time.time() + WATCH_MAX
    while time.time() < deadline:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            if page.is_closed():
                return
            text = await page.inner_text("body", timeout=5000)
            if looks_submitted(text, page.url):
                status_store.mark(profile, jid, "submitted")
                logger.info("submit detected for %s/%s — marked submitted", profile, jid)
                return
        except Exception:
            continue  # transient (mid-navigation, detached body) — keep polling


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


@app.post("/load")
async def load(jobid: str = Form(...), profile: str = Form("michael")):
    profile = _safe_id(profile) or "michael"
    jobid = _safe_id(jobid)
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
        prof = get_profile(profile)
        niche, variant = variant_for({"title": title, "description": ""}, prof)
        form = prof.to_form_dict()
        if variant and variant.get("years_experience"):
            form["years_experience"] = variant["years_experience"]
        base = variant or prof.resume
        tailored = tailor_resume(base, title, company, "", use_ai=False)
        resume_pdf = str(d / "resume.pdf")
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
                                         facts=load_facts(profile),
                                         profile_id=profile, niche=niche or "")
            _S["current"] = jobid
            _S["owner"] = profile
            _S["loaded_at"] = time.time()
            # Watch (read-only) for the human's Submit so the queue auto-advances.
            _S["watch"] = asyncio.create_task(_watch_submit(page, profile, jobid))
            return JSONResponse({"loaded": jobid, "company": company, "title": title,
                                 "filled": result.get("filled"),
                                 "unfilled": len(result.get("unfilled") or []),
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
    s.textContent='✓ Filled '+(j.company||'')+' — review in the screen above and tap Submit. (filled '+j.filled+', left '+j.unfilled+')';
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
        f"<div class='bar'><b>Co-pilot</b> — tap <b>Fill →</b>, then review the form above &amp; tap Submit yourself.</div>"
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
