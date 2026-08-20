"""Batch pre-fill: roster -> live openings -> tailor + pre-fill each -> a human review queue.

Nothing is submitted. The output is a folder of pre-filled applications (résumé PDF +
screenshot + report per job) and a single review_queue.html the person opens to review
each one and click through to submit themselves.
"""
# Cron (recommended): pre-fill for EVERY ready profile twice a day. Replaces the
# old michael-only line — `--profile all` iterates all profiles, skipping the
# sample profile and any profile missing its facts/etalons files:
#
#   0 8,16 * * * cd /home/projects/jobfinder && timeout 7200 /usr/bin/python3 \
#       -m backend.apply_cli --batch --profile all --source both --limit 60 --draft \
#       >> /tmp/jobfinder-refresh.log 2>&1
#
import json
import logging
import time
from html import escape
from pathlib import Path

from backend.applier import boards
from backend.applier.profile_validator import validate_profile
from backend.applier.runner import OUT_ROOT, _slug, prefill_application
from backend.profiles.store import DATA_DIR, get_profile, is_sample_profile, load_profiles

logger = logging.getLogger(__name__)

# Per-profile data files a profile must have before batching (same critical
# checks as the --check-profile preflight). Module-level so tests can repoint.
FACTS_DIR = DATA_DIR / "facts"
ETALONS_DIR = DATA_DIR / "etalons"

# Queue hygiene: a pending item nobody reviewed in this many days is dead weight —
# dropped from the queue into archived.json (and never re-prefilled, see _archived_urls).
STALE_DAYS = 14

# Public dashboard base for Telegram links (nginx handles auth — never put
# credentials in these URLs).
DASH_URL = "https://jobfinder.systeam.kz"

# Keys a review-queue item carries (subset of the per-job report).
_QUEUE_KEYS = ("job_title", "company", "apply_url", "resume_niche", "match_score", "filled",
               "unfilled", "failed", "submitted", "screenshot", "resume_pdf", "page_type",
               "review_items", "answer_sources", "choice_picks", "drafted_answers")

# Human-set statuses after which a posting must never be re-prefilled or re-queued.
_TERMINAL_STATUSES = {"submitted", "rejected", "interview"}


def _archived_urls(out_dir: Path) -> set[str]:
    """urls dropped as stale in any run — excluded from the queue AND from re-prefill."""
    try:
        entries = json.loads((out_dir / "archived.json").read_text(encoding="utf-8"))
        return {e.get("url", "") for e in entries} - {""}
    except Exception:
        return set()


def _archive(out_dir: Path, entries: list[dict]) -> None:
    """Append entries to archived.json (created on first use)."""
    f = out_dir / "archived.json"
    try:
        existing = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        existing = []
    existing.extend(entries)
    f.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _notify(text: str, parse_mode: str | None = None) -> bool:
    """Send via bot.notify; lazy import so batch still works if bot/ is broken."""
    try:
        from bot.notify import notify
        return notify(text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning("batch notify failed: %s", e)
        return False


def _prior_state(out_dir: Path) -> tuple[dict[str, dict], set[str]]:
    """(url -> queue item) for every job already pre-filled, and the set of urls the
    human already resolved (submitted/rejected/interview in status.json).

    Pending items whose report.json is older than STALE_DAYS are dropped from the
    queue and appended to archived.json — nobody reviews a two-week-old pre-fill and
    the posting is likely gone. Archived urls stay excluded from re-prefill (see the
    dedup in batch_prefill), so a dropped item never re-enters as "new"."""
    status: dict = {}
    try:
        status = json.loads((out_dir / "status.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    archived = _archived_urls(out_dir)
    now = time.time()
    pending: dict[str, dict] = {}
    done: set[str] = set()
    stale: list[dict] = []
    for rep in out_dir.glob("*/report.json"):
        try:
            r = json.loads(rep.read_text(encoding="utf-8"))
        except Exception:
            continue
        url = r.get("apply_url", "")
        if not url:
            continue
        st = (status.get(rep.parent.name) or {}).get("status", "")
        # Terminal: the human resolved it, the engine auto-submitted it, OR the match
        # gate rejected it (fit below threshold) — a gated report must count as resolved
        # so its URL never re-enters the queue as "new" on the next run.
        if st in _TERMINAL_STATUSES or r.get("submitted") is True or r.get("gated_out") is True:
            done.add(url)
        elif url in archived:
            continue  # already dropped as stale in an earlier run
        elif now - rep.stat().st_mtime > STALE_DAYS * 86400:
            stale.append({"jid": rep.parent.name, "url": url, "reason": "stale"})
        else:
            item = {k: r.get(k) for k in _QUEUE_KEYS}
            # digest metadata — stripped before review_queue.json is written
            item["_jid"] = rep.parent.name
            item["_mtime"] = rep.stat().st_mtime
            pending[url] = item
    if stale:
        _archive(out_dir, stale)
        logger.info("Dropped %d stale pending item(s) (>%dd old) -> archived.json",
                    len(stale), STALE_DAYS)
    return pending, done


def _build_digest(profile_id: str, queue: list[dict], new_count: int,
                  status_age_days: float | None) -> str:
    """Telegram digest (HTML parse_mode) for one profile's batch — pure, so tests
    call it directly. Ready = a real application form with nothing failed; the
    top-5 links jump straight to the dashboard card (id="job-<jid>" anchors).
    Queue items carry _jid/_mtime set by _prior_state / batch_prefill."""
    ready: list[dict] = []
    need_info = 0
    for it in queue:
        if it.get("error"):
            continue
        if it.get("page_type") == "application_form" and not (it.get("failed") or 0):
            ready.append(it)
        else:
            need_info += 1
    lines = [f"<b>{len(ready)} ready</b> / {need_info} need info / "
             f"{new_count} new this run — {escape(profile_id)}"]
    # freshest first, tie-break higher match score
    ready.sort(key=lambda it: (-(it.get("_mtime") or 0), -(it.get("match_score") or 0)))
    for it in ready[:5]:
        score = it.get("match_score")
        lines.append(
            f"<a href=\"{DASH_URL}/#job-{it.get('_jid', '')}\">"
            f"{escape(str(it.get('company') or ''))} — {escape(str(it.get('job_title') or ''))}</a>"
            f" (score {score if score is not None else '?'})")
    if queue and status_age_days is not None and status_age_days * 24 > 48:
        lines.append(f"⚠️ queue untouched for {int(status_age_days)} days")
    return "\n".join(lines)


def _render_queue_html(profile_id: str, items: list[dict], out_html: Path) -> None:
    rows = []
    for it in items:
        if it.get("error"):
            rows.append(
                f"<div class='card err'><b>{escape(it.get('job_title',''))}</b> "
                f"@ {escape(it.get('company',''))}<br><span class='e'>error: {escape(str(it['error']))}</span></div>")
            continue
        shot = it.get("screenshot", "")
        rel = Path(shot).relative_to(out_html.parent) if shot and Path(shot).exists() else ""
        review = it.get("review_items") or []
        review_html = ""
        if review:
            rows_r = "".join(
                f"<div class='ri'>{escape(str(r.get('question','')))} — "
                f"{escape(str(r.get('answer','')))}</div>" for r in review)
            review_html = (
                f"<div class='m rev'>needs review: {len(review)}</div>"
                f"<details><summary>review these answers before submitting</summary>{rows_r}</details>")
        rows.append(
            "<div class='card'>"
            f"<div class='h'><b>{escape(it.get('job_title',''))}</b> @ {escape(it.get('company',''))}</div>"
            f"<div class='m'>résumé <b>{escape(str(it.get('resume_niche') or '—'))}</b> · "
            f"match {it.get('match_score','?')} · filled {it.get('filled','?')} · "
            f"unfilled {len(it.get('unfilled',[]) or [])} · submitted {it.get('submitted')}</div>"
            f"{review_html}"
            f"<a href='{escape(it.get('apply_url',''))}' target='_blank'>open posting → fill via co-pilot / Apply Assist</a><br>"
            + (f"<img src='{escape(str(rel))}'>" if rel else "<i>no screenshot</i>")
            + "</div>")
    html = (
        "<!doctype html><meta charset='utf-8'><title>Review queue</title>"
        "<style>body{font-family:Arial;max-width:1100px;margin:20px auto;background:#f6f7f9}"
        "h1{font-size:18px}.card{background:#fff;border:1px solid #ddd;border-radius:8px;"
        "padding:12px;margin:12px 0}.card.err{border-color:#e88}.h{font-size:15px}"
        ".m{color:#555;font-size:13px;margin:4px 0}.e{color:#b00}.m.rev{color:#b60;font-weight:bold}"
        ".ri{background:#fff8e8;border:1px solid #eedba8;border-radius:6px;padding:6px 8px;"
        "margin:4px 0;font-size:13px}details{margin:4px 0}summary{cursor:pointer;color:#777;font-size:13px}"
        "img{max-width:100%;border:1px solid #eee;margin-top:8px}a{color:#06c}</style>"
        f"<h1>Pre-filled applications for '{escape(profile_id)}' — {len(items)} jobs (review &amp; submit yourself)</h1>"
        + "".join(rows))
    out_html.write_text(html, encoding="utf-8")


async def batch_prefill(profile_id: str = "sample", atses: list[str] | None = None,
                        keywords: list[str] | None = None, limit: int = 6,
                        use_ai: bool = False, headless: bool = True,
                        draft: bool = False, use_variants: bool = True,
                        source: str = "both", only_urls: set[str] | None = None,
                        supplied_jobs: list[dict] | None = None,
                        allow_sample: bool = False,
                        resume_parser_only: bool = False) -> dict:
    profile = get_profile(profile_id)
    if is_sample_profile(profile) and not allow_sample:
        raise ValueError(
            f"profile {profile_id!r} is the SAMPLE profile — batch pre-filling live "
            "postings with fake data burns them. Pass allow_sample=True (CLI: "
            "--allow-sample) only for throwaway test runs.")

    # Reality gate: refuse to spend prefill slots on a profile whose contact data
    # can't receive a recruiter response (fictional phone / placeholder email).
    # The sample profile is fake by design — an explicit allow_sample run skips this.
    if not is_sample_profile(profile):
        problems = validate_profile(profile.to_form_dict())
        if problems:
            logger.error("Batch blocked for %r: %s", profile_id, "; ".join(problems))
            notified = _notify(
                f"⛔ {profile_id}: batch blocked — applications would be undeliverable\n"
                + "\n".join(f"• {p}" for p in problems)
                + f"\nfix it in /setup: {DASH_URL}/setup")
            return {"summary": {"profile": profile_id, "blocked_reasons": problems,
                                "notify": "ok" if notified else "fail"},
                    "items": []}

    # Source openings from the live ATS APIs and/or the scraped DB pool, then dedup by URL.
    # Collect a pool larger than `limit`: it doubles as the liveness check for jobs
    # pre-filled in earlier runs (still collected = posting still open).
    # supplied_jobs (Feature C: pre-assigned Salmon roles) bypass the roster collection
    # entirely — the assignment already chose the exact postings for this profile.
    if supplied_jobs is not None:
        collected = list(supplied_jobs)
    else:
        pool = max(limit * 4, 80)
        collected = []
        if source in ("live", "both"):
            collected += await boards.collect(atses, keywords, pool)
        if source in ("db", "both"):
            collected += await boards.collect_from_db(limit=pool, keywords=keywords)
    seen: set[str] = set()
    live: list[dict] = []
    for j in collected:
        u = j.get("apply_url", "")
        if u and u not in seen:
            seen.add(u)
            live.append(j)

    out_dir = OUT_ROOT / profile_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pending, done = _prior_state(out_dir)
    archived = _archived_urls(out_dir)  # read AFTER _prior_state: includes this run's drops
    # `limit` now means: max NEW postings to pre-fill this run. Already-pre-filled
    # postings are never redone; human-resolved ones (submitted etc.) and stale-archived
    # ones never reappear.
    jobs = [j for j in live if j["apply_url"] not in pending and j["apply_url"] not in done
            and j["apply_url"] not in archived]
    if only_urls is not None:  # Feature C: only the roles this profile was assigned
        # assignment urls are normalized (query stripped); normalize here too so a
        # boards apply_url carrying "?utm=..." still matches its assignment.
        jobs = [j for j in jobs if j.get("apply_url", "").split("?")[0] in only_urls]
    jobs = jobs[:limit]
    logger.info("Batch: %d live, %d already queued, %d resolved, %d archived -> %d new to pre-fill",
                len(live), len(pending), len(done), len(archived), len(jobs))

    items: list[dict] = []
    gated: list[dict] = []
    for job in jobs:
        try:
            rep = await prefill_application(job, profile, headless=headless,
                                            use_ai=use_ai, draft_answers=draft,
                                            use_variants=use_variants,
                                            resume_parser_only=resume_parser_only)
            if rep.get("gated_out"):
                # Fit below the match gate — not pre-filled; keep it out of the review
                # queue entirely (its report.json on disk marks the URL resolved so it
                # won't be retried, and lets Feature C hand the position to another profile).
                gated.append({"apply_url": rep.get("apply_url", ""),
                              "job_title": rep.get("job_title", ""),
                              "fit_score": rep.get("fit_score")})
                continue
            item = {k: rep.get(k) for k in _QUEUE_KEYS}
            # digest metadata (report dir name = the dashboard's jid)
            item["_jid"] = _slug(f"{job.get('company', '')}-{job.get('title', '')}")
            item["_mtime"] = time.time()
            items.append(item)
        except Exception as e:
            logger.warning("prefill failed for %s: %s", job.get("title"), e)
            items.append({"job_title": job.get("title", ""), "company": job.get("company", ""),
                          "apply_url": job.get("apply_url", ""), "error": str(e)})

    # Queue = new items + previously pre-filled jobs whose posting is still open.
    live_urls = {j["apply_url"] for j in live}
    kept = [it for u, it in pending.items() if u in live_urls]
    queue = items + kept
    public = [{k: v for k, v in it.items() if not k.startswith("_")} for it in queue]
    (out_dir / "review_queue.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
    out_html = out_dir / "review_queue.html"
    _render_queue_html(profile_id, queue, out_html)

    new_prefilled = sum(1 for i in items if not i.get("error"))
    summary = {
        "profile": profile_id,
        "live_openings": len(live),
        "new_prefilled": new_prefilled,
        "gated_out": len(gated),
        "kept_pending": len(kept),
        "resolved_skipped": len(done),
        "errors": sum(1 for i in items if i.get("error")),
        "queue_size": len(queue),
        "review_queue": str(out_html),
    }

    # N4: actionable digest after every batch — counts, deep links to the top
    # ready-to-submit cards, and a nudge when the queue sat untouched for days
    # (status.json only changes when the human marks something).
    status_f = out_dir / "status.json"
    status_age_days = ((time.time() - status_f.stat().st_mtime) / 86400
                       if status_f.exists() else None)
    digest = _build_digest(profile_id, queue, new_prefilled, status_age_days)
    summary["notify"] = "ok" if _notify(digest, parse_mode="HTML") else "fail"
    logger.info("Batch done: %s", summary)

    return {"summary": summary, "items": items}


def _is_ready(profile) -> bool:
    """Batch-ready = the profile has its facts sheet (form answers). Etalons are
    OPTIONAL — they only enable niche résumé variants (use_variants); the Salmon
    role-matched flow runs with variants off, so a missing etalon must not exclude
    an otherwise-complete profile from assignment."""
    return (FACTS_DIR / f"{profile.id}.json").exists()


# ---- Feature C: family-matched round-robin assignment --------------------------
SALMON_BOARD = "https://api.ashbyhq.com/posting-api/job-board/salmon-group"
_TARGETS_FILE = DATA_DIR / "targets.json"
# Fallback if targets.json is missing/corrupt: behave exactly like the old Salmon-only path.
_DEFAULT_TARGETS = [{"key": "salmon", "company": "Salmon", "ats": "ashby",
                     "slug": "salmon-group", "enabled": True}]
_ASSIGN_FILE = OUT_ROOT / "_assignments.json"
_ASSIGN_LOCK = __import__("threading").Lock()
_TARGETS_LOCK = __import__("threading").Lock()


def _profile_family(pid: str) -> str | None:
    """The PRIMARY archetype family a profile belongs to (its facts sheet)."""
    try:
        return json.loads((FACTS_DIR / f"{pid}.json").read_text()).get("role_family")
    except Exception:
        return None


def _profile_families(pid: str) -> list[str]:
    """EVERY role family a profile honestly covers (multi-family model): its facts
    `role_families` list, falling back to the single `role_family` for older sheets.
    A persona is eligible for any online role whose family is in this list — that's
    what lets one identity apply across adjacent role types (fin-risk AND data AND
    analyst), with the résumé tailored per JD."""
    try:
        f = json.loads((FACTS_DIR / f"{pid}.json").read_text())
    except Exception:
        return []
    fams = f.get("role_families")
    if isinstance(fams, list) and fams:
        return [x for x in fams if x]
    single = f.get("role_family")
    return [single] if single else []


def load_targets(enabled_only: bool = True) -> list[dict]:
    """The target-company registry (backend/data/targets.json). Each entry:
    {key, company, ats, slug, enabled}. Missing/corrupt file -> Salmon-only default,
    so the whole pipeline degrades to its historical behaviour instead of breaking."""
    try:
        data = json.loads(_TARGETS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("targets.json must be a non-empty list")
    except Exception as exc:
        logger.warning("targets.json unreadable (%s); falling back to Salmon-only", exc)
        data = _DEFAULT_TARGETS
    return [t for t in data if t.get("enabled", True)] if enabled_only else data


def online_ats_supported() -> set[str]:
    """ATS kinds that _online_roles can actually fetch today (others log-and-skip)."""
    return set(_ONLINE_FETCHERS)


def set_target_enabled(key: str, enabled: bool) -> list[dict]:
    """Flip one target's `enabled` flag in targets.json (atomic write). Returns the
    FULL registry (enabled + disabled) so callers can re-render a selector."""
    with _TARGETS_LOCK:
        data = load_targets(enabled_only=False)
        for t in data:
            if t.get("key") == key:
                t["enabled"] = bool(enabled)
        _TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TARGETS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_TARGETS_FILE)
    return data


def _board_online_roles(tgt: dict, family_for_role) -> list[dict]:
    """Strictly-REMOTE roles from ONE no-account board (ashby / greenhouse / lever),
    tagged with company + archetype family. ATS-agnostic: `ats_boards.fetch_board`
    normalizes each ATS's JSON to a common shape, so the same code serves every
    platform. Hybrid/onsite excluded (online == Remote per the product decision)."""
    from backend.applier import ats_boards, geo
    jobs = ats_boards.fetch_board(tgt.get("ats", "ashby"), tgt["slug"])
    company = tgt.get("company", tgt.get("slug", ""))
    out = []
    for j in jobs:
        if (j.get("workplaceType") or "").lower() != "remote":
            continue
        loc = j.get("location", "")
        if not geo.is_highpay(loc, company):
            continue  # low-pay region (LatAm / India / SE-Asia / E-EU …) — skip, not worth applying
        apply_url = (j.get("applyUrl") or j.get("jobUrl") or "").split("?")[0]
        if apply_url:
            out.append({"apply_url": apply_url, "title": j.get("title", ""),
                        "company": company, "location": loc,
                        "target": tgt.get("key", ""),
                        "family": family_for_role(j.get("title", "")),
                        "description": j.get("descriptionPlain", "")})
    return out


# Back-compat alias (older references to the Ashby-specific name).
_ashby_online_roles = _board_online_roles

# ats -> fetcher(tgt, family_for_role) -> list[role dict]. All no-account boards.
_ONLINE_FETCHERS = {
    "ashby": _board_online_roles,
    "greenhouse": _board_online_roles,
    "lever": _board_online_roles,
}


def _online_roles(targets: list[dict] | None = None) -> list[dict]:
    """Strictly-REMOTE ("online") roles across all ENABLED targets, each tagged with
    its company + archetype family. Hybrid/onsite excluded (online == Remote per the
    product decision). One dead/slow board never sinks the rest — failures are logged
    and skipped. `targets=None` reads the enabled registry; pass a subset to scope it."""
    from backend.tools.gen_profiles import family_for_role
    targets = load_targets() if targets is None else targets
    out: list[dict] = []
    for tgt in targets:
        fetcher = _ONLINE_FETCHERS.get(tgt.get("ats", "ashby"))
        if fetcher is None:
            logger.warning("no online fetcher for ats=%s (target=%s); skipping",
                           tgt.get("ats"), tgt.get("key"))
            continue
        try:
            out += fetcher(tgt, family_for_role)
        except Exception as exc:
            logger.warning("online roles fetch failed for %s: %s", tgt.get("key"), exc)
    return out


def _load_assignments() -> dict[str, dict]:
    """Ledger per position: {apply_url: {"owners": [...], "tried": [...]}}.
    `owners` = profiles that genuinely pre-filled it (count toward K); `tried` =
    every profile that attempted it (incl. gated) so we never re-offer a gated pair.
    Back-compat: an old list value is read as owners (== tried)."""
    try:
        raw = json.loads(_ASSIGN_FILE.read_text())
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for url, v in raw.items():
        if isinstance(v, list):
            out[url] = {"owners": list(v), "tried": list(v)}
        elif isinstance(v, dict):
            out[url] = {"owners": v.get("owners", []), "tried": v.get("tried", [])}
    return out


def _record_assignment(url: str, profile_id: str, ok: bool) -> None:
    """Atomically record one attempt: always into `tried`, into `owners` iff it
    genuinely pre-filled (not gated). Idempotent."""
    key = url.split("?")[0]
    with _ASSIGN_LOCK:
        led = _load_assignments()
        entry = led.setdefault(key, {"owners": [], "tried": []})
        if profile_id not in entry["tried"]:
            entry["tried"].append(profile_id)
        if ok and profile_id not in entry["owners"]:
            entry["owners"].append(profile_id)
        _ASSIGN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ASSIGN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(led, indent=2))
        tmp.replace(_ASSIGN_FILE)


def assign_round_robin(roles: list[dict], fam_profiles: dict[str, list[str]],
                       ledger: dict[str, dict], k: int) -> dict[str, set[str]]:
    """Give each online role up to `k` family-matched candidates, cycling profiles.

    A role needs (k - len(owners)) more genuine candidates; profiles already in the
    role's `tried` list (owned OR previously gated) are skipped, so a gated position
    flows to the NEXT untried profile instead of re-offering the one that gated it.
    A profile can be handed MANY roles in its family (one identity, many positions).
    Returns {profile_id: {apply_url, ...}} — the per-profile assignment for this run.
    """
    plan: dict[str, set[str]] = {}
    cursor: dict[str, int] = {}  # per-family rotation pointer
    for role in roles:
        fam = role.get("family")
        pool = fam_profiles.get(fam or "", [])
        if not pool:
            continue
        url = role["apply_url"]
        entry = ledger.get(url, {"owners": [], "tried": []})
        need = k - len(entry["owners"])
        if need <= 0:
            continue
        tried = set(entry["tried"])
        picked, loops = 0, 0
        i = cursor.get(fam, 0)
        while picked < need and loops < len(pool):
            pid = pool[i % len(pool)]
            i += 1
            loops += 1
            if pid in tried:
                continue  # already owns or already gated this role -> next profile
            plan.setdefault(pid, set()).add(url)
            tried.add(pid)
            picked += 1
        cursor[fam] = i
    return plan


async def batch_prefill_all(per_vacancy: int = 1, **kwargs) -> dict:
    """Assign Salmon's online roles across READY profiles (family-matched, up to
    `per_vacancy` candidates per role, cycling profiles), then pre-fill each.

    Replaces the old "every profile re-collects the whole pool" behaviour, which
    sent N identities at the SAME posting (a datacenter ban signal). Now each online
    role is handed to distinct family-matched candidates; the match gate is still the
    final guard, and gated positions flow to the next profile on a future run.

    Returns {"profiles": {id: summary_or_error}, "ready": [...], "skipped": [...],
             "assignments": {id: [urls]}, "unassigned_families": [...]}.
    """
    kwargs.pop("limit", None)  # per-profile cap is now the size of its assignment
    profiles = load_profiles()
    ready: list[str] = []
    skipped: list[str] = []
    fam_profiles: dict[str, list[str]] = {}
    for pid in sorted(profiles):  # deterministic order
        profile = profiles[pid]
        if is_sample_profile(profile) or not _is_ready(profile):
            skipped.append(pid)
            continue
        ready.append(pid)
        # Register the persona under EVERY family it covers, so assign_round_robin can
        # offer it roles across its whole cluster (one identity → many role types).
        for fam in (_profile_families(pid) or ["?"]):
            fam_profiles.setdefault(fam, []).append(pid)

    roles = _online_roles()
    jobmap = {r["apply_url"]: r for r in roles}
    plan = assign_round_robin(roles, fam_profiles, _load_assignments(), max(1, per_vacancy))
    role_fams = {r["family"] for r in roles}
    unassigned = sorted(f for f in role_fams if f and not fam_profiles.get(f))
    unclassified = sorted({r["apply_url"] for r in roles if not r.get("family")})

    results: dict[str, dict] = {}
    for pid in ready:
        only = plan.get(pid)
        if not only:  # this profile drew no roles this run (its family is covered)
            results[pid] = {"assigned": 0}
            continue
        assigned_jobs = [jobmap[u] for u in only if u in jobmap]
        try:
            res = await batch_prefill(profile_id=pid, supplied_jobs=assigned_jobs,
                                      limit=len(assigned_jobs), **kwargs)
            results[pid] = res.get("summary", {})
            # Ledger: which of this profile's assigned roles genuinely pre-filled
            # (owner, counts toward K) vs merely attempted (tried -> never re-offered).
            prefilled = {it.get("apply_url", "").split("?")[0]
                         for it in res.get("items", []) if not it.get("error")}
            for url in only:
                _record_assignment(url, pid, ok=url in prefilled)
        except Exception as e:
            logger.warning("batch_prefill failed for profile %s: %s", pid, e)
            results[pid] = {"error": str(e)}
    logger.info("Batch-all done: ready=%d assigned=%d unassigned_families=%s unclassified=%d",
                len(ready), sum(1 for v in plan.values() if v), unassigned, len(unclassified))
    return {"profiles": results, "ready": ready, "skipped": skipped,
            "assignments": {p: sorted(u) for p, u in plan.items()},
            "unassigned_families": unassigned, "unclassified_roles": unclassified}
