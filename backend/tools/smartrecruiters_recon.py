"""SmartRecruiters (guest one-click) auto-apply driver.

Drives the real ``SmartRecruitersStrategy`` through a **patchright-stealth** browser to a
REAL "application received" ack in the synthetic persona's ``@takhet.com`` Maildir. Stealth is
used (not the plain co-pilot) because SmartRecruiters fronts its posting pages with DataDome
bot-management that a vanilla browser can trip; patchright clears it. The one-click apply form
itself is a **shadow-DOM web-component** app, which is why the strategy's location/rescan
helpers must pierce shadow roots (see ``strategies/smartrecruiters.py``).

Run headful under ``DISPLAY=:98`` + ``sg mail`` (the persona's mailbox is read from disk)::

    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        python3 -m backend.tools.smartrecruiters_recon --job 536'

``SMARTRECRUITERS_ADVANCE=1`` is forced on so the strategy walks to and records the final
Submit; this driver clicks it only when the form has no unfilled required field (the honest
gate — never submit an incomplete form). A real submit is authorized for these synthetic
personas; the driver then polls the persona Maildir for the SmartRecruiters ack and exits as
soon as it lands.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time

os.environ.setdefault("SMARTRECRUITERS_ADVANCE", "1")  # BEFORE importing the strategy (class attr)

from patchright.async_api import async_playwright  # noqa: E402

from backend.applier.strategies.smartrecruiters import (  # noqa: E402
    SmartRecruitersStrategy)
from backend.profiles.store import Profile  # noqa: E402
from backend.tools import mail_db, mass_hiring_apply  # noqa: E402

REPO = "/home/projects/jobfinder"
NOPECHA_EXT = os.path.join(REPO, "backend", "vendor", "nopecha_ext")
STEALTH_PROFILE = os.getenv("SR_PROFILE_DIR") or os.path.join(REPO, "backend", "data", "sr_stealth_profile")
PREFILL = os.path.join(REPO, "uploads", "prefill")
_ACK_HINTS = ("appl", "thank", "receiv", "sutherland", "smartrecruiters", "next step")


def _maildir_ack(email: str, since: float) -> list[tuple[str, str]]:
    """(from, subject) of messages in the persona's Maildir received at/after `since` — the
    ground-truth proof the application was accepted (never a submit-click)."""
    local = (email or "").split("@")[0]
    base = f"/var/mail/vhosts/takhet.com/{local}"
    out: list[tuple[str, str]] = []
    for f in glob.glob(f"{base}/new/*") + glob.glob(f"{base}/cur/*"):
        try:
            if os.path.getmtime(f) < since - 5:
                continue
            with open(f, "rb") as fh:
                raw = fh.read(4000).decode("utf-8", "ignore")
            frm = subj = ""
            for line in raw.splitlines():
                lo = line.lower()
                if lo.startswith("from:"):
                    frm = line[5:].strip()
                elif lo.startswith("subject:"):
                    subj = line[8:].strip()
                if frm and subj:
                    break
            out.append((frm, subj))
        except Exception:
            pass
    return out


def _load_row(jobid: int) -> dict:
    cols = ["id", "source", "company", "title", "location_raw", "apply_url", "source_id"]
    with mail_db.conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT {','.join(cols)} FROM mass_hiring_jobs WHERE id=%s", (jobid,))
        r = cur.fetchone()
    if not r:
        raise SystemExit(f"job {jobid} not found")
    return dict(zip(cols, r))


async def apply_job(jobid: int, keep_min: int = 8) -> dict:
    row = _load_row(jobid)
    print(f"[job] {row['id']} {row['company']} | {row['title']} | {row['apply_url']}", flush=True)
    profile_id, mh_jobid = mass_hiring_apply.prepare(row)
    d = os.path.join(PREFILL, profile_id, mh_jobid)
    rep = json.load(open(os.path.join(d, "report.json")))
    persona = json.load(open(os.path.join(d, "persona.json")))
    prof = Profile.from_dict(persona.get("profile") or {})
    facts = persona.get("facts") or {}
    form = prof.to_form_dict()
    email = (form.get("email") or "").strip()
    resume_pdf = os.path.join(d, "resume.pdf")
    known = rep.get("drafted_answers") or {}
    url = rep["apply_url"]
    print(f"[persona] {form.get('full_name')} <{email}> | {form.get('city')},{form.get('state')} "
          f"| resume={os.path.exists(resume_pdf)}", flush=True)

    result_out = {"jobid": jobid, "persona": email, "clicked": False, "ack": False, "subject": None,
                  "unfilled": None, "page_type": None}
    os.makedirs(STEALTH_PROFILE, exist_ok=True)
    async with async_playwright() as pw:
        ext = [f"--disable-extensions-except={NOPECHA_EXT}", f"--load-extension={NOPECHA_EXT}"]
        ctx = await pw.chromium.launch_persistent_context(
            STEALTH_PROFILE, headless=False, channel="chromium", no_viewport=True,
            locale="en-US", timezone_id="America/New_York", args=["--start-maximized"] + ext)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            key = os.getenv("NOPECHA_KEY", "").strip()
            cfg = ("input_method=javascript|enabled=true|hcaptcha_auto_solve=true|recaptcha_auto_solve=true|"
                   "turnstile_auto_solve=true|awswaf_auto_solve=true|datadome_auto_solve=true"
                   + (f"|key={key}" if key else ""))
            sp = await ctx.new_page()
            await sp.goto("https://nopecha.com/setup#" + cfg, wait_until="domcontentloaded", timeout=45000)
            await sp.wait_for_timeout(3000)
            await sp.close()
        except Exception as e:
            print(f"[nopecha {type(e).__name__}]", flush=True)

        since = time.time()
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(3500)
        strat = SmartRecruitersStrategy()
        await strat.open_form(page)
        await page.wait_for_timeout(2500)
        result = await strat.prefill(page, form, resume_pdf,
                                     job={"title": row["title"], "company": row["company"]},
                                     draft=True, known_answers=known, facts=facts, profile_id=profile_id)
        unfilled = result.get("unfilled") or []
        result_out["unfilled"] = unfilled
        result_out["page_type"] = result.get("page_type")
        print(f"[prefill] page_type={result.get('page_type')} wizard_at_submit={result.get('wizard_at_submit')} "
              f"unfilled={unfilled}", flush=True)
        if result.get("page_type") in ("captcha", "login_required", "expired"):
            print(f"[WALL] page_type={result.get('page_type')} — cannot proceed", flush=True)
            await ctx.close()
            return result_out

        if not unfilled:
            # The strategy walked to the final screen (SMARTRECRUITERS_ADVANCE=1). Press the REAL
            # SmartRecruiters submit via its shadow-piercing finder — NOT a raw `button:has-text`
            # locator, which matches the "Apply With Indeed" integration and never submits.
            try:
                clicked = await strat.click_submit(page)
                if clicked:
                    print("[submit] clicked the SmartRecruiters primary submit", flush=True)
                    result_out["clicked"] = True
                    await page.wait_for_timeout(5000)
                    body = (await page.evaluate("() => document.body ? document.body.innerText : ''"))[:400]
                    print(f"[post-submit body] {body!r}", flush=True)
                else:
                    # Not yet at the submit screen — one guarded advance, then retry the submit.
                    info = await strat._tag_primary_button(page)
                    print(f"[submit] primary is {info!r} — not a submit; leaving for the human", flush=True)
            except Exception as e:
                print(f"[submit err {type(e).__name__}: {e}]", flush=True)
        else:
            print(f"[not submitting] {len(unfilled)} required unfilled: {unfilled}", flush=True)

        if result_out["clicked"]:
            print(f"[watch] polling Maildir up to {keep_min} min for ack…", flush=True)
            for i in range(keep_min * 6):
                await page.wait_for_timeout(10000)
                hits = _maildir_ack(email, since)
                fresh = [h for h in hits if any(k in (h[1] or "").lower() for k in _ACK_HINTS)]
                if fresh:
                    result_out["ack"] = True
                    result_out["subject"] = fresh[0][1]
                    print(f"[ACK] {email}", flush=True)
                    for frm, subj in fresh:
                        print(f"   FROM {frm} | SUBJ {subj}", flush=True)
                    break
            else:
                print("[no ack within keep window]", flush=True)
        await ctx.close()
    return result_out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, required=True, help="mass_hiring_jobs id to apply to")
    ap.add_argument("--keep", type=int, default=8, help="minutes to wait for the ack after submit")
    args = ap.parse_args()
    res = asyncio.run(apply_job(args.job, keep_min=args.keep))
    print("[result] " + json.dumps(res, ensure_ascii=False), flush=True)
    sys.exit(0 if res.get("ack") else 1)


if __name__ == "__main__":
    main()
