"""Career-page and ATS enrichment isolated to verified employer-master rows."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_master_db as master_db
from backend.tools.company_enrichment import enrich_company
from backend.tools.employer_domain_verifier import RequestLimiter


def enrich_verified_careers(*, limit: int = 2000, workers: int = 4,
                            min_interval: float = 0.25) -> dict:
    rows = master_db.list_verified_employers(limit=limit)
    limiter = RequestLimiter(min_interval)
    completed: list[dict] = []
    errors = 0

    def work(row: dict) -> dict:
        result = enrich_company(row, before_request=limiter.wait)
        if result.get("ats") == "successfactors" and result.get("careers_url"):
            # RMK custom career domains are the public canonical board.  The
            # shared careerN.successfactors host alone is not tenant identity.
            result["ats_url"] = result["careers_url"]
        elif result.get("ats") == "eightfold" and result.get("ats_url"):
            parsed = urlparse(result["ats_url"])
            query = parse_qs(parsed.query)
            if not query.get("domain") and row.get("domain"):
                query["domain"] = [company_db.normalize_domain(row["domain"])]
                result["ats_url"] = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
                result["ats_slug"] = (
                    f"{result.get('ats_slug') or parsed.hostname}:{query['domain'][0]}"
                )
        if result.get("careers_url") and not result.get("ats"):
            result["ats"] = "custom"
            result["ats_slug"] = company_db.normalize_domain(result["careers_url"])
            result["ats_url"] = result["careers_url"]
        result["id"] = row["id"]
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 4))) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for future in as_completed(futures):
            try:
                completed.append(future.result())
            except Exception:
                errors += 1
    updated = company_db.update_enrichment_results(completed)
    return {"selected": len(rows), "updated": updated, "errors": errors,
            **master_db.verified_career_counts()}
