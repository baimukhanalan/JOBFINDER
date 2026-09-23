"""Driver: auto-apply to one Afni (ADP "myjobs") job end-to-end via an email-OTP account.

Afni's public board is `myjobs.adp.com/afniexternalcareers` (mass_hiring `source='afni'`: e.g.
"Remote Customer Service Representative" / "Full-Time Remote Insurance Representative", location
"<ST>, United States"). Its apply URL is `myjobs.adp.com/afniexternalcareers/cx/job-details/<reqId>`,
and the guest apply is gated behind a MANDATORY candidate account — but NOT a hard wall, because
ADP AIM verifies the account with an EMAILED one-time password that lands in the persona's own
@takhet.com Maildir (read via verify_code.read_code). `strategies/adp.AdpStrategy` owns that flow:
job-details -> "Sign in"/Apply -> /auth -> email -> Continue -> create-profile -> EMAIL OTP ->
authenticated apply form (AckPrivacyStatement + fields) -> RECORD the final Submit selector.

Recon 2026-09-23: NO captcha at any step; the ADP flow is reachable from the plain server IP, so
this lane is DIRECT-default (no US egress needed — opt in with AFNI_US=1 / AFNI_PROXY=<url> only).

GATING (mirrors the Amazon/Avature/Oracle-ORC lanes):
  * Default (AFNI_ADVANCE unset) = a DRY-RUN: navigate to /auth and FILL the email box, then STOP
    before pressing Continue. NOTHING is created, NO OTP is generated and NO PII is transmitted;
    the report says `needs_account`/`login_required` with `reached_auth=True`.
  * AFNI_ADVANCE=1 lets the strategy bootstrap the account (create the profile, GENERATE + verify
    the emailed OTP) and walk the apply wizard.
  * The final Submit is clicked by THIS driver only when advancing AND the wizard reached Submit
    with `unfilled==[]` (mirrors smartrecruiters_recon's "submit only when complete"); otherwise
    the recorded selector is left for a human. Ground truth of success = the Afni/ADP
    "application received" email in the persona Maildir (or the on-page confirmation).

    # Dry-run (default) — reaches the account-create/OTP step, side-effect-free:
    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && python3 -m backend.tools.afni_recon --job <id>'
    # Live end-to-end (creates the account, sends the OTP, submits):
    DISPLAY=:98 AFNI_ADVANCE=1 sg mail -c 'cd /home/projects/jobfinder && python3 -m backend.tools.afni_recon --job <id> --fresh --keep 8'

Run under `sg mail` (mailbox provisioning + the Maildir OTP/confirmation read need the mail group).
Headful on :98 by default (the AIM auth micro-frontend behaves best headful); headless via
AFNI_HEADLESS=1."""
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

# state-full -> (city, zip) so the persona's US address is coherent with the state the posting
# names; reused from the Foundever lane's table (a remote Afni CSR/insurance-rep persona needs a
# plausible in-state city+zip for the address / residence fields).
try:
    from backend.tools.foundever_recon import _DEFAULT_STATE, _STATE_PLACE
except Exception:  # pragma: no cover - fallback if the sibling import shape changes
    _STATE_PLACE = {"Arizona": ("Phoenix", "85004"), "Texas": ("Austin", "78701"),
                    "Ohio": ("Columbus", "43215"), "South Carolina": ("Columbia", "29201"),
                    "North Carolina": ("Charlotte", "28202")}
    _DEFAULT_STATE = "Texas"


def _is_bilingual(title: str) -> bool:
    """True when the posting is a bilingual role — the persona is then DEFINED bilingual (an owner
    persona attribute) and the language screeners answer affirmatively."""
    return "bilingual" in (title or "").lower()


def _state_from_afni_location(location_raw: str) -> str:
    """Full US state name the Afni row is tied to, or '' when none is named.

    Afni rows are '<ST>, United States' (a state code) or 'City, ST, United States' — returns the
    resolved full state name ('' when none is found)."""
    from backend.tools.synth_persona import _us_state_full
    loc = (location_raw or "").strip()
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    for tok in parts:
        low = tok.lower()
        if low in ("virtual", "usa", "us", "united states", "remote", "any location", "any",
                   "work at home", "work from home"):
            continue
        full = _us_state_full(tok)
        if full:
            return full
    return ""


def _build_persona(row: dict) -> dict:
    """Fresh synthetic US persona for this Afni job, PLACED in the job's state (or a default when the
    row is state-less). Provisions the mailbox + registers the demo persona (so the OTP + any Afni
    reply land in a CRM-visible box), writes the prefill dir, and returns the profile_form."""
    from backend.tools import catalog_drafts, drafts_ui, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    title = row.get("title") or ""
    state = _state_from_afni_location(row.get("location_raw") or "") or _DEFAULT_STATE
    city, zc = _STATE_PLACE.get(state, ("Austin", "78701"))
    job = {"title": title, "company": "Afni",
           "company_key": "afni", "description": title,
           "location": f"Remote, {city}, {state}, United States", "regions": ["US"],
           "ats": "adp", "external_id": str(row.get("id") or ""),
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
    if _is_bilingual(title):
        facts["bilingual"] = True
        facts.setdefault("second_language", "Spanish")
        facts.setdefault("spanish_level", "Fluent")

    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""),
                                      prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[afni] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    try:
        d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)
        out.joinpath("resume.pdf").write_bytes(drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")
    except Exception as e:  # noqa: BLE001
        print(f"[afni] resume gen skipped: {type(e).__name__}: {e}", flush=True)
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": facts}, ensure_ascii=False), encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": "afni", "mass_hiring_id": row["id"]}), encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": job["url"], "job_title": title, "company": "Afni",
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


_CONFIRM_SUBJECT_RE = re.compile(
    r"thank you for applying|thank you for your application|received your application|"
    r"application (?:has been )?received|application (?:has been )?submitted|"
    r"your application (?:to|for|with)|we('| ha)?ve received your", re.I)


def _is_afni_confirmation(from_hdr: str, subject: str) -> bool:
    """PURE: True iff a mail looks like the Afni/ADP application confirmation — from an adp.com /
    afni sender, or a matching 'thank you for applying' subject. An OTP / account-verification mail
    is NOT a confirmation (it precedes the submit) so its subject is excluded here."""
    frm = (from_hdr or "").lower()
    subj = (subject or "")
    # An OTP / verification mail is not an application confirmation.
    if re.search(r"one[\s-]?time|verification code|passcode|security code|verify your", subj, re.I):
        return False
    if re.search(r"@afni\.com|adp\.com|myworkday|afniexternalcareers", frm):
        # a real ADP/Afni sender still needs an application-ish subject (not a marketing blast)
        if _CONFIRM_SUBJECT_RE.search(subj) or "afni" in frm:
            return True
    return bool(_CONFIRM_SUBJECT_RE.search(subj))


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once an Afni/ADP application confirmation has landed in the persona's Maildir at/after
    since_ts."""
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
            if _is_afni_confirmation(frm.group(0) if frm else "", subj.group(0) if subj else ""):
                return True
    return False


def _afni_proxy(name: str = ""):
    """Resolve the egress for the Afni ADP lane — DIRECT-default, US-egress opt-in.

    ADP AIM presents NO captcha and the flow is reachable from the plain server IP, so DIRECT is
    the default (unlike the Amazon lane, which arms US egress for the AWS-WAF score). The owner's
    connected phone slots are KAZAKHSTAN residential (a geo-mismatch for a US apply), so this lane
    routes ONLY through the US-egress resolver when explicitly opted in, NEVER a residential phone:
      * AFNI_PROXY=<url>   → that EXACT proxy (`direct`/`none`/`off`/`0`/`false`/`''` → DIRECT)
      * AFNI_US truthy     → us_egress.us_proxy()  (validated free US → Bright Data US → DIRECT)
      * nothing set        → DIRECT (the server's own datacenter IP)
    Guarded (any resolver error → DIRECT). Returns a Playwright proxy dict, or None."""
    try:
        from backend.tools import us_egress
        return us_egress.lane_us_egress("AFNI_US", "AFNI_PROXY", name)
    except Exception:
        return None


def _advance_enabled() -> bool:
    for n in ("AFNI_ADVANCE", "ADP_ADVANCE"):
        if os.getenv(n, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title, apply_url, location_raw FROM mass_hiring_jobs "
                    "WHERE id=%s AND source='afni'", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no afni mass_hiring_jobs row id={job_id}", flush=True)
        return
    row = {"id": r[0], "title": r[1], "apply_url": r[2], "location_raw": r[3]}
    print(f"=== Afni apply: job {row['id']} — {row['title']}  [{row['location_raw']}]", flush=True)

    advance = _advance_enabled()
    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state']} "
          f"| AFNI_ADVANCE={os.getenv('AFNI_ADVANCE', os.getenv('ADP_ADVANCE', ''))}", flush=True)

    profile_dir = os.getenv("AFNI_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"afni_prof_{job_id}_{os.getpid()}")
    if fresh:
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    headless = os.getenv("AFNI_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    px = _afni_proxy(pf["email"])
    print(f"egress: {('US proxy ' + px['server']) if px else 'DIRECT (server IP)'}", flush=True)

    from playwright.async_api import async_playwright

    from backend.applier.strategies.adp import AdpStrategy

    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if not headless:
        args.insert(0, "--start-maximized")
    launch = dict(headless=headless, channel="chromium", no_viewport=not headless,
                  locale="en-US", timezone_id="America/New_York", args=args)
    if px:
        launch["proxy"] = px

    start_ts = time.time()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(profile_dir, **launch)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        shot_dir = os.path.join(REPO, "logs", "afni_recon", str(job_id))
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
            await _shot("job_details")
            strat = AdpStrategy()
            result = await strat.prefill(
                page, pf, p["resume_path"],
                job={"title": row["title"], "company": "Afni"},
                draft=not advance, facts=p["facts"], profile_id=p["profile_id"])
            await _shot("after_fill")
            print(f"[page_type={result.get('page_type')} reached_auth={result.get('reached_auth')} "
                  f"needs_account={result.get('needs_account')} "
                  f"wizard_at_submit={result.get('wizard_at_submit')} unfilled={result.get('unfilled')}]",
                  flush=True)

            if result.get("needs_account"):
                if advance:
                    print("[CEILING: gated on but did not pass the ADP OTP account step — read the "
                          "shots (OTP mail may have lagged / a create-profile field changed).]",
                          flush=True)
                else:
                    print("[DRY-RUN: reached the ADP auth/account-create step (email filled, no "
                          "Continue) — nothing created, no OTP generated, no PII transmitted.]",
                          flush=True)

            submitted = False
            sel = result.get("submit_selector")
            if (advance and result.get("wizard_at_submit")
                    and not result.get("unfilled") and sel):
                try:
                    await page.click(sel, timeout=8000)
                    await page.wait_for_timeout(3000)
                    submitted = True
                    await _shot("after_submit")
                    print("[SUBMIT clicked — awaiting the Afni/ADP confirmation email]", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[submit click failed: {type(e).__name__}: {str(e)[:120]}]", flush=True)
            elif result.get("wizard_at_submit"):
                print("[wizard reached Submit but NOT clicked (dry-run / unfilled) — selector "
                      "recorded for a human]", flush=True)

            deadline = start_ts + keep_minutes * 60
            confirmed = False
            while submitted and time.time() < deadline:
                if _app_confirmed(pf["email"], start_ts - 60):
                    confirmed = True
                    print("[application CONFIRMED — Afni/ADP receipt in the Maildir]", flush=True)
                    break
                await asyncio.sleep(10)
            if submitted and not confirmed:
                print("[no confirmation within --keep (ADP acks can lag; read the shots)]",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:180]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
    print("=== afni apply done", flush=True)


def afni_job_ids() -> list[int]:
    """Active Afni jobs on the mass-hiring board."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM mass_hiring_jobs WHERE source='afni' AND active ORDER BY id")
        out = [r[0] for r in cur.fetchall()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=afni)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list active Afni job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = afni_job_ids()
        print(f"{len(ids)} active Afni jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
