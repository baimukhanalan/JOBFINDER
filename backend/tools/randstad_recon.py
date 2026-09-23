"""Driver: drop a synthetic persona's résumé into the Randstad talent POOL, FriendlyCaptcha-solved.

Randstad's `www.randstadusa.com/job-seeker/submit-your-resume/` is a no-account résumé DROP
(`join_randstad` Drupal webform). It is fully fillable server-side; the ONE wall is **FriendlyCaptcha**
(a browser-integrity proof-of-work). With `captcha_solver` now routing FriendlyCaptcha to 2captcha,
this driver navigates the drop, fills every field via `RandstadStrategy`, uploads the résumé, and —
under **RANDSTAD_ADVANCE=1** — submits while the solver clears FriendlyCaptcha. Ground truth of a live
drop = the on-page thank-you, corroborated by any recruiter reply in the persona @takhet.com Maildir.

    # dry run (fill + STOP, transmit nothing):
    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && python3 -m backend.tools.randstad_recon'
    # real drop (2captcha solves FriendlyCaptcha):
    DISPLAY=:98 RANDSTAD_ADVANCE=1 sg mail -c 'cd /home/projects/jobfinder && \
        python3 -m backend.tools.randstad_recon --keep 8'
    python3 -m backend.tools.randstad_recon --list   # active randstad mass_hiring rows (for targeting)

`RANDSTAD_HEADLESS=1` runs headless (the FriendlyCaptcha solve is a token injection, no visible
challenge to click, so headless works); default is headful on `:98`. Run under `sg mail` (mailbox
provisioning + the Maildir read). The 2captcha key is read from `backend/.env` (pm2/sg-mail don't
export it into `os.environ`)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"
LOGDIR = os.path.join(REPO, "logs", "randstad_recon")

# .env keys the solver needs but pm2/sg-mail (+ config.py extra='ignore') don't export into os.environ.
_ENV_KEYS = ("CAPTCHA_SOLVER_KEY", "CAPTCHA_SOLVER_PROVIDER", "TWOCAPTCHA_KEY", "NOPECHA_KEY")

_STATE_PLACE = {
    "Ohio": ("Columbus", "43215"), "Texas": ("Austin", "78701"), "Florida": ("Orlando", "32801"),
    "Georgia": ("Atlanta", "30303"), "Arizona": ("Phoenix", "85004"),
    "Tennessee": ("Nashville", "37203"), "North Carolina": ("Charlotte", "28202"),
}
_DEFAULT_STATE = "Texas"


def load_env() -> dict:
    """Populate os.environ from backend/.env for the captcha keys (idempotent, never overwrites an
    already-set var). Returns which keys are now present (values masked)."""
    present: dict = {}
    envp = Path(REPO) / "backend" / ".env"
    try:
        lines = envp.read_text().splitlines()
    except Exception:
        lines = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if k in _ENV_KEYS and not os.getenv(k):
            os.environ[k] = v.strip().strip('"').strip("'")
    for k in _ENV_KEYS:
        present[k] = bool(os.getenv(k))
    return present


def _build_persona(job_title: str, state: str = _DEFAULT_STATE) -> dict:
    """Fresh synthetic US persona for a Randstad drop (job-agnostic, coherently US-placed). Provisions
    the mailbox, registers the demo persona, writes a prefill dir + rendered résumé PDF."""
    from backend.tools import catalog_drafts, drafts_ui, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    city, zc = _STATE_PLACE.get(state, ("Austin", "78701"))
    job = {"title": job_title, "company": "Randstad", "company_key": "randstad_pool",
           "description": "Remote customer service / support role (US).", "regions": ["US"],
           "location": f"{city}, {state}, United States", "ats": "randstad",
           "external_id": "", "url": "", "questions": []}
    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job)
    prof = cand["profile"]
    prof["city"] = city
    prof["state"] = state
    prof["zip"] = zc
    prof["location"] = f"{city}, {state}"

    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""),
                                      prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[randstad] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = "randstad_pool"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    resume_path = out / "resume.pdf"
    try:
        d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)
        resume_path.write_bytes(drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")
    except Exception as e:  # noqa: BLE001
        print(f"[randstad] resume gen skipped: {type(e).__name__}: {e}", flush=True)
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "randstad"}),
        encoding="utf-8")
    return {"profile": prof, "facts": cand.get("facts") or {}, "state": state,
            "profile_id": profile_id, "jobid": jobid, "resume_path": str(resume_path)}


def _any_recruiter_mail(email: str, since_ts: float) -> bool:
    """True once ANY inbound mail lands in the persona Maildir since `since_ts` (recruiter outreach is
    passive — this corroborates the mailbox is live/reachable, not a per-drop ack)."""
    local = (email or "").split("@", 1)[0]
    if not local:
        return False
    for sub in ("new", "cur"):
        d = os.path.join(MAILROOT, local, sub)
        try:
            for n in os.listdir(d):
                if os.path.getmtime(os.path.join(d, n)) >= since_ts:
                    return True
        except Exception:
            continue
    return False


def _target_title() -> str:
    """Sample an active Randstad mass_hiring title to aim the drop at (else a generic CSR)."""
    try:
        from backend.tools import mail_db
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute("SELECT title FROM mass_hiring_jobs WHERE source='randstad' AND active "
                        "ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
        if r and r[0]:
            from backend.applier.strategies.randstad import row_is_staffable
            if row_is_staffable(r[0]):
                return r[0]
    except Exception:
        pass
    return "Customer Service Representative"


async def run(keep_minutes: int = 8) -> dict:
    from playwright.async_api import async_playwright

    from backend.applier.strategies.randstad import RandstadStrategy, advance_enabled

    present = load_env()
    advance = advance_enabled()
    headless = os.getenv("RANDSTAD_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    os.makedirs(LOGDIR, exist_ok=True)

    job_title = _target_title()
    p = _build_persona(job_title)
    prof = p["profile"]
    keys_on = sorted(k for k, v in present.items() if v)
    print(f"persona: {prof.get('full_name')} <{prof.get('email')}> {prof.get('city')}, {p['state']} "
          f"| target={job_title!r} ADVANCE={advance} headless={headless} "
          f"keys={keys_on} resume={os.path.exists(p['resume_path'])}", flush=True)
    if advance and not (present.get("CAPTCHA_SOLVER_KEY") or present.get("TWOCAPTCHA_KEY")):
        print("[WARN: RANDSTAD_ADVANCE=1 but no 2captcha key in env/.env — FriendlyCaptcha cannot be "
              "solved; the submit will hit 'Browser check failed'. Set TWOCAPTCHA_KEY/CAPTCHA_SOLVER_KEY.]",
              flush=True)

    report: dict = {"pool": "randstad", "advance": advance, "submitted": False, "success": False,
                    "headless": headless, "target_title": job_title, "keys_present": present}

    profile_dir = os.getenv("RANDSTAD_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"randstad_prof_{os.getpid()}")
    shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    since = time.time()
    async with async_playwright() as pw:
        _lk = dict(headless=headless, channel="chromium", no_viewport=not headless,
                   locale="en-US", timezone_id="America/New_York",
                   args=["--no-sandbox"] if headless else ["--no-sandbox", "--start-maximized"])
        # Egress DIRECT by default (US résumé drop from a US-only site; opt-in residential via
        # RANDSTAD_RESIDENTIAL/RANDSTAD_PROXY, guarded — never breaks the run).
        try:
            from backend.tools import proxy_pool
            _px = proxy_pool.lane_egress("RANDSTAD_RESIDENTIAL", "RANDSTAD_PROXY", str(os.getpid()))
        except Exception:
            _px = None
        if _px:
            _lk["proxy"] = _px
            print(f"[egress: {_px['server']} (residential)]", flush=True)
        ctx = await pw.chromium.launch_persistent_context(profile_dir, **_lk)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        _n = [0]

        async def _shot(tag):
            _n[0] += 1
            try:
                await page.screenshot(path=os.path.join(LOGDIR, f"{_n[0]:02d}_{tag}.png"),
                                      full_page=True)
            except Exception:
                pass

        try:
            strat = RandstadStrategy()
            await page.goto(strat.DROP_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            await _dismiss_cookies(page)
            await page.wait_for_timeout(2000)
            await strat.fill(page, prof, p["resume_path"], report)
            await _shot("filled")
            print(f"[filled={report.get('filled_fields')} resume_uploaded={report.get('resume_uploaded')} "
                  f"friendlycaptcha={report.get('friendlycaptcha')} "
                  f"missing_required={report.get('missing_required')}]", flush=True)

            if not advance:
                print("[dry run — filled, NOT submitting. Set RANDSTAD_ADVANCE=1 to drop.]", flush=True)
                return report

            await strat.submit(page, report)
            await _shot("after_submit")
            print(f"[submit submitted={report.get('submitted')} success={report.get('success')} "
                  f"captcha_solved={report.get('captcha_solved')}] "
                  f"body[:180]={(report.get('body_head') or '')[:180]!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            report["error"] = f"{type(e).__name__}: {str(e)[:180]}"
            print(f"[run error: {report['error']}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)

    if report.get("success"):
        deadline = time.time() + keep_minutes * 60
        while time.time() < deadline:
            if _any_recruiter_mail(prof.get("email", ""), since - 60):
                report["mail_seen"] = True
                break
            time.sleep(15)
    return report


async def _dismiss_cookies(page) -> None:
    for sel in ('#onetrust-accept-btn-handler', 'button:has-text("Accept All")',
                'button:has-text("accept all")', 'button:has-text("Accept")'):
        try:
            b = page.locator(sel)
            if await b.count():
                await b.first.click(timeout=3000)
                await page.wait_for_timeout(700)
                return
        except Exception:
            pass


def randstad_job_ids() -> list[int]:
    """Active Randstad mass_hiring rows we can honestly target (excludes licensed roles)."""
    from backend.applier.strategies.randstad import row_is_staffable
    from backend.tools import mail_db
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs WHERE source='randstad' AND active ORDER BY id")
        for jid, title in cur.fetchall():
            if row_is_staffable(title):
                out.append(jid)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=8, help="minutes to watch the Maildir after a drop")
    ap.add_argument("--list", action="store_true", help="list active randstad mass_hiring ids + exit")
    args = ap.parse_args()
    if args.list:
        try:
            ids = randstad_job_ids()
            print(f"{len(ids)} active staffable Randstad rows: {ids}")
        except Exception as e:  # noqa: BLE001
            print(f"[list unavailable: {type(e).__name__}: {e}]")
        return
    asyncio.run(run(keep_minutes=args.keep))


if __name__ == "__main__":
    main()
