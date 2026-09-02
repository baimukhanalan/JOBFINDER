"""Driver: auto-apply to one Oracle Recruiting Cloud (ORC) / Candidate Experience job
(Alorica) end-to-end via OracleORCStrategy.

Modeled on `taleo_recon.py`. Oracle CX guest apply is LOGIN-LESS and has NO interactive
captcha — the only anti-bot is (a) an emailed PIN (machine-readable from the persona's
Maildir, exactly like the GH/Ashby "security code") and (b) an INVISIBLE reCAPTCHA v3 the
page JS runs itself. So it runs autonomously (headful `:98` by default, headless via
`ORC_HEADLESS=1`). Each run: fresh synthetic US persona in a per-job isolated Chromium
profile, navigate to the CX job URL, drive `OracleORCStrategy.prefill` (which clicks Apply,
fills the JET wizard, and — with ORC_ADVANCE — walks Continue→EEO→Review to the final
Submit), then a self-contained submit+PIN watch clicks Submit, fills the emailed PIN, and
awaits the confirmation.

The real Submit is gated by env **ORC_ADVANCE=1** (default OFF → side-effect-free fill:
no account, no PII transmitted, no submit).

Ground truth = the Oracle "thank you for applying / application received" confirmation
email in the persona's @takhet.com Maildir (reuse the GH/Ashby/Taleo confirmation-read
pattern).

    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        ORC_ADVANCE=1 python3 -m backend.tools.orc_recon --job <mass_hiring_id> --fresh'

LIVE STATE (first real run on Alorica id=153, 2026-09-02) — REACHES the form, does NOT yet
confirm. The datacenter IP loads the CX site with NO WAF; the guest email+terms step fills and
NEXT reveals the full SINGLE-PAGE application form (this Alorica tenant `fa-euxw` is Redwood JET,
NOT the classic multi-step wizard the strategy assumed). On submit Oracle returns a
"You have 15 issues" validation panel — the Redwood fill layer in oracle_orc.py does not COMMIT
on this tenant: Title radio unset, Address cascade (City/State/Postal/County) empty, the
Application-Question button[role=radio] Yes/No not committed, Veteran/Disability EEO not declined,
résumé not uploaded. Plus TWO structural blockers: (1) the reserved-fiction 555-01xx persona phone
fails Oracle's libphonenumber check ("Enter a valid number") — a phone can't be both
guaranteed-fake AND format-valid, so this is an OWNER policy decision; (2) a required WOTC
"Tax Credit Assessment" sub-flow that isn't built. Screenshots: logs/orc_recon/153/. Next: live
iteration on oracle_orc.py's Redwood commit (`_fill_orc_redwood`/`_commit_orc_text`/
`_fill_orc_comboboxes`/`_fill_orc_radiobuttons`) against the saved DOM + a phone-policy call + a
WOTC filler. NOT yet cron-wired (no end-to-end ack).
"""
from __future__ import annotations

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

# The Oracle application-confirmation subjects (a COMPLETED apply, not the "don't forget to
# complete" reminder — that one is filtered out below).
_ACK_RE = re.compile(
    r"thank you for applying|thank you for your application|received your application|"
    r"application (?:has been )?received|successfully submitted|application (?:is )?complete|"
    r"thank you for your interest", re.I)
_ACK_SKIP_RE = re.compile(r"don.?t forget|complete your application|finish your application", re.I)


def orc_job_ids() -> list[int]:
    """Active Alorica (Oracle ORC) rows we can honestly staff (drop exotic-language roles a
    synthetic English/Spanish/Russian persona can't truthfully claim)."""
    from backend.tools.synth_persona import job_is_staffable
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs "
                    "WHERE source='alorica' AND active ORDER BY id")
        for jid, title in cur.fetchall():
            if not job_is_staffable({"title": title}):
                continue
            out.append(jid)
    return out


def _pick_state(title: str, location_raw: str) -> tuple[str, str, str, str]:
    """(full, code, city, zip) placing the persona in a coherent US state. Alorica is US-remote
    with no state gate, so reuse the shared iCIMS allow-list picker (defaults to Ohio)."""
    from backend.tools.icims_recon import _pick_state as _ps
    full, code, city, zc = _ps(title or "", location_raw or "")
    return full, code, city, zc


def _build_persona(row: dict) -> dict:
    """Fresh synthetic persona for this ORC job (mirrors taleo_recon._build_persona)."""
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
    }
    return {"profile_form": profile_form, "facts": facts,
            "resume_path": str(pdir / "resume.pdf"),
            "state_code": code, "jobid": jobid, "profile_id": profile_id}


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once an Oracle application-confirmation email has landed in the persona's Maildir
    (received at/after since_ts). Skips the 'don't forget to complete' reminder."""
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
                    head = f.read(4000).decode("utf-8", "ignore")
            except Exception:
                continue
            subj = re.search(r"^Subject:.*$", head, re.I | re.M)
            s = subj.group(0) if subj else ""
            if _ACK_RE.search(s) and not _ACK_SKIP_RE.search(s):
                return True
    return False


async def _submit_and_watch(page, email: str, load_ts: float, report: dict,
                            shot, deadline: float) -> bool:
    """Self-contained port of the co-pilot's submit + emailed-PIN watch (no co-pilot globals):
    click the final Submit, then poll the page — when Oracle's 'enter the verification code /
    verify your email' PIN step appears, read the code from the persona's OWN Maildir, type it,
    and click the step's confirm. Also nudges any post-PIN Continue/Submit. Returns True on a
    real Maildir ack. Never navigates."""
    from backend.applier.analyzer import find_submit_button
    from backend.applier.filler import click_submit
    from backend.tools.verify_code import read_code
    try:
        from backend.copilot import (_CODE_FIELD_SELECTORS, _CODE_STEP_RE,
                                      _click_code_confirm, looks_submitted)
    except Exception:  # pragma: no cover - fallback if copilot import changes
        _CODE_STEP_RE = re.compile(
            r"(?i)verification code|security code|enter the .{0,20}code|verify your email"
            r"|enter (?:the )?(?:pin|code)|one.?time (?:code|password|pin)")
        _CODE_FIELD_SELECTORS = (
            "input[autocomplete='one-time-code']", "input[aria-label*='verification' i]",
            "input[aria-label*='code' i]", "input[aria-label*='pin' i]",
            "input[id*='pin' i]", "input[name*='pin' i]", "input[id*='code' i]")
        _click_code_confirm = None

        def looks_submitted(text, url):
            return bool(re.search(r"thank you for applying|application (?:has been )?"
                                  r"received|successfully submitted", text or "", re.I))

    # (1) click the final Submit the strategy stopped at.
    sel = report.get("submit_selector") or await find_submit_button(page)
    if sel:
        try:
            await click_submit(page, {"submit_selector": sel})
            print(f"[submit clicked: {sel[:60]}]", flush=True)
        except Exception as e:
            print(f"[submit click err: {type(e).__name__}]", flush=True)
    await page.wait_for_timeout(2500)
    await shot("after_submit")

    code_done = False
    last_sig = ""
    while time.time() < deadline:
        try:
            if page.is_closed():
                return False
            text = await page.inner_text("body", timeout=5000)
        except Exception:
            await asyncio.sleep(3)
            continue
        # ground truth: the ack email
        if _app_confirmed(email, load_ts - 60):
            print("[application CONFIRMED — Oracle receipt in the Maildir]", flush=True)
            return True
        # (2) emailed-PIN step
        if email and not code_done and _CODE_STEP_RE.search(text):
            sel_str = ",".join(_CODE_FIELD_SELECTORS)
            try:
                state = await page.evaluate(
                    "(sel)=>{const i=document.querySelector(sel);"
                    "return i?((i.value||'').trim()?'filled':'empty'):'nofield';}", sel_str)
            except Exception:
                state = "nofield"
            if state == "empty":
                code = read_code(email, load_ts)
                if code:
                    for s in _CODE_FIELD_SELECTORS:
                        try:
                            el = page.locator(s).first
                            if await el.count() and await el.is_visible(timeout=1000):
                                await el.click()
                                await page.keyboard.press("Control+A")
                                await page.keyboard.press("Backspace")
                                await page.keyboard.type(code, delay=60)
                                code_done = True
                                print(f"[emailed PIN filled: {code}]", flush=True)
                                break
                        except Exception:
                            continue
                    if code_done:
                        await page.wait_for_timeout(1200)
                        if _click_code_confirm:
                            try:
                                await _click_code_confirm(page)
                            except Exception:
                                pass
                        await shot("after_pin")
                else:
                    print("[PIN step shown but no code in Maildir yet — waiting]", flush=True)
        # (3) nudge a post-PIN Continue/Submit if the flow parked on one (best-effort, once per
        # distinct page signature so we never hammer the same button).
        sig = (page.url[:80] + "|" + text[:80]).lower()
        if sig != last_sig:
            last_sig = sig
            for bsel in ("oj-button:has-text('Submit') button", "button:has-text('Submit')",
                         "oj-button:has-text('Continue') button", "button:has-text('Continue')"):
                try:
                    b = page.locator(bsel).first
                    if await b.count() and await b.is_visible(timeout=800):
                        await b.click()
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
        await asyncio.sleep(4)
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
    print(f"=== Oracle ORC apply: job {row['id']} [{row['source']}] — {row['title']}", flush=True)

    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state_code']} "
          f"| resume={os.path.exists(p['resume_path'])} | ORC_ADVANCE="
          f"{os.getenv('ORC_ADVANCE', '')}", flush=True)

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
                print(f"[shot {_n[0]:02d} {tag}] url={page.url[:90]}", flush=True)
            except Exception as e:
                print(f"[shot {tag} err: {type(e).__name__}]", flush=True)

        try:
            await page.goto(row["apply_url"], wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            await _shot("landing")
            strat = OracleORCStrategy()
            # ORC_ADVANCE is read at import into the class default; honor a late env set too.
            strat.advance_wizard = os.getenv("ORC_ADVANCE", "").strip().lower() in (
                "1", "true", "yes", "on")
            result = await strat.prefill(
                page, pf, p["resume_path"],
                job={"title": row["title"], "company": row["company"]},
                draft=True, facts=p["facts"], profile_id=p["profile_id"])
            print(f"[filled: unfilled={result.get('unfilled')} "
                  f"review_items={len(result.get('review_items') or [])} "
                  f"page_type={result.get('page_type')} "
                  f"at_submit={result.get('wizard_at_submit')}]", flush=True)
            await _shot("after_prefill")

            confirmed = False
            if strat.advance_wizard:
                deadline = start_ts + keep_minutes * 60
                confirmed = await _submit_and_watch(page, pf["email"], start_ts, result,
                                                     _shot, deadline)
            if not confirmed:
                print("[no confirmation within --keep (expected if ORC_ADVANCE is off, or the "
                      "wizard stalled — inspect logs/orc_recon/%s screenshots)]" % job_id,
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:200]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
    print("=== orc apply done", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, help="mass_hiring_jobs id (alorica / oracle_orc)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list auto-applyable ORC job ids and exit")
    args = ap.parse_args()
    if args.list:
        ids = orc_job_ids()
        print(f"{len(ids)} auto-applyable Oracle ORC jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
