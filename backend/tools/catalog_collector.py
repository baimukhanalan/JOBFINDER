"""Collect remote jobs (+ descriptions + greenhouse form questions) across every
known ATS board into Postgres `job_catalog`.

Boards come from backend/data/{targets.json, discovered_slugs.json}, restricted to
the no-account ATS the app can read (ashby, greenhouse, lever, workable). Threaded +
resumable (upsert), so re-running just refreshes. Greenhouse job ids are pulled from
the apply URL so we can fetch each posting's application questions via the public
`?questions=true` endpoint (ashby/lever/workable questions are scraped separately,
see catalog_forms.py).

    python -m backend.tools.catalog_collector                     # full run (remote-only + questions)
    python -m backend.tools.catalog_collector --no-questions
    python -m backend.tools.catalog_collector --all                # include non-remote too
    python -m backend.tools.catalog_collector --ats greenhouse --limit 20   # smoke run, capped
    python -m backend.tools.catalog_collector --backfill-regions --no-llm  # classify rows missing regions
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from backend.applier import ats_boards
from backend.applier.regions import classify_with_source
from backend.tools import catalog_db

DATA = Path(__file__).resolve().parents[1] / "data"
# Greenhouse job id: standard boards URL (/jobs/<id>) OR a company career-site URL
# that carries it as ?gh_jid=<id> (e.g. careers.datadoghq.com/detail/…?gh_jid=…).
_GH_ID = re.compile(r"(?:/jobs/|[?&]gh_jid=)(\d+)")


def _slugs() -> dict:
    """{ats: {slug: company_name}} from targets + discovered_slugs, supported ATS only."""
    out = {a: {} for a in ats_boards.SUPPORTED}
    try:
        for t in json.loads((DATA / "targets.json").read_text()):
            a = (t.get("ats") or "").lower()
            if a in out and t.get("slug"):
                out[a][t["slug"]] = t.get("company") or t["slug"]
    except Exception:
        pass
    try:
        disc = json.loads((DATA / "discovered_slugs.json").read_text())
        if isinstance(disc, dict):
            for a, lst in disc.items():
                if a in out:
                    for s in lst:
                        out[a].setdefault(s, s)
    except Exception:
        pass
    return out


def _ext_id(ats: str, url: str) -> str:
    if ats == "greenhouse":
        m = _GH_ID.search(url or "")
        if m:
            return m.group(1)
    seg = (url or "").rstrip("/").split("/")[-1].split("?")[0]
    return seg or hashlib.sha1((url or "").encode()).hexdigest()[:16]


def collect_board(ats: str, slug: str, company: str, remote_only: bool) -> list[dict]:
    try:
        jobs = ats_boards.fetch_board(ats, slug)
    except Exception:
        return []
    rows = []
    for j in jobs:
        if remote_only and not j.get("isRemote"):
            continue
        url = j.get("applyUrl") or j.get("jobUrl") or ""
        ext = _ext_id(ats, url)
        row = {
            "ats": ats, "company_key": slug, "company": company, "external_id": ext,
            "title": j.get("title", ""), "location": j.get("location", ""),
            "department": j.get("department", ""), "workplace": j.get("workplaceType", ""),
            "is_remote": bool(j.get("isRemote")), "url": url,
            "description": j.get("descriptionPlain", ""),
            "description_html": j.get("descriptionHtml", ""),
            "questions": None, "q_count": 0,
            "_gh_id": ext if (ats == "greenhouse" and ext.isdigit()) else None,
        }
        regs, src = classify_with_source(row, use_llm=False)
        if regs:
            # A deterministic rule hit on re-collect is authoritative and may narrow a
            # prior LLM multi-region set (e.g. LLM US+CA -> rule US) — by design, not a bug.
            row["regions"], row["region_source"] = regs, src
        rows.append(row)
    return rows


def _gh_questions(slug: str, jid: str):
    try:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?questions=true",
                      timeout=20)
        qs = r.json().get("questions", [])
        out = []
        for q in qs:
            fields = q.get("fields") or []
            f0 = fields[0] if fields else {}
            item = {"label": q.get("label", ""), "required": bool(q.get("required")),
                    "type": f0.get("type", "")}
            # Capture the select/radio choices so answers can be pre-drafted against
            # the REAL allowed values (e.g. Ruby skill "0..4 (expert)", the sponsorship
            # visa list), not free text a dropdown can't accept.
            vals = f0.get("values") or []
            if vals:
                item["options"] = [(v.get("label") if isinstance(v, dict) else v) for v in vals]
            out.append(item)
        return out
    except Exception:
        return None


def run(remote_only: bool = True, with_questions: bool = True,
        workers: int = 8, q_workers: int = 6,
        ats_filter: str | None = None, limit: int = 0) -> dict:
    catalog_db.ensure_schema()
    slugs = _slugs()
    if ats_filter:
        slugs = {ats_filter: slugs.get(ats_filter, {})}
    if limit:
        slugs = {a: dict(list(v.items())[:limit]) for a, v in slugs.items()}
    boards = [(a, s, name) for a in slugs for s, name in slugs[a].items()]
    print(f"collecting {len(boards)} boards {[f'{a}:{len(v)}' for a, v in slugs.items()]}",
          flush=True)

    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(collect_board, a, s, name, remote_only): (a, s)
                for a, s, name in boards}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                all_rows.extend(f.result())
            except Exception:
                pass
            if done % 25 == 0:
                print(f"  boards {done}/{len(boards)} | jobs so far {len(all_rows)}", flush=True)

    print(f"fetched {len(all_rows)} jobs; upserting descriptions...", flush=True)
    B = 500
    for i in range(0, len(all_rows), B):
        catalog_db.upsert_jobs(all_rows[i:i + B])

    if with_questions:
        gh = [r for r in all_rows if r.get("_gh_id")]
        print(f"fetching greenhouse questions for {len(gh)} jobs...", flush=True)

        def qjob(r):
            qs = _gh_questions(r["company_key"], r["_gh_id"])
            if qs is not None:
                r["questions"] = qs
                r["q_count"] = len(qs)
            return r

        updated = []
        with ThreadPoolExecutor(max_workers=q_workers) as ex:
            done = 0
            for f in as_completed([ex.submit(qjob, r) for r in gh]):
                done += 1
                r = f.result()
                if r.get("questions") is not None:
                    updated.append(r)
                if done % 250 == 0:
                    print(f"  questions {done}/{len(gh)}", flush=True)
        for i in range(0, len(updated), B):
            catalog_db.upsert_jobs(updated[i:i + B])
        print(f"questions attached to {len(updated)} jobs", flush=True)

    c = catalog_db.counts()
    print(f"DONE. catalog counts -> {c}", flush=True)
    return c


def backfill_gh_questions(workers: int = 8, refresh_all: bool = False) -> int:
    """Fill greenhouse questions from the API. Default: only rows that missed them.
    refresh_all=True re-fetches ALL greenhouse rows to REFRESH stored questions (used
    to add the select `options` we now capture). Gets the real numeric id from the
    board's /jobs list, matched by URL."""
    from collections import defaultdict
    catalog_db.ensure_schema()
    rows = catalog_db.rows_missing_questions("greenhouse", missing_only=not refresh_all)
    by_slug = defaultdict(list)
    for r in rows:
        by_slug[r["company_key"]].append(r)
    print(f"greenhouse backfill: {len(rows)} rows across {len(by_slug)} boards", flush=True)
    total = 0
    for slug, rws in by_slug.items():
        try:
            jl = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                           timeout=25).json().get("jobs", [])
        except Exception:
            continue
        url2id = {(j.get("absolute_url") or ""): j.get("id") for j in jl}

        def work(r):
            jid = url2id.get(r["url"])
            if not jid:
                m = _GH_ID.search(r["url"] or "")
                jid = m.group(1) if m else None
            if not jid:
                return None
            qs = _gh_questions(slug, jid)
            return (r["external_id"], qs) if qs is not None else None

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(work, r) for r in rws]):
                res = f.result()
                if res:
                    catalog_db.set_questions("greenhouse", slug, res[0], res[1])
                    total += 1
    print(f"DONE greenhouse backfill: +{total}", flush=True)
    print("catalog counts ->", catalog_db.counts(), flush=True)
    return total


def backfill_regions(limit: int = 0, use_llm: bool = True) -> dict:
    """Classify every row whose regions IS NULL. Deterministic first; LLM on residue."""
    catalog_db.ensure_schema()
    rows = catalog_db.rows_missing_regions(limit)
    done = 0
    for r in rows:
        regs, src = classify_with_source(r, use_llm=use_llm)
        if regs or use_llm:
            # store [] (not NULL) so a resolved-empty row isn't re-processed forever —
            # but only once the LLM has had a shot at it (use_llm=True), otherwise a
            # rule-miss on a --no-llm pass would go NULL -> [] and permanently escape
            # the nightly LLM residue pass that's supposed to classify it later.
            # only_if_null guards against clobbering a tag a concurrent collect just set
            # (no flock between the 05:30 collect and 06:15 backfill cron jobs)
            catalog_db.set_regions(r["ats"], r["company_key"], r["external_id"], regs, src,
                                    only_if_null=True)
        # else: deterministic-only pass + rule miss -> leave regions NULL for the LLM cron
        done += 1
    return {"processed": done, **catalog_db.counts()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include non-remote jobs too")
    ap.add_argument("--no-questions", action="store_true", help="skip greenhouse questions")
    ap.add_argument("--backfill-gh", action="store_true",
                    help="only backfill greenhouse questions for rows that miss them")
    ap.add_argument("--refresh-gh", action="store_true",
                    help="re-fetch ALL greenhouse questions to refresh stored ones "
                         "(adds the select options we now capture)")
    ap.add_argument("--backfill-regions", action="store_true",
                    help="classify regions for rows whose regions IS NULL")
    ap.add_argument("--no-llm", action="store_true",
                    help="with --backfill-regions, skip the LLM fallback (deterministic only)")
    ap.add_argument("--limit", type=int, default=0,
                    help="with --backfill-regions, cap rows processed; otherwise cap "
                         "boards per ATS (smoke runs)")
    ap.add_argument("--ats", choices=ats_boards.SUPPORTED, default=None,
                    help="only collect this ATS (smoke runs)")
    args = ap.parse_args()
    if args.refresh_gh:
        backfill_gh_questions(refresh_all=True)
    elif args.backfill_gh:
        backfill_gh_questions()
    elif args.backfill_regions:
        print(backfill_regions(limit=args.limit, use_llm=not args.no_llm), flush=True)
    else:
        run(remote_only=not args.all, with_questions=not args.no_questions,
            ats_filter=args.ats, limit=args.limit)
