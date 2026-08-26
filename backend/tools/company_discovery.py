"""CLI for the independent company-universe pipeline (no dashboard/UI wiring).

Examples:

  python -m backend.tools.company_discovery collect --source usaspending --limit 100
  python -m backend.tools.company_discovery collect --source sec --sec-bulk --limit 10000
  python -m backend.tools.company_discovery collect --source sam --limit 1000
  python -m backend.tools.company_discovery collect --source gleif --limit 10000 --max-pages 50
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from backend.tools import company_discovery_db as company_db
from backend.tools import company_sources
from backend.tools.company_enrichment import enrich_company
from backend.tools.company_domain_resolver import (
    RateLimiter, bulk_mediawiki_candidates, bulk_wikidata_candidates,
    resolve_company, resolve_from_candidates,
)


_SOURCE_NAMES = ("sec", "usaspending", "sam", "gleif")


def _external_ids(record: dict) -> dict[str, str]:
    source = record.get("source")
    external_id = str(record.get("source_external_id") or "")
    metadata = record.get("metadata") or {}
    if source == "sec_edgar":
        return {"sec_cik": external_id}
    if source == "sam_gov":
        return {"sam_uei": external_id}
    if source == "gleif_lei":
        return {"lei": external_id}
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
    if source == "gleif":
        return company_sources.fetch_gleif_companies(
            limit=limit, max_pages=max_pages, country="US")
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


def _resolve_domains_command(args) -> dict:
    rows = company_db.list_without_domain(
        limit=args.limit, source=args.source_name, offset=args.offset,
        retry_attempted=args.retry_attempted)
    ambiguous_ids = _ambiguous_company_ids(rows)
    limiter = RateLimiter(args.min_interval)
    if args.wikidata_bulk or args.wikidata_api_bulk:
        return _resolve_domains_bulk(rows, args, limiter, ambiguous_ids)
    resolved: list[tuple[dict, dict]] = []
    provider_counts: dict[str, int] = {}
    errors = 0
    bulk_map = None
    bulk_available = False
    bulk_completed: set[int] = set()
    bulk_failed: set[int] = set()
    if args.wikidata_bulk and rows:
        bulk_map, bulk_completed, bulk_failed = bulk_wikidata_candidates(
            rows, limiter=limiter, batch_size=args.bulk_size)
        bulk_available = bool(bulk_completed)

    def work(row: dict):
        attempt: dict = {}
        if int(row["id"]) in ambiguous_ids:
            attempt.update({
                "attempted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "resolver": "company_name_guard", "result": "ambiguous_name",
            })
            result = None
        elif args.wikidata_bulk and int(row["id"]) in bulk_completed:
            result = resolve_from_candidates(
                row, bulk_map.get(int(row["id"]), []), limiter=limiter,
                threshold=args.threshold, attempt_out=attempt)
        elif (not args.wikidata_bulk or
              (args.bulk_per_item_fallback and int(row["id"]) in bulk_failed)):
            result = resolve_company(
                row, limiter=limiter, search_fallback=not args.no_search_fallback,
                threshold=args.threshold, attempt_out=attempt)
        else:
            attempt.update({
                "attempted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "resolver": "wikidata_sparql_p856", "result": "provider_unavailable",
            })
            result = None
        return result, attempt

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for future in as_completed(futures):
            try:
                result, attempt = future.result()
            except Exception:
                # One malformed/temporarily failing public response must not abort
                # an otherwise resumable bounded batch.
                errors += 1
                continue
            if result:
                resolved.append((futures[future], result))
                provider = result["domain_resolution"]["resolver"]
                provider_counts[provider] = provider_counts.get(provider, 0) + 1
            else:
                resolved.append((futures[future], {"domain_resolution": attempt}))

    updated = 0
    attempted = 0
    transient_errors = errors
    if not args.dry_run:
        successful = [(row, result) for row, result in resolved if result.get("domain")]
        negative = [(row["id"], result["domain_resolution"])
                    for row, result in resolved
                    if not result.get("domain") and (
                        (int(row["id"]) in bulk_completed
                         and result["domain_resolution"].get("result") == "no_exact_match")
                        or (not args.wikidata_bulk
                            and result["domain_resolution"].get("result") == "unresolved"))]
        transient_errors += sum(
            1 for _row, result in resolved
            if not result.get("domain")
            and result["domain_resolution"].get("result") in
                ("verification_failed", "provider_unavailable", "ambiguous_name"))
        for row, result in successful:
            updated += int(company_db.update_resolved_company(row["id"], result))
        attempted = company_db.record_domain_resolution_attempts(negative)
    resolved_count = sum(1 for _row, result in resolved if result.get("domain"))
    return {
        "selected": len(rows), "resolved": resolved_count, "updated": updated,
        "unresolved_recorded": attempted,
        "dry_run": bool(args.dry_run), "providers": provider_counts,
        "errors": errors, "transient_errors": transient_errors,
        "bulk_used": bool(args.wikidata_bulk and bulk_completed),
        "bulk_fallback": bool(args.wikidata_bulk and bulk_failed),
        "bulk_completed": len(bulk_completed), "bulk_retryable": len(bulk_failed),
        "coverage": round(resolved_count / len(rows), 4) if rows else 0.0,
    }


def _ambiguous_company_ids(rows: list[dict]) -> set[int]:
    """Return rows whose names cannot identify one legal entity in this batch."""
    by_name: dict[str, set[int]] = {}
    for row in rows:
        company_id = int(row["id"])
        for field in ("legal_name", "trade_name", "canonical_name"):
            name = company_db.normalize_company_name(row.get(field))
            if name:
                by_name.setdefault(name, set()).add(company_id)
    return {company_id for ids in by_name.values() if len(ids) > 1 for company_id in ids}


def _resolve_domains_bulk(rows: list[dict], args, limiter: RateLimiter,
                          ambiguous_ids: set[int] | None = None) -> dict:
    """Resolve SPARQL chunks concurrently and checkpoint each completed chunk."""
    ambiguous_ids = ambiguous_ids or set()
    resolvable = [row for row in rows if int(row["id"]) not in ambiguous_ids]
    chunk_size = 100
    chunks = [resolvable[i:i + chunk_size]
              for i in range(0, len(resolvable), chunk_size)]

    def process(chunk: list[dict]) -> dict:
        bulk_fn = bulk_mediawiki_candidates if args.wikidata_api_bulk \
            else bulk_wikidata_candidates
        mapping, completed, failed = bulk_fn(
            chunk, limiter=limiter, batch_size=args.bulk_size)
        successful: list[tuple[dict, dict]] = []
        negative: list[tuple[int, dict]] = []
        verification_failed = 0
        for row in chunk:
            company_id = int(row["id"])
            if company_id not in completed:
                continue
            attempt: dict = {}
            result = resolve_from_candidates(
                row, mapping.get(company_id, []), limiter=limiter,
                threshold=args.threshold, attempt_out=attempt,
                resolver_name=("wikidata_api_p856" if args.wikidata_api_bulk
                               else "wikidata_sparql_p856"))
            if result:
                successful.append((row, result))
            elif attempt.get("result") == "no_exact_match":
                negative.append((company_id, attempt))
            else:
                verification_failed += 1
        updated = unresolved = 0
        if not args.dry_run:
            for row, result in successful:
                updated += int(company_db.update_resolved_company(row["id"], result))
            unresolved = company_db.record_domain_resolution_attempts(negative)
        providers: dict[str, int] = {}
        for _row, result in successful:
            provider = result["domain_resolution"]["resolver"]
            providers[provider] = providers.get(provider, 0) + 1
        return {
            "resolved": len(successful), "updated": updated,
            "unresolved": unresolved, "completed": len(completed),
            "retryable": len(failed) + verification_failed,
            "providers": providers,
        }

    totals = {"resolved": 0, "updated": 0, "unresolved": 0,
              "completed": 0, "retryable": len(ambiguous_ids)}
    providers: dict[str, int] = {}
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, chunk) for chunk in chunks]
        for future, chunk in zip(futures, chunks):
            try:
                result = future.result()
            except Exception:
                errors += 1
                totals["retryable"] += len(chunk)
                continue
            for key in totals:
                totals[key] += result[key]
            for provider, count in result["providers"].items():
                providers[provider] = providers.get(provider, 0) + count
    return {
        "selected": len(rows), "resolved": totals["resolved"],
        "updated": totals["updated"], "unresolved_recorded": totals["unresolved"],
        "dry_run": bool(args.dry_run), "providers": providers, "errors": errors,
        "transient_errors": totals["retryable"], "bulk_used": totals["completed"] > 0,
        "bulk_fallback": totals["retryable"] > 0,
        "bulk_completed": totals["completed"], "bulk_retryable": totals["retryable"],
        "coverage": round(totals["resolved"] / len(rows), 4) if rows else 0.0,
    }


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

    resolve = sub.add_parser(
        "resolve-domains",
        help="resolve missing official domains without reading vacancy-derived data")
    resolve.add_argument("--limit", type=int, default=100)
    resolve.add_argument("--offset", type=int, default=0)
    resolve.add_argument("--source-name")
    resolve.add_argument("--workers", type=int, default=2,
                         help="bounded concurrency (maximum 4)")
    resolve.add_argument("--min-interval", type=float, default=1.0,
                         help="global delay between public request starts (minimum 0.25s)")
    resolve.add_argument("--threshold", type=float, default=0.88)
    resolve.add_argument("--no-search-fallback", action="store_true",
                         help="use only exact-name Wikidata P856 evidence")
    resolve.add_argument("--dry-run", action="store_true")
    resolve.add_argument("--retry-attempted", action="store_true",
                         help="retry rows with a previous negative resolution attempt")
    resolve.add_argument("--wikidata-bulk", action="store_true",
                         help="use exact-label Wikidata SPARQL VALUES batches")
    resolve.add_argument("--wikidata-api-bulk", action="store_true",
                         help="use exact-title MediaWiki wbgetentities batches")
    resolve.add_argument("--bulk-size", type=int, default=75,
                         help="labels per SPARQL request (25-100)")
    resolve.add_argument("--bulk-per-item-fallback", action="store_true",
                         help="explicitly fall back to slow per-company Wikidata requests")

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
        elif args.command == "resolve-domains":
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            if not 1 <= args.workers <= 4:
                raise ValueError("--workers must be between 1 and 4")
            if args.min_interval < 0.25:
                raise ValueError("--min-interval must be at least 0.25 seconds")
            if not 0.8 <= args.threshold <= 1.0:
                raise ValueError("--threshold must be between 0.8 and 1.0")
            if not 25 <= args.bulk_size <= 100:
                raise ValueError("--bulk-size must be between 25 and 100")
            if args.wikidata_bulk and args.wikidata_api_bulk:
                raise ValueError("choose only one Wikidata bulk mode")
            if (args.wikidata_bulk or args.wikidata_api_bulk) and not args.no_search_fallback:
                raise ValueError("Wikidata bulk modes require --no-search-fallback")
            result = _resolve_domains_command(args)
        else:
            result = _export_command(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
