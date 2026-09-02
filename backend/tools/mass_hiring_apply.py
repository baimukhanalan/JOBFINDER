"""Auto-fill lane for the Mass Hiring board — currently Maximus (Avature), the one
stable-comp ATS that submits end-to-end without a live captcha (verified 2026-08-28).

Reuses the /catalog building blocks (synth_persona → generate_draft → résumé render) but
reads a `mass_hiring_jobs` row instead of the auto-apply `job_catalog`, and writes the
co-pilot's prefill dir under a distinct 'mh_<id>' jobid namespace. The co-pilot picks
`AvatureStrategy` by URL (maximus.avature.net). The caller drives it in DRY-RUN by default
(`copilot /load` with dry_run=1) — the strategy fills but never submits, so nothing is
transmitted to the employer until we explicitly enable the live path.
"""
from __future__ import annotations

import json
import re

from backend.tools import catalog_drafts, drafts_ui
from backend.tools.catalog_drafts import PREFILL_ROOT

# Maximus/BPO titles carry the work-location city+state in parens, e.g.
# "CSR II Operations (Temporary, Remote Lawrence KS)" / "... Remote McAllen, TX)" /
# "... (Remote - New York, NY)". Some of these jobs require residence within N miles of that
# site (onsite equipment pickup), so the persona must be LOCATED there for the residence
# screener to answer Yes truthfully-by-design.
_TITLE_CITY_RE = re.compile(
    r"[Rr]emote\s*[-–,]?\s*([A-Za-z][A-Za-z .'-]+?),?\s+([A-Z]{2})\b")


def _city_from_title(title: str) -> str:
    m = _TITLE_CITY_RE.search(title or "")
    if not m:
        return ""
    city = m.group(1).strip(" .,-")
    st = m.group(2).strip()
    return f"{city}, {st}, United States"

# Apply hosts that have a working auto-fill strategy on THIS board. Avature (Maximus) and
# Oracle Recruiting Cloud / Candidate Experience (Alorica) complete without a live human
# captcha/assessment (ORC's only gate is an emailed PIN + invisible reCAPTCHA v3 the page
# auto-executes; see strategies/oracle_orc.py). Working Solutions' apply portal
# (apply.workingsolutions.com) is gated by a reCAPTCHA v2 CHECKBOX + an emailed 6-digit code
# — the checkbox is solved via captcha_solver (needs CAPTCHA_SOLVER_KEY) and a US RESIDENTIAL
# proxy for the risk/geo gate; see strategies/workingsolutions.py. SmartRecruiters (Sutherland,
# jobs.smartrecruiters.com) is a login-less guest apply whose SUBMIT carries no captcha — the
# oneclick form sits behind DataDome, so it needs a US RESIDENTIAL egress (which clears DataDome
# silently) and NO captcha key; see strategies/smartrecruiters.py. Kelly (KellyConnect,
# www.mykelly.com) is a login-less WordPress Gravity Form backed by Bullhorn whose SUBMIT carries
# NO captcha (verified live) — the only gate is Akamai bot-management on the host, cleared by a US
# RESIDENTIAL egress; no captcha key needed. See strategies/kelly.py. Amazon corporate ATS
# (account.amazon.jobs, SAML -> passport.amazon.jobs) is ACCOUNT-gated: register + emailed OTP
# guard the form, and the register step carries an AWS WAF CAPTCHA — so it needs BOTH a
# CAPTCHA_SOLVER_KEY (AWS WAF, solved via captcha_solver.solve_aws_waf) AND a US RESIDENTIAL
# proxy (the WAF token is IP-bound; datacenter IPs are risk-flagged); the account/OTP/wizard walk
# is gated behind AMAZON_ADVANCE. See strategies/amazon_apply.py.
SUPPORTED_HOSTS = ("avature.net", "oraclecloud.com", "apply.workingsolutions.com",
                   "smartrecruiters.com", "mykelly.com",
                   "account.amazon.jobs", "passport.amazon.jobs",
                   # Phenom family: Conduent (careers.conduent.com → Oracle HCM guest apply)
                   # + Humana (its own Workday tenant).
                   "careers.conduent.com", "humana.wd5.myworkdayjobs.com",
                   # Workday CxS mass-hiring family (strategies/workday.py) — the four validated
                   # tenants only, NOT a blanket myworkdayjobs.com, so an unvetted Workday tenant
                   # isn't silently attempted. reCAPTCHA v2-checkbox / Enterprise on the account
                   # step only (solved via captcha_solver + a US residential IP); the CxS Submit
                   # itself has none. Concentrix / CVS Health / Centene / Cigna.
                   "cnx.wd1.myworkdayjobs.com", "cvshealth.wd1.myworkdayjobs.com",
                   "centene.wd5.myworkdayjobs.com", "cigna.wd5.myworkdayjobs.com",
                   # iCIMS family: Teleperformance (careersus-teleperformance.icims.com) — the
                   # iframe iForm is account-gated with AWS-WAF + reCAPTCHA on submit
                   # (strategies/icims.py). Account creation + submit gated behind ICIMS_ADVANCE;
                   # needs a CapSolver key + a US residential egress to go live.
                   "icims.com")


def is_supported(apply_url: str) -> bool:
    u = (apply_url or "").lower()
    return any(h in u for h in SUPPORTED_HOSTS)


def _job_from_row(row: dict) -> dict:
    """Shape a mass_hiring_jobs row into the job dict synth_persona/generate_draft expect."""
    # Prefer the concrete city+state named in the title (residence screeners need it); fall
    # back to the raw location, then a bare US so _country_of still resolves United States.
    location = (_city_from_title(row.get("title") or "")
                or row.get("location_raw") or "United States")
    return {
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "company_key": row.get("company_key") or "",
        "description": "",                       # the board stores no JD body
        "location": location,
        "regions": ["US"],                       # the board is US-only
        "ats": "avature",
        "external_id": str(row.get("source_id") or row.get("id") or ""),
        "url": row.get("apply_url") or "",
        "questions": [],
    }


def _drafted_from_answers(d: dict) -> dict:
    """Fillable known-answers from the generated draft (skip file/none), same shape the
    co-pilot replays. Avature has no scraped questions, so this is usually empty and the
    strategy answers screeners deterministically."""
    drafted: dict[str, str] = {}
    for a in d.get("answers") or []:
        if not a or a.get("source") in ("file", "none"):
            continue
        v = a.get("value")
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        v = str(v or "").strip()
        lbl = drafts_ui._clean_label(a.get("label"))
        if lbl and v:
            drafted[lbl] = v
    return drafted


def prepare(row: dict, gender: str | None = None) -> tuple[str, str]:
    """Synthesize a US persona for this mass-hiring job, tailor a résumé, and write the
    co-pilot prefill dir. Returns (profile_id, jobid='mh_<id>'). No employer contact here —
    only the local persona/résumé/prefill artifacts are produced."""
    if not is_supported(row.get("apply_url", "")):
        raise ValueError("auto-fill not supported for this source yet")
    from backend.tools.synth_persona import synth_persona

    job = _job_from_row(row)

    # fast on-demand tier for the résumé tailoring (mirrors ensure_and_wire); guarded so a
    # config quirk never breaks the fill (tailoring falls back to the deterministic path).
    try:
        from backend.config import settings
        if settings.llm_model != "gpt-5.6-luna":
            settings.llm_model = "gpt-5.6-luna"
    except Exception:
        pass

    cand = synth_persona(job, gender=gender)

    # Force the persona to LIVE at the job's city/state (parsed from the title) so residence
    # screeners ("do you reside within 75 miles of <site>?") are coherent — synth_persona only
    # knows major cities, so a "Lawrence, KS" job would otherwise land the persona elsewhere.
    place = _city_from_title(job["title"])
    if place:
        from backend.tools.synth_persona import _us_state_full
        city, st_code = place.split(",")[0].strip(), place.split(",")[1].strip()
        prof = cand["profile"]
        prof["city"] = city
        prof["state"] = _us_state_full(st_code) or prof.get("state") or ""
        prof["location"] = f"{city}, {st_code}"
        pi = (prof.get("resume") or {}).get("personal_info")
        if isinstance(pi, dict):
            pi["location"] = f"{city}, {st_code}"

    # live, deliverable @takhet.com mailbox + CRM registration (best-effort, never fatal) so
    # a Maximus "Application Complete" reply lands in a box the CRM shows.
    try:
        from backend.tools import mailcrm
        from backend.tools.provision_mailboxes import provision_email
        prof = cand["profile"]
        provision_email(prof.get("email", ""), prof.get("full_name", ""))
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""),
                                      prof.get("id", ""))
    except Exception as e:
        print(f"[mh-fill] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)

    d = catalog_drafts.generate_draft(job, cand, use_ai=True, ideal=True)

    profile_id = cand["profile"]["id"]
    jobid = f"mh_{row['id']}"
    out = PREFILL_ROOT / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    out.joinpath("resume.pdf").write_bytes(
        drafts_ui.render_resume_pdf(d.get("resume") or {}) or b"")

    drafted = _drafted_from_answers(d)
    # City/country help generic Avature identity fields; the strategy also answers residence
    # screeners from the persona's own country, so this is belt-and-suspenders.
    ploc = (((d.get("resume") or {}).get("personal_info") or {}).get("location") or "").strip()
    city = ploc.split(",")[0].strip()
    country = cand["profile"].get("country") or (
        ploc.rsplit(",", 1)[-1].strip() if "," in ploc else "")
    if city:
        for lbl in ("Location (City)", "Location", "City", "Current location", "City/Town"):
            drafted.setdefault(lbl, city)
    if country:
        for lbl in ("Country", "Country/Region", "Country of residence"):
            drafted.setdefault(lbl, country)

    report = {"apply_url": job["url"], "job_title": job["title"], "company": job["company"],
              "profile": profile_id, "resume_niche": None, "drafted_answers": drafted,
              "submitted": False}
    out.joinpath("report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    out.joinpath("persona.json").write_text(
        json.dumps({"profile": cand["profile"], "facts": cand.get("facts") or {}},
                   ensure_ascii=False), encoding="utf-8")
    return profile_id, jobid


# ---- PARALLEL lane (headless worker pool) --------------------------------------
# Avature (Maximus) submits are independent, so run them across N headless co-pilot workers
# (bulk_pool, ports 8110+) instead of one-at-a-time on the single noVNC co-pilot. Each worker
# is its own browser (clean session per job), so this is faster AND avoids the shared-login
# pollution entirely. Workers get AVATURE_ADVANCE=1 so the wizard walks to the real Submit.
# Apply hosts whose bot-management 403s our datacenter IP but is CLEARED by the rotating BD pool
# gateway — route the co-pilot's browser context through a pool proxy for these (the co-pilot loads
# DIRECT otherwise, so an Akamai/DataDome host would 403 before the strategy ever fills). Verified
# live 2026-09-02: www.mykelly.com (Akamai) returns 200 + the Gravity Form through the pool. Others
# in SUPPORTED_HOSTS that the comments call "residential" (SmartRecruiters/DataDome, Working
# Solutions, Amazon/WAF) are NOT added here until the pool is verified to clear them.
_PROXY_APPLY_HOSTS = ("mykelly.com",)


def _host_needs_proxy(row: dict) -> bool:
    url = (row or {}).get("apply_url") or ""
    return any(h in url for h in _PROXY_APPLY_HOSTS)


def _proxy_for(row: dict) -> dict | None:
    """A rotating pool proxy (dict with server/username/password) for an apply host that needs one
    to get past host bot-management, else None (load direct). Best-effort — a dead pool returns
    None and the load proceeds direct (and 403s, surfaced as a normal fill failure)."""
    if not _host_needs_proxy(row):
        return None
    try:
        from backend.tools import proxy_pool
        return proxy_pool.next_proxy()
    except Exception:
        return None


def run_batch_parallel(row_ids, workers: int = 6, gender: str | None = None,
                       dry_run: bool = True, per_job_timeout: int = 360,
                       progress_path: str | None = None) -> list[dict]:
    """Apply to every row_id across a fresh headless worker pool. Returns a result per job.
    dry_run=True fills but never submits (safe). Writes progress to progress_path after each
    job if given. Tears the pool down on exit."""
    import queue as _queue
    import threading

    import httpx

    from backend.tools import bulk_pool, mass_hiring

    row_ids = list(row_ids)
    if not row_ids:
        return []
    n = max(1, min(int(workers), len(row_ids)))
    # Each worker reads its ATS-advance switch at import: Avature (Maximus) walks its wizard on
    # AVATURE_ADVANCE, Oracle ORC (Alorica) on ORC_ADVANCE, Working Solutions solves its captcha
    # + records the submit on WS_ADVANCE, SmartRecruiters (Sutherland) on SMARTRECRUITERS_ADVANCE,
    # Kelly (KellyConnect) records its Gravity Forms submit on KELLY_ADVANCE. Phenom: Conduent
    # (Oracle HCM) walks on PHENOM_ADVANCE (ORC_ADVANCE also enables it) and Humana (Workday)
    # creates its account + walks on WORKDAY_ADVANCE. All are set so a mixed batch drives whichever
    # ATS the job's URL routes to (dry_run still gates the final Submit click; the captcha solve is
    # itself a no-op without CAPTCHA_SOLVER_KEY — and Kelly carries no captcha).
    ports = bulk_pool.start_workers(
        n, wait=90, extra_env={"AVATURE_ADVANCE": "1", "ORC_ADVANCE": "1",
                               "WS_ADVANCE": "1", "SMARTRECRUITERS_ADVANCE": "1",
                               "KELLY_ADVANCE": "1", "PHENOM_ADVANCE": "1",
                               "WORKDAY_ADVANCE": "1"})
    if not ports:
        raise RuntimeError("no headless workers came up")

    q: _queue.Queue = _queue.Queue()
    for rid in row_ids:
        q.put(rid)
    results: list[dict] = []
    lock = threading.Lock()

    def _flush():
        if not progress_path:
            return
        conf = sum(1 for r in results if r.get("confirmed"))
        with open(progress_path, "w") as f:
            json.dump({"done": len(results), "total": len(row_ids), "confirmed": conf,
                       "workers": len(ports), "dry_run": dry_run, "results": results}, f, indent=2)

    def _worker(port: int):
        while True:
            try:
                rid = q.get_nowait()
            except _queue.Empty:
                return
            row = mass_hiring.job_by_id(rid)
            rec: dict = {"id": rid, "title": (row or {}).get("title"), "port": port}
            try:
                pid, jobid = prepare(row, gender=gender)
                rec["profile"] = pid
                httpx.post(f"http://127.0.0.1:{port}/release", data={"profile": pid}, timeout=10)
                # Akamai/DataDome hosts (Kelly) load DIRECT otherwise -> 403 before the strategy can
                # fill; route the co-pilot context through a pool proxy, and retry with a FRESH egress
                # if a bad IP still 403s (nothing filled, no click).
                needs_proxy = _host_needs_proxy(row)
                attempts = 3 if needs_proxy else 1
                res: dict = {}
                for attempt in range(attempts):
                    data = {"jobid": jobid, "profile": pid,
                            "dry_run": "1" if dry_run else "0", "wait_submit": "1"}
                    prox = _proxy_for(row) if needs_proxy else None
                    if prox and prox.get("server"):
                        data.update({"proxy_server": prox["server"],
                                     "proxy_username": prox.get("username", ""),
                                     "proxy_password": prox.get("password", "")})
                    r = httpx.post(f"http://127.0.0.1:{port}/load", data=data, timeout=per_job_timeout)
                    res = r.json() if "application/json" in r.headers.get("content-type", "") else {}
                    rec["http"] = r.status_code
                    sr = res.get("submit_result") or {}
                    if (not needs_proxy) or res.get("filled") or sr.get("clicked"):
                        break  # loaded fine (or a real fill/click) — a 403 leaves filled empty
                sr = res.get("submit_result") or {}
                rec.update({"clicked": sr.get("clicked"),
                            "confirmed": sr.get("confirmed"), "blocked": sr.get("blocked"),
                            "post_url": sr.get("post_url"), "filled": res.get("filled"),
                            "unfilled": res.get("unfilled"),
                            "proxy_attempts": (attempt + 1) if needs_proxy else 0})
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:200]
            with lock:
                results.append(rec)
                _flush()
            q.task_done()

    try:
        threads = [threading.Thread(target=_worker, args=(p,), daemon=True) for p in ports]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results
    finally:
        # ALWAYS tear the pool down (on normal finish, an exception, OR a KeyboardInterrupt/
        # SIGINT) so headless workers never orphan. A hard SIGTERM to the parent still needs
        # the caller's own signal handler (the runner installs one).
        try:
            bulk_pool.stop_workers()
        except Exception:
            pass
