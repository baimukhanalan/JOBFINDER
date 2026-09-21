"""Driver: auto-apply to one Gainwell Technologies (SuccessFactors careersection) job end-to-end.

Gainwell's `jobs.gainwelltechnologies.com` postings are the SAME SuccessFactors Recruiting Marketing
(RMK / Jobs2Web) family as Foundever's `jobs.foundever.com`: the "Apply now" dropdown's manual-apply
option hands off (same tab) to the SuccessFactors **careersection** at
`career41.sapsf.com/careers?company=gainwellte` (Foundever's is `career4.successfactors.com`,
company `SitelPROD`), which renders the whole application on ONE page — NO captcha, NO résumé upload.
`SuccessFactorsStrategy` (shared with Foundever) fills it; the real Submit is gated by env
**GAINWELL_ADVANCE=1** (default OFF → a plain fill is side-effect-free: no account, no PII transmitted).

Each run: a fresh synthetic US persona PLACED IN THE JOB'S STATE (so the "reside in <state>" screener
answers truthfully), a per-job isolated Chromium profile, navigate to the RMK apply URL, drive the fill,
and — under GAINWELL_ADVANCE — submit and wait for the SuccessFactors "application received" / account
email in the persona @takhet.com Maildir (the ground truth of success).

    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        GAINWELL_ADVANCE=1 python3 -m backend.tools.gainwell_recon --job <mass_hiring_id> --fresh'

Headless via GAINWELL_HEADLESS=1 (the careersection is captcha-free, so headless works — the cron uses
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
# Reuse the pure helpers from the Foundever lane verbatim — the state-placement dict, the licensed-role
# skip, and the state-from-row resolver all work unchanged on Gainwell's "<city>, <state>, US, <zip>"
# location (its parts[1] 2-letter code resolves via _us_state_full exactly like Foundever's state name).
from backend.tools.foundever_recon import (  # noqa: E402
    _STATE_PLACE, _DEFAULT_STATE, _state_from_row, is_licensed)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"

COMPANY = "Gainwell Technologies"
COMPANY_KEY = "gainwell"


def _gainwell_advance() -> bool:
    return os.getenv("GAINWELL_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _build_persona(row: dict) -> dict:
    """Fresh synthetic US persona for this Gainwell job, PLACED in the job's state. Provisions the
    mailbox + registers the demo persona (so a Gainwell reply lands in a CRM-visible box), writes the
    prefill dir, and returns the profile_form dict the strategy fills."""
    from backend.tools import catalog_drafts, drafts_ui, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    state = _state_from_row(row.get("title") or "", row.get("location_raw") or "") or _DEFAULT_STATE
    city, zc = _STATE_PLACE.get(state, ("Austin", "78701"))
    job = {"title": row.get("title") or "", "company": COMPANY,
           "company_key": COMPANY_KEY, "description": "",
           "location": f"{city}, {state}, United States", "regions": ["US"],
           "ats": "gainwell", "external_id": str(row.get("id") or ""),
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
        print(f"[gainwell] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    # a tailored résumé isn't uploaded (the SF form has no résumé field) but generate + persist it so
    # the CRM/stats artifacts match the other lanes; never fatal.
    try:
        d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)
        out.joinpath("resume.pdf").write_bytes(drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")
    except Exception as e:  # noqa: BLE001
        print(f"[gainwell] resume gen skipped: {type(e).__name__}: {e}", flush=True)
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": "gainwell", "mass_hiring_id": row["id"]}),
        encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": job["url"], "job_title": job["title"], "company": COMPANY,
                    "profile": profile_id, "submitted": False}, indent=2), encoding="utf-8")

    name = prof.get("full_name") or ""
    parts = name.split()
    # current employer/title for the portalcareer 'Current Company' / 'Current Title' required fields
    # (read off the synthetic résumé; the strategy falls back to generic CSR defaults if absent).
    _resume = prof.get("resume") or {}
    _exp = (_resume.get("experience") or []) if isinstance(_resume, dict) else []
    _e0 = _exp[0] if _exp and isinstance(_exp[0], dict) else {}
    cur_company = (_e0.get("company") or "").strip() or "Self-Employed"
    cur_title = ((_resume.get("headline") if isinstance(_resume, dict) else "")
                 or _e0.get("title") or "").strip()
    cur_title = re.sub(r"\s*[-–—]\s*Remote\b.*$", "", cur_title).strip() \
        or "Customer Service Representative"
    profile_form = {
        "full_name": name,
        "first_name": prof.get("first_name") or (parts[0] if parts else ""),
        "last_name": prof.get("last_name") or (parts[-1] if len(parts) > 1 else ""),
        "email": prof.get("email") or "", "phone": prof.get("phone") or "",
        "street_address": prof.get("street_address") or "1200 Market Street",
        "address": prof.get("street_address") or "1200 Market Street",
        "city": city, "state": state, "zip": zc, "postal_code": zc, "country": "United States",
        "current_company": cur_company, "current_title": cur_title,
    }
    return {"profile_form": profile_form, "facts": cand.get("facts") or {},
            "resume_path": str(out / "resume.pdf"), "state": state,
            "jobid": jobid, "profile_id": profile_id}


# A genuine per-JOB application receipt — NOT the account-creation welcome or the OTP email (both of
# which Gainwell's two-step flow sends BEFORE the application is submitted, so matching them would
# falsely confirm a fill that never reached Apply). The on-page "successfully applied" (the strategy's
# report["submitted"]) is the primary truth; this Maildir check only corroborates a real job receipt.
_APP_RECEIPT_RE = re.compile(
    r"thank you for applying|received your application|application received|"
    r"application has been received|thank you for your application|"
    r"your application (to|for)\b|you (have )?successfully applied|"
    r"application (was )?successfully (submitted|received)", re.I)


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once a genuine per-JOB application-receipt email has landed in the persona's Maildir
    (received at/after since_ts). Deliberately does NOT match the SF account-creation welcome email or
    the account-verification passcode email — those are sent during the (pre-submit) account step, so
    matching them would falsely confirm an application that never reached Apply."""
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
                    head = f.read(8000).decode("utf-8", "ignore")
            except Exception:
                continue
            subj = re.search(r"^Subject:.*$", head, re.I | re.M)
            s = subj.group(0) if subj else ""
            if _APP_RECEIPT_RE.search(s):
                return True
    return False


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, location_raw FROM mass_hiring_jobs "
                    "WHERE id=%s AND source='gainwell'", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no gainwell mass_hiring_jobs row id={job_id}", flush=True)
        return
    row = {"id": r[0], "title": r[1], "apply_url": r[2], "location_raw": r[3]}
    print(f"=== Gainwell apply: job {row['id']} — {row['title']}", flush=True)
    if is_licensed(row["title"]):
        print("[skip: licensed role — a synthetic persona can't hold a real state license]", flush=True)
        return

    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state']} "
          f"| GAINWELL_ADVANCE={os.getenv('GAINWELL_ADVANCE', '')}", flush=True)

    profile_dir = os.getenv("GAINWELL_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"gainwell_prof_{job_id}_{os.getpid()}")
    if fresh:
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    headless = os.getenv("GAINWELL_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    from playwright.async_api import async_playwright

    from backend.applier.strategies.foundever import SuccessFactorsStrategy

    start_ts = time.time()
    async with async_playwright() as pw:
        # Egress: DIRECT by DEFAULT (the SF careersection reaches a real ack from the datacenter IP; the
        # connected phones are KZ residential = a geo-mismatch for a US application, and slow/flaky).
        # Residential is OPT-IN: GAINWELL_RESIDENTIAL=1 → a live phone slot (Sutherland Mac excluded),
        # else DIRECT; GAINWELL_PROXY=<url> → that exact proxy (e.g. a US slot). Guarded — never breaks.
        _lk = dict(headless=headless, channel="chromium", no_viewport=not headless,
                   locale="en-US", timezone_id="America/New_York",
                   args=["--no-sandbox"] if headless else ["--no-sandbox", "--start-maximized"])
        try:
            from backend.tools import proxy_pool
            _px = proxy_pool.lane_egress("GAINWELL_RESIDENTIAL", "GAINWELL_PROXY", str(os.getpid()))
        except Exception:
            _px = None
        if _px:
            _lk["proxy"] = _px
            print(f"[egress: {_px['server']} (phone/residential)]", flush=True)
        ctx = await pw.chromium.launch_persistent_context(profile_dir, **_lk)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        shot_dir = os.path.join(REPO, "logs", "gainwell_recon", str(job_id))
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
            # per-lane submit gate: GAINWELL_ADVANCE (the shared strategy's class attr reads
            # FOUNDEVER_ADVANCE, so pin the instance's advance from this lane's env).
            strat.advance = _gainwell_advance()
            result = await strat.prefill(
                page, pf, p["resume_path"],
                job={"title": row["title"], "company": COMPANY},
                draft=True, facts=p["facts"], profile_id=p["profile_id"])
            await _shot("after_fill")
            print(f"[filled={result.get('filled')} unfilled={result.get('unfilled')} "
                  f"page_type={result.get('page_type')} submitted={result.get('submitted')} "
                  f"note={result.get('note', '')}]", flush=True)
            await _shot("final")
            # The on-page confirmation (the application form is REPLACED by the "Back to Job Listings"
            # page) is the ground truth for this careersection — it sends NO per-job "application
            # received" email (only the account-creation "Account Created" mail), so a submit is CONFIRMED
            # by result["submitted"]. We still briefly poll the Maildir to corroborate if the tenant ever
            # sends a per-job receipt, but never block the full --keep on an email that won't come.
            submitted = bool(result.get("submitted"))
            if submitted:
                print("[application SUBMITTED — on-page Gainwell confirmation]", flush=True)
            deadline = start_ts + keep_minutes * 60
            # when already submitted on-page, only spend a short corroboration window
            soft_deadline = time.time() + (90 if submitted else keep_minutes * 60)
            confirmed = False
            while time.time() < min(deadline, soft_deadline):
                if _app_confirmed(pf["email"], start_ts - 60):
                    confirmed = True
                    print("[application CONFIRMED — Gainwell receipt in the Maildir]", flush=True)
                    break
                if not submitted:
                    # fill didn't reach the confirmation page — no point waiting for a receipt
                    break
                await asyncio.sleep(10)
            if not submitted:
                print("[no confirmation within --keep (expected if GAINWELL_ADVANCE is off, or the "
                      "SF ack lags / a field blocked submit — read the shots + unfilled)]", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:180]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
    print("=== gainwell apply done", flush=True)


def gainwell_job_ids() -> list[int]:
    """Active Gainwell jobs we can honestly staff (excludes licensed-insurance roles)."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs WHERE source='gainwell' AND active "
                    "ORDER BY id")
        for jid, title in cur.fetchall():
            if is_licensed(title):
                continue
            out.append(jid)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=gainwell)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list auto-applyable Gainwell job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = gainwell_job_ids()
        print(f"{len(ids)} auto-applyable Gainwell jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
