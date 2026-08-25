"""Step-2 collector for complete, remote-only company job records.

The collector reads ATS targets produced by ``company_discovery`` and writes to
the isolated ``company_remote_*`` tables.  It never imports or updates the
existing vacancy catalog and it never fills or submits an application form.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from backend.tools import company_jobs_db as jobs_db
from backend.tools.company_job_questions import (
    normalize_greenhouse_questions,
    normalize_questions,
    scrape_questions,
)
from backend.tools.company_job_sources import fetch_remote_jobs


def _storage_record(company_id: int, ats_slug: str, job: dict) -> dict:
    """Map a connector row to the stable persistence contract."""
    row = dict(job)
    if row.get("is_remote") is not True or row.get("remote_type") != "remote":
        raise ValueError("connector returned a job without confirmed remote status")
    row["company_id"] = int(company_id)
    row["source_board_id"] = ats_slug
    row["source_payload"] = row.pop("raw_payload", row.get("source_payload") or {})
    row["source_updated_at"] = row.pop("updated_at", row.get("source_updated_at"))
    row["provenance"] = {
        "collector": "company_jobs_phase2",
        "ats": row.get("source"),
        "ats_slug": ats_slug,
        "job_url": row.get("job_url"),
    }
    row.setdefault("status", "active")
    return row


def _api_questions(job: dict) -> tuple[list[dict], bool]:
    """Return normalized API questions and whether the result is authoritative."""
    if job.get("questions_state") != "available":
        return [], False
    raw = job.get("questions") or []
    if job.get("source") == "greenhouse":
        normalized = normalize_greenhouse_questions(raw)
    else:
        normalized = normalize_questions(raw, source=f"{job.get('source')}_api")
    # An explicit empty API array is a complete zero-question result.  If the
    # provider returned data we could not normalize, the browser must retry it.
    return normalized, bool(normalized or not raw)


def _question_error(result: dict) -> str:
    parts = [str(result.get("error") or "").strip()]
    parts.extend(str(item).strip() for item in (result.get("reasons") or []))
    return "; ".join(dict.fromkeys(part for part in parts if part)) \
        or "application questions could not be completely collected"


def collect_company_jobs(
    *,
    status: str = "novel",
    limit_companies: int = 100,
    collect_questions: bool = True,
    question_limit: int = 0,
    headless: bool = True,
    store: Any = jobs_db,
    fetcher: Callable[..., list[dict]] = fetch_remote_jobs,
    question_scraper: Callable[..., dict] = scrape_questions,
) -> dict:
    """Collect all confirmed remote jobs for a bounded set of company boards.

    A board is marked complete only after its connector returned normally.  This
    distinction prevents a network/parse failure from closing previously active
    jobs.  Question collection is authoritative only for a complete ATS API
    response or a complete rendered-form scrape.
    """
    targets = store.list_company_targets(status=status, limit=limit_companies)
    result = {
        "companies_selected": len(targets),
        "companies_succeeded": 0,
        "companies_failed": 0,
        "remote_jobs_seen": 0,
        "jobs_stored": 0,
        "snapshots_created": 0,
        "questions_complete": 0,
        "questions_stored": 0,
        "questions_failed": 0,
        "questions_not_attempted": 0,
        "jobs_closed": 0,
        "errors": [],
    }
    question_attempts = 0

    for target in targets:
        company_id = int(target["id"])
        ats = str(target["ats"]).strip().casefold()
        ats_slug = str(target["ats_slug"]).strip()
        scan_id = store.begin_scan(company_id, ats, ats_slug)
        seen: list[str] = []
        try:
            jobs = fetcher(
                ats, ats_slug, company_id=company_id,
                ats_url=target.get("ats_url") or target.get("careers_url"),
            )
            result["remote_jobs_seen"] += len(jobs)
            for job in jobs:
                row = _storage_record(company_id, ats_slug, job)
                saved = store.upsert_job(company_id, row, scan_id)
                job_id = int(saved["job_id"])
                result["jobs_stored"] += 1
                result["snapshots_created"] += int(bool(saved["snapshot_created"]))
                seen.append(str(row["source_job_id"]))

                api_questions, api_complete = _api_questions(job)
                if api_complete:
                    count = store.save_questions(job_id, api_questions, "success")
                    result["questions_complete"] += 1
                    result["questions_stored"] += count
                    continue

                can_scrape = (collect_questions and
                              (question_limit <= 0 or question_attempts < question_limit))
                if not can_scrape:
                    store.save_questions(job_id, None, "not_attempted")
                    result["questions_not_attempted"] += 1
                    continue

                question_attempts += 1
                scraped = question_scraper(ats, row["apply_url"], headless=headless)
                if scraped.get("state") == "complete":
                    count = store.save_questions(
                        job_id, scraped.get("questions") or [], "success")
                    result["questions_complete"] += 1
                    result["questions_stored"] += count
                else:
                    store.save_questions(
                        job_id, scraped.get("questions") or [], "failed",
                        error=_question_error(scraped),
                    )
                    result["questions_failed"] += 1

            result["jobs_closed"] += store.finish_scan(scan_id, seen, complete=True)
            result["companies_succeeded"] += 1
        except Exception as exc:
            error = f"{target.get('canonical_name') or company_id}: {exc}"
            store.finish_scan(scan_id, seen, complete=False, error=str(exc))
            result["companies_failed"] += 1
            result["errors"].append(error)

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect complete remote jobs from independently discovered companies")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create isolated company_remote_* tables")
    collect = sub.add_parser("collect", help="scan company ATS boards")
    collect.add_argument("--status", default="novel",
                         choices=("novel", "known", "possible_duplicate", "promoted"))
    collect.add_argument("--limit-companies", type=int, default=100)
    collect.add_argument(
        "--skip-questions", action="store_true",
        help="skip rendered-form fallback; ATS API questions are still stored")
    collect.add_argument(
        "--question-limit", type=int, default=0,
        help="maximum rendered forms per run; 0 means all")
    collect.add_argument("--headed", action="store_true",
                         help="show browser while reading application questions")
    sub.add_parser("stats", help="show isolated remote-job counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            jobs_db.ensure_schema()
            output = {"initialized": True}
        elif args.command == "stats":
            output = jobs_db.counts()
        else:
            if args.limit_companies < 1:
                raise ValueError("--limit-companies must be at least 1")
            if args.question_limit < 0:
                raise ValueError("--question-limit cannot be negative")
            jobs_db.ensure_schema()
            output = collect_company_jobs(
                status=args.status,
                limit_companies=args.limit_companies,
                collect_questions=not args.skip_questions,
                question_limit=args.question_limit,
                headless=not args.headed,
            )
        print(json.dumps({"ok": True, **output}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
