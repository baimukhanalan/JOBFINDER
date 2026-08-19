"""CLI for the apply engine. Pre-fills an application (NEVER submits).

Examples:
  python -m backend.apply_cli --roster
  python -m backend.apply_cli --profile sample --url "https://boards.greenhouse.io/<token>/jobs/<id>" \
      --title "Customer Support Specialist" --company "Acme"

A human reviews the screenshot + PDF and clicks Submit themselves.
"""
import argparse
import asyncio
import json

from backend.applier.runner import prefill_application
from backend.data import roster
from backend.profiles.store import (DATA_DIR, PROJECT_ROOT, SESSIONS_DIR, get_profile,
                                    is_sample_profile, load_profiles)


def check_profile(profile_id: str) -> int:
    """Onboarding preflight: is everything in place for this person to apply?
    Returns 0 when the critical pieces (profile entry, facts, etalons) exist."""
    def line(ok: bool, text: str, critical: bool = False) -> bool:
        print(f"  [{'ok' if ok else 'MISS'}]{'' if ok else (' *' if critical else '  ')} {text}")
        return ok

    print(f"Preflight for profile '{profile_id}':")
    failed = False

    # 1) profiles.json entry + non-empty résumé
    prof = None
    try:
        prof = load_profiles().get(profile_id)
    except Exception as e:
        print(f"  [MISS] * profiles.json unreadable: {e}")
    if prof is None:
        line(False, "profiles.json entry (backend/data/profiles.json)", critical=True)
        failed = True
    else:
        ok = bool(prof.resume)
        failed |= not line(ok, "profiles.json entry"
                           + ("" if ok else " — `resume` is EMPTY (variants/tailoring need it)"),
                           critical=True)

    # 2) fact sheet + screener-key coverage vs the committed sample schema
    facts_path = DATA_DIR / "facts" / f"{profile_id}.json"
    if facts_path.exists():
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except Exception:
            facts = {}
        try:
            sample_keys = set(json.loads((DATA_DIR / "facts" / "sample.json")
                                         .read_text(encoding="utf-8")))
        except Exception:
            sample_keys = set()
        missing = sorted(sample_keys - set(facts if isinstance(facts, dict) else {}))
        n_have = len(sample_keys) - len(missing)
        line(True, f"facts: {facts_path.relative_to(PROJECT_ROOT)} "
                   f"({n_have}/{len(sample_keys)} screener keys"
                   + (f"; missing: {', '.join(missing)}" if missing else "") + ")")
    else:
        line(False, f"facts: {facts_path.relative_to(PROJECT_ROOT)} "
                    "(screener answers — copy sample.json and fill it in)", critical=True)
        failed = True

    # 3) résumé niche etalons
    etalons_path = DATA_DIR / "etalons" / f"{profile_id}.json"
    if etalons_path.exists():
        try:
            niches = json.loads(etalons_path.read_text(encoding="utf-8"))
            n = len(niches) if isinstance(niches, list) else 0
        except Exception:
            n = 0
        line(True, f"etalons: {etalons_path.relative_to(PROJECT_ROOT)} ({n} niches)")
    else:
        line(False, f"etalons: {etalons_path.relative_to(PROJECT_ROOT)} "
                    "(niche résumé variants — see sample.json for the shape)", critical=True)
        failed = True

    # 4) inbox index (informational — appears after the mail cron first runs)
    inbox_path = PROJECT_ROOT / "uploads" / "inbox" / f"{profile_id}.json"
    line(inbox_path.exists(),
         f"inbox index: {inbox_path.relative_to(PROJECT_ROOT)} "
         "(set `mailbox` in profiles.json; inbox_index.py cron writes it)")

    # 5) saved ATS sessions (informational — filled in as the person logs in)
    sess_dir = SESSIONS_DIR / profile_id
    line(sess_dir.is_dir(),
         f"sessions: {sess_dir.relative_to(PROJECT_ROOT)}/ (saved ATS logins, optional)")

    print("\n" + ("NOT READY — items marked * are required." if failed
                  else "READY — required pieces in place."))
    return 1 if failed else 0


def _guard_sample(ap: argparse.ArgumentParser, a: argparse.Namespace) -> None:
    """Refuse to touch live postings with the fake sample profile (burns the posting)."""
    if a.allow_sample:
        return
    if a.profile == "all" and getattr(a, "batch", False):
        return  # batch_prefill_all skips the sample profile per-profile itself
    try:
        profile = get_profile(a.profile)
    except KeyError as e:
        ap.error(str(e))  # unknown --profile -> usage error, not a traceback
    if is_sample_profile(profile):
        ap.error(f"profile {a.profile!r} is the SAMPLE profile (fake identity) — "
                 "refusing to pre-fill live postings. Pass --allow-sample for a test run.")


def main():
    ap = argparse.ArgumentParser(description="Pre-fill a job application (no submit).")
    ap.add_argument("--profile", default="sample",
                    help="profile id from profiles.json, or 'all' with --batch "
                         "(every ready profile, sample skipped)")
    ap.add_argument("--url", help="application URL")
    ap.add_argument("--title", default="Customer Support Specialist")
    ap.add_argument("--company", default="")
    ap.add_argument("--description", default="")
    ap.add_argument("--headful", action="store_true", help="show the browser (default headless)")
    ap.add_argument("--ai", action="store_true", help="use the local LLM to polish the résumé")
    ap.add_argument("--draft", action="store_true", help="LLM-draft answers to open questions (you review)")
    ap.add_argument("--parser-only", dest="parser_only", action="store_true",
                    help="résumé-parser-only (anti-spam): upload the résumé to the ATS's own "
                         "parser + attach it, and fill NOTHING else — the parser pre-populates "
                         "what it can, you fill the remaining required fields by hand. "
                         "(env RESUME_PARSER_ONLY=1 flips this on globally.)")
    ap.add_argument("--roster", action="store_true", help="print the target roster and exit")
    ap.add_argument("--eligible", metavar="ST", help="list employers that hire remote CS from a US state")
    ap.add_argument("--batch", action="store_true", help="pre-fill a batch of live roster openings")
    ap.add_argument("--limit", type=int, default=6, help="batch: max jobs to pre-fill")
    ap.add_argument("--per-vacancy", type=int, default=1, dest="per_vacancy",
                    help="batch --profile all: how many candidates to assign per online role (K)")
    ap.add_argument("--categorize", action="store_true",
                    help="collect openings and print which résumé niche each maps to (no browser)")
    ap.add_argument("--source", choices=["live", "db", "both"], default="both",
                    help="where to source openings: live ATS APIs, scraped DB pool, or both")
    ap.add_argument("--no-variants", dest="variants", action="store_false",
                    help="use the profile's single base résumé instead of the niche variants")
    ap.add_argument("--discover", action="store_true",
                    help="refresh the discovered-company cache from remote-job aggregators "
                         "(hundreds of probes; run weekly, not per-batch)")
    ap.add_argument("--allow-sample", action="store_true",
                    help="let the fake sample profile touch live postings (test runs only)")
    ap.add_argument("--check-profile", metavar="ID",
                    help="onboarding preflight: verify a profile's data files and exit")
    a = ap.parse_args()

    if a.check_profile:
        raise SystemExit(check_profile(a.check_profile))

    if a.discover:
        from backend.applier.discovery import refresh_discovered_slugs
        stats = asyncio.run(refresh_discovered_slugs())
        print(json.dumps(stats, indent=2))
        return

    if a.categorize:
        from backend.applier import boards
        from backend.services.tailor.variants import categorize, list_niches
        if not list_niches(a.profile):
            ap.error(f"no résumé variants for profile {a.profile!r} "
                     f"(backend/data/etalons/{a.profile}.json missing)")

        async def _collect():
            out = []
            if a.source in ("live", "both"):
                out += await boards.collect(limit=a.limit)
            if a.source in ("db", "both"):
                out += await boards.collect_from_db(limit=max(a.limit * 4, 40))
            seen, uniq = set(), []
            for j in out:
                u = j.get("apply_url", "")
                if u and u not in seen:
                    seen.add(u)
                    uniq.append(j)
            return uniq

        jobs = asyncio.run(_collect())
        print(f"{len(jobs)} openings (source={a.source}) -> résumé niche:\n")
        for j in jobs:
            niche, score = categorize(j.get("title", ""), j.get("description", ""), a.profile)
            print(f"  [{str(niche):22} s={score:<2}] {j.get('company','')[:18]:18} {j.get('title','')[:50]}")
        return

    if a.batch:
        from backend.applier.batch import batch_prefill, batch_prefill_all
        _guard_sample(ap, a)
        if a.profile == "all":
            res = asyncio.run(batch_prefill_all(per_vacancy=a.per_vacancy, use_ai=a.ai,
                                                headless=not a.headful, draft=a.draft,
                                                use_variants=a.variants, source=a.source,
                                                resume_parser_only=a.parser_only))
            print(f"ready: {', '.join(res['ready']) or 'none'}   "
                  f"skipped: {', '.join(res['skipped']) or 'none'}\n")
            print(f"{'profile':<14} {'live':>5} {'new':>4} {'kept':>5} {'errors':>6} {'queue':>5}")
            for pid, s in res["profiles"].items():
                if "error" in s:
                    print(f"{pid:<14} ERROR: {s['error']}")
                elif "blocked_reasons" in s:
                    print(f"{pid:<14} BLOCKED: {'; '.join(s['blocked_reasons'])}")
                else:
                    print(f"{pid:<14} {s.get('live_openings', '?'):>5} "
                          f"{s.get('new_prefilled', '?'):>4} {s.get('kept_pending', '?'):>5} "
                          f"{s.get('errors', '?'):>6} {s.get('queue_size', '?'):>5}")
            print("\nOpen each profile's review_queue.html, review, and submit yourself.")
            return
        res = asyncio.run(batch_prefill(profile_id=a.profile, limit=a.limit,
                                        use_ai=a.ai, headless=not a.headful, draft=a.draft,
                                        use_variants=a.variants, source=a.source,
                                        allow_sample=a.allow_sample,
                                        resume_parser_only=a.parser_only))
        print(json.dumps(res["summary"], indent=2))
        print("Open the review_queue.html, review each, and submit yourself.")
        return

    if a.roster:
        for e in roster.all_entries():
            print(f"[{e['tier']:24}] {e['name']:30} {e['ats']:14} {e['apply_url']}")
        print("\nAVOID:")
        for x in roster.AVOID:
            print("  -", x)
        return

    if a.eligible:
        st = a.eligible.upper()
        names = roster.eligible_for_state(st)
        url = {e["name"]: e["apply_url"] for e in roster.all_entries()}
        print(f"Employers that hire remote CS from {st} (verify on the posting):")
        for n in names:
            kind = roster.ELIGIBILITY[n].get("kind", "")
            print(f"  [{kind:4}] {n:32} {url.get(n, '')}")
        excluded = [n for n in roster.ELIGIBILITY if n not in names]
        print(f"\nNOT available in {st}: {', '.join(excluded) or 'none'}")
        print("Gig (DataAnnotation/Prolific) work in ALL states — start there regardless.")
        return

    if not a.url:
        ap.error("--url is required (or use --roster)")

    _guard_sample(ap, a)
    profile = get_profile(a.profile)
    job = {"title": a.title, "company": a.company, "description": a.description, "apply_url": a.url}
    rep = asyncio.run(prefill_application(job, profile, headless=not a.headful,
                                          use_ai=a.ai, draft_answers=a.draft,
                                          use_variants=a.variants,
                                          resume_parser_only=a.parser_only))
    keys = ["job_title", "company", "resume_niche", "match_score", "used_ai", "page_type",
            "filled", "failed", "submitted", "resume_pdf", "screenshot"]
    print(json.dumps({k: rep.get(k) for k in keys}, indent=2, default=str))
    print(f"\nReview the screenshot + PDF, then submit yourself. (submitted={rep.get('submitted')})")


if __name__ == "__main__":
    main()
