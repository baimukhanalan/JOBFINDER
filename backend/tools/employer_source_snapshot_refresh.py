"""Bounded, resumable refresh of official GLEIF and USAspending snapshots."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools.company_sources import (
    DEFAULT_SEC_USER_AGENT, GLEIF_LEI_RECORDS_URL, USASPENDING_RECIPIENTS_URL,
    parse_gleif_lei_records, parse_usaspending_recipient_profile,
)

SUPPORTED = ("gleif_lei", "usaspending")
USASPENDING_AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_AWARD_GROUPS = (
    ("02", "03", "04", "05", "F001", "F002"),
    ("06", "10", "F006", "F007"),
    ("A", "B", "C", "D"),
    ("09", "11", "-1", "F005", "F008", "F009", "F010"),
    ("07", "08", "F003", "F004"),
    ("IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"),
)


class PendingSnapshot(RuntimeError):
    """Official fallback returned no usable exact-bound identity data."""


def list_refresh_candidates(source: str, *, limit: int, after_company_id: int = 0,
                            retry_errors: bool = False) -> list[dict]:
    if source not in SUPPORTED:
        raise ValueError(f"unsupported source: {source}")
    status_clause = "<> 'success'" if retry_errors else "IS NULL"
    with company_db._cur() as cur:
        cur.execute(f"""
          SELECT c.id company_id,c.source,c.source_external_id,c.legal_name,c.metadata
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population AND c.source=%s AND c.id>%s
            AND (c.metadata#>>'{{source_snapshot_refresh,status}}') {status_clause}
          ORDER BY c.id LIMIT %s
        """, (source, max(0, int(after_company_id)), max(1, min(int(limit), 500))))
        return [dict(row) for row in cur.fetchall()]


def save_snapshots(rows: list[dict]) -> int:
    if not rows:
        return 0
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_discovery SET metadata=metadata || %s::jsonb,
            provenance=provenance || %s::jsonb,updated_at=now()
          WHERE id=%s AND source=%s AND EXISTS (
            SELECT 1 FROM company_employer_master m
            WHERE m.company_id=company_discovery.id AND m.in_target_population)
        """, [(
            json.dumps({
                "source_snapshot": row.get("snapshot") or {},
                "source_snapshot_refresh": {
                    "status": row["status"], "error": row.get("error"),
                    "refreshed_at": row["refreshed_at"],
                },
            }),
            json.dumps({"official_source_snapshot": {
                "source": row["source"], "status": row["status"],
                "refreshed_at": row["refreshed_at"],
            }}), int(row["company_id"]), row["source"],
        ) for row in rows])
        return cur.rowcount


def _get_json(client: httpx.Client, url: str, *, retries: int = 3,
              params: dict | None = None) -> dict:
    response = None
    for attempt in range(retries + 1):
        try:
            response = client.get(url, params=params)
            if response.status_code == 200:
                value = response.json()
                return value if isinstance(value, dict) else {}
        except (httpx.HTTPError, ValueError):
            response = None
        if attempt < retries:
            time.sleep(min(0.5 * (2 ** attempt), 4))
    raise RuntimeError(f"official source request failed: {response.status_code if response else 'network'}")


def _post_json(client: httpx.Client, url: str, payload: dict, *, retries: int = 3) -> dict:
    response = None
    for attempt in range(retries + 1):
        try:
            response = client.post(url, json=payload)
            if response.status_code == 200:
                value = response.json()
                return value if isinstance(value, dict) else {}
        except (httpx.HTTPError, ValueError):
            response = None
        if attempt < retries:
            time.sleep(min(0.5 * (2 ** attempt), 4))
    raise RuntimeError(f"official source request failed: {response.status_code if response else 'network'}")


def _gleif_snapshot(row: dict, client: httpx.Client) -> dict:
    lei = str(row["source_external_id"])
    payload = _get_json(client, f"{GLEIF_LEI_RECORDS_URL}/{lei}")
    item = payload.get("data")
    parsed = parse_gleif_lei_records({"data": [item] if isinstance(item, dict) else [],
                                      "meta": payload.get("meta") or {}})
    if len(parsed) != 1 or parsed[0]["source_external_id"] != lei:
        raise RuntimeError("GLEIF identity mismatch or inactive record")
    metadata = parsed[0]["metadata"]
    return {
        "lei": lei, "legal_name": parsed[0]["legal_name"],
        "trade_name": parsed[0]["trade_name"],
        "legal_address": metadata.get("legal_address") or {},
        "headquarters_address": metadata.get("headquarters_address") or {},
        "addresses": metadata.get("addresses") or [],
        "jurisdiction": metadata.get("jurisdiction"),
        "entity_status": metadata.get("entity_status"),
        "entity_category": metadata.get("entity_category"),
        "last_update_date": metadata.get("last_update_date"),
    }


def _gleif_snapshots(rows: list[dict], client: httpx.Client) -> dict[str, dict]:
    """Use GLEIF's documented comma-separated multi-LEI filter."""
    if not rows:
        return {}
    leis = [str(row["source_external_id"]) for row in rows]
    parsed = []
    for start in range(0, len(leis), 100):
        chunk = leis[start:start + 100]
        payload = _get_json(client, GLEIF_LEI_RECORDS_URL, params={
            "filter[lei]": ",".join(chunk), "page[size]": len(chunk),
        })
        parsed.extend(parse_gleif_lei_records(payload))
    by_lei = {str(record["source_external_id"]): record for record in parsed}
    output = {}
    for lei in leis:
        record = by_lei.get(lei)
        if record is None:
            continue
        metadata = record["metadata"]
        output[lei] = {
            "lei": lei, "legal_name": record["legal_name"],
            "trade_name": record["trade_name"],
            "legal_address": metadata.get("legal_address") or {},
            "headquarters_address": metadata.get("headquarters_address") or {},
            "addresses": metadata.get("addresses") or [],
            "jurisdiction": metadata.get("jurisdiction"),
            "entity_status": metadata.get("entity_status"),
            "entity_category": metadata.get("entity_category"),
            "last_update_date": metadata.get("last_update_date"),
        }
    return output


def _usaspending_snapshot(row: dict, client: httpx.Client) -> dict:
    metadata = dict(row.get("metadata") or {})
    query = str(metadata.get("uei") or metadata.get("duns") or row["source_external_id"])
    listing = _post_json(client, USASPENDING_RECIPIENTS_URL, {
        "keyword": query, "limit": 20, "page": 1, "award_type": "all",
    })
    candidates = []
    for item in listing.get("results") or []:
        if not isinstance(item, dict):
            continue
        exact = query in {str(item.get("uei") or ""), str(item.get("duns") or ""),
                          str(item.get("id") or "")}
        if exact:
            candidates.append(item)
    if not candidates:
        raise RuntimeError("USAspending exact recipient identity not found")
    candidates.sort(key=lambda item: ({"R": 0, "P": 1, "C": 2}.get(
        str(item.get("recipient_level") or ""), 3), -float(item.get("amount") or 0)))
    exact_ids = {str(item.get("id") or "") for item in candidates}
    group_order = list(_AWARD_GROUPS)
    segment = str(metadata.get("employer_segment") or "")
    if segment != "government":
        group_order.insert(0, group_order.pop(2))  # contracts first for businesses
    for award_types in group_order:
        payload = _post_json(client, USASPENDING_AWARD_SEARCH_URL, {
            "filters": {
                "recipient_search_text": [query],
                "time_period": [{"start_date": "2007-10-01",
                                 "end_date": datetime.now(timezone.utc).date().isoformat()}],
                "award_type_codes": list(award_types),
            },
            "fields": ["Recipient Name", "Recipient UEI", "recipient_id",
                       "Recipient Location", "Award Amount"],
            "limit": 10, "page": 1, "sort": "Award Amount", "order": "desc",
            "subawards": False,
        })
        results = payload.get("results") or []
        if not results:
            continue
        expected_uei = str(metadata.get("uei") or row["source_external_id"])
        award = next((item for item in results if isinstance(item, dict)
                      and str(item.get("Recipient UEI") or "") == expected_uei
                      and str(item.get("recipient_id") or "") in exact_ids), None)
        if award is None:
            continue
        award_uei = str(award.get("Recipient UEI") or "")
        recipient_id = str(award.get("recipient_id") or "")
        location = award.get("Recipient Location")
        location = location if isinstance(location, dict) else {}
        return {
            "name": str(award.get("Recipient Name") or row.get("legal_name") or ""),
            "uei": award_uei, "duns": str(candidates[0].get("duns") or ""),
            "recipient_id": recipient_id,
            "recipient_level": recipient_id.rsplit("-", 1)[-1],
            "listing_recipient_ids": sorted(exact_ids),
            "business_types": [],
            "business_types_gap": "official_award_search_does_not_expose_business_types",
            "recipient_location": {"address_type": "recipient_location", "value": location}
            if location else None,
            "fallback_method": "official_spending_by_award_exact_uei",
        }
    raise PendingSnapshot("no award with exact UEI and recipient location in official search")


def refresh_source(source: str, *, limit: int = 100, after_company_id: int = 0,
                   retry_errors: bool = False, min_interval: float = 0.1,
                   client: httpx.Client | None = None) -> dict:
    limit = max(1, min(int(limit), 500))
    rows = list_refresh_candidates(source, limit=limit,
                                   after_company_id=after_company_id,
                                   retry_errors=retry_errors)
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30), headers={"User-Agent": DEFAULT_SEC_USER_AGENT})
    output = []
    started = time.monotonic()
    try:
        gleif = _gleif_snapshots(rows, client) if source == "gleif_lei" else {}
        def process(row):
            refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                if source == "gleif_lei":
                    snapshot = gleif.get(str(row["source_external_id"]))
                    if snapshot is None:
                        raise RuntimeError("GLEIF multi-filter response missing active LEI")
                else:
                    snapshot = _usaspending_snapshot(row, client)
                return {"company_id": row["company_id"], "source": source,
                        "status": "success", "snapshot": snapshot,
                        "refreshed_at": refreshed_at}
            except PendingSnapshot as exc:
                return {"company_id": row["company_id"], "source": source,
                        "status": "pending", "error": str(exc)[:300],
                        "refreshed_at": refreshed_at}
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                return {"company_id": row["company_id"], "source": source,
                        "status": "error", "error": str(exc)[:300],
                        "refreshed_at": refreshed_at}

        if source == "usaspending" and rows:
            with ThreadPoolExecutor(max_workers=min(4, len(rows))) as executor:
                futures = [executor.submit(process, row) for row in rows]
                for future in as_completed(futures):
                    output.append(future.result())
                    if min_interval:
                        time.sleep(max(0.0, min_interval))
        else:
            output.extend(process(row) for row in rows)
        updated = save_snapshots(output)
    finally:
        if owned:
            client.close()
    elapsed = max(time.monotonic() - started, 0.000001)
    return {"source": source, "selected": len(rows), "updated": updated,
            "success": sum(row["status"] == "success" for row in output),
            "errors": sum(row["status"] == "error" for row in output),
            "pending": sum(row["status"] == "pending" for row in output),
            "elapsed_seconds": round(elapsed, 3),
            "records_per_second": round(len(rows) / elapsed, 2),
            "last_company_id": max((int(row["company_id"]) for row in rows),
                                   default=after_company_id)}


def run_until_done(source: str, *, run_cap: int = 500, batch_size: int = 100,
                   after_company_id: int = 0, retry_errors: bool = False,
                   min_interval: float = 0.1, client: httpx.Client | None = None,
                   progress=None) -> dict:
    """Process multiple durable DB-checkpointed batches up to the hard per-run cap."""
    cap = max(1, min(int(run_cap), 500))
    size = max(1, min(int(batch_size), 200, cap))
    total = {"selected": 0, "updated": 0, "success": 0, "errors": 0,
             "pending": 0}
    cursor = max(0, int(after_company_id))
    batches = 0
    started = time.monotonic()
    while total["selected"] < cap:
        result = refresh_source(
            source, limit=min(size, cap - total["selected"]),
            after_company_id=cursor, retry_errors=retry_errors,
            min_interval=min_interval, client=client)
        batches += int(result["selected"] > 0)
        for key in total:
            total[key] += int(result.get(key, 0))
        cursor = int(result["last_company_id"])
        if progress is not None:
            progress({"batch": batches, "checkpoint_company_id": cursor,
                      "run_cap": cap, **total})
        if result["selected"] == 0:
            break
    elapsed = max(time.monotonic() - started, 0.000001)
    return {"source": source, **total, "batches": batches,
            "checkpoint_company_id": cursor, "run_cap": cap,
            "elapsed_seconds": round(elapsed, 3),
            "records_per_second": round(total["selected"] / elapsed, 2),
            "done": total["selected"] < cap}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=SUPPORTED)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-company-id", type=int, default=0)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--min-interval", type=float, default=0.1)
    parser.add_argument("--run-until-done", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(argv)
    if args.run_until_done:
        emit = (lambda value: print(json.dumps({"progress": value}, ensure_ascii=False),
                                    flush=True)) if args.progress else None
        result = run_until_done(
            args.source, run_cap=args.limit, batch_size=args.batch_size,
            after_company_id=args.after_company_id, retry_errors=args.retry_errors,
            min_interval=args.min_interval, progress=emit)
    else:
        result = refresh_source(args.source, limit=args.limit,
                                after_company_id=args.after_company_id,
                                retry_errors=args.retry_errors,
                                min_interval=args.min_interval)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
