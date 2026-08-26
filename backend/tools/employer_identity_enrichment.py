"""Structured employer identity enrichment without accepting a domain on name alone."""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_master_db as master_db
from backend.tools.employer_sources import USER_AGENT, WIKIDATA_API

_BLOCKED = {"facebook.com", "instagram.com", "linkedin.com", "x.com",
            "wikipedia.org", "youtube.com"}
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
WIKIDATA_ENRICHMENT_CONTRACT = 1


def _text(value) -> str | None:
    value = str(value or "").strip()
    return value or None


def _address(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    parts: list[str] = []
    for key in ("addressLines", "city", "region", "postalCode", "country"):
        item = value.get(key)
        if isinstance(item, list):
            parts.extend(str(part).strip() for part in item if str(part).strip())
        elif _text(item):
            parts.append(str(item).strip())
    return (", ".join(dict.fromkeys(parts)) or None,
            _text(value.get("country")))


def identity_from_stored_source(record: dict) -> dict:
    """Build identity fields solely from the discovery row and stored provenance."""
    metadata = dict(record.get("metadata") or {})
    source_snapshot = dict(metadata.get("source_snapshot") or {})
    qualification = dict(record.get("qualification_evidence") or {})
    source = str(record.get("source") or "")
    legal_name = _text(record.get("legal_name"))
    explicit_brand = (_text(metadata.get("brand_name")) or _text(record.get("trade_name"))
                      or _text(source_snapshot.get("trade_name")))
    employee_count = record.get("employee_count")
    employee_min = record.get("employee_count_min")
    employee_max = record.get("employee_count_max")
    employee_source = _text(record.get("employee_size_source"))
    industry = _text(record.get("industry"))
    naics = _text(record.get("naics")) or _text(metadata.get("naics"))
    headquarters = _text(record.get("headquarters"))
    headquarters_country = _text(record.get("headquarters_country"))
    address_type = None
    hq_pointer = None

    operational = metadata.get("operational_headquarters")
    if isinstance(operational, dict):
        headquarters, country = _address(operational)
        headquarters_country = country or headquarters_country
        address_type = "operational"
        hq_pointer = "metadata.operational_headquarters"
    elif (isinstance(metadata.get("headquarters_address"), dict)
          and bool(metadata.get("headquarters_address"))) or (
            isinstance(source_snapshot.get("headquarters_address"), dict)
            and bool(source_snapshot.get("headquarters_address"))):
        value = metadata.get("headquarters_address") or source_snapshot["headquarters_address"]
        headquarters, country = _address(value)
        headquarters_country = country or headquarters_country
        address_type = "headquarters"
        hq_pointer = "metadata.source_snapshot.headquarters_address"
    elif (isinstance(metadata.get("legal_address"), dict)
          and bool(metadata.get("legal_address"))) or (
            isinstance(source_snapshot.get("legal_address"), dict)
            and bool(source_snapshot.get("legal_address"))):
        value = metadata.get("legal_address") or source_snapshot["legal_address"]
        headquarters, country = _address(value)
        headquarters_country = country or headquarters_country
        address_type = "registered"
        hq_pointer = "metadata.source_snapshot.legal_address"
    elif _text(metadata.get("headquarters")):
        headquarters = _text(metadata.get("headquarters"))
        address_type = "operational"
        hq_pointer = "metadata.headquarters"
    elif headquarters and qualification.get("wikidata_entity"):
        address_type = "operational"
        hq_pointer = "qualification_evidence.wikidata_entity/P159"

    gaps: dict[str, str] = {}
    if not legal_name:
        gaps["legal_name"] = "source_record_missing_legal_name"
    if not explicit_brand:
        gaps["brand_name"] = "source_provides_legal_name_only"
    if employee_count is None and employee_min is None:
        gaps["employee_size"] = "source_provenance_has_no_workforce_evidence"
    if not industry:
        gaps["industry"] = "source_provenance_has_no_industry"
    if not naics:
        gaps["naics"] = "source_provenance_has_no_naics"
    if not headquarters:
        gaps["headquarters"] = "source_provenance_has_no_headquarters_address"
    elif not address_type:
        gaps["headquarters"] = "headquarters_present_without_address_type_evidence"

    field_sources = {
        "legal_name": "company_discovery.legal_name" if legal_name else None,
        "brand_name": ("company_discovery.metadata.brand_name"
                       if _text(metadata.get("brand_name")) else
                       "company_discovery.trade_name" if _text(record.get("trade_name")) else None),
        "employee_size": record.get("employee_size_source") if (
            employee_count is not None or employee_min is not None) else None,
        "industry": ("company_discovery.industry_or_stored_structured_evidence"
                     if industry else None),
        "naics": "company_discovery.naics" if naics else None,
        "headquarters": hq_pointer,
        "segment": "company_discovery.metadata.employer_segment",
    }
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    provenance = {
        "method": "stored_source_provenance",
        "source": source,
        "source_external_id": record.get("source_external_id"),
        "source_url": record.get("source_url"),
        "source_observed_at": (str(record.get("source_observed_at"))
                               if record.get("source_observed_at") is not None else None),
        "processed_at": observed_at,
        "field_sources": field_sources,
    }
    brand_identity = {
        "legal_name": legal_name,
        "brand_name": explicit_brand,
        "trade_name": _text(record.get("trade_name")),
        "brand_aliases": metadata.get("brand_aliases") or [],
        "source": source,
        "source_external_id": record.get("source_external_id"),
        "evidence_method": "stored_source_provenance",
    }
    return {
        "company_id": record["company_id"],
        "brand_name": explicit_brand or legal_name,
        "employee_count": employee_count, "employee_count_min": employee_min,
        "employee_count_max": employee_max, "employee_size_source": employee_source,
        "industry": industry, "naics_code": naics,
        "headquarters": headquarters, "headquarters_country": headquarters_country,
        "headquarters_address_type": address_type,
        "employer_segment": _text(metadata.get("employer_segment")) or record.get("employer_segment"),
        "brand_identity": brand_identity, "provenance": provenance, "gaps": gaps,
        "status": "complete" if not gaps else "incomplete",
    }


def enrich_stored_bulk(*, batch_size: int = 500, max_batches: int | None = None,
                       after_company_id: int = 0,
                       retry_incomplete: bool = False) -> dict:
    """Resume-safe, commit-per-batch enrichment from already persisted source data."""
    processed = updated = batches = 0
    cursor = max(0, int(after_company_id))
    while max_batches is None or batches < max(0, int(max_batches)):
        rows = master_db.list_stored_identity_batch(
            limit=batch_size, after_company_id=cursor,
            retry_incomplete=retry_incomplete)
        if not rows:
            break
        enriched = [identity_from_stored_source(row) for row in rows]
        updated += master_db.update_stored_identities(enriched)
        processed += len(rows)
        batches += 1
        cursor = max(int(row["company_id"]) for row in rows)
    return {"processed": processed, "updated": updated, "batches": batches,
            "last_company_id": cursor}


def stored_identity_report() -> dict:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.source,COUNT(*) total,
            COUNT(*) FILTER (WHERE NULLIF(c.legal_name,'') IS NOT NULL) legal_name,
            COUNT(*) FILTER (WHERE NULLIF(m.brand_identity->>'brand_name','') IS NOT NULL) brand_name,
            COUNT(*) FILTER (WHERE m.employee_count IS NOT NULL OR m.employee_count_min IS NOT NULL) employee_size,
            COUNT(*) FILTER (WHERE NULLIF(m.industry,'') IS NOT NULL) industry,
            COUNT(*) FILTER (WHERE NULLIF(m.naics_code,'') IS NOT NULL) naics,
            COUNT(*) FILTER (WHERE NULLIF(m.headquarters,'') IS NOT NULL
              AND NULLIF(m.headquarters_address_type,'') IS NOT NULL) headquarters,
            COUNT(*) FILTER (WHERE NULLIF(m.employer_segment,'') IS NOT NULL) segment,
            COUNT(*) FILTER (WHERE m.identity_enrichment_status='complete') complete,
            COUNT(*) FILTER (WHERE m.identity_enrichment_status='incomplete') incomplete
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population GROUP BY c.source ORDER BY c.source
        """)
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute("""
          SELECT c.source,gap.key field,gap.value reason,COUNT(*) count
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          CROSS JOIN LATERAL jsonb_each_text(m.identity_enrichment_gaps) gap
          WHERE m.in_target_population
          GROUP BY c.source,gap.key,gap.value
          ORDER BY c.source,gap.key,gap.value
        """)
        gap_rows = [dict(row) for row in cur.fetchall()]
    fields = ("legal_name", "brand_name", "employee_size", "industry", "naics",
              "headquarters", "segment")
    totals = {"total": sum(int(row["total"]) for row in rows)}
    totals.update({field: sum(int(row[field]) for row in rows) for field in fields})
    totals["complete"] = sum(int(row["complete"]) for row in rows)
    totals["incomplete"] = sum(int(row["incomplete"]) for row in rows)
    return {"total": totals, "by_source": rows, "gaps_by_source": gap_rows}


def _claim_values(entity: dict, prop: str) -> list[tuple[object, dict]]:
    output = []
    for claim in (entity.get("claims") or {}).get(prop) or []:
        value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
        if value is not None:
            output.append((value, claim))
    return output


def _entity_id(value) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return ""


def _latest_employee_count(entity: dict) -> int | None:
    choices = []
    for value, claim in _claim_values(entity, "P1128"):
        amount = value.get("amount") if isinstance(value, dict) else value
        try:
            number = int(Decimal(str(amount)))
        except (InvalidOperation, ValueError):
            continue
        dates = [str((((item.get("datavalue") or {}).get("value") or {}).get("time")) or "")
                 for item in (claim.get("qualifiers") or {}).get("P585") or []
                 if isinstance(item, dict)]
        date = max(dates) if dates else ""
        preferred = claim.get("rank") == "preferred"
        if date:
            choices.append(((date, preferred), number))
    return max(choices, key=lambda item: item[0])[1] if choices else None


def _official_domain(entity: dict) -> str:
    for value, _claim in _claim_values(entity, "P856"):
        parsed = urlparse(str(value))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host and not any(host == item or host.endswith("." + item) for item in _BLOCKED):
            return company_db.normalize_domain(host)
    return ""


def _names(entity: dict) -> set[str]:
    labels = [((entity.get("labels") or {}).get("en") or {}).get("value", "")]
    labels.extend(item.get("value", "") for item in
                  ((entity.get("aliases") or {}).get("en") or []))
    return {company_db.normalize_company_name(value) for value in labels if value}


def structured_row(record: dict, entity_id: str, entity: dict,
                   linked_labels: dict[str, str]) -> dict | None:
    record_names = {company_db.normalize_company_name(record.get(field))
                    for field in ("brand_name", "legal_name", "trade_name")
                    if record.get(field)}
    if not (_names(entity) & record_names):
        return None
    domain = _official_domain(entity)
    industries = [linked_labels.get(_entity_id(value), "")
                  for value, _ in _claim_values(entity, "P452")]
    hq = next((linked_labels.get(_entity_id(value), "")
               for value, _ in _claim_values(entity, "P159")
               if linked_labels.get(_entity_id(value))), "")
    linkedin_id = next((str(value) for value, _ in _claim_values(entity, "P4264")
                        if str(value).strip()), "")
    employees = _latest_employee_count(entity)
    employee_min = record.get("employee_count_min")
    employee_conflict = bool(
        employees is not None and employee_min is not None
        and int(employees) < int(employee_min))
    if employee_conflict:
        employees = None
    evidence = [{
        "class": "structured_corporate_source", "provider": "wikidata",
        "provider_id": entity_id, "matched_names": sorted(_names(entity) & record_names),
        "candidate_domain": domain or None, "website_property": "P856" if domain else None,
    }]
    return {
        "company_id": record["company_id"], "candidate_domain": domain or None,
        "employee_count": employees,
        "employee_count_min": employee_min,
        "employee_count_max": record.get("employee_count_max"),
        "employee_size_source": "wikidata:P1128" if employees is not None
        else record.get("employee_size_source"),
        "industry": next((value for value in industries if value), None),
        "headquarters": hq or None,
        "linkedin_url": f"https://www.linkedin.com/company/{linkedin_id}" if linkedin_id else None,
        "identity_confidence": 0.62 if domain else 0.45,
        "domain_evidence": evidence,
        "qualification_evidence": {
            "wikidata_entity": entity_id, "structured_name_match": True,
            "employee_count_conflict": employee_conflict,
        },
    }


class WikidataRateLimiter:
    """One global request cadence shared by all Wikidata workers."""
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, float(interval))
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
                now = time.monotonic()
            self.next_at = max(now, self.next_at) + self.interval


def _wikidata_json(client: httpx.Client, params: dict[str, str], *,
                   limiter: WikidataRateLimiter, retries: int = 3,
                   sleep=time.sleep) -> dict:
    """Bounded retry for network failures, 429 and provider 5xx responses."""
    last_error: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        limiter.wait()
        try:
            response = client.get(WIKIDATA_API, params=params)
            status = int(getattr(response, "status_code", 200))
            if status in _RETRYABLE_STATUS:
                retry_after = getattr(response, "headers", {}).get("Retry-After", "")
                try:
                    delay = min(5.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = min(5.0, 0.25 * (2 ** attempt))
                if attempt < max(0, int(retries)):
                    sleep(delay)
                    continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Wikidata response is not an object")
            return payload
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status = int(getattr(exc.response, "status_code", 0))
            if status not in _RETRYABLE_STATUS:
                break
            if attempt < max(0, int(retries)):
                sleep(min(5.0, 0.25 * (2 ** attempt)))
                continue
        except (httpx.TransportError, OSError, ValueError, AttributeError) as exc:
            last_error = exc
            if attempt < max(0, int(retries)):
                sleep(min(5.0, 0.25 * (2 ** attempt)))
                continue
    raise RuntimeError("Wikidata request failed after bounded retries") from last_error


def _list_wikidata_rows(*, limit: int, retry_transient: bool = False) -> list[dict]:
    if retry_transient:
        checkpoint = " AND COALESCE(m.qualification_evidence#>>'{wikidata_enrichment,status}','')='transient'"
    else:
        checkpoint = " AND NOT (m.qualification_evidence ? 'wikidata_enrichment') AND NOT (m.qualification_evidence ? 'wikidata_entity')"
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.brand_name,m.employee_count,m.employee_count_min,
            m.employee_count_max,m.employee_size_source,m.industry,m.headquarters,
            c.legal_name,c.trade_name
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.identity_status IN ('candidate','quarantined')
        """ + checkpoint + " ORDER BY m.company_id LIMIT %s",
                    (max(1, min(int(limit), 10_000)),))
        return [dict(row) for row in cur.fetchall()]


def _persist_wikidata_results(results: list[dict]) -> dict[str, int]:
    counts = {"matched": 0, "no_match": 0, "transient": 0, "updated": 0}
    if not results:
        return counts
    with company_db._cur(False) as cur:
        for result in results:
            status = str(result.get("status") or "")
            if status not in counts or status == "updated":
                continue
            counts[status] += 1
            checkpoint = json.dumps({
                "status": status, "reason": result.get("reason"),
                "retryable": status == "transient", "checked_at": datetime.now(
                    timezone.utc).isoformat(timespec="seconds"),
                "contract_version": WIKIDATA_ENRICHMENT_CONTRACT,
            })
            company_id = int(result["company_id"])
            enriched = result.get("enriched")
            if status == "matched" and isinstance(enriched, dict):
                evidence = json.dumps(enriched.get("domain_evidence") or [])
                qualification = json.dumps({
                    **dict(enriched.get("qualification_evidence") or {}),
                    "wikidata_enrichment": json.loads(checkpoint),
                })
                cur.execute("""
                  UPDATE company_employer_master SET
                    candidate_domain=COALESCE(%s,candidate_domain),
                    identity_confidence=GREATEST(identity_confidence,%s),
                    domain_evidence=COALESCE((SELECT jsonb_agg(e)
                      FROM jsonb_array_elements(domain_evidence) e
                      WHERE COALESCE(e->>'provider','')<>'wikidata'),'[]'::jsonb)
                      || %s::jsonb,
                    qualification_evidence=qualification_evidence || %s::jsonb,
                    updated_at=now()
                  WHERE company_id=%s AND in_target_population
                """, (enriched.get("candidate_domain"),
                      float(enriched.get("identity_confidence") or 0), evidence,
                      qualification, company_id))
            else:
                cur.execute("""
                  UPDATE company_employer_master SET
                    qualification_evidence=qualification_evidence ||
                      jsonb_build_object('wikidata_enrichment',%s::jsonb),updated_at=now()
                  WHERE company_id=%s AND in_target_population
                """, (checkpoint, company_id))
            counts["updated"] += cur.rowcount
    return counts


def _title_chunk(client: httpx.Client, chunk: list[dict], *,
                 limiter: WikidataRateLimiter, retries: int) -> dict:
    titles = []
    for row in chunk:
        titles.extend(str(row.get(field) or "").strip()
                      for field in ("brand_name", "legal_name") if row.get(field))
    return _wikidata_json(client, {
        "action": "wbgetentities", "sites": "enwiki",
        "titles": "|".join(dict.fromkeys(titles)),
        "props": "labels|aliases|claims", "languages": "en",
        "redirects": "yes", "format": "json", "origin": "*",
    }, limiter=limiter, retries=retries)


def enrich_structured(*, limit: int = 2000, min_interval: float = 0.25,
                      workers: int = 4, checkpoint_size: int = 200,
                      retries: int = 3, retry_transient: bool = False,
                      client: httpx.Client | None = None) -> dict:
    """Concurrent exact-label Wikidata enrichment with commit-per-batch resume."""
    worker_count = max(1, min(int(workers), 4))
    batch_size = max(1, min(int(checkpoint_size), 500))
    rows = _list_wikidata_rows(limit=limit, retry_transient=retry_transient)
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0),
                                    headers={"User-Agent": USER_AGENT})
    limiter = WikidataRateLimiter(min_interval)
    totals = {"matched": 0, "no_match": 0, "transient": 0, "updated": 0}
    batches = errors = processed = 0
    last_company_id = 0
    try:
        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start:batch_start + batch_size]
            chunks = [batch[start:start + 25] for start in range(0, len(batch), 25)]
            entity_matches: dict[int, list[tuple[str, dict]]] = defaultdict(list)
            transient_ids: set[int] = set()
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {pool.submit(_title_chunk, client, chunk, limiter=limiter,
                                       retries=retries): chunk for chunk in chunks}
                for future in as_completed(futures):
                    chunk = futures[future]
                    try:
                        entities = (future.result().get("entities") or {})
                    except Exception:
                        errors += 1
                        transient_ids.update(int(row["company_id"]) for row in chunk)
                        continue
                    for entity_id, entity in entities.items():
                        if entity_id == "-1" or entity.get("missing") is not None:
                            continue
                        entity_names = _names(entity)
                        matched_rows = [row for row in chunk if entity_names & {
                            company_db.normalize_company_name(row.get("brand_name")),
                            company_db.normalize_company_name(row.get("legal_name")),
                        }]
                        if len(matched_rows) == 1:
                            entity_matches[int(matched_rows[0]["company_id"])].append(
                                (entity_id, entity))

            unique_matches = {company_id: items[0] for company_id, items in entity_matches.items()
                              if len(items) == 1 and company_id not in transient_ids}
            linked_ids = sorted({
                _entity_id(value) for _company_id, (_qid, entity) in unique_matches.items()
                for prop in ("P452", "P159") for value, _claim in _claim_values(entity, prop)
                if _entity_id(value).startswith("Q")
            })
            linked_labels: dict[str, str] = {}
            label_failed = False
            label_chunks = [linked_ids[start:start + 50]
                            for start in range(0, len(linked_ids), 50)]
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(_wikidata_json, client, {
                    "action": "wbgetentities", "ids": "|".join(chunk),
                    "props": "labels", "languages": "en", "format": "json", "origin": "*",
                }, limiter=limiter, retries=retries) for chunk in label_chunks]
                for future in as_completed(futures):
                    try:
                        entities = future.result().get("entities") or {}
                    except Exception:
                        errors += 1
                        label_failed = True
                        continue
                    for qid, entity in entities.items():
                        label = (((entity.get("labels") or {}).get("en") or {}).get("value") or "")
                        if label:
                            linked_labels[qid] = label

            results = []
            for row in batch:
                company_id = int(row["company_id"])
                if company_id in transient_ids or (label_failed and company_id in unique_matches):
                    results.append({"company_id": company_id, "status": "transient",
                                    "reason": "wikidata_provider_error"})
                    continue
                matches = entity_matches.get(company_id) or []
                if len(matches) != 1:
                    results.append({"company_id": company_id, "status": "no_match",
                                    "reason": "ambiguous_exact_entity" if len(matches) > 1
                                    else "no_exact_entity"})
                    continue
                entity_id, entity = matches[0]
                enriched = structured_row(row, entity_id, entity, linked_labels)
                has_candidate = bool(enriched and enriched.get("candidate_domain"))
                results.append({"company_id": company_id,
                                "status": "matched" if has_candidate else "no_match",
                                "reason": None if has_candidate else (
                                    "official_website_missing" if enriched
                                    else "exact_label_recheck_failed"),
                                "enriched": enriched if has_candidate else None})
            persisted = _persist_wikidata_results(results)
            for key in totals:
                totals[key] += persisted[key]
            batches += 1
            processed += len(batch)
            last_company_id = max(int(row["company_id"]) for row in batch)
        return {"selected": len(rows), "processed": processed, **totals,
                "batches": batches, "last_company_id": last_company_id,
                "workers": worker_count, "errors": errors}
    finally:
        if owned:
            client.close()


def enrich_structured_search(*, limit: int = 2000, min_interval: float = 0.25,
                             client: httpx.Client | None = None) -> dict:
    """Resolve exact normalized employer labels through Wikidata entity search.

    This is a fallback for legal names that are not exact English Wikipedia titles.
    It only creates structured candidates; domain acceptance still requires the live
    official-site second factor.
    """
    rows = master_db.list_structured_search_candidates(limit=limit)
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0),
                                    headers={"User-Agent": USER_AGENT})
    matched: list[tuple[dict, str]] = []
    errors = 0
    try:
        for row in rows:
            query = str(row.get("brand_name") or row.get("legal_name") or "").strip()
            try:
                response = client.get(WIKIDATA_API, params={
                    "action": "wbsearchentities", "search": query, "language": "en",
                    "uselang": "en", "type": "item", "limit": 5,
                    "format": "json", "origin": "*",
                })
                response.raise_for_status()
                hits = response.json().get("search") or []
            except (httpx.HTTPError, ValueError, AttributeError):
                errors += 1
                if min_interval:
                    time.sleep(min_interval)
                continue
            record_names = {
                company_db.normalize_company_name(row.get(field))
                for field in ("brand_name", "legal_name", "trade_name") if row.get(field)
            }
            exact_ids = []
            for hit in hits:
                names = [hit.get("label", ""), ((hit.get("match") or {}).get("text", ""))]
                if any(company_db.normalize_company_name(name) in record_names for name in names):
                    qid = str(hit.get("id") or "")
                    if qid.startswith("Q") and qid not in exact_ids:
                        exact_ids.append(qid)
            if len(exact_ids) == 1:
                matched.append((row, exact_ids[0]))
            if min_interval:
                time.sleep(min_interval)

        entities: dict[str, dict] = {}
        qids = list(dict.fromkeys(qid for _row, qid in matched))
        for start in range(0, len(qids), 50):
            response = client.get(WIKIDATA_API, params={
                "action": "wbgetentities", "ids": "|".join(qids[start:start + 50]),
                "props": "labels|aliases|claims", "languages": "en",
                "format": "json", "origin": "*",
            })
            response.raise_for_status()
            entities.update(response.json().get("entities") or {})
            if min_interval:
                time.sleep(min_interval)
        linked_ids = {
            _entity_id(value) for entity in entities.values() for prop in ("P452", "P159")
            for value, _claim in _claim_values(entity, prop) if _entity_id(value).startswith("Q")
        }
        linked_labels: dict[str, str] = {}
        ordered = sorted(linked_ids)
        for start in range(0, len(ordered), 50):
            response = client.get(WIKIDATA_API, params={
                "action": "wbgetentities", "ids": "|".join(ordered[start:start + 50]),
                "props": "labels", "languages": "en", "format": "json", "origin": "*",
            })
            response.raise_for_status()
            for qid, entity in (response.json().get("entities") or {}).items():
                label = (((entity.get("labels") or {}).get("en") or {}).get("value") or "")
                if label:
                    linked_labels[qid] = label
            if min_interval:
                time.sleep(min_interval)
        enriched = [result for row, qid in matched
                    if (entity := entities.get(qid))
                    if (result := structured_row(row, qid, entity, linked_labels))]
        updated = master_db.update_structured_evidence(enriched)
        return {"selected": len(rows), "matched": len(enriched), "updated": updated,
                "errors": errors}
    finally:
        if owned:
            client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bulk = sub.add_parser("stored-bulk")
    bulk.add_argument("--batch-size", type=int, default=500)
    bulk.add_argument("--max-batches", type=int)
    bulk.add_argument("--after-company-id", type=int, default=0)
    bulk.add_argument("--retry-incomplete", action="store_true")
    wikidata = sub.add_parser("wikidata")
    wikidata.add_argument("--limit", type=int, default=10_000)
    wikidata.add_argument("--workers", type=int, default=4)
    wikidata.add_argument("--min-interval", type=float, default=0.25)
    wikidata.add_argument("--checkpoint-size", type=int, default=200)
    wikidata.add_argument("--retries", type=int, default=3)
    wikidata.add_argument("--retry-transient", action="store_true")
    sub.add_parser("stored-report")
    args = parser.parse_args(argv)
    master_db.ensure_schema()
    if args.command == "stored-bulk":
        result = enrich_stored_bulk(
            batch_size=args.batch_size, max_batches=args.max_batches,
            after_company_id=args.after_company_id,
            retry_incomplete=args.retry_incomplete)
    elif args.command == "wikidata":
        result = enrich_structured(
            limit=args.limit, workers=args.workers, min_interval=args.min_interval,
            checkpoint_size=args.checkpoint_size, retries=args.retries,
            retry_transient=args.retry_transient)
    else:
        result = stored_identity_report()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
