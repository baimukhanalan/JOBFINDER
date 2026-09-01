"""Driver: auto-apply to one Oracle ORC (Oracle Recruiting Cloud / Candidate Experience) mass-hiring
job end-to-end via OracleORCStrategy. Alorica's board sits on `*.fa.ocs.oraclecloud.com/hcmUI`.

Mirrors taleo_recon / workday_recon / icims_recon: shape a mass_hiring_jobs row, synthesize a fresh
US persona placed in the job's state, tailor a résumé, navigate to the Oracle CX apply URL, and drive
`OracleORCStrategy.prefill` (which fills step 1 + the JET screeners/EEO and walks the wizard to the
final Submit, recording its selector WITHOUT clicking). This driver then, when ORC_ADVANCE=1, clicks
the recorded Submit and runs a watch loop that (a) fills the emailed verification PIN from the persona's
own @takhet.com Maildir (machine-readable, like the GH/Ashby security-code step) and confirms it, and
(b) waits for the Oracle/Alorica "thank you for applying" receipt. The only anti-bot at the end is an
INVISIBLE reCAPTCHA v3 (risk-scored, no solver) — if a full fill never yields a receipt from the
datacenter IP, that is the v3 ceiling (report + skip).

Ground truth = a real application-received email in the persona's Maildir; a Submit click is NOT proof.

Run (mail group needed for the emailed PIN):
    cd /home/projects/jobfinder && ORC_HEADLESS=1 ORC_ADVANCE=1 sg mail -c \
        'python3 -m backend.tools.oracle_orc_recon --job <mass_hiring_id> --fresh'
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"

# The emailed-PIN step (same shape the co-pilot's _watch_submit handles).
_CODE_STEP_RE = re.compile(
    r"(?i)verification code|security code|enter the .{0,20}code|verify your email"
    r"|enter (?:the )?(?:pin|code)|one.?time (?:code|password|pin)|confirm your email")
_CODE_FIELD_SELECTORS = (
    "input[aria-label*='code' i]", "input[placeholder*='code' i]",
    "input[name*='code' i]", "input[id*='security' i]", "input[id*='code' i]",
    "input[aria-label*='verification' i]", "input[aria-label*='pin' i]",
    "input[id*='pin' i]", "input[name*='pin' i]")
_CODE_CONFIRM_SELECTORS = (
    "oj-button:has-text('Verify') button", "button:has-text('Verify')",
    "oj-button:has-text('Confirm') button", "button:has-text('Confirm')",
    "oj-button:has-text('Continue') button", "button:has-text('Continue')",
    "oj-button:has-text('Submit') button", "button:has-text('Submit')")
_CONFIRM_TEXT_RE = re.compile(
    r"(?i)thank you for applying|application (?:has been )?(?:received|submitted|complete)"
    r"|we have received your application|your application (?:was|has been) submitted"
    r"|thanks for applying|successfully submitted|submission (?:is )?complete")


def _pick_state(title: str, location_raw: str):
    """(full, code, city, zip) placing the persona in the job's state (from the icims allow-list)."""
    from backend.tools.icims_recon import _pick_state as _ps
    return _ps(title or "", location_raw or "")


def orc_job_ids() -> list[int]:
    """Active Oracle ORC (oraclecloud.com host) mass-hiring rows."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM mass_hiring_jobs WHERE active AND "
                    "apply_url ILIKE '%oraclecloud%' ORDER BY id")
        out = [r[0] for r in cur.fetchall()]
    return out


def _build_persona(row: dict) -> dict:
    """Fresh synthetic US persona for this ORC job, placed in the job's state (mirrors taleo_recon)."""
    from backend.tools import mass_hiring_apply
    full, code, city, zc = _pick_state(row.get("title") or "", row.get("location_raw") or "")
    profile_id, jobid = mass_hiring_apply.prepare(row, gender=None)
    pdir = Path(PREFILL_ROOT) / profile_id / jobid
    persona = json.loads((pdir / "persona.json").read_text(encoding="utf-8"))
    prof = persona.get("profile") or {}
    facts = persona.get("facts") or {}
    name = prof.get("full_name") or prof.get("name") or ""
    parts = name.split()
    profile_form = {
        "full_name": name,
        "first_name": prof.get("first_name") or (parts[0] if parts else ""),
        "last_name": prof.get("last_name") or (parts[-1] if len(parts) > 1 else ""),
        "email": prof.get("email") or "",
        "phone": prof.get("phone") or "",
        "street_address": prof.get("street_address") or "1200 Market Street",
        "address": prof.get("street_address") or "1200 Market Street",
        "city": city, "state": full, "zip": zc, "postal_code": zc,
        "country": "United States",
        "sex": (prof.get("sex") or "").strip().lower(),
    }
    return {"profile_form": profile_form, "facts": facts,
            "resume_path": str(pdir / "resume.pdf"),
            "state_code": code, "jobid": jobid, "profile_id": profile_id}


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once an Oracle/Alorica application-received email has landed in the persona's Maildir."""
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
            s = subj.group(0).lower() if subj else ""
            if _CONFIRM_TEXT_RE.search(s):
                return True
    return False


async def _maybe_fill_pin(page, email: str, load_ts: float) -> bool:
    """If a 'verification code / PIN' step is on the page and empty, read the code from the persona's
    Maildir, type it (segmented-OTP safe), and click the step's confirm button. Returns True if filled."""
    try:
        text = await page.inner_text("body", timeout=4000)
    except Exception:
        return False
    if not _CODE_STEP_RE.search(text or ""):
        return False
    from backend.tools.verify_code import read_code
    code = read_code(email, load_ts)
    if not code:
        return False
    for sel in _CODE_FIELD_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible(timeout=800):
                cur = (await el.input_value()) if await el.count() else ""
                if (cur or "").strip():
                    return False
                await el.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(code, delay=60)
                await page.wait_for_timeout(1200)
                for csel in _CODE_CONFIRM_SELECTORS:
                    try:
                        b = page.locator(csel).first
                        if await b.count() and await b.is_visible(timeout=800):
                            await b.click()
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:
                        continue
                print(f"[PIN {code} auto-filled from Maildir for {email}]", flush=True)
                return True
        except Exception:
            continue
    return False


async def _dismiss_idle(page) -> bool:
    """Oracle CX pops an 'Are You Still With Us?' session-idle modal during a slow fill; it
    intercepts Submit. Click its keep-alive button so the application can proceed."""
    try:
        body = await page.inner_text("body", timeout=3000)
    except Exception:
        return False
    if not re.search(r"still with us|still there|session.*(expir|time)|are you there", body or "", re.I):
        return False
    for sel in ("button:has-text('Continue')", "button:has-text('Yes')",
                "button:has-text(\"I'm still here\")", "button:has-text('Stay')",
                "button:has-text('Keep')", "oj-button:has-text('Continue') button",
                "button:has-text('OK')"):
        try:
            b = page.locator(sel).first
            if await b.count() and await b.is_visible(timeout=800):
                await b.click()
                await page.wait_for_timeout(1200)
                print(f"[dismissed idle modal via {sel}]", flush=True)
                return True
        except Exception:
            continue
    return False


async def _click_submit(page, selector_str: str) -> bool:
    await _dismiss_idle(page)
    for sel in [s.strip() for s in (selector_str or "").split(",") if s.strip()]:
        try:
            b = page.locator(sel).first
            if await b.count() and await b.is_visible(timeout=1500):
                await b.scroll_into_view_if_needed(timeout=2000)
                await b.click()
                await page.wait_for_timeout(2500)
                print(f"[submit clicked: {sel}]", flush=True)
                return True
        except Exception:
            continue
    return False


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, company, location_raw, source "
                    "FROM mass_hiring_jobs WHERE id=%s", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no mass_hiring_jobs row id={job_id}", flush=True)
        return
    row = {"id": r[0], "title": r[1], "apply_url": r[2], "company": r[3],
           "location_raw": r[4], "source": r[5]}
    print(f"=== Oracle ORC apply: job {row['id']} [{row['company']}] — {row['title']}", flush=True)

    advance = os.getenv("ORC_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")
    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state_code']} "
          f"| resume={os.path.exists(p['resume_path'])} | ORC_ADVANCE={os.getenv('ORC_ADVANCE', '')}",
          flush=True)

    profile_dir = os.getenv("ORC_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"orc_prof_{job_id}_{os.getpid()}")
    if fresh:
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    headless = os.getenv("ORC_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    from playwright.async_api import async_playwright
    from backend.applier.strategies.oracle_orc import OracleORCStrategy

    start_ts = time.time()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile_dir, headless=headless, channel="chromium", no_viewport=not headless,
            locale="en-US", timezone_id="America/New_York",
            args=[] if headless else ["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        shot_dir = os.path.join(REPO, "logs", "orc_recon", str(job_id))
        os.makedirs(shot_dir, exist_ok=True)
        _n = [0]

        async def _shot(tag):
            _n[0] += 1
            try:
                await page.screenshot(path=os.path.join(shot_dir, f"{_n[0]:02d}_{tag}.png"),
                                      full_page=True)
                with open(os.path.join(shot_dir, f"{_n[0]:02d}_{tag}.html"), "w") as fh:
                    fh.write(await page.content())
                print(f"[shot {_n[0]:02d} {tag}] url={page.url[:100]}", flush=True)
            except Exception as e:
                print(f"[shot {tag} err: {type(e).__name__}]", flush=True)

        try:
            await page.goto(row["apply_url"], wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            await _shot("landing")

            strat = OracleORCStrategy()
            strat.advance_wizard = advance
            result = await strat.prefill(
                page, pf, p["resume_path"], job={"title": row["title"], "company": row["company"]},
                draft=True, facts=p["facts"], profile_id=p["profile_id"])
            print(f"[filled: unfilled={result.get('unfilled')} "
                  f"review_items={len(result.get('review_items') or [])} "
                  f"page_type={result.get('page_type')} "
                  f"wizard_at_submit={result.get('wizard_at_submit')} "
                  f"wizard_blocked_step={result.get('wizard_blocked_step')}]", flush=True)
            await _shot("after_prefill")

            deadline = start_ts + keep_minutes * 60
            confirmed = False
            submit_clicked = False

            if advance:
                # If an emailed PIN gates email-verification upfront, clear it, then (re)advance.
                if await _maybe_fill_pin(page, pf["email"], start_ts - 60):
                    await page.wait_for_timeout(1500)
                    await _shot("after_early_pin")
                sub_sel = result.get("submit_selector") or (
                    "oj-button:has-text('Submit') button, button:has-text('Submit'), "
                    "button[title*='Submit' i]")
                # Single-page ORC forms never set wizard_at_submit; attempt Submit regardless.
                submit_clicked = await _click_submit(page, sub_sel)
                await _shot("after_submit_click")

                while time.time() < deadline:
                    if _app_confirmed(pf["email"], start_ts - 60):
                        confirmed = True
                        print("[application CONFIRMED — Oracle/Alorica receipt in the Maildir]", flush=True)
                        break
                    try:
                        body = await page.inner_text("body", timeout=5000)
                        if _CONFIRM_TEXT_RE.search(body or ""):
                            print("[page shows a submission-confirmation — awaiting mail receipt]", flush=True)
                    except Exception:
                        body = ""
                    await _dismiss_idle(page)
                    # Fill a late PIN / retry submit if it re-appears.
                    if await _maybe_fill_pin(page, pf["email"], start_ts - 60):
                        await page.wait_for_timeout(1500)
                        await _shot("after_pin")
                    elif not submit_clicked and result.get("submit_selector"):
                        submit_clicked = await _click_submit(page, result["submit_selector"])
                    await asyncio.sleep(10)

            if not confirmed:
                await _shot("final")
                print("[no confirmation within --keep — expected if ORC_ADVANCE is off, a required "
                      "field is unfilled, or the invisible reCAPTCHA v3 blocked the submit from this IP]",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:200]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
    print("=== oracle orc apply done", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, help="mass_hiring_jobs id (Oracle ORC / Alorica)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list active Oracle ORC job ids and exit")
    args = ap.parse_args()
    if args.list:
        ids = orc_job_ids()
        print(f"{len(ids)} active Oracle ORC jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
