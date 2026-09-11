"""Live probe: drive ONE campaign-style fill through a NopeCHA-enabled co-pilot and report
whether the captcha was solved and the submit actually went through.

Reuses the PROVEN pipeline verbatim — `catalog_drafts.ensure_and_wire` (synthetic persona +
JD-tailored draft + résumé PDF + provisioned @takhet.com mailbox) and the co-pilot `/load`
(fill + auto-submit + emailed-code/confirmation watch). The ONLY new ingredient is the co-pilot
launched with `COPILOT_NOPECHA=1` so the NopeCHA extension auto-solves hCaptcha (Lever) /
Turnstile (Workable) / reCAPTCHA in-page. Never touches the live campaign or co-pilot 8102.

Usage (run HEADFUL under DISPLAY=:98 + sg mail, from repo root; a throwaway co-pilot must be up):
    # start a NopeCHA co-pilot on a free port first, e.g.
    #   COPILOT_NOPECHA=1 DISPLAY=:98 [COPILOT_PROXY=http://user:pass@host:port] \
    #     uvicorn backend.copilot:app --host 127.0.0.1 --port 8132
    python -m backend.tools.campaign_captcha_probe --job 27091 --base http://127.0.0.1:8132
"""
import argparse
import json
import time
from pathlib import Path

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, required=True, help="job_catalog id to apply to")
    ap.add_argument("--base", default="http://127.0.0.1:8132", help="NopeCHA co-pilot base url")
    ap.add_argument("--gender", default=None)
    ap.add_argument("--no-wait", action="store_true", help="do not wait for the confirmation")
    args = ap.parse_args()

    from backend.tools import catalog_drafts

    print(f"[probe] wiring synthetic persona for job {args.job} ...", flush=True)
    pid, jid, generated = catalog_drafts.ensure_and_wire(args.job, gender=args.gender)
    d = Path("uploads/prefill") / pid / str(jid)
    persona = {}
    if (d / "persona.json").exists():
        persona = json.loads((d / "persona.json").read_text(encoding="utf-8"))
    email = (persona.get("profile") or {}).get("email", "")
    rep = {}
    if (d / "report.json").exists():
        rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
    print(f"[probe] pid={pid} jid={jid} generated={generated}", flush=True)
    print(f"[probe] company={rep.get('company')} ats_url={rep.get('apply_url')}", flush=True)
    print(f"[probe] persona email={email}", flush=True)

    try:
        httpx.post(f"{args.base}/release", data={"profile": pid}, timeout=10)
    except Exception:
        pass
    print(f"[probe] POST {args.base}/load — NopeCHA solves any captcha, co-pilot clicks Submit ...",
          flush=True)
    t0 = time.time()
    r = httpx.post(
        f"{args.base}/load",
        data={"jobid": str(jid), "profile": pid, "wait_submit": "" if args.no_wait else "1"},
        timeout=620,
    )
    dt = time.time() - t0
    try:
        res = r.json()
    except Exception:
        res = {"raw": r.text[:600]}
    print(f"[probe] /load -> HTTP {r.status_code} in {dt:.0f}s", flush=True)
    print(json.dumps(res, indent=2, ensure_ascii=False)[:2400], flush=True)

    sub = res.get("submit_result") or {}
    shot = d / "after_submit.png"
    local = email.split("@")[0] if email else "?"
    print("\n=== VERDICT ===", flush=True)
    print(f"  filled={res.get('filled')} unfilled={res.get('unfilled')} "
          f"page_type={res.get('page_type')}", flush=True)
    print(f"  submit: clicked={sub.get('clicked')} confirmed={sub.get('confirmed')} "
          f"blocked={sub.get('blocked')!r} reason={sub.get('reason')}", flush=True)
    print(f"  screenshot: {shot if shot.exists() else '(none)'}", flush=True)
    print(f"  maildir:    /var/mail/vhosts/takhet.com/{local}", flush=True)


if __name__ == "__main__":
    main()
