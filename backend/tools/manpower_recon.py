"""Driver: auto-apply to one ManpowerGroup (Manpower / Experis) job end-to-end, SERVER-SIDE.

ManpowerGroup's guest apply is a plain multipart POST to `JobApplyWithEmail` (NO auth token, NO CSRF,
NO Azure-B2C session, NO captcha, NO résumé required) — see `strategies/manpower.py`. So unlike the
Playwright BPO lanes, this lane needs NO browser: it fetches the job page (cookie jar + the posting's
GUID `jobItemID`), then POSTs the persona's application with httpx. The real POST is gated by env
**MANPOWER_ADVANCE=1** (default OFF — a plain run builds the payload but transmits nothing).

Each run mints a FRESH synthetic US persona (placed in a coherent US city/state/zip), provisions its
@takhet.com mailbox, submits, and — under MANPOWER_ADVANCE — treats the API `status==1000` (+entityID)
as the on-page "application received" success, then watches the persona Maildir for a corroborating ack.

    python3 -m backend.tools.manpower_recon --job <mass_hiring_id>            # dry run (no submit)
    MANPOWER_ADVANCE=1 python3 -m backend.tools.manpower_recon --job <id>    # real submit

Run under `sg mail` (mailbox provisioning + the Maildir read need the mail group)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFILL_ROOT = os.path.join(REPO, "uploads", "prefill")
MAILROOT = "/var/mail/vhosts/takhet.com"

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")

# state full name -> (capital city, a valid zip) — real triples so the address is coherent (the apply
# form's zip→city/state autocomplete would otherwise reconcile them). Reused shape from the TTEC lane.
_STATE_PLACE = {
    "Alabama": ("Birmingham", "35203"), "Alaska": ("Anchorage", "99501"),
    "Arizona": ("Phoenix", "85004"), "Arkansas": ("Little Rock", "72201"),
    "California": ("Sacramento", "95814"), "Colorado": ("Denver", "80202"),
    "Connecticut": ("Hartford", "06103"), "Delaware": ("Wilmington", "19801"),
    "Florida": ("Orlando", "32801"), "Georgia": ("Atlanta", "30303"),
    "Idaho": ("Boise", "83702"), "Illinois": ("Chicago", "60602"),
    "Indiana": ("Indianapolis", "46204"), "Iowa": ("Des Moines", "50309"),
    "Kansas": ("Wichita", "67202"), "Kentucky": ("Louisville", "40202"),
    "Louisiana": ("Baton Rouge", "70802"), "Maine": ("Portland", "04101"),
    "Maryland": ("Baltimore", "21201"), "Massachusetts": ("Boston", "02108"),
    "Michigan": ("Detroit", "48226"), "Minnesota": ("Minneapolis", "55401"),
    "Mississippi": ("Jackson", "39201"), "Missouri": ("Kansas City", "64106"),
    "Montana": ("Billings", "59101"), "Nebraska": ("Omaha", "68102"),
    "Nevada": ("Las Vegas", "89101"), "New Hampshire": ("Manchester", "03101"),
    "New Jersey": ("Newark", "07102"), "New Mexico": ("Albuquerque", "87102"),
    "New York": ("Albany", "12207"), "North Carolina": ("Charlotte", "28202"),
    "North Dakota": ("Fargo", "58102"), "Ohio": ("Columbus", "43215"),
    "Oklahoma": ("Oklahoma City", "73102"), "Oregon": ("Portland", "97204"),
    "Pennsylvania": ("Philadelphia", "19103"), "Rhode Island": ("Providence", "02903"),
    "South Carolina": ("Columbia", "29201"), "South Dakota": ("Sioux Falls", "57104"),
    "Tennessee": ("Nashville", "37203"), "Texas": ("Austin", "78701"),
    "Utah": ("Salt Lake City", "84101"), "Vermont": ("Burlington", "05401"),
    "Virginia": ("Richmond", "23219"), "Washington": ("Seattle", "98104"),
    "West Virginia": ("Charleston", "25301"), "Wisconsin": ("Milwaukee", "53202"),
    "Wyoming": ("Cheyenne", "82001"),
}
_DEFAULT_STATE = "Ohio"


def _state_for_persona(location_raw: str) -> str:
    """A full US state name to place the persona in. Manpower's remote-job `jobLocation` is the branch
    city ('Plymouth, MI'); the role is remote so any US state is fine, but placing the persona in the
    branch's state keeps the address plausible. Falls back to Ohio."""
    from backend.tools.synth_persona import _us_state_full
    loc = location_raw or ""
    for tok in reversed([p.strip() for p in loc.split(",")]):
        full = _us_state_full(tok)
        if full and full in _STATE_PLACE:
            return full
    return _DEFAULT_STATE


def _build_persona(row: dict) -> dict:
    """Fresh synthetic US persona for this Manpower/Experis job, placed in a coherent US city/state/zip.
    Provisions the mailbox, registers the demo persona, writes the prefill dir, returns the persona +
    the fields the apply POST needs."""
    from backend.tools import mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    state = _state_for_persona(row.get("location_raw") or "")
    city, zc = _STATE_PLACE.get(state, ("Columbus", "43215"))
    job = {"title": row.get("title") or "", "company": (row.get("company") or "Manpower"),
           "company_key": (row.get("source") or "manpower"), "description": "",
           "category": row.get("category") or "",
           "location": f"{city}, {state}, United States", "regions": ["US"],
           "ats": row.get("source") or "manpower", "external_id": str(row.get("source_id") or ""),
           "url": row.get("apply_url") or "", "questions": []}
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
    prof["zip_code"] = zc
    prof["location"] = f"{city}, {state}"
    prof["_tools"] = (cand.get("facts") or {}).get("tools") or []

    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""), prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[manpower] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    # MANDATORY attractiveness pass via the shared engine (role-targeted, no-fabrication, guarded:
    # a tailor/LLM failure falls back to the base résumé — never empty, never raises).
    from backend.tools import mass_hiring_apply as _mha
    d = _mha.tailored_draft(job, cand)
    out.joinpath("resume.pdf").write_bytes(_mha.resume_pdf_bytes(d, cand))
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": row.get("source"), "mass_hiring_id": row["id"]}), encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": job["url"], "job_title": job["title"],
                    "company": row.get("company"), "profile": profile_id, "submitted": False},
                   indent=2), encoding="utf-8")
    return {"profile": prof, "facts": cand.get("facts") or {}, "state": state,
            "jobid": jobid, "profile_id": profile_id}


def _fetch_job_item_id(client, apply_url: str, job_id) -> str | None:
    """GET the stored job page (populates the cookie jar like a browser AND yields the GUID jobItemID)."""
    from backend.applier.strategies import manpower
    try:
        r = client.get(apply_url, headers={"User-Agent": _BROWSER_UA}, timeout=30)
        if r.status_code != 200:
            return None
        return manpower.extract_job_item_id(r.text, job_id)
    except Exception as e:  # noqa: BLE001
        print(f"[manpower] job-page fetch failed: {type(e).__name__}: {e}", flush=True)
        return None


_ACK_RE = re.compile(
    r"thank you for (applying|your application|your interest)|application (has been )?received|"
    r"received your application|we('| ha)ve received|application (has been )?submitted|"
    r"your application to", re.I)


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once a ManpowerGroup application-received mail lands in the persona Maildir (from a
    manpower/experis/manpowergroup sender or a matching 'thank you for applying' subject)."""
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
            if any(h in s for h in ("manpower", "experis", "manpowergroup")) or _ACK_RE.search(s):
                return True
    return False


def run(job_id: int, keep_minutes: int = 8) -> dict:
    import httpx

    from backend.applier.strategies import manpower

    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, source, source_id, company, title, apply_url, location_raw "
                    "FROM mass_hiring_jobs WHERE id=%s AND source IN ('manpower','experis')", (job_id,))
        r = cur.fetchone()
    if not r:
        print(f"no manpower/experis mass_hiring_jobs row id={job_id}", flush=True)
        return {"error": "no_row"}
    row = {"id": r[0], "source": r[1], "source_id": r[2], "company": r[3],
           "title": r[4], "apply_url": r[5], "location_raw": r[6]}
    print(f"=== {row['source']} apply: job {row['id']} — {row['title']}  (source_id={row['source_id']})",
          flush=True)

    p = _build_persona(row)
    prof = p["profile"]
    print(f"persona: {prof.get('full_name')} <{prof.get('email')}> {prof.get('city')}, {p['state']} "
          f"| MANPOWER_ADVANCE={os.getenv('MANPOWER_ADVANCE', '')}", flush=True)

    start_ts = time.time()
    with httpx.Client(follow_redirects=True, headers={"User-Agent": _BROWSER_UA}, timeout=30) as client:
        jii = _fetch_job_item_id(client, row["apply_url"], row["source_id"])
        print(f"jobItemID={jii}", flush=True)
        report = manpower.submit(row["source"], row["source_id"], jii, prof, client=client)
    print(f"[advanced={report.get('advanced')} submitted={report.get('submitted')} "
          f"http={report.get('http_status')} api_status={report.get('api_status')} "
          f"success={report.get('success')} entity_id={report.get('entity_id')} "
          f"note={report.get('note','')}]", flush=True)
    if not report.get("advanced"):
        print("[dry run — payload built, nothing transmitted. Set MANPOWER_ADVANCE=1 to submit.]",
              flush=True)
        print("[profile_data] " + json.dumps(report.get("profile_data") or {}), flush=True)
        print("[job_details] " + json.dumps(report.get("job_details") or {}), flush=True)
        return report

    if report.get("success"):
        print("[application SUBMITTED — API status 1000 (application received)]", flush=True)
        # update the CRM report artifact
        try:
            outp = Path(PREFILL_ROOT) / p["profile_id"] / p["jobid"] / "report.json"
            d = json.loads(outp.read_text())
            d["submitted"] = True
            d["entity_id"] = report.get("entity_id")
            outp.write_text(json.dumps(d, indent=2))
        except Exception:
            pass
    deadline = start_ts + keep_minutes * 60
    while time.time() < deadline:
        if _app_confirmed(prof.get("email", ""), start_ts - 60):
            report["mail_confirmed"] = True
            print("[application CONFIRMED — a ManpowerGroup receipt landed in the persona Maildir]",
                  flush=True)
            break
        if not report.get("success"):
            break
        time.sleep(10)
    print("=== manpower apply done", flush=True)
    return report


def manpower_job_ids(source: str | None = None) -> list[int]:
    """Active Manpower/Experis mass_hiring_jobs ids (both brands, or one via `source`)."""
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        if source in ("manpower", "experis"):
            cur.execute("SELECT id FROM mass_hiring_jobs WHERE source=%s AND active ORDER BY id",
                        (source,))
        else:
            cur.execute("SELECT id FROM mass_hiring_jobs WHERE source IN ('manpower','experis') "
                        "AND active ORDER BY id")
        out = [x[0] for x in cur.fetchall()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source manpower/experis)")
    ap.add_argument("--keep", type=int, default=8, help="minutes cap to await a Maildir receipt")
    ap.add_argument("--list", action="store_true", help="list auto-applyable Manpower/Experis ids + exit")
    ap.add_argument("--source", default="", help="restrict --list to 'manpower' or 'experis'")
    args = ap.parse_args()
    if args.list:
        ids = manpower_job_ids(args.source or None)
        print(f"{len(ids)} auto-applyable Manpower/Experis jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    run(args.job, keep_minutes=args.keep)


if __name__ == "__main__":
    main()
