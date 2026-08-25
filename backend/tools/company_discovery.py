"""CLI for the independent company-universe pipeline (no dashboard/UI wiring).

Examples:

  python -m backend.tools.company_discovery collect --source usaspending --limit 100
  python -m backend.tools.company_discovery collect --source sec --sec-bulk --limit 10000
  python -m backend.tools.company_discovery collect --source sam --limit 1000
  python -m backend.tools.company_discovery stats
  python -m backend.tools.company_discovery export --status novel --output novel.jsonl

The acquisition sources never read existing vacancies.  After storage, ``reconcile``
uses a read-only snapshot of ``job_catalog`` solely to label overlaps.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from backend.tools import company_discovery_db as company_db
from backend.tools import company_sources
from backend.tools.company_enrichment import enrich_company


_SOURCE_NAMES = ("sec", "usaspending", "sam")


def _external_ids(record: dict) -> dict[str, str]:
    source = record.get("source")
    external_id = str(record.get("source_external_id") or "")
    metadata = record.get("metadata") or {}
    if source == "sec_edgar":
        return {"sec_cik": external_id}
    if source == "sam_gov":
        return {"sam_uei": external_id}
    if source == "usaspending":
        uei = str(metadata.get("uei") or "")
        return {"sam_uei": uei} if uei else {"usaspending": external_id}
    return {str(source or "source"): external_id}


def prepare_source_record(record: dict) -> dict:
    """Add provenance and stable identity namespaces before persistence."""
    out = dict(record)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out["external_ids"] = _external_ids(out)
    out["provenance"] = {
        "source": out.get("source"),
        "source_external_id": out.get("source_external_id"),
        "collected_at": now,
    }
    out["discovery_confidence"] = 1.0
    out.setdefault("status", "novel")
    return out


def fetch_source(source: str, *, limit: int, sec_bulk: bool = False,
                 sec_submissions: bool = False, sec_archive: str = "",
                 max_pages: int = 20) -> list[dict]:
    if source == "sec":
        if sec_archive:
            return company_sources.parse_sec_submissions_zip(
                Path(sec_archive).read_bytes(), limit=limit)
        if sec_bulk:
            return company_sources.fetch_sec_bulk_companies(limit=limit)
        return company_sources.fetch_sec_companies(
            limit=limit, enrich_submissions=sec_submissions)
    if source == "usaspending":
        return company_sources.fetch_usaspending_recipients(
            limit=limit, max_pages=max_pages)
    if source == "sam":
        if not os.getenv("SAM_API_KEY"):
            raise RuntimeError("SAM_API_KEY is required for --source sam")
        return company_sources.fetch_sam_companies(limit=limit, max_pages=max_pages)
    raise ValueError(f"unsupported company source: {source}")


def collect_records(sources: list[str], *, limit: int, sec_bulk: bool = False,
                    sec_submissions: bool = False, sec_archive: str = "",
                    max_pages: int = 20,
                    enrich_web: bool = False, workers: int = 4,
                    country: str = "US") -> tuple[list[dict], dict[str, int]]:
    records: list[dict] = []
    by_source: dict[str, int] = {}
    seen_source_ids: set[tuple[str, str]] = set()
    for source in sources:
        fetched = fetch_source(source, limit=limit, sec_bulk=sec_bulk,
                               sec_submissions=sec_submissions, sec_archive=sec_archive,
                               max_pages=max_pages)
        kept = 0
        for raw in fetched:
            record = prepare_source_record(raw)
            if country and str(record.get("country") or "").upper() != country.upper():
                continue
            identity = (str(record.get("source")), str(record.get("source_external_id")))
            if identity in seen_source_ids:
                continue
            seen_source_ids.add(identity)
            records.append(record)
            kept += 1
        by_source[source] = kept

    if enrich_web:
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
            records = list(pool.map(enrich_company, records))
    return records, by_source


def write_jsonl(records: list[dict], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


def write_csv(records: list[dict], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for record in records for key in record})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({key: (json.dumps(value, ensure_ascii=False, default=str)
                                   if isinstance(value, (dict, list)) else value)
                             for key, value in record.items()})
    return path


def _collect_command(args) -> dict:
    records, by_source = collect_records(
        args.source, limit=args.limit, sec_bulk=args.sec_bulk,
        sec_submissions=args.sec_submissions, sec_archive=args.sec_archive,
        max_pages=args.max_pages,
        enrich_web=args.enrich_web, workers=args.workers, country=args.country)
    if args.output:
        write_jsonl(records, args.output)
    result = {"fetched": len(records), "by_source": by_source,
              "dry_run": bool(args.dry_run), "stored": 0, "reconciled": 0}
    if not args.dry_run:
        company_db.ensure_schema()
        result["stored"] = company_db.upsert_records(records)
        result["reconciled"] = company_db.reconcile_records()
        result["counts"] = company_db.counts()
    return result


def _export_command(args) -> dict:
    records = company_db.list_companies(status=args.status, source=args.source_name,
                                        limit=args.limit, offset=args.offset)
    path = write_csv(records, args.output) if args.format == "csv" \
        else write_jsonl(records, args.output)
    return {"exported": len(records), "output": str(path), "format": args.format}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independent US company discovery (separate from vacancy collection)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the company_discovery table")
    collect = sub.add_parser("collect", help="fetch independent company registries")
    collect.add_argument("--source", action="append", choices=_SOURCE_NAMES, required=True,
                         help="repeat to combine sources")
    collect.add_argument("--limit", type=int, default=1000,
                         help="maximum records per source")
    collect.add_argument("--country", default="US", help="country code filter; empty keeps all")
    collect.add_argument("--max-pages", type=int, default=20)
    collect.add_argument("--sec-bulk", action="store_true",
                         help="use SEC's nightly submissions ZIP instead of ticker JSON")
    collect.add_argument("--sec-submissions", action="store_true",
                         help="fetch per-CIK SEC submissions (bounded smoke only)")
    collect.add_argument("--sec-archive",
                         help="read a previously downloaded SEC submissions.zip")
    collect.add_argument("--enrich-web", action="store_true",
                         help="inspect official domains for careers page and ATS")
    collect.add_argument("--workers", type=int, default=4)
    collect.add_argument("--dry-run", action="store_true", help="do not write PostgreSQL")
    collect.add_argument("--output", help="also write normalized JSONL")

    reconcile = sub.add_parser("reconcile", help="label overlap with current job_catalog")
    reconcile.add_argument("--limit", type=int, default=0)
    sub.add_parser("stats", help="show company discovery counts")

    export = sub.add_parser("export", help="export discovered companies")
    export.add_argument("--status", choices=company_db.STATUSES)
    export.add_argument("--source-name")
    export.add_argument("--limit", type=int, default=100000)
    export.add_argument("--offset", type=int, default=0)
    export.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    export.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            company_db.ensure_schema()
            result = {"initialized": True}
        elif args.command == "collect":
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            result = _collect_command(args)
        elif args.command == "reconcile":
            result = {"reconciled": company_db.reconcile_records(args.limit),
                      "counts": company_db.counts()}
        elif args.command == "stats":
            result = company_db.counts()
        else:
            result = _export_command(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
