"""Driver: drop a synthetic persona's résumé + contact into a staffing TALENT POOL (BROWSER lane).

A talent-pool drop is a NEW inbound offer channel: instead of applying to a specific job
(ATS → assessment → offer), we submit a persona's résumé into a recruiter POOL; recruiters then
reach out with matching roles, and their mail lands in the persona's @takhet.com Maildir (the SAME
CRM the apply lanes feed). It bypasses per-job ATS + assessments entirely.

Per the recon (`talent_pool_recon.POOLS`) the ONE server-reachable generic résumé drop is **Randstad**
(`join_randstad` Drupal webform). It carries an INVISIBLE reCAPTCHA v2 — so this driver is a REAL
BROWSER (persistent context + the vendored **NopeCHA** extension, same free in-browser solver the
TP/iCIMS/SmartRecruiters lanes use), NOT pure httpx: we navigate the page, fill the reverse-engineered
fields, upload the résumé via the dropzone, and let NopeCHA solve the invisible reCAPTCHA on submit.
`talent_pool_recon.py` stays the field/endpoint REFERENCE.

    python3 -m backend.tools.talent_pool_drop --pool randstad            # DRY RUN (open+fill, do NOT submit)
    python3 -m backend.tools.talent_pool_drop --list                     # per-pool recon verdicts
    TALENT_POOL_ADVANCE=1 python3 -m backend.tools.talent_pool_drop --pool randstad   # real drop (NopeCHA solves)

`TALENT_POOL_ADVANCE` off ⇒ DRY RUN: mint/use a persona, open the page, fill every field + attach the
résumé, screenshot, but NEVER click submit. `TALENT_POOL_HEADFUL=1` forces a headful browser (needs
`DISPLAY=:98`); default tries new-headless (no `:98` contention). Run under `sg mail`.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import talent_pool_recon as tpr  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"
NOPECHA_EXT = os.path.join(REPO, "backend", "vendor", "nopecha_ext")
STEALTH_PROFILE = os.getenv("TALENT_POOL_PROFILE") or os.path.join(REPO, "backend", "data", "talent_pool_profile")
LOGDIR = os.path.join(REPO, "logs", "talent_pool")

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")

_STATE_PLACE = {
    "Ohio": ("Columbus", "43215"), "Texas": ("Austin", "78701"), "Florida": ("Orlando", "32801"),
    "Georgia": ("Atlanta", "30303"), "Arizona": ("Phoenix", "85004"), "Tennessee": ("Nashville", "37203"),
    "North Carolina": ("Charlotte", "28202"),
}
_DEFAULT_STATE = "Ohio"


def _nopecha_key() -> str:
    """The NopeCHA key from the env or backend/.env (os.getenv is EMPTY under pm2/sg-mail)."""
    key = os.getenv("NOPECHA_KEY", "").strip()
    if key:
        return key
    try:
        for ln in (Path(REPO) / "backend" / ".env").read_text().splitlines():
            if ln.strip().startswith("NOPECHA_KEY="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _build_persona(job_title: str, state: str = _DEFAULT_STATE) -> dict:
    """Fresh synthetic US persona for a talent-pool drop (job-agnostic — placed in a coherent US
    city/state/zip). Provisions the mailbox, registers the demo persona, writes a prefill dir with a
    rendered résumé PDF. Returns the persona + the résumé path."""
    from backend.tools import catalog_drafts, drafts_ui, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    city, zc = _STATE_PLACE.get(state, ("Columbus", "43215"))
    job = {"title": job_title, "company": "Randstad", "company_key": "talent_pool",
           "description": "Remote customer service / support role (US).", "regions": ["US"],
           "location": f"{city}, {state}, United States", "ats": "talent_pool",
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
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""), prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[talent_pool] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = "talent_pool_randstad"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    resume_path = out / "resume.pdf"
    try:
        d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)
        resume_path.write_bytes(drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")
    except Exception as e:  # noqa: BLE001
        print(f"[talent_pool] resume gen skipped: {type(e).__name__}: {e}", flush=True)
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "talent_pool"}),
        encoding="utf-8")
    return {"profile": prof, "facts": cand.get("facts") or {}, "state": state,
            "profile_id": profile_id, "jobid": jobid, "resume_path": str(resume_path)}


def _any_recruiter_mail(email: str, since_ts: float) -> bool:
    """True once ANY inbound mail lands in the persona Maildir since `since_ts` (recruiter outreach is
    passive/slow — this only corroborates the mailbox is live, not a per-drop ack)."""
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


async def _configure_nopecha(ctx) -> str:
    """Arm the vendored NopeCHA extension for reCAPTCHA (invisible v2). Returns 'key'/'free'/'err'."""
    key = _nopecha_key()
    cfg = ("input_method=javascript|enabled=true|hcaptcha_auto_solve=true|recaptcha_auto_solve=true|"
           "recaptcha_auto_open=true|recaptcha_solve_delay_time=200|turnstile_auto_solve=true"
           + (f"|key={key}" if key else ""))
    try:
        sp = await ctx.new_page()
        await sp.goto("https://nopecha.com/setup#" + cfg, wait_until="domcontentloaded", timeout=45000)
        await sp.wait_for_timeout(3500)
        await sp.close()
        return "key" if key else "free"
    except Exception as e:  # noqa: BLE001
        print(f"[nopecha config {type(e).__name__}: {e}]", flush=True)
        return "err"


async def _dismiss_cookies(page) -> None:
    for sel in ('#onetrust-accept-btn-handler', 'button:has-text("accept all")',
                'button:has-text("Accept All")', 'button:has-text("accept")'):
        try:
            b = page.locator(sel)
            if await b.count():
                await b.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                return
        except Exception:
            pass


async def _fill_typeahead(page, name: str, value: str) -> bool:
    """location / job_title are AUTOCOMPLETE typeaheads — type the value, wait, pick the first
    suggestion (ArrowDown+Enter). Free-typed text alone shows 'no results' and is NOT accepted."""
    try:
        loc = page.locator(f'[name="{name}"]')
        if not (await loc.count()) or not value:
            return False
        el = loc.first
        await el.click()
        await el.fill("")
        await el.type(str(value), delay=60)
        await page.wait_for_timeout(1800)
        # accept the first surfaced option
        for _ in range(2):
            await el.press("ArrowDown")
            await page.wait_for_timeout(300)
        await el.press("Enter")
        await page.wait_for_timeout(500)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[typeahead {name} err {type(e).__name__}]", flush=True)
        return False


async def _fill_randstad(page, prof: dict, resume_path: str, report: dict) -> None:
    """Fill the join_randstad webform fields + attach the résumé via the dropzone (no submit)."""
    form = tpr.build_randstad_form(prof)
    report["form"] = form
    async def _fill(name, value):
        try:
            loc = page.locator(f'[name="{name}"]')
            if await loc.count() and value:
                await loc.first.fill(str(value))
                return True
        except Exception as e:  # noqa: BLE001
            print(f"[fill {name} err {type(e).__name__}]", flush=True)
        return False
    filled = []
    for name in ("first_name", "last_name", "email_address", "phone_number"):
        if await _fill(name, form.get(name)):
            filled.append(name)
    # location + job_title are typeaheads (need a dropdown pick)
    for name in ("job_location", "job_title"):
        if await _fill_typeahead(page, name, form.get(name)):
            filled.append(name)
    report["filled_fields"] = filled
    # résumé upload: dropzone.js creates `input.dz-hidden-input` (the original edit-resume becomes a
    # click zone) — set files on the dz input, else fall back to any file input.
    uploaded = False
    try:
        fi = page.locator('input.dz-hidden-input')
        if not await fi.count():
            fi = page.locator('input[type="file"]')
        if await fi.count() and os.path.exists(resume_path):
            await fi.first.set_input_files(resume_path)
            for _ in range(25):
                await page.wait_for_timeout(1000)
                val = await page.evaluate(
                    "() => { const e=document.querySelector('[name=\"resume[uploaded_files]\"]');"
                    " return e ? e.value : ''; }")
                if val:
                    uploaded = True
                    report["resume_file_id"] = val
                    break
    except Exception as e:  # noqa: BLE001
        print(f"[resume upload err {type(e).__name__}: {e}]", flush=True)
    report["resume_uploaded"] = uploaded


async def run_randstad(*, advance: bool, keep_minutes: int) -> dict:
    from playwright.async_api import async_playwright

    os.makedirs(STEALTH_PROFILE, exist_ok=True)
    os.makedirs(LOGDIR, exist_ok=True)
    headful = os.getenv("TALENT_POOL_HEADFUL", "").strip().lower() in ("1", "true", "yes", "on")
    job_title = "Customer Service Representative"
    p = _build_persona(job_title)
    prof = p["profile"]
    print(f"persona: {prof.get('full_name')} <{prof.get('email')}> {prof.get('city')}, {p['state']} "
          f"| ADVANCE={advance} headful={headful} resume={os.path.exists(p['resume_path'])}", flush=True)

    report: dict = {"pool": "randstad", "advanced": False, "submitted": False, "success": False,
                    "headful": headful}
    ext = [f"--disable-extensions-except={NOPECHA_EXT}", f"--load-extension={NOPECHA_EXT}"]
    args = ext + (["--start-maximized"] if headful else ["--headless=new", "--window-size=1400,1600"])
    since = time.time()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            STEALTH_PROFILE, headless=not headful, channel="chromium", no_viewport=True,
            locale="en-US", timezone_id="America/New_York", args=args)
        try:
            report["nopecha"] = await _configure_nopecha(ctx)
            print(f"[nopecha armed: {report['nopecha']}]", flush=True)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(tpr.RANDSTAD_PAGE_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            await _dismiss_cookies(page)
            await page.wait_for_timeout(2500)
            captcha = tpr.parse_captcha(await page.content())  # LIVE content → FriendlyCaptcha classes
            report["captcha"] = captcha
            print(f"[page loaded; captcha={captcha}]", flush=True)
            if captcha.get("kind") == "friendly_captcha":
                print("[WALL: the live captcha is FriendlyCaptcha — NopeCHA (reCAPTCHA/hCaptcha/"
                      "Turnstile only) CANNOT solve it; a submit will hit 'Browser check failed'.]",
                      flush=True)

            await _fill_randstad(page, prof, p["resume_path"], report)
            print(f"[filled={report.get('filled_fields')} resume_uploaded={report.get('resume_uploaded')}]",
                  flush=True)
            try:
                await page.screenshot(path=os.path.join(LOGDIR, "01_filled.png"), full_page=True)
            except Exception:
                pass

            if not advance:
                print("[dry run — filled, NOT submitting. Set TALENT_POOL_ADVANCE=1 to drop.]", flush=True)
                return report

            # --- advance: submit; NopeCHA solves the invisible reCAPTCHA that fires on click ---
            try:
                btn = page.locator('[name="op"], button.webform-button--submit, #edit-actions-submit')
                await btn.first.click(timeout=15000)
                report["advanced"] = True
                report["submitted"] = True
            except Exception as e:  # noqa: BLE001
                report["note"] = f"submit click failed: {type(e).__name__}: {e}"
                print(f"[{report['note']}]", flush=True)
                return report
            # wait for the reCAPTCHA solve + navigation/confirmation
            body = ""
            for i in range(40):  # up to ~120s
                await page.wait_for_timeout(3000)
                try:
                    body = await page.evaluate("() => document.body ? document.body.innerText : ''")
                except Exception:
                    body = ""
                if tpr.randstad_ack(200, body):
                    break
                low = (body or "").lower()
                if "recaptcha" in low or "verification" in low or "not a robot" in low:
                    continue  # NopeCHA still working
            report["success"] = tpr.randstad_ack(200, body)
            report["body_head"] = (body or "")[:500]
            try:
                await page.screenshot(path=os.path.join(LOGDIR, "02_after_submit.png"), full_page=True)
            except Exception:
                pass
            print(f"[submit success={report['success']}] body[:200]={ (body or '')[:200]!r}", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    if report.get("success"):
        deadline = time.time() + keep_minutes * 60
        while time.time() < deadline:
            if _any_recruiter_mail(prof.get("email", ""), since - 60):
                report["mail_seen"] = True
                break
            time.sleep(15)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="randstad", help="talent pool key (see --list)")
    ap.add_argument("--keep", type=int, default=4, help="minutes to watch the Maildir after a drop")
    ap.add_argument("--list", action="store_true", help="print per-pool recon verdicts + exit")
    args = ap.parse_args()

    if args.list:
        for k, v in tpr.POOLS.items():
            flag = "VIABLE " if v["viable"] else "walled "
            print(f"[{flag}] {k:16} reachable={v['reachable_serverside']!s:5} "
                  f"resume_drop={v['resume_drop']!s:5} account={v['account_required']!s:5} "
                  f"captcha={v['captcha']:22} — {v['note']}")
        print(f"\nviable: {tpr.viable_pools()}")
        return

    advance = os.getenv("TALENT_POOL_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")
    if args.pool == "randstad":
        asyncio.run(run_randstad(advance=advance, keep_minutes=args.keep))
    else:
        meta = tpr.POOLS.get(args.pool)
        if not meta:
            ap.error(f"unknown pool '{args.pool}' (see --list)")
        print(f"[{args.pool}] NOT a built lane — {meta['note']}")


if __name__ == "__main__":
    main()
