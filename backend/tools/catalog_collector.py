"""Collect remote jobs (+ descriptions + greenhouse form questions) across every
known ATS board into Postgres `job_catalog`.

Boards come from backend/data/{targets.json, discovered_slugs.json}, restricted to
the no-account ATS the app can read (ashby, greenhouse, lever). Threaded + resumable
(upsert), so re-running just refreshes. Greenhouse job ids are pulled from the apply
URL so we can fetch each posting's application questions via the public
`?questions=true` endpoint.

    python -m backend.tools.catalog_collector            # full run (remote-only + questions)
    python -m backend.tools.catalog_collector --no-questions
    python -m backend.tools.catalog_collector --all      # include non-remote too
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
from backend.tools import catalog_db

DATA = Path(__file__).resolve().parents[1] / "data"
_GH_ID = re.compile(r"/jobs/(\d+)")


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
        rows.append({
            "ats": ats, "company_key": slug, "company": company, "external_id": ext,
            "title": j.get("title", ""), "location": j.get("location", ""),
            "department": j.get("department", ""), "workplace": j.get("workplaceType", ""),
            "is_remote": bool(j.get("isRemote")), "url": url,
            "description": j.get("descriptionPlain", ""),
            "description_html": j.get("descriptionHtml", ""),
            "questions": None, "q_count": 0,
            "_gh_id": ext if (ats == "greenhouse" and ext.isdigit()) else None,
        })
    return rows


def _gh_questions(slug: str, jid: str):
    try:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?questions=true",
                      timeout=20)
        qs = r.json().get("questions", [])
        out = []
        for q in qs:
            fields = q.get("fields") or []
            out.append({"label": q.get("label", ""), "required": bool(q.get("required")),
                        "type": (fields[0].get("type") if fields else "")})
        return out
    except Exception:
        return None


def run(remote_only: bool = True, with_questions: bool = True,
        workers: int = 8, q_workers: int = 6) -> dict:
    catalog_db.ensure_schema()
    slugs = _slugs()
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include non-remote jobs too")
    ap.add_argument("--no-questions", action="store_true", help="skip greenhouse questions")
    ap.add_argument("--limit-boards", type=int, default=0, help="cap boards (for a quick test)")
    args = ap.parse_args()
    run(remote_only=not args.all, with_questions=not args.no_questions)
