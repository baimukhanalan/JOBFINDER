r"""Workday CxS mass-hiring auto-apply DRIVER (Humana + Centene), headful on DISPLAY=:98 with the
NopeCHA extension solving the register-step reCAPTCHA.

WHY A STANDALONE DRIVER (not the co-pilot bulk_pool)
----------------------------------------------------
Workday's "Create Account" step is gated by a reCAPTCHA (v2-checkbox / v2-Enterprise). The paid
solver we already have is the **NopeCHA browser extension** (`backend/vendor/nopecha_ext`, key in
`.env` NOPECHA_KEY) — it auto-solves reCAPTCHA in-page, but ONLY when loaded into a HEADFUL,
persistent-context Chromium (`--load-extension`). The co-pilot bulk_pool
(`backend/tools/bulk_pool.py`) runs HEADLESS and strips DISPLAY, and `copilot.py` uses a
non-persistent context — neither can load an extension. So, exactly like the Teleperformance/iCIMS
lane (`backend/tools/icims_recon.py`), Workday needs its own headful `:98` + NopeCHA driver.
`captcha_solver.solve_on_page` (the CapSolver TOKEN path) stays a harmless no-op — the extension
does the solving. Buy a CAPTCHA_SOLVER_KEY instead only if you want the headless bulk_pool path.

REUSE (nothing rewritten):
  - persona + tailored résumé + prefill dir  ← mass_hiring_apply.prepare(row)  (location-first
    synth_persona already RESIDES in the job's state — Humana location_raw='Remote, Oklahoma', etc.)
  - the fill/submit engine  ← the ALREADY-BUILT + registered strategy the URL routes to:
    WorkdayMassHiringStrategy / PhenomWorkdayStrategy (`backend/applier/strategies/workday.py`,
    `phenom.py`) via runner.STRATEGIES + strat.prefill(...) (the SAME call copilot._load makes).
  - NopeCHA launch args + `_pick_state` (eligibility) + the Maildir confirmation scan  ← icims_recon.

SAFETY: the final Submit is clicked ONLY when env WORKDAY_ADVANCE=1 (default OFF → the strategy
fills the whole form + creates the account but stops at Submit — a dry run that never files an
application). Ground truth of a real submit = the "thank you for applying" email in the persona's
@takhet.com Maildir (`_confirmed`), same as the GH/Ashby/TP lanes.

HONEST STATUS: feasible_needs_live_iteration. The persona/prefill path is tested; the strategy is
built + wired; this driver wires them to a NopeCHA-extension browser. It has NOT been driven live
end-to-end from here — the register reCAPTCHA solve, the Workday wizard step-walk on a live tenant,
and the exact confirmation sender/subject need one live pass on :98 to tune (like TP took).

RUN (mail group needed for the persona mailbox + emailed code + the confirmation read):
    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && python3 -m backend.tools.workday_recon --job <id>'
    # add WORKDAY_ADVANCE=1 to REALLY submit; --fresh forces a new persona; --keep caps minutes.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, mass_hiring_apply as mha, icims_recon  # noqa: E402

# Confirmation senders/subjects for a submitted Workday application (best-effort — tune live).
_CONFIRM_SUBJECT_RE = r"thank you for applying|application (was )?received|we received your application|your application (to|for)"
_CONFIRM_FROM_RE = r"myworkday|workday|humana|centene|no-?reply@"


def _mha_prefill_root():
    return mha.PREFILL_ROOT


def workday_job_ids(sources=("humana", "centene"), limit: int | None = None) -> list[int]:
    """Active Workday-CxS mass-hiring rows to drive — Humana + Centene, on the *.wd5 host."""
    with mail_db.conn() as c:
        cur = c.cursor()
        q = ("SELECT id FROM mass_hiring_jobs WHERE source = ANY(%s) AND active "
             "AND apply_url ILIKE %s ORDER BY id")
        params = [list(sources), "%.wd5.myworkdayjobs.com%"]
        if limit:
            q += " LIMIT %s"
            params.append(int(limit))
        cur.execute(q, params)
        return [r[0] for r in cur.fetchall()]


def expected_state(title: str, location_raw: str) -> tuple[str, str]:
    """The (state_code, state_full) the synthetic persona should RESIDE in for this posting, from the
    job location — so a state-scoped Workday posting ('reside in Oklahoma') is answered truthfully by
    a persona designed to live there. Reuses icims_recon._pick_state; falls back to Ohio."""
    full, code, _city, _zip = icims_recon._pick_state(title or "", location_raw or "")
    return code, full


def _row(job_id: int) -> dict | None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, company, company_key, source, source_id, location_raw "
                    "FROM mass_hiring_jobs WHERE id=%s", (job_id,))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "title": r[1], "apply_url": r[2], "company": r[3], "company_key": r[4],
            "source": r[5], "source_id": r[6], "location_raw": r[7]}


def _confirmed(email: str, since_ts: float) -> bool:
    """True once a Workday 'thank you for applying' receipt for this application has landed in the
    persona's @takhet.com Maildir (received at/after since_ts). Generic sender/subject match — tune
    the regexes once the real tenant confirmation is observed live."""
    import re
    local = (email or "").split("@", 1)[0]
    if not local:
        return False
    base = f"/var/mail/vhosts/takhet.com/{local}"
    subj_re = re.compile(_CONFIRM_SUBJECT_RE, re.I)
    from_re = re.compile(_CONFIRM_FROM_RE, re.I)
    for sub in ("new", "cur"):
        d = os.path.join(base, sub)
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
            frm = re.search(r"^From:.*$", head, re.I | re.M)
            subj = re.search(r"^Subject:.*$", head, re.I | re.M)
            if subj and subj_re.search(subj.group(0)):
                return True
            if frm and from_re.search(frm.group(0)) and subj and "appl" in subj.group(0).lower():
                return True
    return False


def _pick_strategy(url: str):
    """The registered strategy whose matches(url) is True (Workday CxS -> WorkdayMassHiringStrategy /
    PhenomWorkdayStrategy). Same selection copilot._pick_strategy does, via runner.STRATEGIES."""
    from backend.applier import runner
    for cls in runner.STRATEGIES:
        try:
            if cls.matches(url):
                return cls()
        except Exception:
            continue
    from backend.applier.strategies.base import GenericStrategy
    return GenericStrategy()


async def drive_apply(row: dict, *, advance_env: str, keep_minutes: int = 13,
                      confirm=_confirmed) -> dict:
    """Drive ONE mass-hiring application end-to-end in a fresh headful :98 Chromium with the NopeCHA
    extension: synth persona (via mha.prepare) -> route to the built strategy -> strat.prefill fills
    the whole wizard (incl. account creation + the reCAPTCHA the extension solves) -> submit ONLY if
    <advance_env>=1 -> poll the Maildir for the confirmation. Never raises; returns a result dict.

    advance_env: the env var that must be '1' to actually click Submit (WORKDAY_ADVANCE / PHENOM_ADVANCE).
    """
    from pathlib import Path
    from playwright.async_api import async_playwright

    advance = os.getenv(advance_env, "0").strip().lower() in ("1", "true", "yes", "on")
    out = {"jobid": row["id"], "persona": None, "filled": None, "clicked": False,
           "confirmed": False, "advance": advance, "error": None}
    url = row.get("apply_url") or ""
    if not url:
        out["error"] = "no apply_url"
        return out

    # 1) persona + tailored résumé + prefill dir (mha.prepare places the persona in the job's state)
    try:
        profile_id, jobid = mha.prepare(row)
    except Exception as e:
        out["error"] = f"prepare: {type(e).__name__}: {e}"
        return out
    out["profile_id"] = profile_id
    d = Path(_mha_prefill_root()) / profile_id / jobid
    import json
    try:
        persona = json.loads((d / "persona.json").read_text(encoding="utf-8"))
        report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    except Exception as e:
        out["error"] = f"prefill artifacts: {type(e).__name__}: {e}"
        return out
    from backend.profiles.store import Profile
    prof = Profile.from_dict(persona.get("profile") or {})
    facts = persona.get("facts") or {}
    form = prof.to_form_dict()
    email = (form.get("email") or "").strip()
    out["persona"] = email
    resume_pdf = str(d / "resume.pdf")
    known = report.get("drafted_answers") or {}
    title = row.get("title") or ""
    company = row.get("company") or ""

    # 2) fresh isolated Chromium profile + the NopeCHA extension (headful :98) — mirrors icims_recon.
    profile_dir = os.path.join(tempfile.gettempdir(), f"wd_prof_{row['id']}_{os.getpid()}")
    shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)
    ext = icims_recon.NOPECHA_EXT
    ext_args = ([f"--disable-extensions-except={ext}", f"--load-extension={ext}"]
                if os.path.isdir(ext) else [])
    deadline = time.time() + keep_minutes * 60
    started = time.time()
    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                profile_dir, headless=False, channel="chromium", no_viewport=True,
                locale="en-US", timezone_id="America/New_York",
                args=["--start-maximized"] + ext_args)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            # preseed the NopeCHA key (same setup URL icims_recon uses) so the extension solves reCAPTCHA
            if ext_args:
                try:
                    key = (os.getenv("NOPECHA_KEY") or "").strip()
                    cfg = ("input_method=javascript|recaptcha_auto_open=true|recaptcha_auto_solve=true|"
                           "recaptcha_solve_delay_time=300|enabled=true" + (f"|key={key}" if key else ""))
                    sp = await ctx.new_page()
                    await sp.goto("https://nopecha.com/setup#" + cfg,
                                  wait_until="domcontentloaded", timeout=45000)
                    await sp.wait_for_timeout(3000)
                    await sp.close()
                except Exception as e:
                    print(f"[nopecha setup: {type(e).__name__}: {e}]"[:120], flush=True)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                out["error"] = f"goto: {type(e).__name__}: {e}"
                await ctx.close()
                return out

            # 3) route to the built strategy + fill the whole wizard (account creation + reCAPTCHA
            #    handled inside prefill; the NopeCHA extension solves the on-page challenge).
            strat = _pick_strategy(url)
            out["strategy"] = type(strat).__name__
            try:
                result = await strat.prefill(page, form, resume_pdf,
                                             job={"title": title, "company": company},
                                             draft=True, resume_summary="",
                                             known_answers=known, facts=facts,
                                             profile_id=profile_id, niche="")
                out["filled"] = {"filled": (result or {}).get("filled"),
                                 "unfilled": (result or {}).get("unfilled"),
                                 "review_items": (result or {}).get("review_items")}
            except Exception as e:
                out["error"] = f"prefill: {type(e).__name__}: {e}"
                await ctx.close()
                return out

            # 4) submit ONLY on the advance gate — best-effort generic submit (needs live tuning per
            #    Workday tenant); default OFF = dry-run fill that never files an application.
            if advance:
                try:
                    from backend.applier import analyzer, filler
                    ok = False
                    try:
                        ok = await filler.click_submit(page, result or {})
                    except Exception:
                        ok = False
                    if not ok:
                        sel = await analyzer.find_submit_button(page)
                        if sel:
                            await page.click(sel, timeout=8000)
                            ok = True
                    out["clicked"] = bool(ok)
                except Exception as e:
                    out["error"] = f"submit: {type(e).__name__}: {e}"
            else:
                print("[dry-run — filled, NOT clicking Submit (set "
                      f"{advance_env}=1 to really apply)]", flush=True)

            # 5) poll the Maildir for the real confirmation (exit early once seen)
            while time.time() < deadline:
                if email and confirm(email, started):
                    out["confirmed"] = True
                    print("[application CONFIRMED — receipt in the persona mailbox]", flush=True)
                    break
                if not advance:            # dry-run: no receipt is coming, don't idle
                    break
                await page.wait_for_timeout(10000)
            try:
                await ctx.close()
            except Exception:
                pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    return out


def main() -> None:
    import asyncio
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Auto-apply to Workday CxS mass-hiring jobs (Humana/Centene).")
    ap.add_argument("--job", type=int, default=0, help="one mass_hiring_jobs id")
    ap.add_argument("--limit", type=int, default=0, help="first N Workday jobs (when --job omitted)")
    ap.add_argument("--fresh", action="store_true", help="(accepted for CLI parity; each job is a fresh persona)")
    ap.add_argument("--keep", type=int, default=13, help="minutes cap per application")
    args = ap.parse_args()

    ids = [args.job] if args.job else workday_job_ids(limit=(args.limit or None))
    if not ids:
        print("no Workday (Humana/Centene) jobs on the board", flush=True)
        return
    adv = os.getenv("WORKDAY_ADVANCE", "0") in ("1", "true", "yes", "on")
    print(f"{'APPLYING' if adv else 'DRY-RUN'} to {len(ids)} Workday job(s) "
          f"(WORKDAY_ADVANCE={'1' if adv else '0'})", flush=True)
    conf = 0
    for jid in ids:
        row = _row(jid)
        if not row:
            print(f"job {jid}: no row", flush=True)
            continue
        res = asyncio.run(drive_apply(row, advance_env="WORKDAY_ADVANCE", keep_minutes=args.keep))
        conf += 1 if res.get("confirmed") else 0
        print(f"job {jid} persona={res.get('persona')} strategy={res.get('strategy')} "
              f"clicked={res.get('clicked')} confirmed={res.get('confirmed')} "
              f"error={res.get('error')}", flush=True)
    print(f"done: {len(ids)} jobs, confirmed={conf}", flush=True)


if __name__ == "__main__":
    main()
