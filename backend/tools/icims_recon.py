"""Supervised noVNC recon of the Teleperformance iCIMS apply flow.

Goal: COLLECT the real form so we can build the full auto-fill etalon from data, not guesses —
the live **State dropdown (the real allowed-states)**, every **screener** (question + options),
the identity/résumé/EEO fields, and the step ordering. It drives the flow headful on
**DISPLAY=:98** (watch it live at https://jobs.systeam.kz/vnc/) through the owner's **residential
tunnel** (slot 8120 → his home IP) with **patchright** stealth, which beats the entry AWS-WAF.

Division of labour: the bot fills every step it can with a state-constrained synthetic persona;
the HUMAN solves any captcha (AWS-WAF / hCaptcha) in noVNC — the bot detects the challenge, logs
"WAITING FOR HUMAN", and polls until it clears, then continues. The emailed account-verification
code is read from the persona's own @takhet.com Maildir automatically.

Nothing is submitted: the walk stops at the application form and captures it; it never clicks the
final Submit.

Run (mail group needed for the emailed verification code):
    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        python3 -m backend.tools.icims_recon --job 459'

Output: logs/icims_recon/<jobid>/recon.json  (+ NN_<tag>.png screenshots per step).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECON_ROOT = os.path.join(REPO, "logs", "icims_recon")
STEALTH_PROFILE = os.path.join(REPO, "backend", "data", "icims_stealth_profile")
SLOT = "socks5://127.0.0.1:8120"

# Teleperformance US remote-hire allow-list (38 states), confirmed verbatim on two live postings
# (recon 2026-08-31). The synthetic persona's claimed residence MUST be one of these. Excluded:
# AK CA CO CT HI MA NH NY OR RI VT WA + DC/territories. The live dropdown we capture is the
# ground truth; this is the seed the persona uses to get PAST the state field.
ALLOWED_STATES = {
    "AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona", "DE": "Delaware", "FL": "Florida",
    "GA": "Georgia", "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "MD": "Maryland", "ME": "Maine",
    "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri", "MS": "Mississippi", "MT": "Montana",
    "NC": "North Carolina", "ND": "North Dakota", "NE": "Nebraska", "NJ": "New Jersey",
    "NM": "New Mexico", "NV": "Nevada", "OH": "Ohio", "OK": "Oklahoma", "PA": "Pennsylvania",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VA": "Virginia", "WI": "Wisconsin", "WV": "West Virginia", "WY": "Wyoming",
}
# A representative city + ZIP per state so the persona's address is internally consistent; only a
# few are needed (default Ohio/Columbus), the rest fall back to the state capital-ish default.
_STATE_CITY = {
    "OH": ("Columbus", "43215"), "TX": ("Houston", "77002"), "TN": ("Nashville", "37203"),
    "FL": ("Orlando", "32801"), "GA": ("Atlanta", "30303"), "PA": ("Philadelphia", "19103"),
    "MI": ("Detroit", "48226"), "NC": ("Charlotte", "28202"), "AZ": ("Phoenix", "85004"),
    "IN": ("Indianapolis", "46204"),
}


def _pick_state(title: str) -> tuple[str, str, str, str]:
    """Choose an allowed (full, code, city, zip). If the title names a hard "X Only" subset,
    take the first listed allowed state; else default Ohio."""
    import re
    t = title or ""
    m = re.search(r"\b((?:[A-Z]{2})(?:\s*,\s*[A-Z]{2})*)\s*Only\b", t)
    codes = []
    if m:
        codes = [c.strip() for c in m.group(1).split(",")]
    for c in codes:
        if c in ALLOWED_STATES:
            city, zc = _STATE_CITY.get(c, (ALLOWED_STATES[c].split()[0], "00000"))
            return ALLOWED_STATES[c], c, city, zc
    # trailing bare "- OH" style
    m2 = re.search(r"[-–]\s*([A-Z]{2})\b\s*$", t.strip())
    if m2 and m2.group(1) in ALLOWED_STATES:
        c = m2.group(1)
        city, zc = _STATE_CITY.get(c, (ALLOWED_STATES[c].split()[0], "00000"))
        return ALLOWED_STATES[c], c, city, zc
    return "Ohio", "OH", "Columbus", "43215"


# ---- capture ------------------------------------------------------------------------------------
_CAP_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const labtext = el => {
    let t = '';
    const id = el.id;
    if (id) { const l = document.querySelector('label[for="' +
      (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]'); if (l) t = l.innerText; }
    if (!t) { const w = el.closest('label'); if (w) t = w.innerText; }
    if (!t) t = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    return (t || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  };
  const fields = [];
  for (const el of document.querySelectorAll('input,select,textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset'].includes(type)) continue;
    if (!vis(el)) continue;
    fields.push({ tag: el.tagName.toLowerCase(), type,
      name: el.name || '', id: el.id || '', label: labtext(el),
      value: (el.value || '').slice(0, 40),
      required: !!(el.required || el.getAttribute('aria-required') === 'true') });
  }
  const selects = [];
  for (const s of document.querySelectorAll('select')) {
    if (!vis(s)) continue;
    selects.push({ label: labtext(s), name: s.name || '', id: s.id || '',
      nopts: s.options.length,
      options: [...s.options].map(o => (o.textContent || '').trim()).filter(Boolean) });
  }
  // radio groups: derive the question by climbing until container text exceeds the option labels
  const byName = {};
  for (const r of document.querySelectorAll('input[type=radio]')) {
    if (!r.name) continue; (byName[r.name] = byName[r.name] || []).push(r);
  }
  const lab = r => { const l = r.id ? document.querySelector('label[for="' +
      (window.CSS && CSS.escape ? CSS.escape(r.id) : r.id) + '"]') : null;
    return ((l && l.innerText) || (r.closest('label') ? r.closest('label').innerText : '') || '')
      .replace(/\s+/g, ' ').trim(); };
  const radios = [];
  for (const nm in byName) {
    const rs = byName[nm];
    const opts = rs.map(r => ({ value: r.value, text: lab(r), checked: r.checked }));
    let box = rs[0].parentElement;
    while (box && !rs.every(r => box.contains(r))) box = box.parentElement;
    const optLen = opts.map(o => o.text).join(' ').replace(/\s+/g, '').length;
    let g = 0;
    while (box && box.parentElement && g < 5) {
      if ((box.innerText || '').replace(/\s+/g, '').length > optLen + 12) break;
      box = box.parentElement; g++;
    }
    let qt = box ? (box.innerText || '') : '';
    for (const o of opts) if (o.text) qt = qt.split(o.text).join(' ');
    qt = qt.replace(/\s+/g, ' ').trim().slice(0, 200);
    radios.push({ question: qt, name: nm,
      options: opts.map(o => ({ value: o.value, text: o.text })),
      answered: opts.some(o => o.checked) });
  }
  const checkboxes = [];
  for (const c of document.querySelectorAll('input[type=checkbox]')) {
    if (!vis(c)) continue;
    checkboxes.push({ label: labtext(c),
      required: !!(c.required || c.getAttribute('aria-required') === 'true'),
      checked: c.checked });
  }
  const body = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  const captcha = /confirm you are human|choose all the|solve a puzzle|human verification|hcaptcha|are you a robot/i.test(body)
    || !!document.querySelector('iframe[src*="hcaptcha"], [id*="awswaf" i], .h-captcha');
  return { url: location.href, title: document.title,
    bodySnip: body.slice(0, 300), captcha,
    fields, selects, radios, checkboxes };
}
"""


def _icims_frames(page):
    """The iCIMS content frame(s) + the main frame, best-effort."""
    out = []
    try:
        for fr in page.frames:
            u = (fr.url or "").lower()
            if fr is page.main_frame or "icims" in u:
                out.append(fr)
    except Exception:
        out = [page.main_frame]
    return out or [page.main_frame]


async def _capture(page, tag: str, recon_dir: str, recon: list, idx: list) -> dict:
    """Snapshot every iCIMS/main frame's form + a full-page screenshot. Appends to recon[]."""
    frames = []
    for fr in _icims_frames(page):
        try:
            data = await fr.evaluate(_CAP_JS)
            data["frame_url"] = fr.url
            frames.append(data)
        except Exception as e:
            frames.append({"frame_url": getattr(fr, "url", "?"), "error": f"{type(e).__name__}: {e}"[:120]})
    n = idx[0]; idx[0] += 1
    shot = os.path.join(recon_dir, f"{n:02d}_{tag}.png")
    try:
        await page.screenshot(path=shot, full_page=True)
    except Exception:
        shot = ""
    entry = {"seq": n, "tag": tag, "ts": int(time.time()), "page_url": page.url,
             "shot": os.path.basename(shot) if shot else "", "frames": frames}
    recon.append(entry)
    with open(os.path.join(recon_dir, "recon.json"), "w", encoding="utf-8") as f:
        json.dump(recon, f, ensure_ascii=False, indent=1)
    # log a compact summary of what we found this step
    sels = [(s.get("label") or s.get("name"), s.get("nopts"))
            for fr in frames for s in (fr.get("selects") or [])]
    rads = [(r.get("question") or r.get("name"))[:60] for fr in frames for r in (fr.get("radios") or [])]
    cap = any(fr.get("captcha") for fr in frames)
    print(f"[capture {n:02d} {tag}] url={page.url[:70]} captcha={cap} "
          f"selects={sels[:6]} radios={rads[:4]}", flush=True)
    # spotlight any state dropdown — the whole point of the recon
    for fr in frames:
        for s in (fr.get("selects") or []):
            lbl = (s.get("label") or s.get("name") or "").lower()
            if any(k in lbl for k in ("state", "province", "reside", "location")) and s.get("nopts", 0) > 5:
                print(f"    >>> STATE-LIKE dropdown '{s.get('label') or s.get('name')}' "
                      f"({s.get('nopts')} opts): {s.get('options')}", flush=True)
    return entry


# A captcha the HUMAN must act on = a full-page AWS-WAF challenge OR a LARGE, visible hCaptcha
# challenge popup. The tiny "Protected by hCaptcha" badge (invisible hCaptcha) is NOT a challenge —
# stealth may pass it silently on the Next click, so we must NOT block on the badge's mere presence.
_CAPTCHA_DOM = r"""
() => {
  const b = document.body ? document.body.innerText : '';
  if (/confirm you are human|choose all the|solve a puzzle|human verification|verify you are human|let's confirm you are human|click the shape/i.test(b)) return true;
  for (const f of document.querySelectorAll('iframe[src*="hcaptcha"], iframe[title*="captcha" i]')) {
    const r = f.getBoundingClientRect();
    if (r.width > 180 && r.height > 180) return true;   // the challenge popup, not the badge
  }
  return false;
}
"""


async def _has_captcha(page) -> bool:
    """True only when a captcha the HUMAN must solve is actually SHOWING (full-page AWS-WAF, or a
    large visible hCaptcha challenge) — never for the invisible hCaptcha badge."""
    for fr in _icims_frames(page):   # main + iCIMS content frame(s)
        try:
            if await fr.evaluate(_CAPTCHA_DOM):
                return True
        except Exception:
            continue
    return False


async def _find_in_frames(page, make_locator, timeout: int = 40):
    """Poll every iCIMS/main frame until make_locator(frame) yields a visible element (or timeout).
    Returns (frame, locator) or (None, None). Handles the slow residential-proxy iframe load."""
    end = time.time() + timeout
    while time.time() < end:
        for fr in _icims_frames(page):
            try:
                loc = make_locator(fr)
                if await loc.count() and await loc.first.is_visible(timeout=700):
                    return fr, loc.first
            except Exception:
                continue
        await page.wait_for_timeout(1500)
    return None, None


async def _wait_human(page, label: str, recon_dir: str, recon: list, idx: list, timeout: int = 1200) -> bool:
    """If a captcha is up, ask the human (in noVNC) to solve it and poll until it clears."""
    if not await _has_captcha(page):
        return True
    await _capture(page, f"{label}_captcha", recon_dir, recon, idx)
    print(f"\n########## CAPTCHA — SOLVE IT IN noVNC: "
          f"https://jobs.systeam.kz/vnc/vnc.html?path=vnc/websockify&autoconnect=true&resize=scale "
          f"(user job2026) ##########\n", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        await page.wait_for_timeout(4000)
        if not await _has_captcha(page):
            print(f"[captcha cleared after {int(time.time()-start)}s]", flush=True)
            await page.wait_for_timeout(2500)
            return True
    print("[captcha wait timed out]", flush=True)
    return False


# ---- autonomous fill + wizard walk (the etalon collector drives itself) --------------------------
import re as _re

_FINAL_SUBMIT_RE = _re.compile(r"submit (your )?application|complete (your )?application|finish", _re.I)
_ADVANCE_TXT = ("Submit Profile", "Save and Continue", "Save & Continue", "Continue",
                "Next", "Review", "Proceed", "Submit")


async def _force_text(root, label_re: str, value: str) -> bool:
    """Set a text input by label — OVERWRITING even a non-empty value (the iCIMS résumé parser
    clobbers City/etc., so identity fills must be able to correct it)."""
    if not value:
        return False
    return bool(await root.evaluate(
        """([re_src, val])=>{
          const rx=new RegExp(re_src,'i');
          const lt=el=>{let t='';const id=el.id;
            if(id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)t=l.innerText;}
            if(!t){const w=el.closest('label');if(w)t=w.innerText;}
            if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';return t;};
          for(const el of document.querySelectorAll('input[type=text],input[type=tel],input[type=email],input:not([type])')){
            const t=(el.type||'').toLowerCase();
            if(['hidden','submit','button','file','password'].includes(t))continue;
            if(!rx.test(lt(el)))continue;
            el.value=val;el.dispatchEvent(new Event('input',{bubbles:true}));
            el.dispatchEvent(new Event('change',{bubbles:true}));return true;}
          return false;}""", [label_re, value]))


async def _select_first_real(root, label_substr: str) -> bool:
    """Pick the first non-placeholder option of the <select> whose label contains label_substr
    (for a required dependent select like 'Please specify further')."""
    info = await root.evaluate(
        """(lbl)=>{const n=s=>(s||'').toLowerCase();
          const ph=t=>!t||/make a selection|select an option|select a |please select|choose|specify a|select a source/.test(n(t));
          for(const l of document.querySelectorAll('label')){
            if(!n(l.innerText).includes(lbl))continue;
            let el=l.getAttribute('for')?document.getElementById(l.getAttribute('for')):null;
            if(!el||el.tagName!=='SELECT')el=(l.closest('div,li,fieldset,tr')||document).querySelector('select');
            if(!el||el.tagName!=='SELECT')continue;
            const cur=el.options[el.selectedIndex];
            if(el.value&&!ph(cur&&cur.text))continue;
            const o=[...el.options].find(o=>o.value&&!ph(o.text));
            if(!o)continue;el.setAttribute('data-jf1','1');return {value:o.value};}
          return null;}""", label_substr.lower())
    if not info:
        return False
    try:
        await root.select_option("select[data-jf1='1']", value=info["value"])
    except Exception:
        return False
    finally:
        try:
            await root.eval_on_selector("select[data-jf1='1']", "e=>e.removeAttribute('data-jf1')")
        except Exception:
            pass
    return True


async def _force_select(root, label_substr: str, value_substr: str) -> bool:
    """Select an option on the <select> whose label contains label_substr — even if it already has
    a value (needed to CHANGE 'How did you hear' while hunting a choice with a valid sub-select)."""
    info = await root.evaluate(
        """([lbl,val])=>{const n=s=>(s||'').toLowerCase();
          for(const l of document.querySelectorAll('label')){
            if(!n(l.innerText).includes(lbl))continue;
            let el=l.getAttribute('for')?document.getElementById(l.getAttribute('for')):null;
            if(!el||el.tagName!=='SELECT')el=(l.closest('div,li,fieldset,tr')||document).querySelector('select');
            if(!el||el.tagName!=='SELECT')continue;
            const o=[...el.options].find(o=>o.value&&(n(o.text).includes(val)||n(o.value).includes(val)));
            if(!o)continue;el.setAttribute('data-jf2','1');return {value:o.value};}
          return null;}""", [label_substr.lower(), value_substr.lower()])
    if not info:
        return False
    try:
        await root.select_option("select[data-jf2='1']", value=info["value"])
    except Exception:
        return False
    finally:
        try:
            await root.eval_on_selector("select[data-jf2='1']", "e=>e.removeAttribute('data-jf2')")
        except Exception:
            pass
    return True


async def _fill_how_heard(page, root) -> bool:
    """'How did you hear about us?' has a REQUIRED dependent 'Please specify further' select whose
    options depend on the parent choice (e.g. Google Search yields none -> unsubmittable). Try each
    source until 'specify further' can be satisfied (a real sub-option, or a text 'Online')."""
    for v in ("job board", "indeed", "media", "events", "agenc", "school", "digital",
              "google", "other", "teleperformance"):
        if not await _force_select(root, "how did you hear", v):
            continue
        await page.wait_for_timeout(900)          # let the dependent select repopulate
        if await _select_first_real(root, "specify further"):
            return True
        if await _force_text(root, "specify further", "Online"):
            return True
    return False


async def _tp_fill(page, root, pf, facts, strat) -> None:
    """Fill the Teleperformance iCIMS Candidate Profile / screener step COMPLETELY (frame-aware,
    idempotent, no résumé re-attach). Order matters: Country BEFORE State (the State dropdown is
    Country-dependent), and City/Zip are force-set AFTER (the résumé parser overwrites them)."""
    try:
        await strat._fill_identity(root, pf)     # only fills EMPTY identity fields
    except Exception:
        pass
    # phone: Type=Mobile + Number as digits only ("+16145550187")
    try:
        await strat._select_by_label(root, "type", "mobile")
    except Exception:
        pass
    digits = _re.sub(r"[^0-9]", "", pf.get("phone", "") or "")
    if len(digits) >= 10:
        await _force_text(root, r"^\s*number|include country code|^phone|mobile number", "+" + digits)
    # "How did you hear about us?" + its REQUIRED dependent "specify further"
    try:
        await _fill_how_heard(page, root)
    except Exception:
        pass
    # Country FIRST (unlocks State), then State/Province, then correct City/Zip
    try:
        await strat._select_by_label(root, "country", "united states")
    except Exception:
        pass
    await page.wait_for_timeout(1200)
    st = (pf.get("state") or "").strip()
    if st:
        try:
            await strat._select_by_label(root, "state", st)
        except Exception:
            pass
    await _force_text(root, r"^\s*city\b|city/town|^town\b", pf.get("city") or "")
    await _force_text(root, r"zip|postal", pf.get("zip") or "")
    # required acknowledgements / consent / EEO decline / deterministic screeners
    for fn in (lambda: strat._tick_acknowledge(page, root),
               lambda: strat._tick_required_checkboxes(page, root),
               lambda: strat._decline_demographics(root, pf.get("full_name") or ""),
               lambda: strat._answer_screeners(page, root, facts or {})):
        try:
            await fn()
        except Exception:
            pass


async def _advance(page, root) -> str:
    """Click the step's advance button (Submit Profile / Continue / Next / …) but NOT the final
    application submit. Returns 'clicked' if an advance was clicked, 'final' if only the final
    Submit Application remains (we never click it), else 'none'."""
    try:
        has_final = await root.evaluate(
            """(re)=>{const rx=new RegExp(re,'i');
               for(const b of document.querySelectorAll('button,input[type=submit],a[role=button]')){
                 const t=((b.innerText||'')+' '+(b.value||'')).trim();
                 if(rx.test(t)){const r=b.getBoundingClientRect();if(r.width>1)return true;}}
               return false;}""", _FINAL_SUBMIT_RE.pattern)
    except Exception:
        has_final = False
    for txt in _ADVANCE_TXT:
        if _FINAL_SUBMIT_RE.search(txt):
            continue
        try:
            b = root.locator(
                f'button:has-text("{txt}"), input[type=submit][value="{txt}"], '
                f'a[role="button"]:has-text("{txt}")').first
            if await b.count() and await b.is_visible(timeout=500):
                await b.click(timeout=5000)
                return "clicked"
        except Exception:
            continue
    return "final" if has_final else "none"


# ---- persona ------------------------------------------------------------------------------------
def _build_persona(row: dict, reuse: bool = True) -> dict:
    """Persona + résumé for this TP job, residence forced to an allowed state. `reuse` (default)
    picks up the newest existing demo_*/mh_<id>/persona.json to AVOID the LLM résumé tailoring on
    every relaunch (the local LLM is often saturated by the SHL runner) — and it matches the reused
    stealth-profile session. Falls back to a fresh mass_hiring_apply.prepare() when none exists."""
    import glob
    from pathlib import Path

    from backend.tools.catalog_drafts import PREFILL_ROOT

    full, code, city, zc = _pick_state(row.get("title") or "")
    jobid = f"mh_{row['id']}"
    pdir = None
    if reuse:
        cands = sorted(glob.glob(str(Path(PREFILL_ROOT) / "demo_*" / jobid / "persona.json")),
                       key=os.path.getmtime, reverse=True)
        for c in cands:
            d = os.path.dirname(c)
            if os.path.exists(os.path.join(d, "resume.pdf")):
                pdir = d
                profile_id = os.path.basename(os.path.dirname(d))
                print(f"[reusing persona {profile_id} (no LLM)]", flush=True)
                break
    if pdir is None:
        from backend.tools import mass_hiring_apply
        profile_id, jobid = mass_hiring_apply.prepare(row, gender=None)
        pdir = str(Path(PREFILL_ROOT) / profile_id / jobid)
    pdir = Path(pdir)
    persona = json.loads((pdir / "persona.json").read_text(encoding="utf-8"))
    prof = persona.get("profile") or {}
    facts = persona.get("facts") or {}

    # constrain residence to the allowed state
    prof["state"] = full
    prof["city"] = city
    prof["zip"] = zc
    prof["location"] = f"{city}, {code}"
    prof["street_address"] = prof.get("street_address") or "1200 Market Street"

    name = prof.get("full_name") or prof.get("name") or ""
    parts = name.split()
    profile_form = {
        "full_name": name,
        "first_name": prof.get("first_name") or (parts[0] if parts else ""),
        "last_name": prof.get("last_name") or (parts[-1] if len(parts) > 1 else ""),
        "email": prof.get("email") or "",
        "phone": prof.get("phone") or "",
        "street_address": prof["street_address"],
        "address": prof["street_address"],
        "city": city, "state": full, "zip": zc, "postal_code": zc,
        "country": "United States",
    }
    resume_path = str(pdir / "resume.pdf")
    return {"profile_form": profile_form, "facts": facts, "resume_path": resume_path,
            "state_code": code, "state_full": full, "jobid": jobid, "profile_id": profile_id}


# ---- driver -------------------------------------------------------------------------------------
async def run(job_id: int, url: str | None = None, keep_minutes: int = 20, reuse: bool = True) -> None:
    from patchright.async_api import async_playwright

    from backend.applier.strategies.icims import ICIMSStrategy

    with mail_db.conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, title, apply_url, company FROM mass_hiring_jobs WHERE id=%s", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no mass_hiring_jobs row id={job_id}", flush=True); return
    row = {"id": r[0], "title": r[1], "apply_url": r[2], "company": r[3]}
    job_url = url or row["apply_url"]

    print(f"=== iCIMS recon: job {row['id']} — {row['title']}", flush=True)
    p = _build_persona(row, reuse=reuse)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state_code']} "
          f"({p['state_full']}) | resume={os.path.exists(p['resume_path'])}", flush=True)

    recon_dir = os.path.join(RECON_ROOT, str(row["id"]))
    os.makedirs(recon_dir, exist_ok=True)
    os.makedirs(STEALTH_PROFILE, exist_ok=True)
    recon: list = []
    idx = [0]
    strat = ICIMSStrategy()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            STEALTH_PROFILE, headless=False, channel="chromium",
            proxy={"server": SLOT}, no_viewport=True, locale="en-US",
            timezone_id="America/New_York", args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            import re
            from backend.applier.strategies.icims import _gen_password
            acct_pw = _gen_password()
            start_ts = time.time()
            code_shown = False
            NOVNC = "https://jobs.systeam.kz/vnc/vnc.html?path=vnc/websockify&autoconnect=true&resize=scale"

            # 1) job page — beat the WAF (stealth), wait for the Welcome/Apply to render (slow
            #    through the residential proxy; don't act before the iframe exists).
            await page.goto(job_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(4000)
            await _wait_human(page, "entry", recon_dir, recon, idx)   # AWS-WAF may show at entry
            fr, apply_btn = await _find_in_frames(
                page, lambda f: f.get_by_role("link", name=re.compile("Apply for this job online", re.I)), 50)
            if not apply_btn:
                fr, apply_btn = await _find_in_frames(
                    page, lambda f: f.get_by_role("button", name=re.compile(r"^Apply", re.I)), 8)
            await _capture(page, "job", recon_dir, recon, idx)

            # 2) Apply -> the email-first register wall
            if apply_btn:
                try:
                    await apply_btn.click(timeout=8000)
                    print("[clicked Apply]", flush=True)
                except Exception as e:
                    print(f"[apply click: {type(e).__name__}: {e}]"[:120], flush=True)
            else:
                print("[Apply link not found — click it in noVNC]", flush=True)
            await page.wait_for_timeout(5000)
            await _wait_human(page, "apply", recon_dir, recon, idx)

            # 3) PRE-FILL email + tick privacy — but do NOT click Next. The hCaptcha spins forever on
            #    an AUTOMATED Next click; a genuine human click in noVNC makes it show a solvable
            #    challenge. So the bot fills, the HUMAN clicks Next + solves the captcha.
            fr, em = await _find_in_frames(
                page, lambda f: f.locator('input[type=email], #email, input[name="css_loginName"]'), 50)
            if em and fr:
                try:
                    await em.fill(pf["email"], timeout=5000)
                    cbs = fr.locator('input[type=checkbox]')
                    for i in range(await cbs.count()):
                        try:
                            await cbs.nth(i).check(timeout=1500)
                        except Exception:
                            pass
                    print(f"[pre-filled email {pf['email']} + privacy]", flush=True)
                except Exception as e:
                    print(f"[email prefill: {type(e).__name__}: {e}]"[:120], flush=True)
            await _capture(page, "after_apply", recon_dir, recon, idx)

            print("\n" + "=" * 72, flush=True)
            print("AUTONOMOUS — the bot fills every field (Country->State/Ohio, phone, screeners, EEO)", flush=True)
            print("and clicks Continue/Submit through the wizard, capturing each step. You ONLY solve", flush=True)
            print(f"a captcha if one pops (until CapSolver):  {NOVNC}", flush=True)
            print("=" * 72 + "\n", flush=True)

            async def _sig():
                try:
                    r = await strat._content_frame(page) or page.main_frame
                    return await r.evaluate(
                        "()=>{const h=document.querySelector('h1,h2,legend,.title');"
                        "return (h?h.innerText.slice(0,45):'')+'|'+"
                        "document.querySelectorAll('input,select,textarea').length+'|'+location.href;}")
                except Exception:
                    return ""

            # 4) AUTONOMOUS walk. On each NEW step: fill it fully, capture, and click advance ONCE —
            #    then WAIT. Re-filling / re-clicking every cycle resets the hCaptcha token and loops
            #    forever (the bug the owner hit), so we advance once per step and only RETRY a step
            #    that has stalled >75s (e.g. a validation gap the fill just closed).
            deadline = time.time() + keep_minutes * 60
            cur_sig = None
            resume_attached = False
            step_started = time.time()
            advanced = False
            while time.time() < deadline:
                await _wait_human(page, "step", recon_dir, recon, idx)   # pause on a VISIBLE captcha
                frame = await strat._content_frame(page)
                root = frame or page
                sig = await _sig()
                new_step = sig != cur_sig
                stalled = advanced and (time.time() - step_started > 75)
                if not (new_step or stalled):
                    await page.wait_for_timeout(5000)
                    continue
                if new_step:
                    cur_sig = sig
                    await _capture(page, "step", recon_dir, recon, idx)
                step_started = time.time()
                advanced = False
                # (a) account password (empty only)
                try:
                    pw = root.locator('input[type=password]')
                    for i in range(await pw.count()):
                        b = pw.nth(i)
                        try:
                            if not (await b.input_value()).strip():
                                await b.fill(acct_pw, timeout=3000)
                        except Exception:
                            pass
                except Exception:
                    pass
                # (b) résumé attach ONCE — re-attaching re-triggers the parser -> clobbers City
                if not resume_attached:
                    try:
                        if await strat._attach_resume_in_frame(root, p["resume_path"]):
                            resume_attached = True
                            print("[résumé attached]", flush=True)
                            await page.wait_for_timeout(3000)     # let the parser settle once
                    except Exception:
                        pass
                # (c) fill the whole step
                try:
                    await _tp_fill(page, root, pf, p["facts"], strat)
                except Exception as e:
                    print(f"[fill: {type(e).__name__}: {e}]"[:120], flush=True)
                # (d) emailed verification code
                if not code_shown:
                    try:
                        from backend.tools.verify_code import read_code
                        code = read_code(pf["email"], since_ts=start_ts - 60)
                        if code:
                            code_shown = True
                            print(f"\n>>> VERIFICATION CODE {pf['email']}: {code}\n", flush=True)
                    except Exception:
                        pass
                await _capture(page, "filled", recon_dir, recon, idx)   # filled values + live State list
                # (e) advance ONCE (never the final submit)
                kind = await _advance(page, root)
                advanced = True
                if kind == "final":
                    print("[reached FINAL Submit Application — recon complete, NOT clicking]", flush=True)
                    await _capture(page, "final_form", recon_dir, recon, idx)
                    break
                print(f"[step filled + advance={kind} -> waiting for next step / captcha]", flush=True)
                await page.wait_for_timeout(5000)
        finally:
            try:
                await _capture(page, "final", recon_dir, recon, idx)
            except Exception:
                pass
            await ctx.close()
    print(f"=== recon done — {len(recon)} snapshots in {recon_dir}/recon.json", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=459, help="mass_hiring_jobs id (Teleperformance)")
    ap.add_argument("--url", default=None, help="override apply URL")
    ap.add_argument("--keep", type=int, default=20, help="minutes to stay open for manual capture")
    ap.add_argument("--fresh", action="store_true",
                    help="build a fresh LLM-tailored persona instead of reusing the newest one")
    args = ap.parse_args()
    asyncio.run(run(args.job, url=args.url, keep_minutes=args.keep, reuse=not args.fresh))


if __name__ == "__main__":
    main()
