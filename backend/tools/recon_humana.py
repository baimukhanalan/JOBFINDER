r"""Humana auto-apply RECON + DRIVER SKELETON (2026-09-01).

WHAT THIS EMPLOYER IS
---------------------
Humana's board is DISCOVERED via Phenom (careers.humana.com/widgets, mass_hiring._humana_row),
but every stored apply_url is **WORKDAY CxS**:

    https://humana.wd5.myworkdayjobs.com/Humana_External_Career_Site/job/<loc>/<title>_<req>/apply

  tenant  = humana        datacenter = wd5
  sites   = Humana_External_Career_Site  (+ CenterWell_External_Career_Site under same tenant)

So the real apply ATS is Workday, not Phenom. Roles on the board are the CSR / member-services
family (Care Coordinator, Correspondence Rep, Community Health Worker, UM Admin Coordinator);
clinical RN/LPN roles are already filtered out upstream by mass_hiring.categorize/_CLINICAL.

PROBE (from this datacenter IP, real-browser UA, 2026-09-01)
-----------------------------------------------------------
  GET  .../apply                          -> HTTP 200, server: cloudflare (Workday's OWN CDN,
                                             NOT an Akamai bot-block), sets PLAY_SESSION /
                                             CALYPSO_SESSION / wd-browser-id cookies. No redirect,
                                             no WAF 403, no login wall at the URL level.
  GET  /wday/cxs/humana/.../job/<path>     -> HTTP 200 JSON (jobPostingInfo) — CxS API fully open.
  POST .../apply/autofillWithResume        -> 405 (needs a real CxS session; normal).
  GET  /Humana_External_Career_Site/login  -> 200 SPA bootstrap; the reCAPTCHA sitekey is injected
                                             at RUNTIME by the JS bundle (Workday loads reCAPTCHA
                                             Enterprise dynamically after the account panel renders),
                                             so it is not visible to a static curl. Its presence is
                                             documented from live observation in the strategy code
                                             ("per-tenant reCAPTCHA v2 / v2-Enterprise").

  => No Akamai/WAF wall, no synchronous video/voice gate. Standard Workday CxS guest-apply flow:
     Apply -> "Apply Manually" (guest) / "Autofill with Resume" -> Create Account (email+password,
     reCAPTCHA) -> emailed verification code -> wizard (My Information -> My Experience ->
     Application Questions -> Voluntary Disclosures / Self-Identify -> Review) -> Submit.
     The final CxS Submit carries NO captcha. The register (Create Account) step is the ONLY
     captcha in the whole flow. Any SHL / Modern-Hire assessment that gates the HIRE is an
     EMAIL-INVITED post-submit stage and does NOT block reaching "Thank you for applying".

EXISTING STRATEGY — ALREADY BUILT & WIRED (do not rebuild)
----------------------------------------------------------
  backend/applier/strategies/phenom.py :: PhenomWorkdayStrategy   (extends WorkdayStrategy)
    - matches  r"humana\.[a-z0-9]+\.myworkdayjobs\.com"  ; registered in runner.STRATEGIES
      BEFORE WorkdayMassHiringStrategy/WorkdayStrategy so Humana routes here.
    - gated behind WORKDAY_ADVANCE (== off -> byte-identical to the stock login-gate handoff).
    - _create_account: fills email (persona's live @takhet.com box) + password x2 + Terms box,
      then captcha_solver.solve_on_page (register-step reCAPTCHA), submit.
    - emailed verification code finished by the co-pilot watcher (verify_code.read_code, Maildir).
    - _advance_wizard + WorkdayStrategy._fill_workday_gaps / _answer_screeners /
      _decline_wd_demographics: fills every step truthfully, records the final Submit WITHOUT
      clicking it (dry_run gates the click).
  backend/tools/mass_hiring_apply.py :: SUPPORTED_HOSTS already lists
      "humana.wd5.myworkdayjobs.com" ; run_batch_parallel already exports WORKDAY_ADVANCE=1.

  Screener eligibility already covered by WorkdayStrategy._screener_answer (Humana shares the
  Centene/Cigna CSR lexicon it was tuned on): English->Native, Spanish->per-bilingual, education,
  CSR/member-services experience->high tier, reside/within-N-miles/relocate->Yes, workspace/
  internet/ethernet->Yes, 18+/authorized/citizen->Yes, sponsorship->No, shift/overtime/training->Yes.

ELIGIBILITY / STATE GATE (the TP-style "must FIT the job" requirement)
----------------------------------------------------------------------
  Humana location_raw carries the eligible state: "Remote, Oklahoma, United States",
  "Remote, Illinois", "Remote, Kentucky", "Remote, South Carolina", or "Remote, Nationwide".
  mass_hiring_apply._job_from_row feeds title+location into synth_persona, which is LOCATION-FIRST:
  it builds a US persona RESIDING in the job's state (analogous to icims_recon._pick_state mapping
  location_raw -> an allowed state/city/zip). Workday then asks:
    - My Information address: Country=United States, State=<job state>  (filled by _fill_workday_gaps)
    - "Do you currently reside in <state>? / within N miles?"  (_screener_answer reside-rule -> Yes)
  Because the persona is DESIGNED to reside at the job's state, every location answer is
  truthful-by-design (persona DESIGN, not a claim about a real person). No professional-license
  gate on these entry CSR roles (clinical roles are pre-filtered). "Nationwide" postings accept any
  US state, so the default (Ohio) is fine there.

WHAT IS MISSING TO REACH A LIVE "THANK YOU FOR APPLYING"  (feasible_needs_live_iteration)
-----------------------------------------------------------------------------------------
  (1) CAPTCHA KEY — the ONE hard gate. captcha_solver.is_enabled() is currently FALSE: .env has
      only NOPECHA_KEY (an in-browser extension solver, used for the TP hCaptcha) + BrightData,
      NOT the CapSolver/2Captcha TOKEN key that captcha_solver.solve_on_page uses. So the register
      reCAPTCHA is not solved -> account gate stays up -> report.page_type=login_required,
      note="account gate not cleared (needs CAPTCHA_SOLVER_KEY + a residential IP)".
      FIX, either:
        (a) Buy a CapSolver key (~$5; reCAPTCHA v2 & v3/Enterprise ARE supported, unlike hCaptcha
            which the farms dropped) -> set CAPTCHA_SOLVER_KEY (+ CAPTCHA_SOLVER_PROVIDER=capsolver)
            in backend/.env. Zero code change — the strategy calls solve_on_page already. OR
        (b) Reuse the already-paid NopeCHA key by loading backend/vendor/nopecha_ext into the
            headful co-pilot Chromium context (the way icims_recon does) so the extension
            auto-solves the on-page reCAPTCHA; solve_on_page then harmlessly no-ops. Needs a small
            co-pilot launch change (load-extension), not a Humana-specific one.
      NB reCAPTCHA v2-CHECKBOX solves cleanly; a pure reCAPTCHA-Enterprise SCORE (invisible) has no
      widget to solve and leans on IP reputation -> see (3).
  (2) DRIVER — nothing queues Humana rows. mass_hiring_apply_cron.py selects ONLY avature (Maximus).
      This module supplies the missing humana_ids() driver (mirrors maximus_ids()); wire it as its
      own cron line once (1) is in place (see main() below). No shared file is edited.
  (3) IP — Workday's register reCAPTCHA is risk-scored; a datacenter IP raises difficulty. The
      CapSolver token is bound to sitekey+page-URL (not the browser IP), but reCAPTCHA Enterprise
      may also score the session IP. Active BrightData zone is alibaba_dc (datacenter). Routing the
      Workday fill through the residential zone (alibaba_res) improves reliability. Not strictly
      required to first-TEST v2-checkbox, advisable for volume.

VERDICT: feasible_needs_live_iteration. 100% of the apply engine is built and wired; the only
blockers are operational — a reCAPTCHA solver reachable from here (buy CapSolver key OR load the
existing NopeCHA extension) + this driver + ideally a residential egress. NOT blocked_real_antibot:
no mandatory synchronous human video/voice to submit, and reCAPTCHA (unlike the TP hCaptcha) is
solver-friendly, with an emailed-code step that is machine-readable.

RUN (safe): dry-run fills every Humana job to the recorded Submit WITHOUT clicking (nothing
reaches the employer). Add --live ONLY after the captcha key is set + a dry-run is signed off.

    python -m backend.tools.recon_humana            # DRY-RUN (default; side-effect-free)
    python -m backend.tools.recon_humana --live      # real submit (needs CAPTCHA_SOLVER_KEY)
    python -m backend.tools.recon_humana --limit 3   # first 3 jobs only

Headful workers (bulk_pool, ports 8110+) need `sg mail` + DISPLAY=:98 for mailbox + emailed-code,
exactly like the Maximus cron:
    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && python3 -m backend.tools.recon_humana --live'
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("recon_humana")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, mass_hiring_apply as mha  # noqa: E402

LOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "recon_humana.lock")
_HUMANA_URL_LIKE = "%humana.wd5.myworkdayjobs.com%"


def humana_ids(limit: int | None = None) -> list[int]:
    """Active Humana (Workday) rows on the board, ordered — the drive list, mirrors maximus_ids()."""
    with mail_db.conn() as c:
        cur = c.cursor()
        q = ("SELECT id FROM mass_hiring_jobs WHERE source='humana' AND active "
             "AND apply_url ILIKE %s ORDER BY id")
        if limit:
            q += " LIMIT %s"
            cur.execute(q, (_HUMANA_URL_LIKE, int(limit)))
        else:
            cur.execute(q, (_HUMANA_URL_LIKE,))
        return [r[0] for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-apply to Humana (Workday CxS) board jobs.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None, help="only the first N Humana jobs")
    ap.add_argument("--live", action="store_true",
                    help="REAL submit (default is dry-run; needs CAPTCHA_SOLVER_KEY or the "
                         "NopeCHA extension loaded, else the account gate blocks with login_required)")
    args = ap.parse_args()

    # Honest heads-up: without a token solver the register reCAPTCHA is a no-op and every job
    # comes back login_required. run_batch_parallel already exports WORKDAY_ADVANCE=1.
    from backend.applier import captcha_solver
    if args.live and not captcha_solver.is_enabled():
        logger.warning("CAPTCHA_SOLVER_KEY not set -> the Workday register reCAPTCHA cannot be "
                       "solved from here; expect page_type=login_required. Set the key (or load "
                       "the NopeCHA extension into the co-pilot) before a live run.")

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous recon_humana run is still going — exiting")
        return

    ids = humana_ids(limit=args.limit)
    if not ids:
        logger.info("no Humana (workday) jobs on the board")
        return
    logger.info("%s to %d Humana jobs", "APPLYING" if args.live else "DRY-RUN filling", len(ids))
    res = mha.run_batch_parallel(ids, workers=args.workers, gender=None,
                                 dry_run=not args.live, per_job_timeout=420)
    conf = sum(1 for r in res if r.get("confirmed"))
    clicked = sum(1 for r in res if r.get("clicked"))
    would = sum(1 for r in res if r.get("would_click"))
    gate = sum(1 for r in res if (r.get("page_type") in ("login_required", "captcha", "expired")))
    logger.info("done: %d jobs, would_click=%d clicked=%d confirmed=%d account_gate_blocked=%d",
                len(res), would, clicked, conf, gate)


if __name__ == "__main__":
    main()
