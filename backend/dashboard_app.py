"""Mobile-first apply dashboard — review the pre-filled queue and submit by hand.

Self-contained: reads the per-job report.json files the batch already writes under
uploads/prefill/<profile>/, overlays a small status.json the user updates by tapping
"mark submitted". No DB, no auth (runs on 127.0.0.1, exposed only via nginx +
basic-auth per the project's security rules).

    uvicorn backend.dashboard_app:app --host 127.0.0.1 --port 8089
"""
import json
import os
import re
import threading
import time
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)

from backend.applier.profile_validator import validate_profile
from backend.profiles import facts as facts_lib
from backend.profiles import store as profile_store
from backend.profiles.store import _source_path, load_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFILL_ROOT = PROJECT_ROOT / "uploads" / "prefill"
ETALONS_DIR = PROJECT_ROOT / "backend" / "data" / "etalons"

_TOKEN_FILE = Path(__file__).resolve().parent / ".assist_token"
ASSIST_TOKEN = _TOKEN_FILE.read_text().strip() if _TOKEN_FILE.exists() else ""
INBOX_DIR = PROJECT_ROOT / "uploads" / "inbox"

# Inbox category → (emoji, css class) for the tracker feed.
_CAT_META = {
    "interview": ("📞", "intv"),
    "assessment": ("📝", "asmt"),
    "rejection": ("✕", "rej"),
    "ack": ("•", "ack"),
    "other": ("✉", "oth"),
}
_CAT_ORDER = ["interview", "assessment", "ack", "other", "rejection"]


def _load_inbox(profile: str) -> dict:
    """Per-profile inbox index (written by inbox_index.py). Missing file -> empty."""
    try:
        return json.loads((INBOX_DIR / f"{_safe_id(profile)}.json").read_text())
    except Exception:
        return {"total": 0, "counts": {}, "messages": []}


def _inbox_html(profile: str) -> str:
    data = _load_inbox(profile)
    msgs = data.get("messages", [])
    if not msgs:
        return ("<details class='inbox'><summary>📬 Inbox (0)</summary>"
                "<div class='empty'>No mail yet.</div></details>")
    counts = data.get("counts", {})
    # Priority first: interviews/assessments on top regardless of date.
    rank = {c: i for i, c in enumerate(_CAT_ORDER)}
    msgs = sorted(msgs, key=lambda m: rank.get(m.get("category", "other"), 99))
    chips = " ".join(
        f"<span class=' chip {_CAT_META.get(c, _CAT_META['other'])[1]}'>"
        f"{_CAT_META.get(c, _CAT_META['other'])[0]} {counts[c]}</span>"
        for c in _CAT_ORDER if counts.get(c))
    rows = []
    for m in msgs:
        cat = m.get("category", "other")
        emoji, cls = _CAT_META.get(cat, _CAT_META["other"])
        unread = " unread" if m.get("unread") else ""
        rows.append(
            f"<div class='mail {cls}{unread}'>"
            f"<div class='mhead'><span class='mcat'>{emoji}</span>"
            f"<span class='msubj'>{escape(m.get('subject',''))}</span></div>"
            f"<div class='mfrom'>{escape(m.get('from',''))} · {escape(m.get('date','')[:16])}</div>"
            f"<div class='mprev'>{escape(m.get('preview',''))}</div>"
            "</div>")
    open_attr = " open" if (counts.get("interview") or counts.get("assessment")) else ""
    return (f"<details class='inbox'{open_attr}><summary>📬 Inbox ({data.get('total',0)}) {chips}</summary>"
            f"<div class='maillist'>{''.join(rows)}</div></details>")

app = FastAPI(title="JobFinder apply dashboard")
# The /draft, /assist and /profile_form calls are made by the browser extension
# from arbitrary job-site origins.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"], max_age=86400,
)


# profiles.json mtime cache: /draft is hit per page load from the extension, the
# file changes rarely — re-parse only when its mtime moves.
_PROFILES_CACHE: dict = {"mtime": None, "profiles": {}}


def _profiles() -> dict:
    """{profile_id: Profile}, cached on the source file's mtime. {} on any error."""
    try:
        src = _source_path(None)
        mtime = src.stat().st_mtime
    except OSError:
        return {}
    if _PROFILES_CACHE["mtime"] != mtime:
        try:
            _PROFILES_CACHE["profiles"] = load_profiles()
            _PROFILES_CACHE["mtime"] = mtime
        except Exception:
            return {}
    return _PROFILES_CACHE["profiles"]


def _profile_form(profile_id: str) -> tuple[dict, str]:
    """The profile's OWN identity facts + résumé summary for grounding drafted
    answers. Unknown profile -> ({}, '') — never another person's identity."""
    p = _profiles().get(profile_id)
    if p is None:
        return {}, ""
    facts = {k: v for k, v in p.to_form_dict().items() if isinstance(v, (str, int)) and v}
    return facts, (p.resume or {}).get("summary", "")


def _safe_id(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", (s or "").lower())


def _split_review(answers: dict) -> tuple[dict, dict]:
    """Convert the '[review]' wire prefix to metadata: returns (clean answers,
    {question: True} review map). Raw wire strings never leave the server."""
    from backend.applier.strategies.base import strip_review
    clean, review = {}, {}
    for q, a in answers.items():
        text, flagged = strip_review(a)
        clean[q] = text
        if flagged:
            review[q] = True
    return clean, review


def _status_path(profile: str) -> Path:
    return PREFILL_ROOT / profile / "status.json"


def _load_status(profile: str) -> dict:
    p = _status_path(profile)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


_STATUS_LOCK = threading.Lock()


def _save_status(profile: str, data: dict) -> None:
    """Atomic write (tmp + replace) so a concurrent reader never sees a torn file."""
    p = _status_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _apply_mark(profile: str, jid: str, to: str) -> None:
    """Locked status.json update shared by /mark (dashboard) and /mark_ext
    (extension). Empty `to` removes the entry (undo).

    Delegates to status_store.mark which is the single source of truth for
    atomic status writes (mirrors the same lock+tmp+replace pattern)."""
    from backend import status_store
    status_store.mark(profile, jid, to)


def _badge(job: dict, status: str) -> tuple[str, str]:
    """(label, css-class) for the job's current stage."""
    if status == "submitted":
        return "SUBMITTED", "sub"
    if status == "interview":
        return "INTERVIEW", "intv"
    if status == "rejected":
        return "REJECTED", "rej"
    unfilled = job.get("unfilled") or []
    blob = " ".join(str(u) for u in unfilled).lower()
    if any(k in blob for k in ("loom", "video", "record a")):
        return "NEEDS VIDEO", "warn"
    if any(k in blob for k in ("python", "coding", "assessment", "skills test", "take-home", "write a")):
        return "NEEDS TEST", "warn"
    if unfilled:
        return f"NEEDS INFO ({len(unfilled)})", "warn"
    if job.get("review_items"):
        return f"NEEDS REVIEW ({len(job['review_items'])})", "warn"
    return "READY TO SUBMIT", "ready"


def _current_urls(profile: str) -> set | None:
    """apply_urls of the LATEST batch (review_queue.json). None = no filter (show all).

    Keeps the dashboard to the current, still-open openings instead of every job ever
    pre-filled — stale postings from past runs 404 ('couldn't find anything here')."""
    f = PREFILL_ROOT / profile / "review_queue.json"
    if not f.exists():
        return None
    try:
        items = json.loads(f.read_text(encoding="utf-8"))
        urls = {it.get("apply_url", "") for it in items if it.get("apply_url")}
        return urls or None
    except Exception:
        return None


def _load_jobs(profile: str) -> list[dict]:
    base = PREFILL_ROOT / profile
    status = _load_status(profile)
    current = _current_urls(profile)
    jobs = []
    seen_urls = set()
    for rep in sorted(base.glob("*/report.json")):
        try:
            j = json.loads(rep.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("gated_out"):
            continue  # match gate rejected it — never pre-filled, keep it off the queue
        url = j.get("apply_url", "")
        if current is not None and url not in current:
            continue  # not in the latest batch -> stale, skip
        if url in seen_urls:
            continue  # de-dup
        seen_urls.add(url)
        jid = rep.parent.name
        j["_id"] = jid
        st = status.get(jid, {}).get("status", "")
        j["_status"] = st
        j["_badge"], j["_cls"] = _badge(j, st)
        jobs.append(j)
    # ready first, then needs-info, submitted last
    order = {"ready": 0, "warn": 1, "intv": 0, "rej": 3, "sub": 4}
    jobs.sort(key=lambda j: order.get(j["_cls"], 2))
    return jobs


# Reports whose page never showed a fillable form — nothing a reviewer can act on.
_NO_FORM_PAGE_TYPES = {"unknown", "job_listing", "expired"}
_TERMINAL_CLS = ("sub", "intv", "rej")


def _tab_of(job: dict) -> str:
    """Which home tab a card belongs to: 'ready' (form pre-filled cleanly, submit
    is the only step left), 'info' (needs a human touch first), 'skip' (no form on
    the page — not reviewable, stats-bar count only)."""
    if job.get("page_type") in _NO_FORM_PAGE_TYPES:
        return "skip"
    if job["_cls"] in _TERMINAL_CLS:
        return "info"  # history (submitted/interview/rejected) lives in tab 2
    if (job.get("page_type") == "application_form"
            and (job.get("filled") or 0) > 0 and not job.get("failed")
            and not job.get("unfilled")):
        return "ready"  # unfilled questions mean a human touch first — tab 2
    return "info"


def _last_review_days(profile: str) -> float | None:
    """Days since the reviewer last marked anything (status.json mtime);
    None = no review action ever (file absent, unreadable or empty)."""
    p = _status_path(profile)
    if not _load_status(profile):
        return None
    try:
        return max(0.0, (time.time() - p.stat().st_mtime) / 86400.0)
    except OSError:
        return None


_CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#0f1216;color:#e7ebf0;overflow-x:hidden}
.wrap{max-width:680px;margin:0 auto;padding:14px}
h1{font-size:18px;margin:6px 0 12px}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.stat{flex:1;min-width:70px;background:#171c23;border:1px solid #232a33;border-radius:10px;padding:10px;text-align:center}
.stat b{display:block;font-size:20px}.stat span{font-size:11px;color:#8a94a3}
.stat.alert{background:#3a1010;border-color:#5e1c1c}.stat.alert b{color:#e05a5a}
.tabs{display:flex;gap:8px;margin:0 0 12px}
.tab{flex:1;background:#171c23;border:1px solid #232a33;color:#8a94a3;font-size:13px;font-weight:600;
 padding:10px;border-radius:9px;cursor:pointer}
.tab.active{background:#2563eb;border-color:#2563eb;color:#fff}
.blockbanner{background:#3a1010;border:1px solid #5e1c1c;border-radius:12px;padding:12px 14px;
 margin:0 0 12px;font-size:13px;color:#e08585}
.blockbanner b{color:#e05a5a}.blockbanner ul{margin:8px 0;padding-left:20px}.blockbanner li{margin:3px 0}
.blockbanner a{color:#49a8e0;font-weight:600;text-decoration:none}
.card{background:#171c23;border:1px solid #232a33;border-radius:12px;padding:13px;margin:10px 0}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.co{font-size:15px;font-weight:600}.ti{font-size:13px;color:#aab3c0;margin-top:2px}
.meta{font-size:11px;color:#7b8593;margin-top:4px}
.badge{font-size:10px;font-weight:700;padding:4px 8px;border-radius:20px;white-space:nowrap}
.ready{background:#10391f;color:#46d17f;border:1px solid #1c5e35}
.warn{background:#3a2f10;color:#e0b341;border:1px solid #5e4d1c}
.sub{background:#10293a;color:#49a8e0;border:1px solid #1c4a5e}
.intv{background:#2a103a;color:#c061e0;border:1px solid #4a1c5e}
.rej{background:#3a1010;color:#e05a5a;border:1px solid #5e1c1c}
.row{display:flex;gap:8px;margin-top:11px;flex-wrap:wrap}
a.btn,button.btn{flex:1;text-align:center;text-decoration:none;font-size:13px;font-weight:600;
 padding:11px;border-radius:9px;border:0;cursor:pointer;min-width:90px}
.open{background:#2563eb;color:#fff}.res{background:#232a33;color:#cfd6df}
.copilot{background:#2a103a;color:#c061e0;border:1px solid #4a1c5e}
.done{background:#10391f;color:#46d17f;border:1px solid #1c5e35}
.undo{background:#232a33;color:#8a94a3}
details{margin-top:10px}summary{font-size:12px;color:#8a94a3;cursor:pointer}
.ans{background:#0f1216;border:1px solid #232a33;border-radius:8px;padding:9px;margin-top:7px;font-size:12px}
.ans .q{color:#7b8593}.ans .a{color:#dbe2ea;margin:3px 0 9px;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
img.shot{width:100%;border-radius:8px;border:1px solid #232a33;margin-top:9px}
.empty{color:#7b8593;text-align:center;padding:40px}
.extbar{display:block;text-align:center;background:#10391f;color:#46d17f;border:1px solid #1c5e35;
 border-radius:10px;padding:11px;margin:0 0 12px;text-decoration:none;font-size:13px;font-weight:600}
.inbox{background:#171c23;border:1px solid #232a33;border-radius:12px;padding:11px 13px;margin:0 0 14px}
.inbox>summary{font-size:14px;color:#e7ebf0;font-weight:600}
.chip{font-size:11px;font-weight:700;padding:2px 7px;border-radius:20px;margin-left:5px}
.chip.intv{background:#2a103a;color:#c061e0}.chip.asmt{background:#3a2f10;color:#e0b341}
.chip.rej{background:#3a1010;color:#e05a5a}.chip.ack{background:#10293a;color:#49a8e0}
.chip.oth{background:#232a33;color:#8a94a3}
.maillist{margin-top:10px}
.mail{background:#0f1216;border:1px solid #232a33;border-left:3px solid #2b3340;border-radius:8px;padding:9px 11px;margin:7px 0}
.mail.intv{border-left-color:#c061e0}.mail.asmt{border-left-color:#e0b341}
.mail.rej{border-left-color:#e05a5a;opacity:.7}.mail.ack{border-left-color:#49a8e0}
.mail.unread{box-shadow:inset 2px 0 0 #fff2}
.mhead{display:flex;gap:7px;align-items:baseline}
.mcat{font-size:13px}.msubj{font-size:13px;font-weight:600;color:#e7ebf0}
.mfrom{font-size:11px;color:#7b8593;margin-top:3px}
.mprev{font-size:12px;color:#aab3c0;margin-top:5px;line-height:1.4}
"""


_JS = """
function showTab(t){
  ['ready','info'].forEach(function(k){
    document.getElementById('pane-'+k).style.display = (k===t)?'':'none';
    document.getElementById('tab-'+k).classList.toggle('active', k===t);
  });
}
async function copilotLoad(jid,btn){
  btn.disabled=true; btn.textContent='Loading\\u2026';
  // open the co-pilot screen right away (a popup after the slow fill round-trip
  // would be blocked); the fill result lands on the button when it returns
  window.open('/copilot/?profile='+encodeURIComponent(PROFILE),'_blank');
  try{
    const r=await fetch('/copilot/load',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'jobid='+encodeURIComponent(jid)+'&profile='+encodeURIComponent(PROFILE)});
    const j=await r.json();
    btn.textContent = j.error ? ('\\u2717 '+String(j.error).slice(0,60)) : '\\u2713 sent to co-pilot';
  }catch(e){btn.textContent='\\u2717 connection error';}
  btn.disabled=false;
}
(function(){ // deep links (#job-<jid>) may target a card in the hidden tab
  const h=location.hash;
  if(!h||h.indexOf('#job-')!==0)return;
  const el=document.getElementById(h.slice(1));
  if(!el)return;
  const info=document.getElementById('pane-info');
  if(info&&info.contains(el))showTab('info');
  el.scrollIntoView();
})();
"""


def _card_html(j: dict, profile: str, blocked: bool) -> str:
    """One job card. `blocked` (profile failed the reality gate) removes every
    apply affordance — open/1-click/co-pilot — but keeps viewing and marks."""
    rev_items = j.get("review_items") or []
    rev_html = ""
    if rev_items:
        rev_blocks = "".join(
            f"<div class='q'>{escape(str(r.get('question',''))[:160])} "
            f"<span class='badge warn'>{escape(str(r.get('kind') or 'review'))}</span></div>"
            f"<div class='a'>{escape(str(r.get('answer','')))}</div>"
            for r in rev_items)
        rev_html = (f"<details open><summary>Needs review ({len(rev_items)})</summary>"
                    f"<div class='ans'>{rev_blocks}</div></details>")
    ans = j.get("drafted_answers") or {}
    ans_html = ""
    if ans:
        # the '[review]' wire prefix must not be copy-pasted into a live form —
        # show stripped text + a visible badge instead
        clean, review = _split_review(ans)
        blocks = "".join(
            f"<div class='q'>{escape(q[:160])}"
            + (" <span class='badge warn'>review</span>" if review.get(q) else "")
            + f"</div><div class='a'>{escape(str(a))}</div>"
            for q, a in clean.items())
        ans_html = (f"<details><summary>Answers to paste ({len(ans)})</summary>"
                    f"<div class='ans'>{blocks}</div></details>")
    submitted = j["_status"] == "submitted"
    mark = (f"<form method='post' action='/mark/{escape(j['_id'])}?profile={escape(profile)}' style='flex:1'>"
            f"<input type='hidden' name='to' value='{'' if submitted else 'submitted'}'>"
            f"<button class='btn {'undo' if submitted else 'done'}'>"
            f"{'↩ undo' if submitted else '✓ mark submitted'}</button></form>")
    resume_btn = (f"<a class='btn res' href='/resume/{escape(j['_id'])}?profile={escape(profile)}'"
                  " target='_blank'>Résumé PDF</a>")
    if blocked:
        apply_row = resume_btn  # viewing only — nothing that leads to a submit
    else:
        apply_row = (
            f"<a class='btn open' href='{escape(j.get('apply_url',''))}#aa={escape(profile)}:{escape(j['_id'])}'"
            " target='_blank' rel='noopener'>Apply (1-click) →</a>"
            f"<a class='btn res' href='{escape(j.get('apply_url',''))}' target='_blank' rel='noopener'>Open form →</a>"
            f"{resume_btn}"
            f"<button class='btn copilot' onclick=\"copilotLoad('{escape(j['_id'])}',this)\">Open in co-pilot</button>")
    return (
        f"<div class='card' id='job-{escape(j['_id'])}'>"
        f"<div class='top'><div><div class='co'>{escape(j.get('company',''))}</div>"
        f"<div class='ti'>{escape(j.get('job_title',''))}</div>"
        f"<div class='meta'>résumé: {escape(str(j.get('resume_niche') or '—'))} · match {j.get('match_score','?')}</div></div>"
        f"<span class='badge {j['_cls']}'>{escape(j['_badge'])}</span></div>"
        f"<div class='row'>{apply_row}</div>"
        f"<div class='row'>{mark}</div>"
        f"{rev_html}"
        f"{ans_html}"
        f"<details><summary>Pre-fill screenshot</summary>"
        f"<img class='shot' loading='lazy' src='/shot/{escape(j['_id'])}?profile={escape(profile)}'></details>"
        "</div>")


def _blocked_banner(profile: str, problems: list[str]) -> str:
    items = "".join(f"<li>{escape(p)}</li>" for p in problems)
    return ("<div class='blockbanner'><b>⚠ Applications are paused for this profile</b> — "
            "recruiters could not reach the person as it is set up now:"
            f"<ul>{items}</ul>"
            f"<a href='/setup?profile={escape(profile)}'>Fix in Setup →</a></div>")


def _render(profile: str) -> str:
    jobs = _load_jobs(profile)
    prof = _profiles().get(profile)
    problems = (validate_profile(prof.to_form_dict()) if prof is not None
                else ["profile is not set up — no identity on file"])
    blocked = bool(problems)

    tabs: dict[str, list] = {"ready": [], "info": [], "skip": []}
    for j in jobs:
        tabs[_tab_of(j)].append(j)
    n_skip = len(tabs["skip"])
    n_ready = len(tabs["ready"])
    n_sub = sum(1 for j in jobs if j["_status"] == "submitted")
    n_intv = sum(1 for j in jobs if j["_status"] == "interview")
    n_rej = sum(1 for j in jobs if j["_status"] == "rejected")

    age = _last_review_days(profile)
    rev_alert = " alert" if age is not None and age > 2 else ""
    rev_txt = "never" if age is None else ("today" if age < 1 else f"{int(age)}d ago")

    ready_cards = "".join(_card_html(j, profile, blocked) for j in tabs["ready"])
    info_cards = "".join(_card_html(j, profile, blocked) for j in tabs["info"])
    if not (ready_cards or info_cards or n_skip):
        body = "<div class='empty'>No pre-filled jobs yet. Run a batch first.</div>"
    else:
        body = (
            "<div class='tabs'>"
            f"<button class='tab active' id='tab-ready' onclick=\"showTab('ready')\">Ready ({n_ready})</button>"
            f"<button class='tab' id='tab-info' onclick=\"showTab('info')\">Needs info ({len(tabs['info'])})</button>"
            "</div>"
            f"<div id='pane-ready'>{ready_cards or '<div class=empty>Nothing ready to submit yet.</div>'}</div>"
            f"<div id='pane-info' style='display:none'>"
            f"{info_cards or '<div class=empty>Nothing needs extra info.</div>'}</div>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Apply — {escape(profile)}</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>Apply queue — {escape(profile)}</h1>"
        + (_blocked_banner(profile, problems) if blocked else "") +
        "<div class='stats'>"
        f"<div class='stat'><b>{len(jobs) - n_skip}</b><span>in queue</span></div>"
        f"<div class='stat'><b>{n_ready}</b><span>ready</span></div>"
        f"<div class='stat'><b>{n_sub}</b><span>submitted</span></div>"
        f"<div class='stat'><b>{n_intv}</b><span>interview</span></div>"
        f"<div class='stat'><b>{n_rej}</b><span>rejected</span></div>"
        f"<div class='stat'><b>{n_skip}</b><span>skipped: no form</span></div>"
        f"<div class='stat{rev_alert}'><b>{rev_txt}</b><span>last review</span></div>"
        "</div>"
        "<a class='extbar' href='/extension'>⬇ Install the Apply Assist extension (it fills forms in your Chrome)</a>"
        f"{_inbox_html(profile)}"
        f"{body}"
        f"<script>const PROFILE={json.dumps(profile)};{_JS}</script>"
        "</div></body></html>")


@app.get("/")
def home():
    # Candidate-first: land on the candidate list (+ funnel), not the merged inbox.
    return RedirectResponse("/mail/candidates")


@app.get("/queue", response_class=HTMLResponse)
def queue(profile: str = "michael"):
    return _render(_safe_id(profile) or "michael")


@app.get("/apply", response_class=HTMLResponse)
def apply_index():
    """Candidate-first index of pre-filled applications ready to submit — a jump-off
    to each candidate's review queue (where the co-pilot / submit lives)."""
    from html import escape as esc

    from backend.tools import mailcrm, mailcrm_ui
    names = {c["id"]: c["name"] for c in mailcrm.candidates()}
    rows = []
    total_ready = total_apps = 0
    if PREFILL_ROOT.exists():
        for pdir in sorted(PREFILL_ROOT.iterdir()):
            if not pdir.is_dir():
                continue
            profile = pdir.name
            jobs = _load_jobs(profile)
            apps = [j for j in jobs if j.get("page_type") == "application_form"
                    and j.get("_status") != "submitted"]
            if not apps:
                continue
            ready = sum(1 for j in apps if _tab_of(j) == "ready")
            total_apps += len(apps)
            total_ready += ready
            rows.append((profile, names.get(profile, profile), ready, len(apps)))
    rows.sort(key=lambda r: (-r[2], -r[3]))
    cards = "".join(
        f'<a class="approw" href="/queue?profile={esc(p)}">'
        f'<span class="apn">{esc(nm)}</span>'
        f'<span class="apc">{"✅ " + str(r) + " готовы · " if r else ""}{t} заявок →</span></a>'
        for p, nm, r, t in rows
    ) or '<div class="empty">Пока нет пре-заполненных заявок. Запусти прогон apply.</div>'
    css = ("<style>.apphead{font-size:19px;font-weight:700;margin:0 0 14px;color:var(--ink)}"
           ".apphead b{color:var(--accent)}.applist{display:flex;flex-direction:column;gap:9px}"
           ".approw{display:flex;justify-content:space-between;align-items:center;gap:12px;"
           "background:var(--panel);border:1px solid var(--line);border-radius:var(--r);"
           "padding:14px 16px;text-decoration:none;color:var(--ink);min-height:54px}"
           ".approw:hover{border-color:var(--accent);text-decoration:none}.apn{font-weight:600}"
           ".apc{font-size:13px;color:var(--ink-mute);white-space:nowrap}"
           ".empty{color:var(--ink-mute);text-align:center;padding:40px}</style>")
    body = (css + f'<div class="apphead">Заявки к отправке — <b>{total_ready}</b> готовы из {total_apps}</div>'
            f'<div class="applist">{cards}</div>')
    return HTMLResponse(mailcrm_ui._page("apply", body))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/roles", response_class=HTMLResponse)
def roles_page(company: str = ""):
    """Live roles dashboard. No `company` -> index of every tracked company; a
    `company` key -> that company's Salmon-style table (job list from Ashby, counts
    polled from our tracker)."""
    from backend.tools import roles_dashboard as rd
    try:
        if not company:
            return HTMLResponse(rd.render_index_html(rd.supported_targets(),
                                                     rd.counts_by_company()))
        return HTMLResponse(rd.render_live_html(rd.fetch_jobs(company), company))
    except Exception as exc:
        return HTMLResponse("<!doctype html><meta name='viewport' content='width=device-width, initial-scale=1'>"
                            f"<p style='font-family:sans-serif;padding:16px'>Дашборд компаний недоступен: {escape(str(exc))}</p>",
                            status_code=502)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(company: str = "", q: str = "", allpos: int = 0):
    """Unified live "all jobs" feed across every tracked ATS board — mobile-first
    card list with company chips + search. Remote-only by default (allpos=1 shows
    every workplace type). Status badges read from our tracker."""
    from backend.tools import jobs_feed
    try:
        return HTMLResponse(jobs_feed.render_page(company=company, q=q, remote_only=(allpos == 0)))
    except Exception as exc:
        return HTMLResponse("<!doctype html><meta name='viewport' content='width=device-width, initial-scale=1'>"
                            f"<p style='font-family:sans-serif;padding:16px'>Лента вакансий недоступна: {escape(str(exc))}</p>",
                            status_code=502)


@app.get("/jobs/more", response_class=HTMLResponse)
def jobs_more(company: str = "", q: str = "", offset: int = 0, allpos: int = 0):
    """Pagination fragment for the /jobs infinite scroll."""
    from backend.tools import jobs_feed
    try:
        return HTMLResponse(jobs_feed.render_more(company=company, q=q, offset=offset, remote_only=(allpos == 0)))
    except Exception:
        return HTMLResponse("", status_code=200)


@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(company: str = "", q: str = ""):
    """Persisted remote-job catalog (Postgres) with descriptions + application-form
    questions, collected across every known ATS board."""
    from backend.tools import catalog_ui
    try:
        return HTMLResponse(catalog_ui.render_page(company=company, q=q))
    except Exception as exc:
        return HTMLResponse("<!doctype html><meta name='viewport' content='width=device-width, initial-scale=1'>"
                            f"<p style='font-family:sans-serif;padding:16px'>Каталог недоступен: {escape(str(exc))}</p>",
                            status_code=502)


@app.get("/catalog/more", response_class=HTMLResponse)
def catalog_more(company: str = "", q: str = "", offset: int = 0):
    """Pagination fragment for the /catalog infinite scroll."""
    from backend.tools import catalog_ui
    try:
        return HTMLResponse(catalog_ui.render_more(company=company, q=q, offset=offset))
    except Exception:
        return HTMLResponse("", status_code=200)


@app.get("/roles/counts")
def roles_counts(company: str = ""):
    from backend.tools import roles_dashboard as rd
    return JSONResponse(rd.counts_by_url(company))


@app.get("/roles/summary")
def roles_summary():
    """Network-free per-company counts for the index cards."""
    from backend.tools import roles_dashboard as rd
    return JSONResponse(rd.counts_by_company())


@app.get("/roles/events")
async def roles_events(company: str = ""):
    """Server-Sent Events: push the counts the instant they change (checked every
    ~1s server-side), so the dashboard reflects the bot's work near-instantly instead
    of waiting for a poll. Scoped to one `company` when given."""
    import asyncio

    from backend.tools import roles_dashboard as rd

    async def stream():
        last = None
        while True:
            try:
                payload = json.dumps(rd.counts_by_url(company))
            except Exception:
                payload = None
            if payload and payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            else:
                yield ": ping\n\n"  # heartbeat keeps the connection open
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/roles/reset")
def roles_reset(company: str = ""):
    from backend.tools import roles_dashboard as rd
    return JSONResponse({"ok": True, "company": company, "epoch": rd.reset(company)})


# ---- Candidate mailboxes (Mailgun inbound, or the local Mailpit sink) ------
_mail_poller_started = False


def _start_mail_poller():
    """Background thread: merge the mail provider -> durable store on a loop so the
    inbox survives restarts and nothing is lost. Soft no-op if the provider is down."""
    global _mail_poller_started
    if _mail_poller_started:
        return
    _mail_poller_started = True

    def loop():
        from backend.tools import mail_sink
        # Mailpit is local and free to hammer; Mailgun's events API is a remote
        # rate-limited endpoint, so poll it far less often.
        every = 20.0 if mail_sink.PROVIDER == "mailgun" else 4.0
        while True:
            try:
                mail_sink.poll_once(int(time.time()))
            except Exception:
                pass
            time.sleep(every)

    threading.Thread(target=loop, daemon=True).start()


# --- Self-hosted candidate mail CRM (Gmail-style): reads the Dovecot Maildir on
#     our own server, sends via our own Postfix. No Mailgun, no third party.
@app.get("/mail", response_class=HTMLResponse)
def mail_page(q: str = "", mailbox: str = ""):
    from backend.tools import mail_health, mailcrm, mailcrm_ui
    rows = mailcrm.list_messages(mailbox=mailbox or None, q=q, limit=50)
    counts = mailcrm.counts()
    name = ""
    if mailbox:
        name = next((c["name"] for c in mailcrm.candidates() if c["email"] == mailbox.lower()), "")
    try:
        warning = mail_health.dashboard_warning() or ""
    except Exception:
        warning = ""
    return HTMLResponse(mailcrm_ui.render_inbox(rows, counts, q=q, mailbox=mailbox,
                                                mailbox_name=name, warning=warning))


@app.get("/mail/more", response_class=HTMLResponse)
def mail_more(ts: int = 0, id: str = "", q: str = "", mailbox: str = ""):
    from backend.tools import mailcrm, mailcrm_ui
    rows = mailcrm.list_messages(mailbox=mailbox or None, q=q, limit=50,
                                 before_ts=ts or None, before_id=id or None)
    return HTMLResponse(mailcrm_ui.render_rows(rows))


def _submitted_mailboxes() -> set:
    """Candidate emails with at least one application marked 'submitted' (status.json).
    Empty until the apply engine runs; wired so the funnel lights up when it does."""
    from backend import status_store
    from backend.tools import mailcrm
    out = set()
    for c in mailcrm.candidates():
        try:
            st = status_store.load(c["id"]) or {}
        except Exception:
            continue
        for entry in st.values():
            if isinstance(entry, dict) and entry.get("status") == "submitted":
                out.add(c["email"])
                break
    return out


@app.get("/mail/candidates", response_class=HTMLResponse)
def mail_candidates(filter: str = ""):
    from backend.tools import mail_db, mailcrm, mailcrm_ui
    all_cands = mailcrm.candidates()
    try:  # one query instead of scanning 261 Maildirs
        unread = mail_db.unread_by_mailbox()
    except Exception:
        unread = {}
    for c in all_cands:
        c["unread"] = unread.get(c["email"], 0)
    # funnel counts (distinct candidates per bucket)
    try:
        kc = mail_db.kind_counts()
    except Exception:
        kc = {}
    submitted = _submitted_mailboxes()
    counts = {"submitted": len(submitted), "ack": kc.get("ack", 0),
              "interview": kc.get("interview", 0), "offer": kc.get("offer", 0),
              "rejection": kc.get("rejection", 0)}
    # apply the clicked filter -> only candidates in that bucket
    f = (filter or "").lower()
    cands = all_cands
    if f == "submitted":
        cands = [c for c in all_cands if c["email"] in submitted]
    elif f in ("ack", "interview", "offer", "rejection"):
        try:
            keep = mail_db.mailboxes_with_kind(f)
        except Exception:
            keep = set()
        cands = [c for c in all_cands if c["email"] in keep]
    cands.sort(key=lambda c: (-c.get("unread", 0), c["name"]))
    return HTMLResponse(mailcrm_ui.render_candidates(cands, counts=counts,
                                                     active_filter=f, total=len(all_cands)))


@app.get("/mail/message", response_class=HTMLResponse)
def mail_message(id: str = ""):
    from backend.tools import mailcrm, mailcrm_ui
    t = mailcrm.get_thread(id, mark=True)
    if not t:
        return HTMLResponse("<!doctype html><meta name='viewport' content='width=device-width, initial-scale=1'>"
                            "<p style='font-family:sans-serif;padding:16px'>\u041f\u0438\u0441\u044c\u043c\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e. <a href='/mail'>\u041a \u0441\u043f\u0438\u0441\u043a\u0443</a></p>", status_code=404)
    return HTMLResponse(mailcrm_ui.render_thread(t))


@app.get("/mail/attachment")
def mail_attachment(id: str = "", i: int = 0):
    from urllib.parse import quote
    from backend.tools import mailcrm
    res = mailcrm.get_attachment(id, i)
    if not res:
        return JSONResponse({"error": "not found"}, status_code=404)
    filename, ctype, data = res
    from fastapi.responses import Response
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


@app.post("/mail/send")
def mail_send(from_email: str = Form(...), to: str = Form(...),
              subject: str = Form(""), body: str = Form(""),
              in_reply_to: str = Form("")):
    from backend.tools import mailcrm
    res = mailcrm.send(from_email, to, subject, body, in_reply_to=in_reply_to)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/mail/count")
def mail_count(q: str = "", mailbox: str = ""):
    from backend.tools import mailcrm
    return JSONResponse({"n": mailcrm.counts().get("unread", 0)})


@app.get("/mail/events")
async def mail_events():
    import asyncio
    from backend.tools import mailcrm

    async def stream():
        last = None
        while True:
            try:
                n = mailcrm.counts().get("unread", 0)
            except Exception:
                n = None
            if n is not None and n != last:
                last = n
                yield f"data: {n}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/draft")
def draft(payload: dict, x_assist_token: str = Header(default="")):
    """Draft answers for open-ended questions. Cache-first: the LLM is only hit for
    questions not already in the answer cache; new answers are written back."""
    if not ASSIST_TOKEN or x_assist_token != ASSIST_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from backend import answer_cache
    questions = [q for q in (payload.get("questions") or []) if isinstance(q, str) and q.strip()][:12]
    company = (payload.get("company") or "").strip()
    title = (payload.get("job_title") or "").strip()
    if not questions:
        return {"answers": {}, "review": {}, "from_cache": 0, "from_llm": 0}

    niche = " ".join((payload.get("niche") or "").split())[:80]
    profile = _safe_id(payload.get("profile") or "") or "michael"
    if profile not in _profiles():
        # never draft (or cache) answers grounded in nobody's / the wrong identity
        return JSONResponse({"error": "unknown profile"}, status_code=404)
    cached = answer_cache.get_many(questions, company, profile=profile, niche=niche)
    misses = [q for q in questions if q not in cached]
    drafted = {}
    if misses:
        try:
            from backend.profiles.facts import load_facts
            from backend.services.tailor.answers import draft_answers
            form_facts, summary = _profile_form(profile)
            drafted = draft_answers(misses, form_facts, {"title": title, "company": company},
                                    summary, facts=load_facts(profile), niche_label=niche)
            if drafted:
                answer_cache.put_many(drafted, company, profile=profile, niche=niche)
        except Exception as e:  # never 500 the extension — return what we have
            drafted = {}
            print(f"[draft] LLM error: {e}")
    answers, review = _split_review({**cached, **drafted})
    return {"answers": answers, "review": review,
            "from_cache": len(cached), "from_llm": len(drafted)}


def assist_closed(closed: list[dict], form: dict, facts: dict, job: dict,
                  niche: str = "") -> list[dict]:
    """Resolve closed (option-list) screener questions, one result per input item.

    Cascade mirrors the batch engine: deterministic analyzer rules first
    (_match_field -> _resolve_value -> _pick_option), then the constrained-choice
    engine for whatever the rules didn't answer. Items the engine deliberately
    skips (demographics -> "_skip") are NOT forwarded to the choice engine — they
    stay index=None for the human, same policy as batch runs.

    closed: [{"question": str, "options": [str, ...]}, ...]
    Returns [{"index": int|None, "option": str|None, "source": str, "review": bool}].
    """
    from backend.applier.analyzer import _match_field, _pick_option, _resolve_value
    results = [{"index": None, "option": None, "source": "", "review": False}
               for _ in closed]
    remaining: list[int] = []
    for i, item in enumerate(closed):
        question = str(item.get("question") or "").strip()
        options = [str(o) for o in (item.get("options") or []) if str(o).strip()]
        if not question or len(options) < 2:
            continue
        m = _match_field(question)
        if m and m[0] == "_skip":  # engine policy: never auto-answer these
            results[i]["source"] = "skip"
            continue
        if m:
            value = _resolve_value(m[0], form, "", {}, facts)
            if value:
                opt = _pick_option([{"text": o, "value": str(j)} for j, o in enumerate(options)],
                                   value, m[0])
                if opt:
                    idx = int(opt["value"])
                    results[i] = {"index": idx, "option": options[idx],
                                  "source": "rule", "review": False}
                    continue
        remaining.append(i)
    if remaining:
        from backend.services.tailor import choices
        qs = [{"question_text": str(closed[i].get("question") or ""),
               "options": [str(o) for o in (closed[i].get("options") or [])]}
              for i in remaining]
        for i, pick in zip(remaining, choices.choose_options(qs, facts, job, niche)):
            idx = pick.get("index")
            opts = closed[i].get("options") or []
            if idx is None or not 0 <= idx < len(opts):
                continue
            results[i] = {"index": idx, "option": str(opts[idx]),
                          "source": "choice", "review": not pick.get("backed")}
    return results


# nginx: `location = /assist` needs the same auth_basic off + long proxy timeout
# treatment as `location = /draft` (extension calls it with the token from job-site
# origins; the choice/draft round-trips can take minutes). Same for /profile_form
# (that one is fast, but it must also bypass the dashboard's basic-auth).
@app.post("/assist")
def assist(payload: dict, x_assist_token: str = Header(default="")):
    """Smart-fill backend for the extension: closed screeners (rules -> constrained
    choice) + open questions (cache-first drafting, same flow as /draft). /draft is
    kept as-is for older extension installs — this supersedes it for v2.1+."""
    if not ASSIST_TOKEN or x_assist_token != ASSIST_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    profile = _safe_id(payload.get("profile") or "") or "michael"
    if profile not in _profiles():
        return JSONResponse({"error": "unknown profile"}, status_code=404)
    from backend import answer_cache
    from backend.profiles.facts import load_facts
    company = (payload.get("company") or "").strip()
    title = (payload.get("job_title") or "").strip()
    niche = " ".join((payload.get("niche") or "").split())[:80]
    form, summary = _profile_form(profile)
    facts = load_facts(profile)
    job = {"title": title, "company": company}

    closed_in = [c for c in (payload.get("closed") or []) if isinstance(c, dict)][:40]
    closed = assist_closed(closed_in, form, facts, job, niche)

    questions = [q for q in (payload.get("open") or []) if isinstance(q, str) and q.strip()][:12]
    cached = answer_cache.get_many(questions, company, profile=profile, niche=niche) if questions else {}
    misses = [q for q in questions if q not in cached]
    drafted = {}
    if misses:
        try:
            from backend.services.tailor.answers import draft_answers
            drafted = draft_answers(misses, form, job, summary,
                                    facts=facts, niche_label=niche)
            if drafted:
                answer_cache.put_many(drafted, company, profile=profile, niche=niche)
        except Exception as e:  # never 500 the extension — return what we have
            drafted = {}
            print(f"[assist] draft error: {e}")
    answers, review = _split_review({**cached, **drafted})
    return {"closed": closed, "answers": answers, "review": review,
            "counts": {"closed": len(closed_in),
                       "closed_rule": sum(1 for r in closed if r["source"] == "rule"),
                       "closed_choice": sum(1 for r in closed if r["source"] == "choice"),
                       "from_cache": len(cached), "from_llm": len(drafted)}}


# Identity keys the extension may pull for its fill pass — contact + location only
# (no work-auth/salary: those stay rule/facts territory server-side).
_IDENTITY_KEYS = ("full_name", "email", "phone", "location", "city", "state",
                  "zip_code", "country", "linkedin_url")
_IDENTITY_RENAME = {"zip_code": "zip", "linkedin_url": "linkedin"}


@app.get("/profile_form")
def profile_form(profile: str = "michael", x_assist_token: str = Header(default="")):
    """Identity fields of one profile for the extension's fill pass (token-gated;
    cached client-side for 24h). nginx: same auth_basic off treatment as /draft."""
    if not ASSIST_TOKEN or x_assist_token != ASSIST_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pid = _safe_id(profile) or "michael"
    if pid not in _profiles():
        return JSONResponse({"error": "unknown profile"}, status_code=404)
    form, _ = _profile_form(pid)
    out = {_IDENTITY_RENAME.get(k, k): form[k] for k in _IDENTITY_KEYS if form.get(k)}
    parts = str(out.get("full_name") or "").split()
    if parts:
        out["first_name"] = parts[0]
        if len(parts) > 1:
            out["last_name"] = parts[-1]
    return out


@app.get("/extension")
def extension():
    f = PROJECT_ROOT / "extension" / "apply-assist.zip"
    if f.exists():
        return FileResponse(str(f), media_type="application/zip", filename="apply-assist.zip")
    return HTMLResponse("not found", status_code=404)


def _job_dir(profile: str, jid: str) -> Path | None:
    d = PREFILL_ROOT / _safe_id(profile) / _safe_id(jid)
    return d if d.is_dir() else None


@app.get("/resume/{jid}")
def resume(jid: str, profile: str = "michael"):
    d = _job_dir(profile, jid)
    f = d / "resume.pdf" if d else None
    if f and f.exists():
        return FileResponse(str(f), media_type="application/pdf")
    return HTMLResponse("not found", status_code=404)


@app.get("/shot/{jid}")
def shot(jid: str, profile: str = "michael"):
    d = _job_dir(profile, jid)
    f = d / "prefilled.png" if d else None
    if f and f.exists():
        return FileResponse(str(f), media_type="image/png")
    return HTMLResponse("not found", status_code=404)


@app.post("/mark/{jid}")
def mark(jid: str, profile: str = "michael", to: str = Form("")):
    profile = _safe_id(profile) or "michael"
    _apply_mark(profile, _safe_id(jid), to)
    return RedirectResponse(f"/?profile={profile}", status_code=303)


# nginx: the one-click extension endpoints below need the same treatment as
# `location = /draft` (auth_basic off — they're called cross-origin from job-site
# pages where the dashboard's basic-auth cookie/header is absent; access is gated
# by X-Assist-Token instead). Locations to mirror:
#   /assist, /profile_form, /job_pack, /resume_file, /mark_ext
@app.get("/job_pack")
def job_pack(profile: str = "", jid: str = "", x_assist_token: str = Header(default="")):
    """One job's prefill pack for the extension's one-click fill: the answers the
    batch already drafted (no LLM here), review metadata, choice picks. The
    '[review]' wire prefix never leaves the server — same policy as /draft."""
    if not ASSIST_TOKEN or x_assist_token != ASSIST_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    profile, jid = _safe_id(profile), _safe_id(jid)
    d = PREFILL_ROOT / profile / jid
    try:
        report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    except Exception:
        return JSONResponse({"error": "not found"}, status_code=404)
    answers, review = _split_review(report.get("drafted_answers") or {})
    return {
        "jid": jid,
        "profile": profile,
        "job_title": report.get("job_title") or "",
        "company": report.get("company") or "",
        "apply_url": report.get("apply_url") or "",
        "niche": report.get("resume_niche") or "",
        "answers": answers,
        "review": review,
        "review_items": report.get("review_items") or [],
        "choice_picks": report.get("choice_picks") or {},
        "unfilled": report.get("unfilled") or [],
        "has_resume": (d / "resume.pdf").exists(),
    }


@app.get("/resume_file")
def resume_file(profile: str = "", jid: str = "", x_assist_token: str = Header(default="")):
    """The job's tailored résumé PDF for the extension's auto-attach (token-gated
    twin of /resume/{jid}, which sits behind the dashboard's basic-auth)."""
    if not ASSIST_TOKEN or x_assist_token != ASSIST_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    f = PREFILL_ROOT / _safe_id(profile) / _safe_id(jid) / "resume.pdf"
    if not f.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(f), media_type="application/pdf", filename="resume.pdf")


_MARK_EXT_STATES = {"submitted", "rejected", "interview", "pending"}


@app.post("/mark_ext")
def mark_ext(payload: dict, x_assist_token: str = Header(default="")):
    """Token-gated twin of /mark: the extension reports the human's submit from
    the job-site origin so the dashboard card flips without a manual tap."""
    if not ASSIST_TOKEN or x_assist_token != ASSIST_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    to = str(payload.get("to") or "")
    if to not in _MARK_EXT_STATES:
        return JSONResponse({"error": "bad status"}, status_code=400)
    profile = _safe_id(payload.get("profile") or "") or "michael"
    _apply_mark(profile, _safe_id(payload.get("jid") or ""), to)
    return {"ok": True, "status": to}


# --- /setup: friend onboarding editor ------------------------------------------
# Dashboard pages behind the same nginx basic-auth as / (no token: these are never
# called from job-site origins). Edits exactly two files per person, both under
# backend/data/: profiles.json (identity entry) and facts/<id>.json (screener
# facts). Etalons are shown read-only — they are built from the person's real
# résumé and are too easy to break by hand-editing JSON blind.

_SETUP_CSS = """
textarea{width:100%;min-height:280px;background:#0f1216;color:#e7ebf0;border:1px solid #232a33;
 border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;resize:vertical}
input.idfield{flex:1;background:#0f1216;color:#e7ebf0;border:1px solid #232a33;border-radius:8px;padding:10px;font-size:13px}
.err{background:#3a1010;border:1px solid #5e1c1c;color:#e08585;border-radius:8px;padding:10px;
 margin:10px 0;font-size:13px;white-space:pre-wrap}
.ok{background:#10391f;border:1px solid #1c5e35;color:#46d17f;border-radius:8px;padding:10px;margin:10px 0;font-size:13px}
.note{font-size:12px;color:#8a94a3;margin:8px 0;line-height:1.5}
.note code{color:#aab3c0;background:#0f1216;border:1px solid #232a33;border-radius:4px;padding:1px 5px}
a.back{color:#49a8e0;font-size:13px;text-decoration:none}
"""


def _setup_page(title: str, body: str) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{_CSS}{_SETUP_CSS}</style></head>"
            f"<body><div class='wrap'>{body}</div></body></html>")


def _load_profiles_raw() -> list:
    """profiles.json as the raw list of dicts (the file the editor writes).
    Missing/unreadable -> [] (saving then creates/repairs the file)."""
    src = profile_store.REAL_PROFILES
    if not src.exists():
        return []
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _etalon_status(pid: str) -> tuple[bool, int, Path]:
    """(exists, niche count, path) for etalons/<id>.json — display only."""
    path = ETALONS_DIR / f"{pid}.json"
    if not path.exists():
        return False, 0, path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return True, len(data) if isinstance(data, list) else 0, path
    except Exception:
        return True, 0, path


def _profile_entry_text(pid: str) -> str:
    """Pretty-printed profiles.json entry; new ids get the minimal template
    (saving it is what creates the entry)."""
    for entry in _load_profiles_raw():
        if isinstance(entry, dict) and entry.get("id") == pid:
            return json.dumps(entry, indent=2, ensure_ascii=False)
    return json.dumps({"id": pid, "full_name": "", "email": "", "phone": ""},
                      indent=2, ensure_ascii=False)


def _facts_prefill_text(pid: str) -> str:
    """facts/<id>.json content; missing -> the committed sample as a starting
    template; an unreadable existing file is shown raw so it can be fixed here."""
    own = facts_lib.FACTS_DIR / f"{pid}.json"
    src = own if own.exists() else facts_lib.FACTS_DIR / "sample.json"
    if not src.exists():
        return "{}"
    text = src.read_text(encoding="utf-8")
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except Exception:
        return text


def _setup_index_html() -> str:
    rows = []
    for entry in _load_profiles_raw():
        if not isinstance(entry, dict):
            continue
        pid = _safe_id(str(entry.get("id") or ""))
        if not pid:
            continue
        has_facts = (facts_lib.FACTS_DIR / f"{pid}.json").exists()
        et_ok, et_n, _ = _etalon_status(pid)
        has_mail = bool(entry.get("mailbox"))
        badges = (
            f"<span class='badge {'ready' if has_facts else 'rej'}'>facts {'✓' if has_facts else '✗'}</span>"
            f"<span class='badge {'ready' if et_ok else 'rej'}'>etalons "
            f"{f'✓ {et_n} niches' if et_ok else '✗'}</span>"
            f"<span class='badge {'sub' if has_mail else 'warn'}'>mailbox {'✓' if has_mail else '✗'}</span>")
        rows.append(
            "<div class='card'><div class='top'><div>"
            f"<div class='co'>{escape(pid)}</div>"
            f"<div class='ti'>{escape(str(entry.get('full_name') or ''))}</div></div>"
            f"<a class='btn res' style='flex:0' href='/setup?profile={escape(pid)}'>Edit →</a></div>"
            f"<div class='row'>{badges}</div></div>")
    body = "".join(rows) or "<div class='empty'>No profiles yet — create the first one below.</div>"
    new_form = (
        "<div class='card'><div class='co'>New profile</div>"
        "<form method='get' action='/setup' class='row'>"
        "<input class='idfield' name='profile' placeholder='id — lowercase letters/digits'"
        " pattern='[a-z0-9]+' required>"
        "<button class='btn open' style='flex:0'>Create →</button></form>"
        "<div class='note'>The id goes into file names and links: short, lowercase, no spaces.</div>"
        "</div>")
    return _setup_page("Setup", "<h1>Profiles — setup</h1>" + body + new_form)


def _setup_editor_html(pid: str, profile_text: str, facts_text: str,
                       error: str = "", error_kind: str = "", saved: bool = False) -> str:
    et_ok, et_n, et_path = _etalon_status(pid)

    def block(kind: str, label: str, note: str, text: str) -> str:
        err = (f"<div class='err'>{escape(error)}</div>"
               if error and error_kind == kind else "")
        return (
            f"<div class='card'><div class='co'>{escape(label)}</div>"
            f"<div class='note'>{note}</div>{err}"
            "<form method='post' action='/setup/save'>"
            f"<input type='hidden' name='profile' value='{escape(pid)}'>"
            f"<input type='hidden' name='kind' value='{kind}'>"
            f"<textarea name='body' spellcheck='false'>{escape(text)}</textarea>"
            f"<div class='row'><button class='btn open'>Save {kind}</button></div>"
            "</form></div>")

    et_badge = (f"<span class='badge ready'>✓ {et_n} niches</span>" if et_ok
                else "<span class='badge rej'>✗ not set</span>")
    etalons = (
        f"<div class='card'><div class='top'><div class='co'>Résumé variants (etalons)</div>{et_badge}</div>"
        f"<div class='note'>Read-only: this file is built from the person's real résumé "
        f"and breaks easily when hand-edited. File: <code>{escape(str(et_path))}</code></div></div>")
    body = (
        "<a class='back' href='/setup'>← all profiles</a>"
        f"<h1>Setup — {escape(pid)}</h1>"
        + ("<div class='ok'>Saved.</div>" if saved else "")
        + block("profile", "Profile (identity)",
                "Contact and identity fields — the entry in profiles.json. "
                "JSON object; <code>id</code> must stay "
                f"<code>{escape(pid)}</code>.", profile_text)
        + block("facts", "Facts",
                "Answers used for screener questions — edit truthfully for this person.",
                facts_text)
        + etalons)
    return _setup_page(f"Setup — {pid}", body)


@app.get("/setup", response_class=HTMLResponse)
def setup(profile: str = "", saved: str = ""):
    pid = _safe_id(profile)
    if not pid:
        return _setup_index_html()
    return _setup_editor_html(pid, _profile_entry_text(pid), _facts_prefill_text(pid),
                              saved=saved == "1")


def _setup_error(pid: str, kind: str, body: str, msg: str) -> HTMLResponse:
    """Re-render the editor with the error and the submitted text (never lose the
    user's input); the other editor keeps its normal prefill."""
    profile_text = body if kind == "profile" else _profile_entry_text(pid)
    facts_text = body if kind == "facts" else _facts_prefill_text(pid)
    return HTMLResponse(_setup_editor_html(pid, profile_text, facts_text,
                                           error=msg, error_kind=kind),
                        status_code=400)


@app.post("/setup/save")
def setup_save(profile: str = Form(""), kind: str = Form(""), body: str = Form("")):
    pid = _safe_id(profile)
    if not pid or pid != profile:  # traversal / uppercase / empty -> reject, never remap
        return HTMLResponse("bad profile id", status_code=400)
    if kind not in ("profile", "facts"):
        return HTMLResponse("bad kind", status_code=400)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return _setup_error(pid, kind, body, f"Invalid JSON: {e}")
    if not isinstance(data, dict):
        return _setup_error(pid, kind, body, "Top-level value must be a JSON object {…}")

    if kind == "facts":
        _atomic_write_json(facts_lib.FACTS_DIR / f"{pid}.json", data)
    else:
        try:
            profile_store.Profile.from_dict(data)  # catches unknown/missing keys
        except (TypeError, ValueError) as e:
            return _setup_error(pid, kind, body, str(e))
        if data.get("id") != pid:
            return _setup_error(pid, kind, body,
                                f'"id" must be "{pid}" (it names this person\'s files)')
        entries = _load_profiles_raw()
        for i, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("id") == pid:
                entries[i] = data
                break
        else:
            entries.append(data)
        _atomic_write_json(profile_store.REAL_PROFILES, entries)
        _PROFILES_CACHE["mtime"] = None  # mtime moved anyway; belt and braces
    return RedirectResponse(f"/setup?profile={pid}&saved=1", status_code=303)
