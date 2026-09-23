"""Driver: auto-apply to one Adecco job (candidate.adecco.com easyApply account) end-to-end.

Adecco's per-job "Apply now" hands off to its candidate SPA (candidate.adecco.com/easyApply):
create an account (email + password, the signUp_portal flow) → verify an emailed OTP → fill the
easyApply form → Submit, with a reCAPTCHA on register/submit. `strategies/adecco.AdeccoStrategy`
owns the flow; this driver mints a fresh synthetic US persona (with a PROVISIONED @takhet.com
mailbox so the OTP is receivable), drives one job, and polls the Maildir for the ack.

Gating mirrors amazon_recon / roberthalf_recon:
  * Default (ADECCO_ADVANCE unset) = a DRY-RUN: fill only to the candidate.adecco.com account
    wall, report `needs_account`. NOTHING is created and NO PII is transmitted.
  * ADECCO_ADVANCE=1 lets the strategy create the account (verify the email OTP) + walk the form,
    arming the FREE in-browser NopeCHA reCAPTCHA path (headful). The final Submit is clicked only
    when advancing AND the wizard reached Submit with `unfilled==[]`. Ground truth = a real Adecco
    "application received" email in the persona Maildir.

Run under `sg mail`. Headful on :98 by default; headless via ADECCO_HEADLESS=1.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import roberthalf_recon as rr  # noqa: E402


def _is_adecco_confirmation(from_hdr: str, subject: str) -> bool:
    return rr._is_confirmation(from_hdr, subject, ("adecco", "modis", "lhh"))


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    from backend.applier.strategies.adecco import AdeccoStrategy
    row = rr._row(job_id, "adecco")
    if not row:
        print(f"no adecco mass_hiring_jobs row id={job_id}", flush=True)
        return
    await rr._drive(row, source="adecco", company="Adecco",
                    strategy_cls=AdeccoStrategy, advance_env="ADECCO_ADVANCE",
                    us_env="ADECCO_US", proxy_env="ADECCO_PROXY",
                    nopecha_env="ADECCO_NOPECHA", confirm_matcher=_is_adecco_confirmation,
                    keep_minutes=keep_minutes, fresh=fresh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=adecco)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list active Adecco job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = rr.job_ids("adecco")
        print(f"{len(ids)} active Adecco jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
