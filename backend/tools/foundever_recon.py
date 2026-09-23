"""Driver: auto-apply to one Foundever (SuccessFactors careersection) job end-to-end.

Foundever's `jobs.foundever.com` postings hand off (via the RMK "Apply now" dropdown → manual apply,
same tab) to the SuccessFactors careersection at `career4.successfactors.com/careers?company=SitelPROD`,
which renders the whole application on ONE page — NO captcha, NO résumé upload. `SuccessFactorsStrategy`
fills it; the real Submit is gated by env **FOUNDEVER_ADVANCE=1** (default OFF → a plain fill is
side-effect-free: no account, no PII transmitted).

Each run: a fresh synthetic US persona PLACED IN THE JOB'S STATE (so the "reside in <state>" screener
answers truthfully), a per-job isolated Chromium profile, navigate to the RMK apply URL, drive the fill,
and — under FOUNDEVER_ADVANCE — submit and wait for the SuccessFactors "application received" auto-reply
in the persona @takhet.com Maildir (the ground truth of success).

    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        FOUNDEVER_ADVANCE=1 python3 -m backend.tools.foundever_recon --job <mass_hiring_id> --fresh'

Headless via FOUNDEVER_HEADLESS=1 (the careersection is captcha-free, so headless works — the cron uses
it). Run under `sg mail` (mailbox provisioning + the Maildir confirmation read need the mail group)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"

# A synthetic persona can't hold a real state insurance license -> skip licensed roles (same policy as
# the TTEC lane: never attach a fabricated license/diploma).
_LICENSED_RE = re.compile(r"\blicensed\b|\blicense\b|\bP&C\b|property\s*&?\s*casualty|life\s*&?\s*health",
                          re.I)

# state-full -> (city, zip) so the persona's address is coherent with the state named in the posting
# (the residence screener only checks the STATE; a plausible in-state city/zip keeps the form honest).
_STATE_PLACE = {
    "Alabama": ("Birmingham", "35203"), "Alaska": ("Anchorage", "99501"),
    "Arizona": ("Phoenix", "85004"), "Arkansas": ("Little Rock", "72201"),
    "California": ("Sacramento", "95814"), "Colorado": ("Denver", "80202"),
    "Connecticut": ("Hartford", "06103"), "Delaware": ("Wilmington", "19801"),
    "District of Columbia": ("Washington", "20001"), "Florida": ("Orlando", "32801"),
    "Georgia": ("Atlanta", "30303"), "Hawaii": ("Honolulu", "96813"),
    "Idaho": ("Boise", "83702"), "Illinois": ("Chicago", "60602"),
    "Indiana": ("Indianapolis", "46204"), "Iowa": ("Des Moines", "50309"),
    "Kansas": ("Wichita", "67202"), "Kentucky": ("Louisville", "40202"),
    "Louisiana": ("Baton Rouge", "70802"), "Maine": ("Portland", "04101"),
    "Maryland": ("Baltimore", "21201"), "Massachusetts": ("Boston", "02108"),
    "Michigan": ("Detroit", "48226"), "Minnesota": ("Minneapolis", "55401"),
    "Mississippi": ("Jackson", "39201"), "Missouri": ("Kansas City", "64106"),
    "Montana": ("Billings", "59101"), "Nebraska": ("Omaha", "68102"),
    "Nevada": ("Las Vegas", "89101"), "New Hampshire": ("Manchester", "03101"),
    "New Jersey": ("Newark", "07102"), "New Mexico": ("Albuquerque", "87102"),
    "New York": ("Albany", "12207"), "North Carolina": ("Charlotte", "28202"),
    "North Dakota": ("Fargo", "58102"), "Ohio": ("Columbus", "43215"),
    "Oklahoma": ("Oklahoma City", "73102"), "Oregon": ("Portland", "97204"),
    "Pennsylvania": ("Philadelphia", "19103"), "Rhode Island": ("Providence", "02903"),
    "South Carolina": ("Columbia", "29201"), "South Dakota": ("Sioux Falls", "57104"),
    "Tennessee": ("Nashville", "37203"), "Texas": ("Austin", "78701"),
    "Utah": ("Salt Lake City", "84101"), "Vermont": ("Burlington", "05401"),
    "Virginia": ("Richmond", "23219"), "Washington": ("Seattle", "98104"),
    "West Virginia": ("Charleston", "25301"), "Wisconsin": ("Milwaukee", "53202"),
    "Wyoming": ("Cheyenne", "82001"), "Puerto Rico": ("San Juan", "00901"),
}
_DEFAULT_STATE = "Texas"


def is_licensed(title: str) -> bool:
    return bool(_LICENSED_RE.search(title or ""))


def _state_from_row(title: str, location_raw: str) -> str:
    """Full state name the posting is tied to. Foundever locations are 'Remote, <State|Any Location>,
    US' and titles often end '- <State>'. Returns '' for 'Any Location' / none (any US state fits)."""
    from backend.tools.synth_persona import _us_state_full
    loc = location_raw or ""
    parts = [p.strip() for p in loc.split(",")]
    if len(parts) >= 2 and parts[1].lower() not in ("any location", "any", "united states", "us", "usa"):
        # the middle token is the state (may be a name or a slightly-misspelled name); resolve exact
        full = _us_state_full(parts[1])
        if full:
            return full
        # tolerate the collector's 'Conneticut' typo etc. via a loose prefix match
        low = parts[1].lower().replace(" ", "")
        for st in _STATE_PLACE:
            if st.lower().replace(" ", "").startswith(low[:5]) and len(low) >= 4:
                return st
    # try the title tail '- <State>'
    m = re.search(r"[-–]\s*([A-Za-z][A-Za-z .]+?)\s*$", title or "")
    if m:
        full = _us_state_full(m.group(1).strip())
        if full:
            return full
    return ""


def _build_persona(row: dict) -> dict:
    """Fresh synthetic US persona for this Foundever job, PLACED in the job's state. Provisions the
    mailbox + registers the demo persona (so a Foundever reply lands in a CRM-visible box), writes the
    prefill dir, and returns the profile_form dict the strategy fills."""
    from backend.tools import mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    state = _state_from_row(row.get("title") or "", row.get("location_raw") or "") or _DEFAULT_STATE
    city, zc = _STATE_PLACE.get(state, ("Austin", "78701"))
    job = {"title": row.get("title") or "", "company": "Foundever",
           "company_key": "foundever", "description": "",
           "category": row.get("category") or "",
           "location": f"{city}, {state}, United States", "regions": ["US"],
           "ats": "foundever", "external_id": str(row.get("id") or ""),
           "url": row.get("apply_url") or "", "questions": []}

    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job)
    prof = cand["profile"]
    # place the persona in the job's state (residence screener truthfulness)
    prof["city"] = city
    prof["state"] = state
    prof["location"] = f"{city}, {state}"
    pi = (prof.get("resume") or {}).get("personal_info")
    if isinstance(pi, dict):
        pi["location"] = f"{city}, {state}"

    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""),
                                      prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[foundever] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    # MANDATORY attractiveness pass via the shared engine (role-targeted, no-fabrication, guarded:
    # a tailor/LLM failure falls back to the base résumé — never empty, never raises). The SF form
    # has no résumé field, but generate + persist it so the CRM/stats artifacts match the lanes.
    from backend.tools import mass_hiring_apply as _mha
    d = _mha.tailored_draft(job, cand)
    out.joinpath("resume.pdf").write_bytes(_mha.resume_pdf_bytes(d, cand))
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": "foundever", "mass_hiring_id": row["id"]}),
        encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": job["url"], "job_title": job["title"], "company": "Foundever",
                    "profile": profile_id, "submitted": False}, indent=2), encoding="utf-8")

    name = prof.get("full_name") or ""
    parts = name.split()
    profile_form = {
        "full_name": name,
        "first_name": prof.get("first_name") or (parts[0] if parts else ""),
        "last_name": prof.get("last_name") or (parts[-1] if len(parts) > 1 else ""),
        "email": prof.get("email") or "", "phone": prof.get("phone") or "",
        "street_address": prof.get("street_address") or "1200 Market Street",
        "address": prof.get("street_address") or "1200 Market Street",
        "city": city, "state": state, "zip": zc, "postal_code": zc, "country": "United States",
    }
    return {"profile_form": profile_form, "facts": cand.get("facts") or {},
            "resume_path": str(out / "resume.pdf"), "state": state,
            "jobid": jobid, "profile_id": profile_id}


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once a SuccessFactors/Foundever confirmation email has landed in the persona's Maildir
    (received at/after since_ts). For this careersection the submit creates the candidate account AND
    submits the application in one step, so the mailbox receipt is the SF 'Welcome to Foundever's
    Career Portal' account email (from system@successfactors.com) — which is only ever sent after a
    successful submit; a per-job 'application received' email is also matched if the tenant sends one."""
    local = (email or "").split("@", 1)[0]
    if not local:
        return False
    for sub in ("new", "cur"):
        d = os.path.join(MAILROOT, local, sub)
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for n in names:
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < since_ts - 30:
                    continue
                with open(p, "rb") as f:
                    head = f.read(6000).decode("utf-8", "ignore")
            except Exception:
                continue
            subj = re.search(r"^Subject:.*$", head, re.I | re.M)
            frm = re.search(r"^From:.*$", head, re.I | re.M)
            s = (subj.group(0).lower() if subj else "") + " " + (frm.group(0).lower() if frm else "")
            if ("successfactors.com" in s or "sitelprod" in s
                    or "thank you for applying" in s or "received your application" in s
                    or "application received" in s or "application has been received" in s
                    or "thank you for your application" in s or "your application to foundever" in s
                    or "career portal" in s or ("foundever" in s and ("appl" in s or "welcome" in s))):
                return True
    return False


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, location_raw FROM mass_hiring_jobs "
                    "WHERE id=%s AND source='foundever'", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no foundever mass_hiring_jobs row id={job_id}", flush=True)
        return
    row = {"id": r[0], "title": r[1], "apply_url": r[2], "location_raw": r[3]}
    print(f"=== Foundever apply: job {row['id']} — {row['title']}", flush=True)
    if is_licensed(row["title"]):
        print("[skip: licensed role — a synthetic persona can't hold a real state license]", flush=True)
        return

    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state']} "
          f"| FOUNDEVER_ADVANCE={os.getenv('FOUNDEVER_ADVANCE', '')}", flush=True)

    profile_dir = os.getenv("FOUNDEVER_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"foundever_prof_{job_id}_{os.getpid()}")
    if fresh:
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    headless = os.getenv("FOUNDEVER_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    from playwright.async_api import async_playwright

    from backend.applier.strategies.foundever import SuccessFactorsStrategy

    start_ts = time.time()
    async with async_playwright() as pw:
        # Egress: DIRECT by DEFAULT (Foundever reaches a real ack from the datacenter IP; the connected
        # phones are KZ residential = a geo-mismatch for a US application, and slow/flaky). Residential
        # is OPT-IN: FOUNDEVER_RESIDENTIAL=1 → a live phone slot (Sutherland Mac excluded), else DIRECT;
        # FOUNDEVER_PROXY=<url> → that exact proxy (e.g. a US slot). Guarded — never breaks a fill.
        _lk = dict(headless=headless, channel="chromium", no_viewport=not headless,
                   locale="en-US", timezone_id="America/New_York",
                   args=["--no-sandbox"] if headless else ["--no-sandbox", "--start-maximized"])
        try:
            from backend.tools import proxy_pool
            _px = proxy_pool.lane_egress("FOUNDEVER_RESIDENTIAL", "FOUNDEVER_PROXY", str(os.getpid()))
        except Exception:
            _px = None
        if _px:
            _lk["proxy"] = _px
            print(f"[egress: {_px['server']} (phone/residential)]", flush=True)
        ctx = await pw.chromium.launch_persistent_context(profile_dir, **_lk)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        shot_dir = os.path.join(REPO, "logs", "foundever_recon", str(job_id))
        os.makedirs(shot_dir, exist_ok=True)
        _n = [0]

        async def _shot(tag):
            _n[0] += 1
            try:
                await page.screenshot(path=os.path.join(shot_dir, f"{_n[0]:02d}_{tag}.png"),
                                      full_page=True)
                print(f"[shot {_n[0]:02d} {tag}] url={page.url[:90]}", flush=True)
            except Exception as e:
                print(f"[shot {tag} err: {type(e).__name__}]", flush=True)

        try:
            await page.goto(row["apply_url"], wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            await _shot("rmk_job")
            strat = SuccessFactorsStrategy()
            result = await strat.prefill(
                page, pf, p["resume_path"],
                job={"title": row["title"], "company": "Foundever"},
                draft=True, facts=p["facts"], profile_id=p["profile_id"])
            await _shot("after_fill")
            print(f"[filled={result.get('filled')} unfilled={result.get('unfilled')} "
                  f"page_type={result.get('page_type')} submitted={result.get('submitted')} "
                  f"note={result.get('note', '')}]", flush=True)
            await _shot("final")
            # On-page "Your Application has been sent. Thank you!" is the primary ground truth.
            if result.get("submitted"):
                print("[application SUBMITTED — on-page Foundever confirmation]", flush=True)
            deadline = start_ts + keep_minutes * 60
            confirmed = False
            while time.time() < deadline:
                if _app_confirmed(pf["email"], start_ts - 60):
                    confirmed = True
                    print("[application CONFIRMED — SuccessFactors receipt in the Maildir]", flush=True)
                    break
                if not result.get("submitted"):
                    # fill didn't reach the confirmation page — no point waiting for a receipt
                    break
                await asyncio.sleep(10)
            if not confirmed and not result.get("submitted"):
                print("[no confirmation within --keep (expected if FOUNDEVER_ADVANCE is off, or the "
                      "SF ack lags / a field blocked submit — read the shots + unfilled)]", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:180]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
    print("=== foundever apply done", flush=True)


def foundever_job_ids() -> list[int]:
    """Active Foundever jobs we can honestly staff (excludes licensed-insurance roles)."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs WHERE source='foundever' AND active "
                    "ORDER BY id")
        for jid, title in cur.fetchall():
            if is_licensed(title):
                continue
            out.append(jid)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=foundever)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list auto-applyable Foundever job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = foundever_job_ids()
        print(f"{len(ids)} auto-applyable Foundever jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
