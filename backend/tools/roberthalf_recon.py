"""Driver: auto-apply to one Robert Half job (Salesforce candidate account) end-to-end.

Robert Half's per-job apply hands off to a Salesforce candidate community: create an account
(email + password) → verify an emailed OTP → fill the authenticated apply form → Submit, with
a reCAPTCHA Enterprise on the register/submit step. `strategies/roberthalf.RobertHalfStrategy`
owns that flow; this driver mints a fresh synthetic US persona (with a PROVISIONED @takhet.com
mailbox so the OTP is receivable), drives one job, and polls the Maildir for the ack.

Mirrors amazon_recon:
  * Default (ROBERTHALF_ADVANCE unset) = a DRY-RUN: fill only to the account wall, report
    `needs_account`. NOTHING is created and NO PII is transmitted.
  * ROBERTHALF_ADVANCE=1 lets the strategy create the account (verify the email OTP) + walk the
    form. On an advance run this driver arms the FREE in-browser reCAPTCHA path (the vendored
    NopeCHA extension, headful) so the register reCAPTCHA can be solved with no key; a paid
    CAPTCHA_SOLVER_KEY escalation still applies where present.
  * The final Submit is clicked by THIS driver only when advancing AND the wizard reached Submit
    with `unfilled==[]`; otherwise the recorded selector is left for a human. Ground truth of
    success = a real Robert Half "application received" email in the persona Maildir.

Run under `sg mail` (mailbox provisioning + the Maildir OTP/confirmation read need the mail
group). Headful on :98 by default (the Salesforce SPA + reCAPTCHA reject headless); headless via
ROBERTHALF_HEADLESS=1 (no reCAPTCHA extension then).
"""
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

# Reuse the Amazon lane's generic bits (NopeCHA arming + the state->city/zip placement table).
from backend.tools.amazon_recon import (  # noqa: E402
    NOPECHA_EXT,
    _arm_nopecha,
    _DEFAULT_STATE,
    _STATE_PLACE,
)


def _state_from_location(location_raw: str) -> str:
    """Full US state name a staffing row is tied to, or '' when the location names none.

    Rows are e.g. 'Remote, Houston, Texas, United States' / 'Remote, TX, United States' /
    'Remote, United States'. Returns the resolved full state name ('' when none is named)."""
    from backend.tools.synth_persona import _us_state_full
    loc = (location_raw or "").strip()
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    for tok in parts:
        low = tok.lower()
        if low in ("remote", "virtual", "usa", "us", "united states", "any location", "any",
                   "work from home", "wfh", "nationwide"):
            continue
        full = _us_state_full(tok)
        if full:
            return full
    return ""


def build_persona(row: dict, source: str, company: str) -> dict:
    """Fresh synthetic US persona for a staffing/insurer job, PLACED in the job's state (or a
    default when the row is state-less). Provisions the mailbox + registers the demo persona (so a
    recruiter reply lands in a CRM-visible box), writes the prefill dir, and returns the
    profile_form the strategy fills. Generic across the Robert Half / Adecco / Progressive lanes."""
    from backend.tools import mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    title = row.get("title") or ""
    state = _state_from_location(row.get("location_raw") or "") or _DEFAULT_STATE
    city, zc = _STATE_PLACE.get(state, ("Austin", "78701"))
    job = {"title": title, "company": company, "company_key": source,
           "description": title, "location": f"Remote, {city}, {state}, United States",
           "regions": ["US"], "ats": source, "external_id": str(row.get("id") or ""),
           "url": row.get("apply_url") or "", "questions": []}

    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job)
    prof = cand["profile"]
    facts = cand.get("facts") or {}
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
        print(f"[{source}] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    # MANDATORY attractiveness pass via the shared engine (role-targeted, no-fabrication, guarded).
    from backend.tools import mass_hiring_apply as _mha
    d = _mha.tailored_draft(job, cand)
    out.joinpath("resume.pdf").write_bytes(_mha.resume_pdf_bytes(d, cand))
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": facts}, ensure_ascii=False), encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": source, "mass_hiring_id": row["id"]}), encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": job["url"], "job_title": title, "company": company,
                    "profile": profile_id, "submitted": False}, indent=2), encoding="utf-8")

    name = prof.get("full_name") or ""
    nparts = name.split()
    profile_form = {
        "full_name": name,
        "first_name": prof.get("first_name") or (nparts[0] if nparts else ""),
        "last_name": prof.get("last_name") or (nparts[-1] if len(nparts) > 1 else ""),
        "email": prof.get("email") or "", "phone": prof.get("phone") or "",
        "street_address": prof.get("street_address") or "1200 Market Street",
        "address": prof.get("street_address") or "1200 Market Street",
        "city": city, "state": state, "zip": zc, "postal_code": zc, "country": "United States",
        "sex": (prof.get("sex") or ""),
    }
    return {"profile_form": profile_form, "facts": facts,
            "resume_path": str(out / "resume.pdf"), "state": state,
            "jobid": jobid, "profile_id": profile_id}


_CONFIRM_SUBJECT_RE = re.compile(
    r"thank you for applying|thank you for your application|received your application|"
    r"application (?:has been )?received|application confirmation|we(?:'| ha)?ve received|"
    r"your application", re.I)


def _is_confirmation(from_hdr: str, subject: str, sender_hints: tuple[str, ...]) -> bool:
    """PURE: True iff a mail looks like the tenant's application confirmation — from a tenant
    sender, or a matching 'thank you for applying' subject."""
    frm = (from_hdr or "").lower()
    if any(h in frm for h in sender_hints):
        return True
    return bool(_CONFIRM_SUBJECT_RE.search(subject or ""))


def _is_roberthalf_confirmation(from_hdr: str, subject: str) -> bool:
    return _is_confirmation(from_hdr, subject, ("roberthalf", "salesforce", "rhi.com"))


def _app_confirmed(email: str, since_ts: float, matcher) -> bool:
    """True once an application confirmation matching `matcher(from,subj)` has landed in the
    persona's Maildir at/after since_ts."""
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
            if matcher(frm.group(0) if frm else "", subj.group(0) if subj else ""):
                return True
    return False


def _lane_proxy(us_env: str, proxy_env: str, name: str = ""):
    """DIRECT-default US-egress resolver for the lane (a US-residential IP if the lane opts in;
    NEVER a KZ phone slot). Guarded."""
    try:
        from backend.tools import us_egress
        return us_egress.lane_us_egress(us_env, proxy_env, name)
    except Exception:
        return None


def _advance_enabled(env: str) -> bool:
    return os.getenv(env, "").strip().lower() in ("1", "true", "yes", "on")


async def _drive(row: dict, *, source: str, company: str, strategy_cls, advance_env: str,
                 us_env: str, proxy_env: str, nopecha_env: str, confirm_matcher,
                 keep_minutes: int, fresh: bool) -> dict:
    """Shared drive loop for the Salesforce/SPA account-wall lanes. Returns a small result dict."""
    from backend.applier import captcha_solver
    from playwright.async_api import async_playwright

    advance = _advance_enabled(advance_env)
    p = build_persona(row, source, company)
    pf = p["profile_form"]

    headless = os.getenv(f"{source.upper()}_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    # arm the FREE in-browser reCAPTCHA path (NopeCHA) by default on an advance run (needs headful).
    nopecha_on = os.path.isdir(NOPECHA_EXT) and (
        _advance_enabled(nopecha_env) or (advance and os.getenv(nopecha_env) is None))
    if nopecha_on and headless:
        headless = False
    try:
        solver_armed = captcha_solver.is_enabled() or nopecha_on
    except Exception:
        solver_armed = nopecha_on
    px = _lane_proxy(us_env, proxy_env, pf["email"])

    print(f"=== {company} apply: job {row['id']} — {row['title']}  [{row['location_raw']}]",
          flush=True)
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state']} | "
          f"{advance_env}={os.getenv(advance_env, '')} solver={'on' if solver_armed else 'off'} "
          f"egress={('US ' + px['server']) if px else 'DIRECT'}", flush=True)

    profile_dir = os.path.join(tempfile.gettempdir(), f"{source}_prof_{row['id']}_{os.getpid()}")
    if fresh:
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if not headless:
        args.insert(0, "--start-maximized")
    if nopecha_on:
        args += [f"--disable-extensions-except={NOPECHA_EXT}", f"--load-extension={NOPECHA_EXT}"]
    launch = dict(headless=headless, channel="chrome", no_viewport=not headless,
                  locale="en-US", timezone_id="America/New_York", args=args)
    if px:
        launch["proxy"] = px

    result = {"job": row["id"], "submitted": False, "confirmed": False}
    start_ts = time.time()
    async with async_playwright() as pw:
        try:
            ctx = await pw.chromium.launch_persistent_context(profile_dir, **launch)
        except Exception:
            # a machine without real Chrome falls back to bundled chromium (no reCAPTCHA ext).
            launch["channel"] = "chromium"
            ctx = await pw.chromium.launch_persistent_context(profile_dir, **launch)
        if nopecha_on:
            await _arm_nopecha(ctx)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        shot_dir = os.path.join(REPO, "logs", f"{source}_recon", str(row["id"]))
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
            await _shot("landed")
            strat = strategy_cls()
            report = await strat.prefill(
                page, pf, p["resume_path"],
                job={"title": row["title"], "company": company},
                draft=not advance, facts=p["facts"], profile_id=p["profile_id"])
            await _shot("after_fill")
            result["report"] = {k: report.get(k) for k in
                                ("page_type", "needs_account", "wizard_at_submit", "unfilled",
                                 "filled")}
            print(f"[page_type={report.get('page_type')} needs_account={report.get('needs_account')} "
                  f"wizard_at_submit={report.get('wizard_at_submit')} "
                  f"unfilled={report.get('unfilled')}]", flush=True)
            if report.get("needs_account"):
                print(f"[CEILING: stopped at the {company} account wall — advance run needs "
                      f"{advance_env}=1 (+ the free NopeCHA reCAPTCHA path / a solver key). "
                      f"Nothing transmitted.]", flush=True)

            sel = report.get("submit_selector")
            if (advance and solver_armed and report.get("wizard_at_submit")
                    and not report.get("unfilled") and sel):
                try:
                    await page.click(sel, timeout=8000)
                    await page.wait_for_timeout(3000)
                    result["submitted"] = True
                    await _shot("after_submit")
                    print("[SUBMIT clicked — awaiting the confirmation email]", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[submit click failed: {type(e).__name__}: {str(e)[:120]}]", flush=True)
            elif report.get("wizard_at_submit"):
                print("[wizard reached Submit but NOT clicked (dry-run / solver off / unfilled) — "
                      "selector recorded for a human]", flush=True)

            deadline = start_ts + keep_minutes * 60
            while result["submitted"] and time.time() < deadline:
                if _app_confirmed(pf["email"], start_ts - 60, confirm_matcher):
                    result["confirmed"] = True
                    print("[application CONFIRMED — receipt in the Maildir]", flush=True)
                    break
                await asyncio.sleep(10)
            if result["submitted"] and not result["confirmed"]:
                print("[no confirmation within --keep (acks can lag; read the shots)]", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:180]}]", flush=True)
            result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
    print(f"=== {source} apply done", flush=True)
    return result


def job_ids(source: str) -> list[int]:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM mass_hiring_jobs WHERE source=%s AND active ORDER BY id",
                    (source,))
        return [r[0] for r in cur.fetchall()]


def _row(job_id: int, source: str) -> dict | None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, location_raw FROM mass_hiring_jobs "
                    "WHERE id=%s AND source=%s", (job_id, source))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "title": r[1], "apply_url": r[2], "location_raw": r[3]}


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    from backend.applier.strategies.roberthalf import RobertHalfStrategy
    row = _row(job_id, "roberthalf")
    if not row:
        print(f"no roberthalf mass_hiring_jobs row id={job_id}", flush=True)
        return
    await _drive(row, source="roberthalf", company="Robert Half",
                 strategy_cls=RobertHalfStrategy, advance_env="ROBERTHALF_ADVANCE",
                 us_env="ROBERTHALF_US", proxy_env="ROBERTHALF_PROXY",
                 nopecha_env="ROBERTHALF_NOPECHA", confirm_matcher=_is_roberthalf_confirmation,
                 keep_minutes=keep_minutes, fresh=fresh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=roberthalf)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list active Robert Half job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = job_ids("roberthalf")
        print(f"{len(ids)} active Robert Half jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
