"""Driver: drop a synthetic persona's résumé + contact into a staffing/BPO TALENT POOL (server-side).

A talent-pool drop is a NEW inbound offer channel: instead of applying to a specific job
(ATS → assessment → offer), we POST a persona's résumé into a recruiter POOL; recruiters then reach
out with matching roles, and their mail lands in the persona's @takhet.com Maildir (the SAME CRM the
apply lanes already feed). It bypasses per-job ATS + assessments entirely.

Per the recon (`talent_pool_recon.POOLS`) the ONE server-reachable generic résumé drop is **Randstad**
(`join_randstad` Drupal webform), and it is INVISIBLE-reCAPTCHA-gated — so a live drop needs a solved
reCAPTCHA token (CapSolver via `applier/capsolver.py`, or a headless grecaptcha exec). Every other
surveyed pool is account-walled, an email-alert subscription (no résumé), or a reCAPTCHA lead form.

    python3 -m backend.tools.talent_pool_drop --pool randstad            # DRY RUN (build payload, send nothing)
    python3 -m backend.tools.talent_pool_drop --list                     # per-pool recon verdicts
    TALENT_POOL_ADVANCE=1 python3 -m backend.tools.talent_pool_drop --pool randstad   # real drop (needs a captcha solver)

`TALENT_POOL_ADVANCE` off ⇒ DRY RUN: mints/uses a persona, builds the EXACT payload, transmits nothing.
Run under `sg mail` (the mailbox provisioning + Maildir read need the mail group).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import talent_pool_recon as tpr  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")

# a coherent US placement for the synthetic persona (any US state is fine for a remote-CSR pool)
_STATE_PLACE = {
    "Ohio": ("Columbus", "43215"), "Texas": ("Austin", "78701"), "Florida": ("Orlando", "32801"),
    "Georgia": ("Atlanta", "30303"), "Arizona": ("Phoenix", "85004"), "Tennessee": ("Nashville", "37203"),
    "North Carolina": ("Charlotte", "28202"),
}
_DEFAULT_STATE = "Ohio"


def _build_persona(job_title: str, state: str = _DEFAULT_STATE) -> dict:
    """Fresh synthetic US persona for a talent-pool drop (job-agnostic — placed in a coherent US
    city/state/zip). Provisions the mailbox, registers the demo persona, writes a prefill dir with a
    rendered résumé PDF. Returns the persona + the résumé path."""
    from backend.tools import catalog_drafts, drafts_ui, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    city, zc = _STATE_PLACE.get(state, ("Columbus", "43215"))
    job = {"title": job_title, "company": "Randstad", "company_key": "talent_pool",
           "description": "Remote customer service / support role (US).", "regions": ["US"],
           "location": f"{city}, {state}, United States", "ats": "talent_pool",
           "external_id": "", "url": "", "questions": []}
    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job)
    prof = cand["profile"]
    prof["city"] = city
    prof["state"] = state
    prof["zip"] = zc
    prof["location"] = f"{city}, {state}"

    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""), prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[talent_pool] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = "talent_pool_randstad"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    resume_path = out / "resume.pdf"
    try:
        d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)
        resume_path.write_bytes(drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")
    except Exception as e:  # noqa: BLE001
        print(f"[talent_pool] resume gen skipped: {type(e).__name__}: {e}", flush=True)
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "talent_pool"}),
        encoding="utf-8")
    return {"profile": prof, "facts": cand.get("facts") or {}, "state": state,
            "profile_id": profile_id, "jobid": jobid, "resume_path": str(resume_path)}


# --- Randstad live flow ---------------------------------------------------------------------------

def _recaptcha_token(sitekey: str | None, page_url: str) -> str | None:
    """Solve the invisible reCAPTCHA v2 via the project's CapSolver client, if a key is configured.
    Returns None when no solver key / no sitekey / any failure (the honest captcha wall)."""
    if not sitekey:
        return None
    try:
        import asyncio

        from backend.applier import capsolver
        if not capsolver.is_enabled():
            print("[talent_pool] CAPTCHA_SOLVER_KEY unset — cannot solve the invisible reCAPTCHA "
                  "(the drop is captcha-gated).", flush=True)
            return None
        tok = asyncio.run(capsolver.solve("recaptcha_v2", page_url=page_url, site_key=sitekey))
        return tok or None
    except Exception as e:  # noqa: BLE001
        print(f"[talent_pool] reCAPTCHA solve failed: {type(e).__name__}: {e}", flush=True)
        return None


def _upload_resume(client, upload_path: str, resume_path: str) -> str | None:
    """Multipart-POST the résumé to the dropzone endpoint; return the uploaded file id/name."""
    url = tpr.RANDSTAD_UPLOAD_BASE + upload_path
    try:
        with open(resume_path, "rb") as f:
            data = f.read()
        r = client.post(url, files={"file": ("resume.pdf", data, "application/pdf")},
                        headers={"User-Agent": _BROWSER_UA, "Origin": tpr.RANDSTAD_ORIGIN,
                                 "Referer": tpr.RANDSTAD_PAGE_URL,
                                 "X-Requested-With": "XMLHttpRequest"}, timeout=60)
        if r.status_code not in (200, 201):
            print(f"[talent_pool] dropzone upload http={r.status_code}: {r.text[:160]}", flush=True)
            return None
        # Drupal dropzonejs returns {"jsonapi":..., "result": <fid or filename>} — be tolerant
        try:
            j = r.json()
        except Exception:
            j = {}
        for k in ("result", "fid", "filename", "name"):
            v = j.get(k) if isinstance(j, dict) else None
            if v:
                return str(v)
        m = re.search(r'"(?:result|fid|filename)"\s*:\s*"([^"]+)"', r.text)
        return m.group(1) if m else (r.text.strip()[:120] or None)
    except Exception as e:  # noqa: BLE001
        print(f"[talent_pool] dropzone upload failed: {type(e).__name__}: {e}", flush=True)
        return None


def run_randstad(*, advance: bool, keep_minutes: int) -> dict:
    import httpx

    job_title = "Customer Service Representative"
    p = _build_persona(job_title)
    prof = p["profile"]
    print(f"persona: {prof.get('full_name')} <{prof.get('email')}> {prof.get('city')}, {p['state']} "
          f"| TALENT_POOL_ADVANCE={os.getenv('TALENT_POOL_ADVANCE', '')}", flush=True)

    report: dict = {"pool": "randstad", "advanced": False, "submitted": False, "success": False}
    with httpx.Client(follow_redirects=True, headers={"User-Agent": _BROWSER_UA}, timeout=30) as client:
        r = client.get(tpr.RANDSTAD_PAGE_URL)
        html = r.text
        upload_path = tpr.parse_upload_path(html)
        captcha = tpr.parse_captcha(html)
        form = tpr.build_randstad_form(prof, job_title=job_title)
        report["form"] = form
        report["upload_path_present"] = bool(upload_path)
        report["captcha"] = captcha
        report["missing"] = tpr.randstad_missing_fields(form)

        print(f"page http={r.status_code} cookies={list(client.cookies.keys())}", flush=True)
        print(f"upload_path={'<found>' if upload_path else None}  captcha={captcha}", flush=True)
        print("[form_fields] " + json.dumps(form), flush=True)

        if report["missing"]:
            print(f"[missing required fields: {report['missing']} — persona incomplete]", flush=True)
            return report
        if not advance:
            print("[dry run — payload built, nothing transmitted. Set TALENT_POOL_ADVANCE=1 to drop.]",
                  flush=True)
            return report

        # --- advance: real drop ---
        if not upload_path:
            report["note"] = "no dropzone upload path on page"
            print("[cannot advance: no dropzone upload path]", flush=True)
            return report
        fid = _upload_resume(client, upload_path, p["resume_path"])
        print(f"resume upload -> file_id={fid}", flush=True)
        if not fid:
            report["note"] = "resume upload failed"
            return report

        token = ""
        if captcha.get("present"):
            token = _recaptcha_token(captcha.get("sitekey"), tpr.RANDSTAD_PAGE_URL) or ""
            if not token:
                report["note"] = "invisible reCAPTCHA required but unsolved — refusing to POST a doomed drop"
                print(f"[{report['note']}]", flush=True)
                return report
        form = tpr.build_randstad_form(prof, job_title=job_title, resume_file_id=fid,
                                       recaptcha_token=token)
        rr = client.post(tpr.RANDSTAD_SUBMIT_URL, data=form,
                         headers={"User-Agent": _BROWSER_UA, "Origin": tpr.RANDSTAD_ORIGIN,
                                  "Referer": tpr.RANDSTAD_PAGE_URL,
                                  "X-Requested-With": "XMLHttpRequest"})
        report["advanced"] = True
        report["submitted"] = True
        report["http_status"] = rr.status_code
        report["success"] = tpr.randstad_ack(rr.status_code, rr.text)
        report["response_head"] = rr.text[:400]
        print(f"[submit http={rr.status_code} success={report['success']}] {rr.text[:200]}", flush=True)

    if report.get("success"):
        # passive: recruiters reach out over days — a short Maildir watch only corroborates delivery.
        deadline = time.time() + keep_minutes * 60
        while time.time() < deadline:
            if _any_recruiter_mail(prof.get("email", ""), time.time() - keep_minutes * 60 - 60):
                report["mail_seen"] = True
                break
            time.sleep(15)
    return report


def _any_recruiter_mail(email: str, since_ts: float) -> bool:
    """True once ANY inbound mail lands in the persona Maildir since `since_ts` (recruiter outreach is
    passive/slow — this only corroborates the mailbox is live, not a per-drop ack)."""
    local = (email or "").split("@", 1)[0]
    if not local:
        return False
    for sub in ("new", "cur"):
        d = os.path.join(MAILROOT, local, sub)
        try:
            for n in os.listdir(d):
                if os.path.getmtime(os.path.join(d, n)) >= since_ts:
                    return True
        except Exception:
            continue
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="randstad", help="talent pool key (see --list)")
    ap.add_argument("--keep", type=int, default=4, help="minutes to watch the Maildir after a drop")
    ap.add_argument("--list", action="store_true", help="print per-pool recon verdicts + exit")
    args = ap.parse_args()

    if args.list:
        for k, v in tpr.POOLS.items():
            flag = "VIABLE " if v["viable"] else "walled "
            print(f"[{flag}] {k:16} reachable={v['reachable_serverside']!s:5} "
                  f"resume_drop={v['resume_drop']!s:5} account={v['account_required']!s:5} "
                  f"captcha={v['captcha']:22} — {v['note']}")
        print(f"\nviable: {tpr.viable_pools()}")
        return

    advance = os.getenv("TALENT_POOL_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")
    if args.pool == "randstad":
        run_randstad(advance=advance, keep_minutes=args.keep)
    else:
        meta = tpr.POOLS.get(args.pool)
        if not meta:
            ap.error(f"unknown pool '{args.pool}' (see --list)")
        print(f"[{args.pool}] NOT a built lane — {meta['note']}")
        print(f"reachable_serverside={meta['reachable_serverside']} resume_drop={meta['resume_drop']} "
              f"account_required={meta['account_required']} captcha={meta['captcha']}")


if __name__ == "__main__":
    main()
