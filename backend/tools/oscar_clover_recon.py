"""Driver: auto-apply to one Oscar Health / Clover Health mass-hiring job.

Oscar and Clover are US health-insurer/payer tenants collected off their public **Greenhouse**
boards (`fetch_oscar`/`fetch_clover` -> `mass_hiring_jobs`, source `oscar`/`clover`). Recon
2026-09-22 confirmed BOTH expose the **STANDARD Greenhouse application form** (the
`boards.greenhouse.io/embed/job_app?for=<board>&token=<id>` embed renders a real form with
first_name/email/screener inputs; the board-api `?questions=true` returns the standard
First/Last/Email + screener question set) — NOT a custom/redirect apply. So there is NO new apply
strategy: these are driven through the EXACT same proven Greenhouse co-pilot auto-submit path the
`job_catalog` GH jobs use (GreenhouseStrategy fill -> co-pilot `_click_submit_after_fill` ->
emailed-code confirm), just seeded from a `mass_hiring_jobs` row instead of a `job_catalog` row.

This lane keeps the two data stores separate (mass_hiring_jobs is NOT written into job_catalog): it
mints a fresh synthetic US persona for the posting, materializes the co-pilot prefill dir with the
Greenhouse EMBED apply URL + drafted screener answers (the same shape `catalog_drafts.
materialize_prefill` writes), then hands it to the single co-pilot (8102) exactly like `_do_fill`.

The real fill+submit is gated by env **OSCAR_CLOVER_ADVANCE=1** (default OFF): with it off `run()`
builds the persona + prefill dir and resolves the embed URL but NEVER drives the co-pilot (dry run,
no submit). Ground truth for a real submit = a Greenhouse "thank you for applying" ack in the
persona's @takhet.com Maildir (same as the catalog GH lane).

    python3 -m backend.tools.oscar_clover_recon --job <mass_hiring_id>          # dry run (no submit)
    OSCAR_CLOVER_ADVANCE=1 python3 -m backend.tools.oscar_clover_recon --job N  # real fill+submit

Run headful under `DISPLAY=:98` + `sg mail` when advancing (the co-pilot is a headful Chromium on
:98; the persona mailbox is provisioned + read under the mail group). The pure URL/answer helpers
are network-free and unit-tested (`test_oscar_clover.py`)."""
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
COPILOT_URL = os.getenv("OSCAR_CLOVER_COPILOT", "http://127.0.0.1:8102")

_SOURCES = ("oscar", "clover")
# source -> (Greenhouse board slug, display company). The board slug DIFFERS from the source for
# Clover ('clover' source, 'cloverhealth' board); the apply_url tail carries the real slug too, so
# `board_slug` prefers parsing it and only falls back to this map.
_BOARD_SLUG = {"oscar": "oscar", "clover": "cloverhealth"}
_COMPANY = {"oscar": "Oscar Health", "clover": "Clover Health"}

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/125.0.0.0 Safari/537.36")

_MON = ["January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December"]


# --- pure helpers (network-free, unit-tested) ----------------------------------------------------

def oscar_clover_advance() -> bool:
    return os.getenv("OSCAR_CLOVER_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


_APPLY_TAIL_RE = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=([a-z0-9_-]+)&token=(\d+)"
                            r"|([a-z0-9_-]+)/jobs/(\d+))", re.I)


def parse_apply_url(apply_url: str) -> tuple[str, str]:
    """(board_slug, gh_job_id) from a Greenhouse apply/board URL. Handles both the board view
    (`job-boards.greenhouse.io/<slug>/jobs/<id>`, what the collector stores) and the embed form
    (`boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>`). ('','') when it doesn't match."""
    m = _APPLY_TAIL_RE.search(apply_url or "")
    if not m:
        return "", ""
    if m.group(1):
        return m.group(1), m.group(2)
    return m.group(3), m.group(4)


def board_slug(source: str, apply_url: str = "") -> str:
    """The Greenhouse board slug — parsed from the apply URL (authoritative) else the source map."""
    slug, _ = parse_apply_url(apply_url)
    return slug or _BOARD_SLUG.get((source or "").lower(), (source or "").lower())


def gh_job_id(apply_url: str, source_id: str | int = "") -> str:
    """The numeric Greenhouse job id — from the apply URL tail, else a numeric source_id."""
    _, jid = parse_apply_url(apply_url)
    if jid:
        return jid
    return str(source_id) if str(source_id or "").isdigit() else ""


def embed_apply_url(slug: str, gh_id: str | int) -> str:
    """The LIVE Greenhouse EMBED apply-form URL (never redirects) — the exact form GreenhouseStrategy
    + the co-pilot auto-submit path handle for every other catalog GH job."""
    return f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={gh_id}"


def embed_url_for(source: str, apply_url: str, source_id: str | int = "") -> str:
    """Resolve a `mass_hiring_jobs` (source, apply_url, source_id) row to its GH embed apply URL."""
    slug = board_slug(source, apply_url)
    gid = gh_job_id(apply_url, source_id)
    if slug and gid:
        return embed_apply_url(slug, gid)
    return apply_url or ""


def _drafted_answers(draft: dict, profile_id: str) -> dict:
    """Build the co-pilot `drafted_answers` map from a generated draft — a faithful mirror of
    `catalog_drafts.materialize_prefill` (label -> our reviewed value; the co-pilot replays these as
    known_answers and re-drafts everything else live). Kept in sync with that function; a change to
    the GH drafting contract should update both."""
    from backend.tools import drafts_ui
    drafted: dict[str, str] = {}
    for a in draft.get("answers") or []:
        if not a or a.get("source") in ("file", "none"):
            continue
        v = a.get("value")
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        v = str(v or "").strip()
        lbl = drafts_ui._clean_label(a.get("label"))
        if lbl and v:
            drafted[lbl] = v

    # Standard education typeahead labels (often not in the scraped questions).
    edu = ((draft.get("resume") or {}).get("education") or [])
    if edu:
        e0 = edu[0]
        if e0.get("school"):
            drafted.setdefault("School", e0["school"])
        if e0.get("degree"):
            drafted.setdefault("Degree", e0["degree"])
        if e0.get("field"):
            drafted.setdefault("Discipline", e0["field"])

    # Structured GH Employment block — supply a concrete Start+End date; NEVER tick 'Current role'
    # (a checked box + a still-required End date is unsatisfiable on natera-style forms — see the
    # materialize_prefill note).
    exp = ((draft.get("resume") or {}).get("experience") or [])
    if exp:
        x0 = exp[0]
        if x0.get("company"):
            drafted.setdefault("Company name", x0["company"])
        if x0.get("title"):
            drafted.setdefault("Title", x0["title"])
        years = re.findall(r"\d{4}", str(x0.get("dates") or ""))
        import datetime as _dt
        _t = _dt.date.today()
        if years:
            drafted.setdefault("Start date year", years[0])
        drafted.setdefault("Start date month", "January")
        drafted.setdefault("End date year", years[-1] if len(years) >= 2 else str(_t.year))
        drafted.setdefault("End date month", "December" if len(years) >= 2 else _MON[_t.month - 1])

    # City / Country geo-typeaheads (usually not scraped).
    ploc = (((draft.get("resume") or {}).get("personal_info") or {}).get("location") or "").strip()
    city = ploc.split(",")[0].strip()
    if city:
        for lbl in ("Location (City)", "Location", "City", "Current location", "City/Town"):
            drafted.setdefault(lbl, city)
    country = (draft.get("country") or "").strip() or (
        ploc.rsplit(",", 1)[-1].strip() if "," in ploc else "")
    if country:
        for lbl in ("Country", "Country/Region", "Country of residence", "Country of Residence",
                    "In which country are you located?", "Which country do you reside in?"):
            drafted.setdefault(lbl, country)

    # Required cover letter — SYNTHETIC personas only (demo_*); a real applicant leaves it blank.
    cover = str(draft.get("cover_letter") or "").strip()
    if cover and str(profile_id).startswith("demo_"):
        for lbl in ("Cover Letter", "Cover letter", "Cover Letter (optional)",
                    "Motivation Letter", "Letter of interest"):
            drafted.setdefault(lbl, cover)
    return drafted


_ACK_RE = re.compile(
    r"thank you for (applying|your application|your interest)|application (has been )?received|"
    r"received your application|we('| ha)ve received|application (has been )?submitted|"
    r"your application (to|has been)", re.I)


def _app_confirmed(email: str, since_ts: float) -> bool:
    """True once a Greenhouse application-received mail lands in the persona Maildir (a
    greenhouse-messages sender or a 'thank you for applying' subject). Same shape as the manpower
    lane's ack poll."""
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
            if "greenhouse" in s or _ACK_RE.search(s):
                return True
    return False


# --- prefill build (network + DB; not unit-tested live) ------------------------------------------

def _build_prefill(row: dict) -> dict:
    """Fresh synthetic US persona for this Oscar/Clover GH job; provisions the mailbox, generates a
    tailored résumé + drafted screener answers against the REAL GH questions, and writes the
    co-pilot prefill dir with the Greenhouse EMBED apply URL. Returns the profile_id/jobid/embed_url
    the co-pilot `/load` needs."""
    from backend.tools import catalog_collector, mailcrm
    from backend.tools.provision_mailboxes import provision_email
    from backend.tools.synth_persona import synth_persona

    slug = board_slug(row.get("source") or "", row.get("apply_url") or "")
    gid = gh_job_id(row.get("apply_url") or "", row.get("source_id") or "")
    embed = embed_apply_url(slug, gid) if (slug and gid) else (row.get("apply_url") or "")

    questions = catalog_collector._gh_questions(slug, gid) if (slug and gid) else None
    job = {"title": row.get("title") or "", "company": row.get("company") or _COMPANY.get(
               (row.get("source") or "").lower(), "Health"),
           "company_key": slug, "description": "", "category": row.get("category") or "",
           "location": "Remote, United States", "regions": ["US"],
           "ats": "greenhouse", "external_id": str(gid),
           "url": row.get("apply_url") or "", "questions": questions or []}
    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job)
    prof = cand["profile"]
    try:
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""), prof.get("id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[oscar_clover] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    profile_id = prof["id"]
    jobid = f"mh_{row['id']}"
    out = Path(PREFILL_ROOT) / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    # MANDATORY attractiveness pass via the shared engine (role-targeted, no-fabrication, guarded:
    # a tailor/LLM failure falls back to the base résumé — never empty, never raises).
    from backend.tools import mass_hiring_apply as _mha
    draft = _mha.tailored_draft(job, cand)
    out.joinpath("resume.pdf").write_bytes(_mha.resume_pdf_bytes(draft, cand))
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": prof, "facts": cand.get("facts") or {}}, ensure_ascii=False),
        encoding="utf-8")
    out.joinpath("status.json").write_text(
        json.dumps({"jobid": jobid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "source": row.get("source"), "mass_hiring_id": row["id"]}), encoding="utf-8")
    out.joinpath("report.json").write_text(
        json.dumps({"apply_url": embed, "job_title": job["title"], "company": job["company"],
                    "profile": profile_id, "resume_niche": None,
                    "drafted_answers": _drafted_answers(draft, profile_id), "submitted": False},
                   indent=2), encoding="utf-8")
    return {"profile_id": profile_id, "jobid": jobid, "embed_url": embed,
            "slug": slug, "gh_id": gid, "email": prof.get("email", ""),
            "full_name": prof.get("full_name", ""), "n_questions": len(questions or [])}


def _default_fill(jid: str, pid: str, *, wait_submit: bool) -> dict:
    """Drive the single co-pilot (8102) exactly like the catalog GH fill (`_do_fill`)."""
    from backend.dashboard_app import _fill_via
    return _fill_via(COPILOT_URL, jid, pid, wait_submit=wait_submit)


def run(job_id: int, keep_minutes: int = 8, *, fill_fn=None) -> dict:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, source, source_id, company, title, apply_url "
                    "FROM mass_hiring_jobs WHERE id=%s AND source IN %s",
                    (job_id, _SOURCES))
        r = cur.fetchone()
    if not r:
        print(f"no oscar/clover mass_hiring_jobs row id={job_id}", flush=True)
        return {"error": "no_row"}
    row = {"id": r[0], "source": r[1], "source_id": r[2], "company": r[3],
           "title": r[4], "apply_url": r[5]}
    print(f"=== {row['source']} apply: job {row['id']} — {row['title']}  (gh id={row['source_id']})",
          flush=True)

    p = _build_prefill(row)
    print(f"persona: {p['full_name']} <{p['email']}> | embed={p['embed_url']} "
          f"| gh_questions={p['n_questions']} | OSCAR_CLOVER_ADVANCE={os.getenv('OSCAR_CLOVER_ADVANCE', '')}",
          flush=True)

    if not oscar_clover_advance():
        print("[dry run — persona + prefill built, co-pilot NOT driven, nothing submitted. "
              "Set OSCAR_CLOVER_ADVANCE=1 to fill+submit.]", flush=True)
        return {"advanced": False, "embed_url": p["embed_url"], "profile_id": p["profile_id"],
                "jobid": p["jobid"], "submitted": False}

    start_ts = time.time()
    fill = fill_fn or _default_fill
    st = fill(p["jobid"], p["profile_id"], wait_submit=True)
    submit = (st or {}).get("submit") or {}
    print(f"[state={st.get('state')} filled={st.get('filled')} unfilled={st.get('unfilled')} "
          f"submit={submit.get('reason') or submit.get('confirmed')} "
          f"blocked={submit.get('blocked')}]", flush=True)
    report = {"advanced": True, "embed_url": p["embed_url"], "profile_id": p["profile_id"],
              "jobid": p["jobid"], "state": st.get("state"), "submit": submit,
              "submitted": bool(submit.get("confirmed")), "mail_confirmed": False}
    if submit.get("confirmed"):
        try:
            outp = Path(PREFILL_ROOT) / p["profile_id"] / p["jobid"] / "report.json"
            d = json.loads(outp.read_text())
            d["submitted"] = True
            outp.write_text(json.dumps(d, indent=2))
        except Exception:
            pass
    deadline = start_ts + keep_minutes * 60
    while time.time() < deadline:
        if _app_confirmed(p["email"], start_ts - 60):
            report["mail_confirmed"] = True
            print("[application CONFIRMED — a Greenhouse receipt landed in the persona Maildir]",
                  flush=True)
            break
        time.sleep(10)
    print("=== oscar/clover apply done", flush=True)
    return report


def oscar_clover_job_ids(source: str | None = None) -> list[int]:
    """Active Oscar/Clover mass_hiring_jobs ids (both, or one via `source`)."""
    with mail_db.conn() as c:
        cur = c.cursor()
        if source in _SOURCES:
            cur.execute("SELECT id FROM mass_hiring_jobs WHERE source=%s AND active ORDER BY id",
                        (source,))
        else:
            cur.execute("SELECT id FROM mass_hiring_jobs WHERE source IN %s AND active ORDER BY id",
                        (_SOURCES,))
        return [x[0] for x in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=0, help="mass_hiring_jobs id (source oscar/clover)")
    ap.add_argument("--keep", type=int, default=8, help="minutes cap to await a Maildir receipt")
    ap.add_argument("--list", action="store_true", help="list auto-applyable Oscar/Clover ids + exit")
    ap.add_argument("--source", default="", help="restrict --list to 'oscar' or 'clover'")
    args = ap.parse_args()
    if args.list:
        ids = oscar_clover_job_ids(args.source or None)
        print(f"{len(ids)} auto-applyable Oscar/Clover jobs: {ids}")
        return
    if not args.job:
        ap.error("--job is required (or --list)")
    run(args.job, keep_minutes=args.keep)


if __name__ == "__main__":
    main()
