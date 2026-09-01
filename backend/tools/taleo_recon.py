"""Driver: auto-apply to one Oracle Taleo job (UnitedHealth / TTEC) end-to-end via TaleoStrategy.

Modeled on `icims_recon.py` but WITHOUT NopeCHA — Taleo has no captcha/WAF (see
`recon_unitedhealth.py` / `recon_ttec.py`), so it runs autonomously (headful `:98` by default, or
headless via `TALEO_HEADLESS=1`). Each run: fresh synthetic persona in a per-job isolated Chromium
profile, placed in the JOB'S state (so residence/prescreen screeners answer truthfully-by-design),
navigate to the resolved Taleo apply URL, and drive `TaleoStrategy.prefill`. The real Submit is gated
by env **TALEO_ADVANCE=1** (default OFF → side-effect-free fill: no account, no PII transmitted).

Ground truth = the Taleo "Thank you for applying" confirmation email in the persona's @takhet.com
Maildir (reuse the GH/Ashby/TP confirmation-read pattern).

    DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
        TALEO_ADVANCE=1 python3 -m backend.tools.taleo_recon --job <mass_hiring_id> --fresh'

NB not run against a live Taleo form yet — the Taleo-classic (akira) selectors in taleo.py need live
iteration on :98 (the driver + eligibility here are testable offline; the browser walk is not).
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"

# TTEC licensed insurance-agent rows — a synthetic persona can't hold a real state license -> skip
# (same policy as never attaching a fabricated diploma/medical report). See recon_ttec.is_licensed.
_TTEC_LICENSED_IDS = {506, 511, 513, 529}


def is_licensed(title: str) -> bool:
    """A licensed insurance-agent role → skip by design."""
    from backend.tools.recon_ttec import is_licensed as _il
    return _il(title)


def _pick_state(source: str, title: str, location_raw: str) -> tuple[str, str, str, str]:
    """(full, code, city, zip) to place the persona in the JOB'S state. TTEC hires WAH in California
    (its own table, CA included); UnitedHealth uses the generic TP/iCIMS allow-list + the requisition
    location. Falls back to Ohio."""
    if source == "ttec":
        from backend.tools.recon_ttec import ttec_state
        code, full, city, zc = ttec_state(title)
        return full, code, city, zc
    from backend.tools.icims_recon import _pick_state as _ps
    full, code, city, zc = _ps(title or "", location_raw or "")
    return full, code, city, zc


def taleo_job_ids() -> list[int]:
    """Active Taleo (UnitedHealth + TTEC) rows, EXCLUDING licensed-insurance roles."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id, title, source FROM mass_hiring_jobs "
            "WHERE source IN ('unitedhealth','ttec') AND active ORDER BY id")
        for jid, title, _src in cur.fetchall():
            if jid in _TTEC_LICENSED_IDS or is_licensed(title):
                continue
            out.append(jid)
    return out


def _resolve_taleo_url(apply_url: str) -> str | None:
    """Fetch the Radancy job/listing page and pull out the embedded taleo.net apply URL."""
    import httpx
    from backend.applier.strategies.taleo import resolve_apply_url
    ua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
          "Accept-Encoding": "identity"}
    try:
        r = httpx.get(apply_url, headers=ua, timeout=30, follow_redirects=True)
        return resolve_apply_url(r.text)
    except Exception as e:  # noqa: BLE001
        print(f"[resolve error: {type(e).__name__}: {e}]", flush=True)
        return None


def _build_persona(row: dict) -> dict:
    """Fresh synthetic persona for this Taleo job, placed in the job's state. Returns the profile_form
    dict + facts + resume path (mirrors icims_recon._build_persona; no reuse — always fresh)."""
    from backend.tools import mass_hiring_apply
    full, code, city, zc = _pick_state(row.get("source") or "", row.get("title") or "",
                                       row.get("location_raw") or "")
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
    """True once a Taleo application-confirmation email has landed in the persona's Maildir (received
    at/after since_ts). Matches the Taleo/employer 'Thank you for applying' / 'we have received your
    application' wording."""
    import re
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
            s = subj.group(0).lower() if subj else ""
            if ("thank you for applying" in s or "received your application" in s
                    or "application received" in s or "application has been received" in s):
                return True
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
    print(f"=== Taleo apply: job {row['id']} [{row['source']}] — {row['title']}", flush=True)
    if is_licensed(row["title"]):
        print("[skip: licensed insurance role — a synthetic persona can't hold a real license]", flush=True)
        return
    taleo_url = _resolve_taleo_url(row["apply_url"])
    if not taleo_url:
        print(f"[skip: could not resolve a taleo.net apply URL from {row['apply_url']}]", flush=True)
        return
    print(f"[taleo apply URL: {taleo_url}]", flush=True)

    p = _build_persona(row)
    pf = p["profile_form"]
    print(f"persona: {pf['full_name']} <{pf['email']}> {pf['city']}, {p['state_code']} "
          f"| resume={os.path.exists(p['resume_path'])} | TALEO_ADVANCE="
          f"{os.getenv('TALEO_ADVANCE', '')}", flush=True)

    profile_dir = os.getenv("TALEO_PROFILE_DIR") or os.path.join(
        tempfile.gettempdir(), f"taleo_prof_{job_id}_{os.getpid()}")
    if fresh:
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)

    headless = os.getenv("TALEO_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")
    from playwright.async_api import async_playwright
    from backend.applier.strategies.taleo import TaleoStrategy

    start_ts = time.time()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile_dir, headless=headless, channel="chromium", no_viewport=not headless,
            locale="en-US", timezone_id="America/New_York",
            args=[] if headless else ["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(taleo_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            strat = TaleoStrategy()
            result = await strat.prefill(
                page, pf, p["resume_path"], job={"title": row["title"], "company": row["company"]},
                draft=True, facts=p["facts"], profile_id=p["profile_id"])
            print(f"[filled: unfilled={result.get('unfilled')} "
                  f"review_items={len(result.get('review_items') or [])} "
                  f"page_type={result.get('page_type')}]", flush=True)
            # wait for the confirmation (or idle out --keep). Only meaningful with TALEO_ADVANCE=1.
            deadline = start_ts + keep_minutes * 60
            confirmed = False
            while time.time() < deadline:
                if _app_confirmed(pf["email"], start_ts - 60):
                    confirmed = True
                    print("[application CONFIRMED submitted — Taleo receipt in the Maildir]", flush=True)
                    break
                await asyncio.sleep(10)
            if not confirmed:
                print("[no confirmation within --keep (expected if TALEO_ADVANCE is off, or needs "
                      "live selector tuning)]", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[run error: {type(e).__name__}: {str(e)[:160]}]", flush=True)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
    print("=== taleo apply done", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, required=True, help="mass_hiring_jobs id (unitedhealth/ttec)")
    ap.add_argument("--fresh", action="store_true", help="fresh persona + wiped profile dir (default)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap to await confirmation")
    ap.add_argument("--list", action="store_true", help="list auto-applyable Taleo job ids and exit")
    args = ap.parse_args()
    if args.list:
        ids = taleo_job_ids()
        print(f"{len(ids)} auto-applyable Taleo jobs: {ids}")
        return
    asyncio.run(run(args.job, keep_minutes=args.keep, fresh=True))


if __name__ == "__main__":
    main()
