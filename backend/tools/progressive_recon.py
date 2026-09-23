"""Driver: auto-apply to one Progressive job (Talemetry applicant account) end-to-end.

Progressive's careers site (careers.progressive.com, Cloudflare-fronted) hands the per-job
"Apply" off to Talemetry's applicant portal (apply.talemetry.com/application/<guid>): create an
account (email + password) → verify an emailed OTP → fill the application form → Submit, with a
reCAPTCHA on register/submit. `strategies/talemetry.TalemetryStrategy` owns the flow; this driver
mints a fresh synthetic US persona (with a PROVISIONED @takhet.com mailbox so the OTP is
receivable), drives one job, and polls the Maildir for the ack.

The Cloudflare wall on the careers hand-off is cleared by the headful real-Chrome stealth
context this driver launches (the co-pilot stealth posture), not a separate curl_cffi step.

Gating mirrors amazon_recon / roberthalf_recon:
  * Default (PROGRESSIVE_ADVANCE unset) = a DRY-RUN: fill only to the apply.talemetry.com account
    wall, report `needs_account`. NOTHING is created and NO PII is transmitted.
  * PROGRESSIVE_ADVANCE=1 lets the strategy create the account (verify the email OTP) + walk the
    form, arming the FREE in-browser NopeCHA reCAPTCHA path (headful). The final Submit is clicked
    only when advancing AND the wizard reached Submit with `unfilled==[]`. Ground truth = a real
    Progressive/Talemetry "application received" email in the persona Maildir.

Run under `sg mail`. Headful on :98 by default; headless via PROGRESSIVE_HEADLESS=1.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import roberthalf_recon as rr  # noqa: E402


def _is_progressive_confirmation(from_hdr: str, subject: str) -> bool:
    return rr._is_confirmation(from_hdr, subject, ("progressive", "talemetry", "myworkday"))


async def run(job_id: int, keep_minutes: int = 12, fresh: bool = True) -> None:
    from backend.applier.strategies.talemetry import TalemetryStrategy
    row = rr._row(job_id, "progressive")
    if not row:
        print(f"no progressive mass_hiring_jobs row id={job_id}", flush=True)
        return
    await rr._drive(row, source="progressive", company="Progressive",
                    strategy_cls=TalemetryStrategy, advance_env="PROGRESSIVE_ADVANCE",
                    us_env="PROGRESSIVE_US", proxy_env="PROGRESSIVE_PROXY",
                    nopecha_env="PROGRESSIVE_NOPECHA",
                    confirm_matcher=_is_progressive_confirmation,
                    keep_minutes=keep_minutes, fresh=fresh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source=progressive)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list active Progressive job ids + exit")
    args = ap.parse_args()
    if args.list:
        ids = rr.job_ids("progressive")
        print(f"{len(ids)} active Progressive jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
