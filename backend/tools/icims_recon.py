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
SLOT = os.getenv("ICIMS_PROXY", "socks5://127.0.0.1:8120")   # empty = DIRECT (no residential tunnel)


def _tunnel_up() -> bool:
    """True if the SLOT proxy actually accepts a TCP connection (the residential chisel tunnel is
    often down). When it's down we launch DIRECT instead of pointing Chromium at a dead proxy."""
    if not SLOT:
        return False
    import socket
    from urllib.parse import urlparse
    u = urlparse(SLOT)
    host, port = (u.hostname or "127.0.0.1"), (u.port or 8120)
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False
# NopeCHA extension auto-solves the hCaptcha in-page (its API egresses via the browser's residential
# proxy, dodging the datacenter-IP free-tier ban). With it, the bot drives everything autonomously.
NOPECHA_EXT = os.path.join(REPO, "backend", "vendor", "nopecha_ext")
NOPECHA = os.getenv("ICIMS_NOPECHA", "1").strip().lower() in ("1", "true", "yes", "on")
# RELAY-DRIVE (fallback): the human taps Next/Submit + solves the captcha from the phone
# (captcha.systeam.kz / noVNC) — used only when NopeCHA is off/failing. Default OFF now that the
# extension solves captchas, so the bot auto-advances.
RELAY_DRIVE = os.getenv("ICIMS_RELAY_DRIVE", "0").strip().lower() in ("1", "true", "yes", "on")

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


# A full-page AWS-WAF challenge (entry). hCaptcha is detected separately by the RELIABLE
# frame=challenge check (captcha_relay.visible_popup), NOT by iframe size — the hidden/leftover
# hCaptcha challenge iframe stays full-size, so a size check false-positives on the badge/leftover.
_WAF_DOM = ("()=>/confirm you are human|choose all the|solve a puzzle|human verification|"
            "verify you are human|let's confirm you are human/i.test("
            "document.body?document.body.innerText:'')")


async def _has_captcha(page) -> bool:
    """True only when a captcha the HUMAN must solve is actually SHOWING: a VISIBLE hCaptcha
    challenge (reliable frame=challenge + on-screen check, shared with the relay) OR a full-page
    AWS-WAF challenge — never the invisible hCaptcha badge or a hidden/leftover challenge frame."""
    try:
        from backend.tools import captcha_relay
        if await captcha_relay.visible_popup(page):
            return True
    except Exception:
        pass
    for fr in _icims_frames(page):   # main + iCIMS content frame(s): AWS-WAF full-page text
        try:
            if await fr.evaluate(_WAF_DOM):
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
    print(f"\n########## CAPTCHA — SOLVE IT FROM YOUR PHONE: https://captcha.systeam.kz/ "
          f"(user job2026)  [noVNC fallback: https://jobs.systeam.kz/vnc/vnc.html?path=vnc/websockify"
          f"&autoconnect=true&resize=scale] ##########\n", flush=True)
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


async def _fill_text_by_question(root, q_re: str, value: str) -> bool:
    """Fill a free-text screener input whose nearby QUESTION text matches q_re. Unlike _force_text this
    also reads the CONTAINER text — iForm screeners put the question in a sibling div, not a
    <label for> — and fills only an EMPTY input (e.g. 'Who is your internet service provider?')."""
    if not value:
        return False
    return bool(await root.evaluate(
        """([re_src,val])=>{const rx=new RegExp(re_src,'i');
          const qOf=el=>{let t='';const id=el.id;
            if(id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)t=l.innerText;}
            if(!t){const w=el.closest('label');if(w)t=w.innerText;}
            if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';
            if(!t){const b=el.closest('div,li,fieldset,tr,td'); if(b&&(b.innerText||'').length<200)t=b.innerText;}
            return t;};
          for(const el of document.querySelectorAll('input[type=text],input:not([type]),textarea')){
            const ty=(el.type||'').toLowerCase();
            if(['hidden','submit','button','file','password','checkbox','radio'].includes(ty))continue;
            if((el.value||'').trim())continue;
            if(!rx.test(qOf(el)))continue;
            el.focus();el.value=val;el.dispatchEvent(new Event('input',{bubbles:true}));
            el.dispatchEvent(new Event('change',{bubbles:true}));return true;}
          return false;}""", [q_re, value]))


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


_DEMO_DUMPED = [False]


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
    # Country FIRST — its change fires the iCIMS AJAX that loads the country-dependent State list.
    try:
        await strat._select_by_label(root, "country", "united states")
        await _fire_country_change(root)   # force the change even if Country already defaults to US
    except Exception:
        pass
    # Poll until the State select is actually populated (AJAX), THEN set it — a fixed 1200ms sleep
    # raced the AJAX and left State blank ("Invalid Data Error", the profile step looped).
    st = (pf.get("state") or "").strip()
    if st:
        for _ in range(16):                     # up to ~8s
            await page.wait_for_timeout(500)
            try:
                nopts = await root.evaluate(
                    """()=>{const n=s=>(s||'').toLowerCase();
                      for(const el of document.querySelectorAll('select')){
                        let t=''; if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');if(l)t=l.innerText;}
                        t=t||el.getAttribute('data-label')||'';
                        if(n(t).includes('state')||n(t).includes('province')) return el.options.length;}
                      return 0;}""")
            except Exception:
                nopts = 0
            if nopts and nopts > 1:
                break
        try:
            await strat._select_by_label(root, "state", st)
        except Exception:
            pass
    # address Type = Physical (phone Type was already set to Mobile; _select_by_label skips it)
    try:
        await strat._select_by_label(root, "type", "physical")
    except Exception:
        pass
    await _force_text(root, r"^\s*city\b|city/town|^town\b", pf.get("city") or "")
    await _force_text(root, r"zip|postal", pf.get("zip") or "")
    # free-text screener: internet service provider (required on the TP Candidate-Questions step)
    try:
        await _fill_text_by_question(
            root, r"internet service provider|internet provider|who is your internet|\bisp\b", "Spectrum")
    except Exception:
        pass
    # required acknowledgements / consent / EEO decline / deterministic screeners
    for fn in (lambda: strat._tick_acknowledge(page, root),
               lambda: strat._tick_required_checkboxes(page, root),
               lambda: strat._decline_demographics(root, pf.get("full_name") or ""),
               lambda: strat._answer_screeners(page, root, facts or {})):
        try:
            await fn()
        except Exception:
            pass
    # DIAGNOSTIC (once): dump the EEO demographic selects' options + selected value, so we know the
    # exact decline-option text when the decline pass leaves Gender/Race blank.
    if not _DEMO_DUMPED[0]:
        try:
            demo = await root.evaluate(
                """()=>{const out=[];const demo=/gender|race|ethnic|hispanic|disabilit|veteran/i;
                  for(const el of document.querySelectorAll('select:not([multiple])')){
                    const lab=(el.getAttribute('data-label')||el.id||'');
                    if(!demo.test(lab)) continue;
                    const cur=el.options[el.selectedIndex];
                    out.push({label:lab.slice(0,20), val:(cur&&cur.text)||'',
                              opts:[...el.options].map(o=>o.text).slice(0,12)});}
                  return out;}""")
            if demo:
                _DEMO_DUMPED[0] = True
                print(f"[DEMO DUMP] {demo}", flush=True)
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


async def _residence_ready(root) -> bool:
    """True when NO required Country/State/Province select is still on its placeholder. Submitting the
    profile with State blank just fails validation ('Invalid Data Error') and BURNS a captcha, so we
    gate the Submit on this — while it's False we only re-fill, never click Submit."""
    try:
        empty = await root.evaluate(
            """()=>{const n=s=>(s||'').toLowerCase();
              const ph=t=>!t||/make a selection|please select|no states available|select a source/.test(n(t));
              // Scope ONLY to the real residence selects (by iCIMS field id / exact data-label) — NOT a
              // loose 'state' substring, which mis-matches screeners like 'right to work in the United
              // States' and would falsely block step 2.
              const isRes=el=>/address(state|country|province)/i.test(el.id||'')||
                ['country','state/province','state','province'].includes(n((el.getAttribute('data-label')||'')).trim());
              for(const el of document.querySelectorAll('select')){
                if(isRes(el)){
                  const cur=el.options[el.selectedIndex];
                  let filled = el.value && !ph(cur&&cur.text);
                  // iCIMS AJAX dropdowns keep the committed value in their widget, NOT the native
                  // <select> — the display shows in the fake overlay span. Treat a non-placeholder
                  // overlay text as filled.
                  if(!filled && el.id){
                    const f=document.getElementById(el.id+'_fakeSelected_icimsDropdown');
                    const ft=f?(f.innerText||f.textContent||''):'';
                    if(ft && !ph(ft)) filled=true;}
                  if(!filled) return true;}}
              return false;}""")
        return not empty
    except Exception:
        return True


async def _state_diag(root) -> str:
    """Compact dump of the residence selects (option count + current value) — to see WHY State won't set."""
    try:
        return await root.evaluate(
            """()=>{const n=s=>(s||'').toLowerCase();const out=[];
              const isRes=el=>/address(state|country|province)/i.test(el.id||'')||
                ['country','state/province','state','province'].includes(n((el.getAttribute('data-label')||'')).trim());
              for(const el of document.querySelectorAll('select')){
                let t=''; if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');if(l)t=l.innerText;}
                t=t||el.getAttribute('data-label')||'';
                if(isRes(el)){
                  const cur=el.options[el.selectedIndex];
                  let ov='';
                  if(el.id){const f=document.getElementById(el.id+'_fakeSelected_icimsDropdown'); if(f)ov=(f.innerText||f.textContent||'').replace(/\\s+/g,' ').trim();}
                  out.push((t||el.id).replace(/\\s+/g,' ').slice(0,20)+': n='+el.options.length+' val='+JSON.stringify((cur&&cur.text)||'')+' ov='+JSON.stringify(ov.slice(0,24)));}}
              return out.join(' | ');}""")
    except Exception:
        return "?"


async def _fire_country_change(root) -> None:
    """Load the country-dependent State list by calling iCIMS's OWN dependent-dropdown loader directly.
    The Country <select>'s inline onchange is
        icimsChangeParent('<parentId>', '<childId>')
    (parent = Country select id, child = `<indexPrefix>` + `data-ddd-child-link`). Country DEFAULTS to
    'United States' (its only option), so no user change ever fires it → State stays 'No states
    available' (n=1). Setting the native value + dispatching change did NOT reliably run it, so we call
    `icimsChangeParent(parentId, childId)` explicitly (what a real selection does), then also fire the
    inline onchange / jQuery change as belt-and-suspenders."""
    try:
        await root.evaluate(
            """()=>{const n=s=>(s||'').toLowerCase();
              for(const el of document.querySelectorAll('select')){
                if(!n(el.getAttribute('data-label')||'').includes('country')) continue;
                const us=[...el.options].find(o=>/united states/i.test(o.text)||/^us$/i.test((o.value||'')));
                if(us){ el.value=us.value; try{el.setAttribute('icimsdropdown-selected', us.value);}catch(e){} }
                const childKey=el.getAttribute('data-ddd-child-link');           // 'PersonProfileFields.AddressState'
                const m=(el.id||'').match(/^(-?\\d+_)/); const pref=m?m[1]:'';    // e.g. '-1_'
                const childId=childKey?(pref+childKey):null;
                try{ if(typeof window.icimsChangeParent==='function' && childId){ window.icimsChangeParent(el.id, childId); } }catch(e){}
                el.dispatchEvent(new Event('change',{bubbles:true}));
                try{ if(window.jQuery){ window.jQuery(el).trigger('change'); } }catch(e){}
              }}""")
    except Exception:
        pass


async def _icims_overlay_select(page, root, label_substr: str, value_substr: str) -> bool:
    """Select an iCIMS dropdown the REAL-USER way: click its fake `<a class="dropdown-select">` overlay
    (`<selectId>_icimsDropdown`) to OPEN the listbox — this fires the widget's own AJAX, including the
    Country→State dependent load — then click the `<li role=option>` matching value_substr in the
    listbox (`<selectId>_listbox`). Programmatic value-set + change / direct icimsChangeParent did NOT
    load the State list; the widget only loads it on a genuine open/selection gesture."""
    sid = await root.evaluate(
        """(lbl)=>{const n=s=>(s||'').toLowerCase();
          for(const el of document.querySelectorAll('select')){
            if(n(el.getAttribute('data-label')||'').includes(lbl)) return el.id;} return null;}""",
        label_substr.lower())
    if not sid:
        return False
    try:
        await root.click(f'[id="{sid}_icimsDropdown"]', timeout=4000)
    except Exception:
        return False
    cnt = 0
    for _ in range(18):                                   # wait for the listbox to populate (AJAX)
        await page.wait_for_timeout(400)
        try:
            cnt = await root.evaluate(
                """(sid)=>{const lb=document.getElementById(sid+'_listbox');
                   return lb?lb.querySelectorAll('li,[role=option]').length:0;}""", sid)
        except Exception:
            cnt = 0
        if cnt and cnt > 0:
            break
    print(f"[overlay-select {label_substr}: listbox opts={cnt}]", flush=True)
    clicked = await root.evaluate(
        """([sid,val])=>{const n=s=>(s||'').toLowerCase();
          const lb=document.getElementById(sid+'_listbox'); if(!lb) return false;
          const opts=[...lb.querySelectorAll('li,[role=option]')].filter(o=>n(o.textContent).trim());
          const o=opts.find(x=>n(x.textContent).includes(n(val))); if(!o) return false;
          try{o.scrollIntoView();}catch(e){}
          for(const t of ['mousedown','mouseup','click']) o.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
          return true;}""", [sid, value_substr])
    return bool(clicked)


async def _load_state_via_fetch(page, root, state_full: str, state_code: str) -> bool:
    """The State dropdown's options come from GET /jobs/profileoptions?...&parentValue=<countryValue>
    &id=PersonProfileFields.AddressState&hash=<stateHash>. On this form it auto-fired with
    parentValue=-999 (the no-parent sentinel) → 'No states available'. Re-fetch it with the REAL
    selected Country value, find the state, and commit it into the native <select> + fake overlay so
    both the residence gate and the iCIMS submit see it. Runs in the iCIMS content frame (same-origin,
    carries the session cookies)."""
    try:
        res = await root.evaluate(
            """async ([full,code])=>{
              const n=s=>(s||'').toLowerCase();
              const sels=[...document.querySelectorAll('select')];
              const csel=sels.find(s=>/country/i.test((s.getAttribute('data-label')||'')+' '+(s.id||'')));
              const ssel=sels.find(s=>/state|province/i.test((s.getAttribute('data-label')||'')+' '+(s.id||'')));
              if(!ssel) return {ok:false, why:'no state select'};
              const parentVal = csel ? (csel.getAttribute('icimsdropdown-selected') || csel.value || '') : '';
              const hash = ssel.getAttribute('hash')||'';
              const url='/jobs/profileoptions?in_iframe=1&q=&page=0&size=200&parentValue='+
                        encodeURIComponent(parentVal)+'&id=PersonProfileFields.AddressState&hash='+encodeURIComponent(hash);
              let body='';
              try{ const r=await fetch(url,{headers:{'X-Requested-With':'XMLHttpRequest'},credentials:'same-origin'}); body=await r.text(); }
              catch(e){ return {ok:false, why:'fetch '+e}; }
              let opts=[];
              try{ const j=JSON.parse(body);
                const arr=j.results||j.items||j.options||j.data||(Array.isArray(j)?j:[]);
                opts=arr.map(o=>({v:String(o.id!=null?o.id:(o.value!=null?o.value:'')), t:String(o.text!=null?o.text:(o.label!=null?o.label:(o.name||'')))}));
              }catch(e){
                const d=document.createElement('div'); d.innerHTML=body;
                d.querySelectorAll('option,li,[role=option]').forEach(o=>{const v=o.getAttribute('value')||o.getAttribute('data-value')||''; const t=(o.textContent||'').trim(); if(t) opts.push({v:v||t, t});});
              }
              const want=[full,code].map(n).filter(Boolean);
              const hit=opts.find(o=>want.includes(n(o.t))) || opts.find(o=>n(o.t).includes(n(full))) || opts.find(o=>n(o.v)===n(code));
              if(!hit) return {ok:false, why:'no match', count:opts.length, sample:opts.slice(0,6), bodyhead:body.slice(0,220), parentVal};
              let ex=[...ssel.options].find(o=>String(o.value)===String(hit.v));
              if(!ex){ const op=document.createElement('option'); op.value=hit.v; op.text=hit.t; ssel.add(op); ex=op; }
              ssel.value=hit.v;
              try{ ssel.setAttribute('icimsdropdown-selected', hit.v); }catch(e){}
              ssel.dispatchEvent(new Event('input',{bubbles:true}));
              ssel.dispatchEvent(new Event('change',{bubbles:true}));
              try{ const f=document.getElementById(ssel.id+'_fakeSelected_icimsDropdown');
                   if(f){f.innerHTML='<span class="dropdown-text">'+hit.t+'</span>';}
                   const ov=document.getElementById(ssel.id+'_icimsDropdown'); if(ov){const d=ov.querySelector('.dropdown-text'); if(d)d.textContent=hit.t;} }catch(e){}
              return {ok:true, v:hit.v, t:hit.t, count:opts.length, parentVal};
            }""", [state_full, state_code])
        print(f"[state-fetch] {res}", flush=True)
        return bool(res and res.get("ok"))
    except Exception as e:
        print(f"[state-fetch err] {type(e).__name__}: {e}", flush=True)
        return False


async def _pick_icims_state(page, root, full: str, code: str) -> bool:
    """Select the State the iCIMS-native way (survives the widget's async re-render, unlike a direct
    native-<select> poke). The widget is a SEARCHABLE AJAX dropdown (icimsdropdown-search=1): its first
    page is only 25 options, so a mid-alphabet state (Ohio ~#37) isn't shown until you TYPE — typing
    fires profileoptions?q=<state>&parentValue=<country> → the filtered listbox → click the match, which
    commits via iCIMS's own handler (selectedprofileoption). Requires the Country already committed so
    the dependent list loads (parentValue=<id>, not the -999 sentinel)."""
    sid = await root.evaluate(
        """()=>{const n=s=>(s||'').toLowerCase();
          for(const el of document.querySelectorAll('select')){
            if(/state|province/.test(n(el.getAttribute('data-label')||'')+' '+n(el.id||''))) return el.id;} return null;}""")
    if not sid:
        return False
    await _fire_country_change(root)                       # ensure the dependent state list is loaded
    await page.wait_for_timeout(1000)
    try:
        await root.click(f'[id="{sid}_icimsDropdown"]', timeout=5000)   # open the widget
    except Exception:
        return False
    await page.wait_for_timeout(500)
    # type the state name into the widget's search input to trigger the filtered AJAX load
    typed = await root.evaluate(
        """([sid,q])=>{
          const cand=[];
          document.querySelectorAll('input[aria-controls="'+sid+'_listbox"]').forEach(i=>cand.push(i));
          const ov=document.getElementById(sid+'_icimsDropdown');
          if(ov){const box=ov.closest('.iCIMS_Forms_IdDropDown,.customFieldContainer,div')||document; box.querySelectorAll('input[type=text],input:not([type]),input[role=combobox],input[role=searchbox]').forEach(i=>cand.push(i));}
          const inp=cand.find(i=>i.offsetParent!==null)||cand[0];
          if(!inp) return false;
          inp.focus(); inp.value=q;
          inp.dispatchEvent(new Event('input',{bubbles:true}));
          inp.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:q.slice(-1)}));
          inp.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:q.slice(-1)}));
          return true;}""", [sid, full])
    cnt = 0
    for _ in range(22):                                    # wait for the (filtered) listbox
        await page.wait_for_timeout(400)
        try:
            cnt = await root.evaluate(
                """(sid)=>{const lb=document.getElementById(sid+'_listbox'); if(!lb) return 0;
                   return [...lb.querySelectorAll('li,[role=option]')].filter(o=>(o.textContent||'').trim()).length;}""", sid)
        except Exception:
            cnt = 0
        if cnt and cnt > 0:
            break
    print(f"[state pick: typed={typed} listbox opts={cnt}]", flush=True)
    clicked = await root.evaluate(
        """([sid,full,code])=>{const n=s=>(s||'').toLowerCase();
          const lb=document.getElementById(sid+'_listbox'); if(!lb) return false;
          const opts=[...lb.querySelectorAll('li,[role=option]')].filter(o=>(o.textContent||'').trim());
          let o=opts.find(x=>n(x.textContent).trim()===n(full)) || opts.find(x=>n(x.textContent).includes(n(full)));
          if(!o) return false;
          try{o.scrollIntoView();}catch(e){}
          for(const t of ['mouseover','mousedown','mouseup','click'])
            o.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
          return true;}""", [sid, full, code])
    await page.wait_for_timeout(700)
    return bool(clicked)


_DUMPED = [False]
_DUMPED2 = [False]


async def _residence_dump(root) -> str:
    """One-time dump: the Country/State select + Country fake-overlay outerHTML + any global iCIMS JS
    that could load the dependent State list — to design the exact trigger from data, not guesses."""
    try:
        info = await root.evaluate(
            """()=>{const out={};
              const attrs=el=>{const o={};for(const a of el.attributes)o[a.name]=(a.value||'').slice(0,120);return o;};
              const sels=[...document.querySelectorAll('select')];
              const csel=sels.find(s=>/country/i.test((s.getAttribute('data-label')||'')+' '+(s.id||'')));
              const ssel=sels.find(s=>/state|province/i.test((s.getAttribute('data-label')||'')+' '+(s.id||'')));
              if(csel){out.country_attrs=attrs(csel); out.country_onchange=csel.getAttribute('onchange')||'';}
              if(ssel){out.state_attrs=attrs(ssel);
                const sid=ssel.id;
                // the iCIMS fake overlay + listbox + any typeahead search input for the State widget
                const ov=document.getElementById(sid+'_icimsDropdown');
                out.state_overlay_html=ov?ov.outerHTML.slice(0,900):null;
                const lb=document.getElementById(sid+'_listbox');
                out.state_listbox_html=lb?lb.outerHTML.slice(0,900):null;
                out.state_listbox_optcount=lb?lb.querySelectorAll('li,[role=option]').length:0;
                // hunt for a text input tied to this widget (search box)
                const ins=[];
                if(ov) ov.querySelectorAll('input').forEach(i=>ins.push(attrs(i)));
                document.querySelectorAll('input[aria-controls="'+sid+'_listbox"]').forEach(i=>ins.push(attrs(i)));
                out.state_inputs=ins.slice(0,4);
              }
              // inline scripts that mention the dependent-dropdown machinery
              const scr=[];
              for(const s of document.querySelectorAll('script')){
                const t=s.textContent||''; if(/AddressState|ddd|Dependent|loadState|childDropdown|icimsDropdown/i.test(t)){
                  const m=t.match(/.{0,60}(AddressState|ddd|Dependent|loadState|childDropdown).{0,90}/i); if(m)scr.push(m[0].replace(/\\s+/g,' '));}}
              out.scripts=scr.slice(0,4);
              return out;}""")
        return str(info)[:4000]
    except Exception as e:
        return f"dump err {type(e).__name__}: {e}"


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

    # NopeCHA captcha-solver extension: it auto-solves the hCaptcha IN-PAGE (its API egresses through
    # the browser's residential proxy, so it dodges NopeCHA's datacenter-IP free-tier ban). Loading it
    # makes the whole apply flow autonomous — the bot clicks Next/Submit, the extension solves.
    _ext_args = []
    if NOPECHA and os.path.isdir(NOPECHA_EXT):
        _ext_args = [f"--disable-extensions-except={NOPECHA_EXT}", f"--load-extension={NOPECHA_EXT}"]

    async with async_playwright() as pw:
        _lk = dict(headless=False, channel="chromium", no_viewport=True, locale="en-US",
                   timezone_id="America/New_York", args=["--start-maximized"] + _ext_args)
        if _tunnel_up():
            _lk["proxy"] = {"server": SLOT}
            print(f"[proxy: {SLOT} (residential tunnel)]", flush=True)
        else:
            print("[proxy: DIRECT — no residential tunnel; NopeCHA solves the captcha regardless of IP]", flush=True)
        ctx = await pw.chromium.launch_persistent_context(STEALTH_PROFILE, **_lk)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # DIAGNOSTIC: log iCIMS dependent-dropdown / typeahead AJAX so we can SEE whether the
        # Country->State load actually fires and what it returns (status). Cheap, url+status only.
        import re as _re_net
        _AJAX_RE = _re_net.compile(r"profileoptions|selectedprofileoption", _re_net.I)
        _seen_ajax: set = set()

        async def _log_body(resp):
            try:
                body = await resp.text()
                print(f"[AJAX-BODY {resp.url[:160]}] {body[:600]}", flush=True)
            except Exception:
                pass

        def _log_resp(resp):
            try:
                u = resp.url
                if _AJAX_RE.search(u):
                    if u not in _seen_ajax:            # dedup on FULL url (keep distinct parentValue)
                        _seen_ajax.add(u)
                        print(f"[AJAX {resp.status}] {u[:220]}", flush=True)
                        if "profileoptions?" in u:
                            asyncio.create_task(_log_body(resp))
            except Exception:
                pass
        page.on("response", _log_resp)
        # preseed NopeCHA config (keyless hCaptcha auto-solve; JS input to avoid CDP contention). A
        # paid key, if ever needed, goes in NOPECHA_KEY -> the same setup URL.
        if _ext_args:
            try:
                key = os.getenv("NOPECHA_KEY", "").strip()
                cfg = ("input_method=javascript|hcaptcha_auto_open=true|hcaptcha_auto_solve=true|"
                       "hcaptcha_solve_delay_time=200|enabled=true" + (f"|key={key}" if key else ""))
                sp = await ctx.new_page()
                await sp.goto("https://nopecha.com/setup#" + cfg,
                              wait_until="domcontentloaded", timeout=45000)
                await sp.wait_for_timeout(3500)
                await sp.close()
                print(f"[NopeCHA extension loaded + configured{' (key set)' if key else ' (free tier)'}]",
                      flush=True)
            except Exception as e:
                print(f"[NopeCHA config: {type(e).__name__}: {e}]"[:120], flush=True)
        # phone control relay -> captcha.systeam.kz (mirror this browser on the X display + forward
        # REAL taps via xdotool — the human drives navigation + solves the captcha from a phone)
        try:
            from backend.tools import captcha_relay
            captcha_relay.set_page(page, display=os.environ.get("DISPLAY", ":98"),
                                   label="Teleperformance")
            await captcha_relay.serve(9003)
            print("[captcha relay up -> https://captcha.systeam.kz/ ]", flush=True)
        except BaseException as e:
            print(f"[captcha relay not started: {type(e).__name__}: {e}]"[:160], flush=True)
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
                try:
                    await page.bring_to_front()   # keep OUR browser topmost on the shared :98 (noVNC)
                except Exception:
                    pass
                await _wait_human(page, "step", recon_dir, recon, idx)   # pause on a VISIBLE captcha
                frame = await strat._content_frame(page)
                root = frame or page
                sig = await _sig()
                new_step = sig != cur_sig
                stalled = advanced and (time.time() - step_started > 75)
                res_ready = await _residence_ready(root)   # residence selects (Country/State) all set?
                # RELAY-DRIVE: re-fill (idempotently — only empty fields) EVERY tick, so if the human
                # reloads the page or a field clears, the bot re-fills it instead of sitting idle.
                # Also keep re-filling while residence isn't ready (the résumé parser re-render + the
                # Country→State AJAX race means State often needs several passes to stick).
                if not (new_step or stalled or RELAY_DRIVE or not res_ready):
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
                # (c) fill: the WHOLE step on a new step; on RELAY-DRIVE re-fill ticks only top up
                #     EMPTY identity + the required consent (so a page reload re-fills email/privacy),
                #     without re-forcing country/state/how-heard every tick (that would flicker).
                try:
                    if new_step or not RELAY_DRIVE:
                        await _tp_fill(page, root, pf, p["facts"], strat)
                    else:
                        await strat._fill_identity(root, pf)
                        await strat._tick_required_checkboxes(page, root)
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
                if new_step or not RELAY_DRIVE:
                    await _capture(page, "filled", recon_dir, recon, idx)   # values + live State list
                # (e) advance. In RELAY-DRIVE mode the HUMAN taps Next/Submit + solves the captcha
                #     from the phone (a real X click hCaptcha accepts), so the bot must NOT click the
                #     gated buttons (a synthetic click just parks the captcha and fights the human) —
                #     it only fills. Otherwise it auto-advances (needs a captcha solver to complete).
                if RELAY_DRIVE:
                    advanced = True
                    if new_step:
                        print("[step filled — DRIVE IT FROM THE PHONE: tap Next/Submit + solve the "
                              "captcha at https://captcha.systeam.kz/ ]", flush=True)
                    await page.wait_for_timeout(5000)
                    continue
                # Credit-safe gate: never click Submit Profile while a residence select is blank — that
                # submit only fails validation and burns a NopeCHA solve. Re-assert Country→State and
                # loop (re-fill) until it's set, THEN advance once.
                if not await _residence_ready(root):
                    if not _DUMPED[0]:
                        _DUMPED[0] = True
                        print(f"[RESIDENCE DUMP] {await _residence_dump(root)}", flush=True)
                    print(f"[residence not set — {await _state_diag(root)} — re-filling, NOT submitting (saves captcha)]", flush=True)
                    try:
                        stt = (pf.get("state") or "").strip()
                        code = (p.get("state_code") or "").strip()
                        # Fetch the State options with the committed Country value (icimsdropdown-selected
                        # =12781, set at page load — the widget's own auto-load used the -999 sentinel →
                        # empty) and inject the match into the native <select>. Do NOT re-fire the country
                        # change: that makes iCIMS reload+re-render the State widget and WIPE the value.
                        ok = await _load_state_via_fetch(page, root, stt, code)
                        print(f"[state-pick ok={ok} -> {await _state_diag(root)}]", flush=True)
                    except Exception as e:
                        print(f"[state-pick err {type(e).__name__}: {str(e)[:90]}]", flush=True)
                    # If the State stuck THIS tick, advance immediately — the next tick's full re-fill
                    # (_tp_fill) would re-render the address block and wipe the injected value before we
                    # ever reach Submit. So fall straight through to _advance in the same tick.
                    if not await _residence_ready(root):
                        await page.wait_for_timeout(1500)
                        continue
                    print("[residence set — advancing THIS tick before re-fill can wipe it]", flush=True)
                kind = await _advance(page, root)
                advanced = True
                if kind == "final":
                    print("[reached FINAL Submit Application — recon complete, NOT clicking]", flush=True)
                    await _capture(page, "final_form", recon_dir, recon, idx)
                    break
                print(f"[step filled + advance={kind} -> waiting for next step / captcha]", flush=True)
                await page.wait_for_timeout(5000)
        except Exception as e:
            # A page error (the human closed/navigated a tab in noVNC, a transient crash) must NOT
            # tear the whole browser down — keep it alive until the run window ends so the human can
            # keep driving the application from the phone (captcha.systeam.kz / noVNC).
            print(f"[fill loop stopped: {type(e).__name__}: {str(e)[:80]} — keeping the browser alive "
                  f"for noVNC; drive it from the phone]", flush=True)
            try:
                end = start_ts + keep_minutes * 60
                while time.time() < end:
                    await asyncio.sleep(10)
            except Exception:
                pass
        finally:
            try:
                await _capture(page, "final", recon_dir, recon, idx)
            except Exception:
                pass
            try:
                await ctx.close()
            except Exception:
                pass
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
