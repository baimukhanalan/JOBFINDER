r"""Conduent (Phenom career site → Oracle HCM guest-apply) mass-hiring auto-apply DRIVER, headful on
DISPLAY=:98 with the NopeCHA extension for the invisible reCAPTCHA v3.

Conduent's board (careers.conduent.com) is a Phenom front end whose real apply backend is Oracle
HCM Recruiting Cloud. The submit gate is an INVISIBLE reCAPTCHA v3 (score-based; often no widget) —
no login wall, no Akamai. A full strategy already exists + is wired:
`backend/applier/strategies/phenom.py::PhenomStrategy` (matches careers.conduent.com), so this driver
just reuses the shared headful-:98 + NopeCHA drive loop from `workday_recon.drive_apply`, selecting
Conduent rows and gating the real submit behind PHENOM_ADVANCE=1 (default OFF = dry-run fill).

HONEST STATUS: feasible_needs_live_iteration — strategy built, driver wires it to a NopeCHA browser,
but not driven live from here (the Oracle HCM guest wizard + reCAPTCHA v3 score from a datacenter IP
+ the exact confirmation sender need one live pass to tune; ~half of Conduent reqs also gate hire
behind a later assessment, which is a POST-submit human step, not a submit wall).

RUN:
    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && python3 -m backend.tools.phenom_recon --job <id>'
    # add PHENOM_ADVANCE=1 to REALLY submit.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db  # noqa: E402
from backend.tools.workday_recon import drive_apply, _row  # noqa: E402  (shared headful drive loop)

_CONFIRM_SUBJECT_RE = re.compile(
    r"thank you for applying|application (was |has been )?received|we(['’ ]?ve| have)? received your "
    r"application|your application (to|for|has been received)|application (confirmation|submitted)", re.I)
_CONFIRM_FROM_RE = re.compile(r"conduent|oracle|oraclecloud|phenom|no-?reply@", re.I)
# A recruiting-MARKETING mail (Conduent 'Talent Community' / job alerts) is NOT an application receipt —
# it comes from careeralerts.conduent.com with a 'Join …' subject; exclude it so it never counts as a
# confirmation (owner audit 2026-09-01). Ground truth stays the real ack subject above.
_CONFIRM_EXCLUDE_RE = re.compile(
    r"talent community|job alert|careeralert|newsletter|subscrib|stay connected|"
    r"join .{0,20}(talent|community|network)|new jobs? (that )?match", re.I)


def phenom_job_ids(limit: int | None = None) -> list[int]:
    """Active Conduent (Phenom → Oracle HCM) rows on the board, ordered."""
    with mail_db.conn() as c:
        cur = c.cursor()
        q = "SELECT id FROM mass_hiring_jobs WHERE source='conduent' AND active ORDER BY id"
        if limit:
            q += " LIMIT %s"
            cur.execute(q, (int(limit),))
        else:
            cur.execute(q)
        return [r[0] for r in cur.fetchall()]


def _confirmed(email: str, since_ts: float) -> bool:
    """Conduent/Oracle-HCM application receipt in the persona's @takhet.com Maildir (tune live)."""
    local = (email or "").split("@", 1)[0]
    if not local:
        return False
    base = f"/var/mail/vhosts/takhet.com/{local}"
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
            subj_t = subj.group(0) if subj else ""
            frm_t = frm.group(0) if frm else ""
            # never count a recruiting-marketing / talent-community / job-alert mail as a receipt
            if _CONFIRM_EXCLUDE_RE.search(subj_t) or _CONFIRM_EXCLUDE_RE.search(frm_t):
                continue
            if subj and _CONFIRM_SUBJECT_RE.search(subj_t):
                return True
            if frm and _CONFIRM_FROM_RE.search(frm_t) and "appl" in subj_t.lower():
                return True
    return False


def main() -> None:
    import asyncio
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Auto-apply to Conduent (Phenom→Oracle HCM) board jobs.")
    ap.add_argument("--job", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--keep", type=int, default=13)
    args = ap.parse_args()

    ids = [args.job] if args.job else phenom_job_ids(limit=(args.limit or None))
    if not ids:
        print("no Conduent (phenom) jobs on the board", flush=True)
        return
    adv = os.getenv("PHENOM_ADVANCE", "0") in ("1", "true", "yes", "on")
    print(f"{'APPLYING' if adv else 'DRY-RUN'} to {len(ids)} Conduent job(s) "
          f"(PHENOM_ADVANCE={'1' if adv else '0'})", flush=True)
    conf = 0
    for jid in ids:
        row = _row(jid)
        if not row:
            print(f"job {jid}: no row", flush=True)
            continue
        # Retry a TRANSIENT browser crash (TargetClosedError from an OOM-killed renderer when the box
        # is oversubscribed by the other lanes) — up to 3 attempts, brief backoff.
        res = {}
        for attempt in range(3):
            res = asyncio.run(drive_apply(row, advance_env="PHENOM_ADVANCE",
                                          keep_minutes=args.keep, confirm=_confirmed))
            err = (res.get("error") or "")
            if res.get("confirmed") or not re.search(r"TargetClosed|has been closed|Timeout.*context", err):
                break
            print(f"job {jid} attempt {attempt+1}: transient {err[:60]} — retrying", flush=True)
            time.sleep(15)
        conf += 1 if res.get("confirmed") else 0
        print(f"job {jid} persona={res.get('persona')} strategy={res.get('strategy')} "
              f"clicked={res.get('clicked')} confirmed={res.get('confirmed')} "
              f"error={res.get('error')}", flush=True)
    print(f"done: {len(ids)} jobs, confirmed={conf}", flush=True)


if __name__ == "__main__":
    main()
