"""Driver: auto-apply to one Amazon corporate/virtual job (account.amazon.jobs) end-to-end.

SCOPE — the Amazon inventory we actually COLLECT + can REACH is the corporate/virtual slice at
`www.amazon.jobs` (the mass_hiring `source='amazon'` rows: e.g. "Bilingual Technical Customer
Support, Ring", location "Virtual, <State>, USA"). Their apply URL is
`account.amazon.jobs/jobs/<id>/apply`, which SAML-redirects to the Amazon **Passport** account
wall. `strategies/amazon_apply.AmazonStrategy` owns that flow: create the Passport account (guarded
by an AWS WAF CAPTCHA) → verify the emailed OTP (read from the persona @takhet.com Maildir) →
fill + advance the authenticated apply wizard → RECORD the final Submit selector.

The **hourly** volume board (`hiring.amazon.com` — warehouse/fulfillment/delivery/CS) is a SEPARATE
system and is NOT driven here: it is CloudFront-403-blocked from this datacenter IP (proven), its
flow terminates in an IN-PERSON New Hire Appointment (badge photo / I-9 / physical first day), and
we do not collect its rows. See the recon notes in CLAUDE.md.

GATING (mirrors the Avature/Oracle-ORC lanes):
  * Default (AMAZON_ADVANCE unset) = a DRY-RUN: fill only to the Passport wall, report
    `needs_account`/`login_required`. NOTHING is created and NO PII is transmitted.
  * AMAZON_ADVANCE=1 lets the strategy bootstrap the account (create it, verify the email OTP) and
    walk the wizard. It ALSO needs a CapSolver key (`CAPTCHA_SOLVER_KEY`, for the AWS WAF challenge)
    and — for a non-flagged reCAPTCHA/WAF score — a US RESIDENTIAL egress (a live phone slot, else
    DIRECT). Both are graceful no-ops otherwise, so an advance run without the key just lands on the
    Passport wall like a dry-run.
  * The final Submit is clicked by THIS driver only when advancing AND the solver is armed AND the
    wizard reached Submit with `unfilled==[]` (mirrors smartrecruiters_recon's "submit only when
    complete"); otherwise the recorded selector is left for a human. Ground truth of success = the
    Amazon "Thank you for applying" / "application received" email in the persona Maildir.

    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        AMAZON_ADVANCE=1 CAPTCHA_SOLVER_KEY=... python3 -m backend.tools.amazon_recon --job <id> --fresh'

Run under `sg mail` (mailbox provisioning + the Maildir OTP/confirmation read need the mail group).
Headful on :98 by default (the Passport SPA + AWS WAF + reCAPTCHA scoring reject headless); headless
via AMAZON_HEADLESS=1."""
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
NOPECHA_EXT = os.path.join(REPO, "backend", "vendor", "nopecha_ext")


def _nopecha_key() -> str:
    """The NopeCHA subscription key (env, else a direct .env parse — pm2/cron don't export .env)."""
    k = os.environ.get("NOPECHA_KEY", "").strip()
    if k:
        return k
    try:
        for line in open(os.path.join(REPO, "backend", ".env")):
            if line.strip().startswith("NOPECHA_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


async def _arm_nopecha(ctx) -> None:
    """Arm the vendored NopeCHA extension (in-page reCAPTCHA/hCaptcha/Turnstile auto-solve) via its
    setup URL — same pattern as the co-pilot. NOTE: NopeCHA does NOT solve Amazon's Passport AWS WAF
    challenge (proprietary `aws-waf-token`, no NopeCHA support) — that gate needs CapSolver's
    AntiAwsWafTask (captcha_solver, CAPTCHA_SOLVER_KEY). NopeCHA only covers a reCAPTCHA that may
    appear at a later step. Never raises."""
    try:
        key = _nopecha_key()
        cfg = ("input_method=javascript|hcaptcha_auto_open=true|hcaptcha_auto_solve=true|"
               "recaptcha_auto_solve=true|turnstile_auto_solve=true|"
               "hcaptcha_solve_delay_time=200|enabled=true" + (f"|key={key}" if key else ""))
        sp = await ctx.new_page()
        await sp.goto("https://nopecha.com/setup#" + cfg, wait_until="domcontentloaded", timeout=45000)
        await sp.wait_for_timeout(3500)
        await sp.close()
        print(f"[nopecha armed ({'key' if key else 'free tier'}) — reCAPTCHA fallback only]",
              flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[nopecha arm failed: {type(e).__name__}]", flush=True)

# state-full -> (city, zip) so the persona's US address is coherent with the state the posting names
# (a virtual role's screeners check the state / residence; a plausible in-state city+zip keeps the
# form honest). Reused from the Foundever lane's table.
try:
    from backend.tools.foundever_recon import _DEFAULT_STATE, _STATE_PLACE
except Exception:  # pragma: no cover - fallback if the sibling import shape changes
    _STATE_PLACE = {"Arizona": ("Phoenix", "85004"), "Texas": ("Austin", "78701"),
                    "Ohio": ("Columbus", "43215")}
    _DEFAULT_STATE = "Texas"


def _is_bilingual(title: str) -> bool:
    """True when the posting is a bilingual role (Amazon's virtual CS reqs are e.g. 'Bilingual
    Technical Customer Support') — so the persona is DEFINED bilingual (a persona attribute, owner
    policy) and the language screeners answer affirmatively."""
    return "bilingual" in (title or "").lower()


def _state_from_amazon_location(location_raw: str) -> str:
    """Full US state name the Amazon row is tied to, or '' for a state-less 'Virtual, USA'.

    Amazon virtual rows are 'Virtual, <State>, USA' (the state-tagged CS roles) or 'Virtual, USA'
    (any US state fits). Returns the resolved full state name ('' when none is named)."""
    from backend.tools.synth_persona import _us_state_full
    loc = (location_raw or "").strip()
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    for tok in parts:
        low = tok.lower()
        if low in ("virtual", "usa", "us", "united states", "remote", "any location", "any"):
            continue
        full = _us_state_full(tok)
        if full:
            return full
    return ""


def _build_persona(row: dict) -> dict:
    """Fresh synthetic US persona for this Amazon job, PLACED in the job's state (or a default when
    the row is state-less). Provisions the mailbox + registers the demo persona (so an Amazon reply
    lands in a CRM-visible box), writes the prefill dir, and returns the profile_form the strategy
    fills."""
    from backend.tools import catalog_drafts, drafts_ui, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    title = row.get("title") or ""
    state = _state_from_amazon_location(row.get("location_raw") or "") or _DEFAULT_STATE
    city, zc = _STATE_PLACE.get(state, ("Austin", "78701"))
    job = {"title": title, "company": "Amazon",
           "company_key": "amazon", "description": title,
           "location": f"Virtual, {city}, {state}, United States", "regions": ["US"],
           "ats": "amazon", "external_id": str(row.get("id") or ""),
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
    # place the persona in the job's state (residence-screener truthfulness)
    prof["city"] = city
    prof["state"] = state
    prof["location"] = f"{city}, {state}"
    pi = (prof.get("resume") or {}).get("personal_info")
    if isinstance(pi, dict):
        pi["location"] = f"{city}, {state}"
    # a bilingual req -> the persona is DEFINED bilingual so the language screeners answer truthfully
    if _is_bilingual(title):
        facts["bilingual"] = True
        facts.setdefault("second_language", "Spanish")
        facts.setdefault("spanish_level", "Fluent")

    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""),
                                      prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[amazon] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    try:
        d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)
        out.joinpath("resume.pdf").write_bytes(drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")
    except Exception as e:  # noqa: BLE001
        print(f"[amazon] resume gen skipped: {type(e).__name__}: {e}", flush=True)
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": facts}, ensure_ascii=False), encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": "amazon", "mass_hiring_id": row["id"]}), encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": job["url"], "job_title": title, "company": "Amazon",
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
    }
    return {"profile_form": profile_form, "facts": facts,
            "resume_path": str(out / "resume.pdf"), "state": state,
            "jobid": jobid, "profile_id": profile_id}


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once an Amazon application confirmation has landed in the persona's Maildir at/after
    since_ts (a *.amazon.jobs / amazon.com sender, or a matching 'thank you for applying' subject)."""
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
            if _is_amazon_confirmation(frm.group(0) if frm else "", subj.group(0) if subj else ""):
                return True
            if "amazon.jobs" in s or "amazon.com" in s:
                if any(k in s for k in ("appl", "thank you", "received", "next step")):
                    return True
    return False


_CONFIRM_SUBJECT_RE = re.compile(
    r"thank you for applying|thank you for your application|received your application|"
    r"application (?:has been )?received|your application to amazon|next steps? (?:in|for) your",
    re.I)


def _is_amazon_confirmation(from_hdr: str, subject: str) -> bool:
    """PURE: True iff a mail looks like the Amazon application confirmation — from a *.amazon.jobs /
    amazon.com sender, or a matching 'thank you for applying' subject."""
    frm = (from_hdr or "").lower()
    if "amazon.jobs" in frm or "@amazon.com" in frm or "hiring.amazon" in frm:
        return True
    return bool(_CONFIRM_SUBJECT_RE.search(subject or ""))


def _amazon_proxy(name: str = ""):
    """A LIVE residential phone egress slot (Playwright proxy dict) or None (DIRECT). Amazon
    risk-flags datacenter IPs for the AWS WAF token + reCAPTCHA score, so a live phone slot is
    preferred; AMAZON_PROXY overrides; None when no phone is live (DIRECT — still reaches the
    Passport wall for a dry-run)."""
    override = (os.getenv("AMAZON_PROXY") or "").strip()
    if override:
        return {"server": override}
    try:
        from backend.tools import proxy_pool
        slots = [s for s in proxy_pool.residential_slots() if str(s).startswith("socks5://")]
    except Exception:
        slots = []
    if not slots:
        return None
    idx = abs(hash(name)) % len(slots) if name else 0
    return {"server": slots[idx]}


def _advance_enabled() -> bool:
    return os.getenv("AMAZON_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, location_raw FROM mass_hiring_jobs "
                    "WHERE id=%s AND source='amazon'", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no amazon mass_hiring_jobs row id={job_id}", flush=True)
        return
    row = {"id": r[0], "title": r[1], "apply_url": r[2], "location_raw": r[3]}
    print(f"=== Amazon apply: job {row['id']} — {row['title']}  [{row['location_raw']}]", flush=True)

    advance = _advance_enabled()
    try:
        from backend.applier import captcha_solver
        solver_armed = captcha_solver.is_enabled()
    except Exception:
        solver_armed = False
    if advance and not solver_armed:
        print("[NOTE] AMAZON_ADVANCE set but no CAPTCHA_SOLVER_KEY — the Passport account CANNOT be "
              "created (AWS WAF gate); this run will land on the account wall like a dry-run.",
              flush=True)

    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state']} "
          f"| AMAZON_ADVANCE={os.getenv('AMAZON_ADVANCE', '')} solver={'on' if solver_armed else 'off'}",
          flush=True)

    profile_dir = os.getenv("AMAZON_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"amazon_prof_{job_id}_{os.getpid()}")
    if fresh:
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    headless = os.getenv("AMAZON_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    nopecha_on = (os.getenv("AMAZON_NOPECHA", "").strip().lower() in ("1", "true", "yes", "on")
                  and os.path.isdir(NOPECHA_EXT))
    if nopecha_on and headless:
        headless = False  # a Chrome extension only loads in a non-headless persistent context
    px = _amazon_proxy(pf["email"])
    print(f"egress: {'residential slot' if px else 'DIRECT (no live phone slot)'}", flush=True)

    from playwright.async_api import async_playwright

    from backend.applier.strategies.amazon_apply import AmazonStrategy

    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if not headless:
        args.insert(0, "--start-maximized")
    if nopecha_on:
        args += [f"--disable-extensions-except={NOPECHA_EXT}", f"--load-extension={NOPECHA_EXT}"]
    launch = dict(headless=headless, channel="chromium", no_viewport=not headless,
                  locale="en-US", timezone_id="America/New_York", args=args)
    if px:
        launch["proxy"] = px

    start_ts = time.time()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(profile_dir, **launch)
        if nopecha_on:
            await _arm_nopecha(ctx)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        shot_dir = os.path.join(REPO, "logs", "amazon_recon", str(job_id))
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
            await _shot("passport_or_form")
            strat = AmazonStrategy()
            result = await strat.prefill(
                page, pf, p["resume_path"],
                job={"title": row["title"], "company": "Amazon"},
                draft=not advance, facts=p["facts"], profile_id=p["profile_id"])
            await _shot("after_fill")
            print(f"[page_type={result.get('page_type')} needs_account={result.get('needs_account')} "
                  f"wizard_at_submit={result.get('wizard_at_submit')} unfilled={result.get('unfilled')}]",
                  flush=True)

            if result.get("needs_account"):
                print("[CEILING: stopped at the Amazon Passport account wall — go-live needs "
                      "CAPTCHA_SOLVER_KEY (AWS WAF) + a US residential IP. Nothing transmitted.]",
                      flush=True)

            # Click the recorded final Submit ONLY when fully armed + complete (real submission).
            submitted = False
            sel = result.get("submit_selector")
            if (advance and solver_armed and result.get("wizard_at_submit")
                    and not result.get("unfilled") and sel):
                try:
                    await page.click(sel, timeout=8000)
                    await page.wait_for_timeout(3000)
                    submitted = True
                    await _shot("after_submit")
                    print("[SUBMIT clicked — awaiting the Amazon confirmation email]", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[submit click failed: {type(e).__name__}: {str(e)[:120]}]", flush=True)
            elif result.get("wizard_at_submit"):
                print("[wizard reached Submit but NOT clicked (dry-run / solver off / unfilled) — "
                      "selector recorded for a human]", flush=True)

            deadline = start_ts + keep_minutes * 60
            confirmed = False
            while submitted and time.time() < deadline:
                if _app_confirmed(pf["email"], start_ts - 60):
                    confirmed = True
                    print("[application CONFIRMED — Amazon receipt in the Maildir]", flush=True)
                    break
                await asyncio.sleep(10)
            if submitted and not confirmed:
                print("[no confirmation within --keep (Amazon acks can lag; read the shots)]",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:180]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
    print("=== amazon apply done", flush=True)


def amazon_job_ids() -> list[int]:
    """Active Amazon (corporate/virtual) jobs on the mass-hiring board."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM mass_hiring_jobs WHERE source='amazon' AND active ORDER BY id")
        out = [r[0] for r in cur.fetchall()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=amazon)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list active Amazon job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = amazon_job_ids()
        print(f"{len(ids)} active Amazon jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
