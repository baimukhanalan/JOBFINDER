"""Structured employer identity enrichment without accepting a domain on name alone."""
from __future__ import annotations

import argparse
import ipaddress
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
WIKIDATA_SEARCH_CONTRACT = 1


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
    for value, claim in _claim_values(entity, "P856"):
        if claim.get("rank") == "deprecated":
            continue
        parsed = urlparse(str(value))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        if ("." in host and host != "localhost"
                and re.fullmatch(r"[a-z0-9.-]+", host)
                and not any(host == item or host.endswith("." + item)
                            for item in _BLOCKED)):
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


def _list_wikidata_search_rows(*, limit: int,
                               retry_transient: bool = False) -> list[dict]:
    checkpoint = (
        " AND COALESCE(m.qualification_evidence#>>"
        "'{wikidata_search_enrichment,status}','')='transient'"
        if retry_transient else
        " AND NOT (m.qualification_evidence ? 'wikidata_search_enrichment')")
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.brand_name,m.headquarters,m.headquarters_country,
            c.legal_name,c.trade_name,c.states,c.country,c.source,c.source_external_id,
            c.metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.identity_status IN ('candidate','quarantined')
            AND NULLIF(m.candidate_domain,'') IS NULL
        """ + checkpoint + " ORDER BY m.company_id LIMIT %s",
                    (max(1, min(int(limit), 10_000)),))
        return [dict(row) for row in cur.fetchall()]


def _record_names(row: dict) -> set[str]:
    return {value for field in ("brand_name", "legal_name", "trade_name")
            if (value := company_db.normalize_company_name(row.get(field)))}


def _record_city(row: dict) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    address = metadata.get("employer_address")
    if not isinstance(address, dict):
        return ""
    return company_db.normalize_company_name(address.get("city"))


def _exact_hit_ids(row: dict, hits: list[dict]) -> list[str]:
    expected = _record_names(row)
    output = []
    for hit in hits:
        hit_names = {company_db.normalize_company_name(value) for value in (
            hit.get("label"), (hit.get("match") or {}).get("text")) if value}
        qid = str(hit.get("id") or "")
        if qid.startswith("Q") and expected & hit_names and qid not in output:
            output.append(qid)
    return output


def _qid_claims(entity: dict, prop: str) -> set[str]:
    return {_entity_id(value) for value, _claim in _claim_values(entity, prop)
            if _claim.get("rank") != "deprecated" and _entity_id(value).startswith("Q")}


def _search_identity_guard(row: dict, entity: dict,
                           location_entities: dict[str, dict]) -> tuple[bool, dict]:
    """Require exact legal/alias identity plus a structured US/location claim."""
    matched_names = sorted(_record_names(row) & _names(entity))
    exact_name = bool(matched_names)
    country_us = "Q30" in _qid_claims(entity, "P17")
    source_city = _record_city(row)
    location_ids = _qid_claims(entity, "P159")
    matched_locations = sorted(qid for qid in location_ids
                               if source_city and source_city in _names(
                                   location_entities.get(qid) or {}))
    location_match = bool(matched_locations)
    return exact_name and (country_us or location_match), {
        "exact_normalized_label_or_alias": exact_name,
        "matched_normalized_names": matched_names,
        "us_country_claim": country_us,
        "source_city": source_city or None,
        "headquarters_location_match": location_match,
        "matched_location_qids": matched_locations,
    }


def _persist_wikidata_search_results(results: list[dict]) -> dict[str, int]:
    counts = {"matched": 0, "no_match": 0, "ambiguous": 0,
              "transient": 0, "updated": 0}
    if not results:
        return counts
    with company_db._cur(False) as cur:
        for result in results:
            status = str(result.get("status") or "")
            if status not in counts or status == "updated":
                continue
            counts[status] += 1
            checkpoint_data = {
                "status": status, "reason": result.get("reason"),
                "retryable": status == "transient",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "contract_version": WIKIDATA_SEARCH_CONTRACT,
            }
            company_id = int(result["company_id"])
            enriched = result.get("enriched")
            if status == "matched" and isinstance(enriched, dict):
                evidence = json.dumps(enriched.get("domain_evidence") or [])
                qualification = json.dumps({
                    **dict(enriched.get("qualification_evidence") or {}),
                    "wikidata_search_enrichment": checkpoint_data,
                })
                cur.execute("""
                  UPDATE company_employer_master SET
                    candidate_domain=COALESCE(%s,candidate_domain),
                    identity_confidence=GREATEST(identity_confidence,%s),
                    domain_evidence=COALESCE((SELECT jsonb_agg(e)
                      FROM jsonb_array_elements(domain_evidence) e
                      WHERE COALESCE(e->>'provider','')<>'wikidata_entity_search'),
                      '[]'::jsonb) || %s::jsonb,
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
                      jsonb_build_object('wikidata_search_enrichment',%s::jsonb),
                    updated_at=now()
                  WHERE company_id=%s AND in_target_population
                """, (json.dumps(checkpoint_data), company_id))
            counts["updated"] += cur.rowcount
    return counts


def _fetch_entity_chunks(client: httpx.Client, qids: list[str], *,
                         limiter: WikidataRateLimiter, retries: int,
                         workers: int, props: str) -> tuple[dict[str, dict], set[str], int]:
    entities: dict[str, dict] = {}
    failed: set[str] = set()
    errors = 0
    chunks = [qids[start:start + 50] for start in range(0, len(qids), 50)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_wikidata_json, client, {
            "action": "wbgetentities", "ids": "|".join(chunk), "props": props,
            "languages": "en", "format": "json", "origin": "*",
        }, limiter=limiter, retries=retries): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                entities.update(future.result().get("entities") or {})
            except Exception:
                errors += 1
                failed.update(chunk)
    return entities, failed, errors


def enrich_structured_search(*, limit: int = 2000, min_interval: float = 0.25,
                             workers: int = 4, checkpoint_size: int = 100,
                             retries: int = 3, retry_transient: bool = False,
                             dry_run: bool = False,
                             client: httpx.Client | None = None) -> dict:
    """Search exact Wikidata labels/aliases and retain P856 as a candidate only."""
    worker_count = max(1, min(int(workers), 4))
    batch_size = max(1, min(int(checkpoint_size), 500))
    rows = _list_wikidata_search_rows(limit=limit,
                                      retry_transient=retry_transient)
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0),
                                    headers={"User-Agent": USER_AGENT})
    limiter = WikidataRateLimiter(min_interval)
    totals = {"matched": 0, "no_match": 0, "ambiguous": 0,
              "transient": 0, "updated": 0}
    reason_counts: dict[str, int] = defaultdict(int)
    processed = batches = errors = 0
    proposals: list[dict] = []
    try:
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            exact_ids: dict[int, list[str]] = {}
            transient_ids: set[int] = set()
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {}
                for row in batch:
                    query = str(row.get("legal_name") or row.get("brand_name") or "").strip()
                    future = pool.submit(_wikidata_json, client, {
                        "action": "wbsearchentities", "search": query, "language": "en",
                        "uselang": "en", "type": "item", "limit": "10",
                        "format": "json", "origin": "*",
                    }, limiter=limiter, retries=retries)
                    futures[future] = row
                for future in as_completed(futures):
                    row = futures[future]
                    company_id = int(row["company_id"])
                    try:
                        exact_ids[company_id] = _exact_hit_ids(
                            row, future.result().get("search") or [])
                    except Exception:
                        errors += 1
                        transient_ids.add(company_id)

            all_qids = sorted({qid for values in exact_ids.values() for qid in values})
            entities, failed_qids, fetch_errors = _fetch_entity_chunks(
                client, all_qids, limiter=limiter, retries=retries,
                workers=worker_count, props="labels|aliases|claims")
            errors += fetch_errors
            location_qids = sorted({qid for entity in entities.values()
                                    for qid in _qid_claims(entity, "P159")})
            locations, failed_locations, location_errors = _fetch_entity_chunks(
                client, location_qids, limiter=limiter, retries=retries,
                workers=worker_count, props="labels|aliases")
            errors += location_errors

            results = []
            for row in batch:
                company_id = int(row["company_id"])
                qids = exact_ids.get(company_id) or []
                if company_id in transient_ids or any(qid in failed_qids for qid in qids):
                    results.append({"company_id": company_id, "status": "transient",
                                    "reason": "wikidata_provider_error"})
                    continue
                eligible = []
                location_dependency_failed = False
                guarded_entity_seen = False
                for qid in qids:
                    entity = entities.get(qid) or {}
                    location_ids = _qid_claims(entity, "P159")
                    country_us = "Q30" in _qid_claims(entity, "P17")
                    if not country_us and location_ids & failed_locations:
                        location_dependency_failed = True
                        continue
                    guarded, guard = _search_identity_guard(row, entity, locations)
                    guarded_entity_seen = guarded_entity_seen or guarded
                    enriched = structured_row(row, qid, entity, {}) if guarded else None
                    if not enriched or not enriched.get("candidate_domain"):
                        continue
                    enriched["identity_confidence"] = min(
                        float(enriched.get("identity_confidence") or 0), 0.68)
                    for item in enriched.get("domain_evidence") or []:
                        item.update({
                            "provider": "wikidata_entity_search",
                            "method": "exact_normalized_label_or_alias_with_us_guard",
                            "identity_guards": guard,
                            "source_record": {
                                "source": row.get("source"),
                                "source_external_id": row.get("source_external_id"),
                                "searched_legal_name": row.get("legal_name"),
                                "states": row.get("states") or [],
                            },
                        })
                    enriched["qualification_evidence"] = {
                        "wikidata_entity": qid,
                        "wikidata_search_identity": guard,
                        "structured_name_match": True,
                    }
                    eligible.append(enriched)
                if location_dependency_failed:
                    results.append({"company_id": company_id, "status": "transient",
                                    "reason": "wikidata_location_provider_error"})
                elif len(eligible) == 1:
                    results.append({"company_id": company_id, "status": "matched",
                                    "reason": None, "enriched": eligible[0]})
                    if len(proposals) < 25:
                        proposals.append({"company_id": company_id,
                                          "candidate_domain": eligible[0]["candidate_domain"],
                                          "wikidata_entity": eligible[0][
                                              "qualification_evidence"]["wikidata_entity"]})
                elif len(eligible) > 1:
                    results.append({"company_id": company_id, "status": "ambiguous",
                                    "reason": "multiple_guarded_exact_entities"})
                else:
                    reason = ("no_exact_search_hit" if not qids else
                              "official_website_missing_or_unsafe" if guarded_entity_seen else
                              "identity_guard_failed")
                    results.append({"company_id": company_id, "status": "no_match",
                                    "reason": reason})

            if dry_run:
                persisted = {key: sum(1 for item in results if item["status"] == key)
                             for key in ("matched", "no_match", "ambiguous", "transient")}
                persisted["updated"] = 0
            else:
                persisted = _persist_wikidata_search_results(results)
            for key in totals:
                totals[key] += persisted[key]
            for item in results:
                reason_counts[str(item.get("reason") or "matched")] += 1
            processed += len(batch)
            batches += 1
        return {"selected": len(rows), "processed": processed, **totals,
                "batches": batches, "workers": worker_count, "errors": errors,
                "dry_run": bool(dry_run), "reasons": dict(sorted(reason_counts.items())),
                "proposals": proposals}
    finally:
        if owned:
            client.close()


def enrich_wikidata_search(*, limit: int = 2000, min_interval: float = 0.25,
                           workers: int = 4, checkpoint_size: int = 100,
                           retries: int = 3, retry_transient: bool = False,
                           dry_run: bool = False,
                           client: httpx.Client | None = None) -> dict:
    """Stable updater-facing name for exact label/alias Wikidata discovery."""
    return enrich_structured_search(
        limit=limit, min_interval=min_interval, workers=workers,
        checkpoint_size=checkpoint_size, retries=retries,
        retry_transient=retry_transient, dry_run=dry_run, client=client)


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
    search = sub.add_parser("wikidata-search")
    search.add_argument("--limit", type=int, default=10_000)
    search.add_argument("--workers", type=int, default=4)
    search.add_argument("--min-interval", type=float, default=0.25)
    search.add_argument("--checkpoint-size", type=int, default=100)
    search.add_argument("--retries", type=int, default=3)
    search.add_argument("--retry-transient", action="store_true")
    search.add_argument("--dry-run", action="store_true")
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
    elif args.command == "wikidata-search":
        result = enrich_wikidata_search(
            limit=args.limit, workers=args.workers, min_interval=args.min_interval,
            checkpoint_size=args.checkpoint_size, retries=args.retries,
            retry_transient=args.retry_transient, dry_run=args.dry_run)
    else:
        result = stored_identity_report()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
