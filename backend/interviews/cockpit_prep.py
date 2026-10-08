"""Interview Cockpit — server-side PREP + STAGING for Alan's MacBook (Phase 2).

Computes Alan's next upcoming собес, builds the prep pack (`interview_prep.build_pack`), and STAGES it
onto his Mac (`macalan` over tailscale) into `~/NativelyInbox/`:
  * `~/NativelyInbox/<mailbox>/resume.pdf`  — the candidate résumé to load into Natively
  * `~/NativelyInbox/next.json`             — the manifest the Mac opener reads (start time, brief,
                                              cockpit URL, join/booking links, résumé path)

The Mac-side LaunchAgent (`cockpit/mac/`) polls that manifest and, 20 min before the собес, opens the
Cockpit (the JobFinder `/cabinet` calendar) + Natively and attaches the staged résumé. This module does
the data/transport half; it never drives the Mac GUI. Guarded: the Mac being offline is a soft failure,
never an exception. No writes to the portal DB (read-only over iv_interviews / mail_index).

CLI:  python -m backend.interviews.cockpit_prep [--dry-run] [--stage] [--mailbox X] [--responsible 1035]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone

from backend.tools import mail_db
from backend.interviews import interview_prep

ALAN_ID = 1035
MAC_SSH = os.environ.get("COCKPIT_MAC_SSH", "macalan")
MAC_INBOX = os.environ.get("COCKPIT_MAC_INBOX", "~/NativelyInbox")
COCKPIT_URL = os.environ.get("COCKPIT_URL", "https://jobs.systeam.kz/cabinet")


def next_interview(responsible_id: int = ALAN_ID, *, mailbox: str | None = None) -> dict | None:
    """The soonest upcoming (or time-unset) non-cancelled собес for this responsible.
    `mailbox` forces a specific one (used to prove the pipeline on a résumé-present persona)."""
    try:
        with mail_db._cur() as cur:
            if mailbox:
                cur.execute(
                    """SELECT id, mailbox, company, jobid, start_ts FROM iv_interviews
                       WHERE mailbox=%s AND status<>'cancelled' ORDER BY start_ts NULLS LAST LIMIT 1""",
                    (mailbox,))
            else:
                # soonest future start first; time-unset rows last (they still need prep once booked)
                cur.execute(
                    """SELECT id, mailbox, company, jobid, start_ts FROM iv_interviews
                       WHERE responsible_id=%s AND status<>'cancelled'
                         AND (start_ts IS NULL OR start_ts >= now())
                       ORDER BY (start_ts IS NULL), start_ts LIMIT 1""",
                    (responsible_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def build_manifest(mailbox: str, iv_row: dict | None = None) -> dict:
    pack = interview_prep.build_pack(mailbox, iv_row=iv_row)
    start = pack.start_ts
    return {
        "mailbox": mailbox,
        "candidate_name": pack.candidate_name,
        "state": pack.state,
        "company": pack.company,
        "role": pack.role,
        "start_ts": start.astimezone(timezone.utc).isoformat() if getattr(start, "astimezone", None) else None,
        "brief": pack.brief,
        "resume_filename": pack.resume_filename,
        "has_resume": pack.has_resume,
        "resume_mac_path": f"{MAC_INBOX}/{mailbox}/resume.pdf" if pack.has_resume else None,
        "join_url": pack.join_url,
        "booking_url": pack.booking_url,
        "cockpit_url": COCKPIT_URL,
        "staged_at": datetime.now(timezone.utc).isoformat(),
    }, pack


def _run(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()[-300:]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def stage_to_mac(manifest: dict, resume_path: str | None) -> dict:
    """scp the résumé + write the manifest onto the Mac inbox. Soft-fails if the Mac is offline."""
    mb = manifest["mailbox"]
    results = {"resume": None, "manifest": None, "mac_online": False}
    ok, _ = _run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", MAC_SSH,
                  f"mkdir -p {MAC_INBOX}/{mb}"], timeout=40)
    results["mac_online"] = ok
    if not ok:
        return results
    if resume_path and os.path.exists(resume_path):
        rok, _ = _run(["scp", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", resume_path,
                       f"{MAC_SSH}:{MAC_INBOX}/{mb}/resume.pdf"], timeout=60)
        results["resume"] = rok
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(manifest, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    try:
        mok, _ = _run(["scp", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", tmp,
                       f"{MAC_SSH}:{MAC_INBOX}/next.json"], timeout=40)
        results["manifest"] = mok
    finally:
        os.unlink(tmp)
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the manifest, stage nothing")
    ap.add_argument("--stage", action="store_true", help="scp résumé + manifest to the Mac")
    ap.add_argument("--mailbox", help="force a specific mailbox (prove the pipeline)")
    ap.add_argument("--responsible", type=int, default=ALAN_ID)
    args = ap.parse_args()

    iv = next_interview(args.responsible, mailbox=args.mailbox)
    if not iv:
        print("cockpit_prep: no upcoming собес to prepare"); return
    manifest, pack = build_manifest(iv["mailbox"], iv_row=iv)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.dry_run or not args.stage:
        print("\n(dry-run — nothing staged; pass --stage to push to the Mac)")
        return
    res = stage_to_mac(manifest, pack.resume_path)
    print(f"\nstaged → mac_online={res['mac_online']} resume={res['resume']} manifest={res['manifest']}")


if __name__ == "__main__":
    main()
