"""Resolve official company domains without consulting vacancy-derived data.

Candidates come from Wikidata's structured ``official website`` claim (P856),
with a conservative DuckDuckGo HTML fallback.  A search result is never trusted
on rank alone: its public homepage must independently match the company name.
"""
from __future__ import annotations

import html as html_lib
import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from backend.tools.company_discovery_db import normalize_company_name, normalize_domain
from backend.tools.company_enrichment import _get, enrich_company, public_http_url


WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
DDG_HTML = "https://html.duckduckgo.com/html/"
BING_HTML = "https://www.bing.com/search"
USER_AGENT = "JobFinder-company-discovery/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
_BLOCKED_HOSTS = {
    "bloomberg.com", "crunchbase.com", "facebook.com", "instagram.com",
    "linkedin.com", "mapquest.com", "opencorporates.com", "wikipedia.org",
    "x.com", "yelp.com", "youtube.com",
}
_BLOCKED_SUFFIXES = (
    ".ashbyhq.com", ".greenhouse.io", ".indeed.com", ".lever.co",
    ".myworkdayjobs.com", ".smartrecruiters.com", ".workable.com",
)


class RateLimiter:
    """Thread-safe global request-start limiter."""

    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            if delay:
                time.sleep(delay)
            self._next = max(now, self._next) + self.min_interval


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.headings: list[str] = []
        self.text: list[str] = []
        self._in_title = False
        self._heading = 0

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        self._in_title = tag == "title" or self._in_title
        if tag in ("h1", "h2"):
            self._heading += 1
        if tag == "meta":
            values = {str(k).lower(): str(v or "") for k, v in attrs}
            if values.get("property") in ("og:site_name", "og:title"):
                self.headings.append(values.get("content", ""))

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in ("h1", "h2") and self._heading:
            self._heading -= 1

    def handle_data(self, data) -> None:
        value = " ".join(str(data).split())
        if not value:
            return
        if self._in_title:
            self.title.append(value)
        if self._heading:
            self.headings.append(value)
        if sum(map(len, self.text)) < 100_000:
            self.text.append(value)


class _DdgResults(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str]] = []
        self._href = ""
        self._title: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() != "a":
            return
        values = {str(k): str(v or "") for k, v in attrs}
        if "result__a" in values.get("class", ""):
            self._href = values.get("href", "")
            self._title = []

    def handle_data(self, data) -> None:
        if self._href:
            self._title.append(str(data))

    def handle_endtag(self, tag) -> None:
        if tag.lower() == "a" and self._href:
            self.results.append((self._href, html_lib.unescape(" ".join(self._title))))
            self._href = ""
            self._title = []


@dataclass(frozen=True)
class Candidate:
    url: str
    provider: str
    provider_id: str = ""
    provider_name: str = ""
    search_rank: int = 0


def _record_names(record: dict) -> set[str]:
    names = {
        str(record.get(field) or "").strip()
        for field in ("legal_name", "trade_name", "canonical_name")
    }
    canonical = normalize_company_name(_company_name(record))
    if canonical:
        names.add(canonical)
    return {name for name in names if name}


def _company_name(record: dict) -> str:
    return str(record.get("trade_name") or record.get("legal_name")
               or record.get("canonical_name") or "").strip()


def _name_similarity(company: str, evidence: str) -> float:
    left = normalize_company_name(company)
    right = normalize_company_name(evidence)
    if not left or not right:
        return 0.0
    if left == right or f" {left} " in f" {right} ":
        return 1.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    coverage = len(left_tokens & right_tokens) / max(1, len(left_tokens))
    return max(SequenceMatcher(None, left, right[:max(len(left) * 3, 80)]).ratio(), coverage)


def _allowed_candidate(url: str) -> bool:
    if not public_http_url(url):
        return False
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if any(host == blocked or host.endswith("." + blocked) for blocked in _BLOCKED_HOSTS):
        return False
    return not any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES)


def _json_request(client, limiter: RateLimiter, params: dict) -> dict:
    limiter.wait()
    try:
        response = client.get(WIKIDATA_API, params=params, follow_redirects=False)
    except (httpx.HTTPError, ValueError):
        return {}
    if response.status_code != 200:
        return {}
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return {}


def wikidata_candidates(record: dict, client, limiter: RateLimiter,
                        max_entities: int = 3) -> list[Candidate]:
    """Return P856 candidates only for exact normalized labels/aliases."""
    company = _company_name(record)
    data = _json_request(client, limiter, {
        "action": "wbsearchentities", "search": company, "language": "en",
        "uselang": "en", "type": "item", "limit": max_entities, "format": "json",
        "origin": "*",
    })
    hits = data.get("search") or []
    matched = [hit for hit in hits if _name_similarity(company, hit.get("label", "")) == 1.0]
    if not matched:
        return []
    ids = [str(hit.get("id")) for hit in matched if hit.get("id")]
    entities = _json_request(client, limiter, {
        "action": "wbgetentities", "ids": "|".join(ids), "props": "labels|aliases|claims",
        "languages": "en", "format": "json", "origin": "*",
    }).get("entities") or {}
    out: list[Candidate] = []
    for entity_id in ids:
        entity = entities.get(entity_id) or {}
        labels = [((entity.get("labels") or {}).get("en") or {}).get("value", "")]
        labels.extend(alias.get("value", "") for alias in
                      ((entity.get("aliases") or {}).get("en") or []))
        exact = next((label for label in labels if _name_similarity(company, label) == 1.0), "")
        if not exact:
            continue
        for claim in (entity.get("claims") or {}).get("P856") or []:
            url = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
            if isinstance(url, str) and _allowed_candidate(url):
                out.append(Candidate(url, "wikidata_p856", entity_id, exact))
    return out


def bulk_wikidata_candidates(records: list[dict], *, limiter: RateLimiter,
                             batch_size: int = 75, client=None,
                             retries: int = 1
                             ) -> tuple[dict[int, list[Candidate]], set[int], set[int]]:
    """Resolve exact English labels to P856 in bounded SPARQL VALUES batches.

    The returned boolean is false only when a batch remains unavailable after
    retries; callers can then fall back to the per-company MediaWiki resolver.
    """
    output: dict[int, list[Candidate]] = {int(row["id"]): [] for row in records}
    by_name: dict[str, list[dict]] = {}
    for row in records:
        for name in _record_names(row):
            matches = by_name.setdefault(normalize_company_name(name), [])
            if not any(int(item["id"]) == int(row["id"]) for item in matches):
                matches.append(row)
    record_variants = {
        int(row["id"]): {variant for name in _record_names(row)
                         for variant in (name, name.title()) if variant}
        for row in records
    }
    query_names = sorted({variant for variants in record_variants.values()
                          for variant in variants})
    failed_variants: set[str] = set()
    ambiguous_ids = {
        int(row["id"])
        for matches in by_name.values() if len(matches) > 1
        for row in matches
    }
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(12.0), headers={"User-Agent": USER_AGENT})
    try:
        for start in range(0, len(query_names), max(1, int(batch_size))):
            names = query_names[start:start + max(1, int(batch_size))]
            values = " ".join(json.dumps(name, ensure_ascii=False) + "@en" for name in names)
            query = f"""
              SELECT DISTINCT ?item ?label ?website WHERE {{
                VALUES ?label {{ {values} }}
                ?item <http://www.wikidata.org/prop/direct/P856> ?website ;
                      <http://www.w3.org/2000/01/rdf-schema#label> ?label .
              }}
            """
            response = None
            for attempt in range(max(0, int(retries)) + 1):
                limiter.wait()
                try:
                    response = client.post(
                        WIKIDATA_SPARQL, data={"query": query, "format": "json"},
                        headers={"Accept": "application/sparql-results+json"},
                        follow_redirects=False,
                    )
                except (httpx.HTTPError, ValueError):
                    response = None
                if response is not None and response.status_code == 200:
                    break
                if attempt < retries:
                    time.sleep(0.5 * (2 ** attempt))
            if response is None or response.status_code != 200:
                failed_variants.update(names)
                continue
            try:
                bindings = response.json().get("results", {}).get("bindings", [])
            except (ValueError, AttributeError):
                failed_variants.update(names)
                continue
            for binding in bindings:
                label = ((binding.get("label") or {}).get("value") or "")
                website = ((binding.get("website") or {}).get("value") or "")
                item_url = ((binding.get("item") or {}).get("value") or "")
                item_id = item_url.rstrip("/").rsplit("/", 1)[-1]
                if not website or not _allowed_candidate(website):
                    continue
                for row in by_name.get(normalize_company_name(label), []):
                    if int(row["id"]) in ambiguous_ids:
                        continue
                    if _name_similarity(_company_name(row), label) != 1.0:
                        continue
                    candidate = Candidate(website, "wikidata_sparql_p856", item_id, label)
                    if candidate not in output[int(row["id"])]:
                        output[int(row["id"])].append(candidate)
        failed_ids = ({company_id for company_id, variants in record_variants.items()
                       if variants & failed_variants} | ambiguous_ids)
        completed_ids = set(output) - failed_ids
        return output, completed_ids, failed_ids
    finally:
        if owns_client:
            client.close()


def bulk_mediawiki_candidates(records: list[dict], *, limiter: RateLimiter,
                              batch_size: int = 25, client=None,
                              retries: int = 1
                              ) -> tuple[dict[int, list[Candidate]], set[int], set[int]]:
    """Resolve enwiki titles to entities/P856 through the stable MediaWiki API."""
    output: dict[int, list[Candidate]] = {int(row["id"]): [] for row in records}
    record_variants = {
        int(row["id"]): {variant for name in _record_names(row)
                         for variant in (name, name.title()) if variant}
        for row in records
    }
    by_name: dict[str, list[dict]] = {}
    for row in records:
        for name in _record_names(row):
            matches = by_name.setdefault(normalize_company_name(name), [])
            if not any(int(item["id"]) == int(row["id"]) for item in matches):
                matches.append(row)
    # Exact labels alone cannot distinguish separate legal entities with the same
    # normalized name. Keep those rows retryable instead of copying one P856 claim
    # to every namesake.
    ambiguous_ids = {
        int(row["id"])
        for matches in by_name.values() if len(matches) > 1
        for row in matches
    }
    failed_ids: set[int] = set(ambiguous_ids)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(12.0), headers={"User-Agent": USER_AGENT})
    try:
        # Keep every record's variants in the same request so a successful response
        # is sufficient evidence for a final no-exact-title result.
        records_per_request = max(1, min(int(batch_size), 10))
        for start in range(0, len(records), records_per_request):
            chunk = records[start:start + records_per_request]
            titles = sorted({title for row in chunk for title in record_variants[int(row["id"])]})
            response = None
            for attempt in range(max(0, int(retries)) + 1):
                limiter.wait()
                try:
                    response = client.get(WIKIDATA_API, params={
                        "action": "wbgetentities", "sites": "enwiki",
                        "titles": "|".join(titles), "props": "labels|aliases|claims",
                        "languages": "en", "redirects": "yes", "format": "json",
                        "origin": "*",
                    }, follow_redirects=False)
                except (httpx.HTTPError, ValueError):
                    response = None
                if response is not None and response.status_code == 200:
                    break
                if attempt < retries:
                    time.sleep(0.5 * (2 ** attempt))
            if response is None or response.status_code != 200:
                failed_ids.update(int(row["id"]) for row in chunk)
                continue
            try:
                payload = response.json()
                if payload.get("error"):
                    failed_ids.update(int(row["id"]) for row in chunk)
                    continue
                entities = payload.get("entities", {})
            except (ValueError, AttributeError):
                failed_ids.update(int(row["id"]) for row in chunk)
                continue
            for entity_id, entity in entities.items():
                if entity_id == "-1" or entity.get("missing") is not None:
                    continue
                labels = [((entity.get("labels") or {}).get("en") or {}).get("value", "")]
                labels.extend(alias.get("value", "") for alias in
                              ((entity.get("aliases") or {}).get("en") or []))
                claims = (entity.get("claims") or {}).get("P856") or []
                for label in labels:
                    for row in by_name.get(normalize_company_name(label), []):
                        if int(row["id"]) in ambiguous_ids:
                            continue
                        if int(row["id"]) not in {int(item["id"]) for item in chunk}:
                            continue
                        if _name_similarity(_company_name(row), label) != 1.0:
                            continue
                        for claim in claims:
                            url = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
                            if isinstance(url, str) and _allowed_candidate(url):
                                candidate = Candidate(
                                    url, "wikidata_api_p856", entity_id, label)
                                if candidate not in output[int(row["id"])]:
                                    output[int(row["id"])].append(candidate)
        return output, set(output) - failed_ids, failed_ids
    finally:
        if owns_client:
            client.close()


def resolve_from_candidates(record: dict, candidates: list[Candidate], *,
                            limiter: RateLimiter, threshold: float = 0.88,
                            attempt_out: dict | None = None,
                            resolver_name: str = "wikidata_sparql_p856") -> dict | None:
    """Verify pre-resolved structured candidates and enrich an accepted domain."""
    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    attempt = {"attempted_at": attempted_at, "resolver": resolver_name,
               "result": "verification_failed" if candidates else "no_exact_match"}
    if attempt_out is not None:
        attempt_out.update(attempt)
    if not candidates:
        return None
    client = httpx.Client(timeout=httpx.Timeout(12.0), headers={"User-Agent": USER_AGENT})
    try:
        accepted = None
        for candidate in candidates[:3]:
            result = verify_candidate(record, candidate, client, limiter)
            if result and result["domain_confidence"] >= threshold:
                accepted = result
                break
        if not accepted:
            return None
        enriched = enrich_company({**record, "domain": accepted["domain"]}, client,
                                  before_request=limiter.wait)
        enriched["domain_confidence"] = accepted["domain_confidence"]
        enriched["domain_resolution"] = {
            **attempt, "resolver": accepted["provider"], "result": "resolved",
            "provider_id": accepted["provider_id"],
            "provider_name": accepted["provider_name"],
            "candidate_url": accepted["candidate_url"],
            "confidence": accepted["domain_confidence"], "evidence": accepted["evidence"],
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if attempt_out is not None:
            attempt_out.update(enriched["domain_resolution"])
        return enriched
    finally:
        client.close()


def _decode_ddg_url(value: str) -> str:
    value = html_lib.unescape(value or "")
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return value


def _decode_bing_url(value: str) -> str:
    value = html_lib.unescape(value or "")
    parsed = urlparse(value)
    if not (parsed.hostname or "").lower().endswith("bing.com"):
        return value
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return ""
    try:
        payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
        return base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def search_candidates(record: dict, client, limiter: RateLimiter,
                      max_results: int = 3) -> list[Candidate]:
    company = _company_name(record)
    limiter.wait()
    try:
        response = client.get(
            f"{DDG_HTML}?q={quote_plus(chr(34) + company + chr(34) + ' official website')}",
            follow_redirects=False,
        )
    except (httpx.HTTPError, ValueError):
        return []
    ddg_ok = (response.status_code == 200
              and "html" in response.headers.get("content-type", "")
              and len((response.text or "").encode("utf-8")) <= 2 * 1024 * 1024)
    parser = _DdgResults()
    if ddg_ok:
        parser.feed(response.text)
    raw_results = [(href, title, "duckduckgo_html") for href, title in parser.results]
    if not raw_results:
        limiter.wait()
        try:
            bing = client.get(BING_HTML, params={
                "q": f'"{company}" official website', "count": max(5, max_results),
            }, headers={"User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0)"},
                            follow_redirects=False)
        except (httpx.HTTPError, ValueError):
            return []
        if (bing.status_code != 200 or "html" not in bing.headers.get("content-type", "")
                or len((bing.text or "").encode("utf-8")) > 2 * 1024 * 1024):
            return []
        matches = re.findall(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                             bing.text or "", re.I | re.S)
        raw_results = [(href, html_lib.unescape(re.sub(r"<[^>]+>", " ", title)),
                        "bing_html") for href, title in matches]
    out: list[Candidate] = []
    seen: set[str] = set()
    for href, _title, provider in raw_results:
        url = _decode_ddg_url(href) if provider == "duckduckgo_html" else _decode_bing_url(href)
        domain = normalize_domain(url)
        if domain and domain not in seen and _allowed_candidate(url):
            seen.add(domain)
            out.append(Candidate(url, provider, search_rank=len(out) + 1))
        if len(out) >= max_results:
            break
    return out


def verify_candidate(record: dict, candidate: Candidate, client,
                     limiter: RateLimiter) -> dict | None:
    """Require strong entity/homepage evidence before returning a domain."""
    response = _get(client, candidate.url, before_request=limiter.wait)
    page_score = 0.0
    final_url = candidate.url
    evidence: dict[str, object] = {}
    if response is not None:
        final_url = str(response.url)
        parser = _PageText()
        try:
            parser.feed(response.text or "")
        except Exception:
            return None
        primary = " ".join(parser.title + parser.headings)
        page_score = max(_name_similarity(_company_name(record), primary),
                         _name_similarity(_company_name(record), " ".join(parser.text)))
        evidence["homepage_name_similarity"] = round(page_score, 4)
    if not _allowed_candidate(final_url):
        return None
    # Production candidates must resolve exclusively to public addresses even if
    # a structured provider points at a temporarily unavailable homepage.
    if isinstance(client, httpx.Client) and not public_http_url(final_url, resolve_dns=True):
        return None

    provider_exact = candidate.provider in (
        "wikidata_p856", "wikidata_sparql_p856", "wikidata_api_p856")
    if provider_exact:
        # Exact Wikidata entity label + P856 is structured official evidence.
        confidence = 0.91 + min(0.06, page_score * 0.06)
    else:
        # Search rank is discovery only; homepage identity is mandatory.
        if page_score < 0.86:
            return None
        host_tokens = set(re.findall(r"[a-z0-9]+", normalize_domain(final_url)))
        name_tokens = set(normalize_company_name(_company_name(record)).split())
        host_overlap = len(host_tokens & name_tokens) / max(1, len(name_tokens))
        confidence = min(0.94, 0.76 + page_score * 0.14 + host_overlap * 0.04)
        evidence["domain_token_overlap"] = round(host_overlap, 4)
    return {
        "domain": normalize_domain(final_url),
        "domain_confidence": round(confidence, 4),
        "candidate_url": final_url,
        "provider": candidate.provider,
        "provider_id": candidate.provider_id,
        "provider_name": candidate.provider_name,
        "evidence": evidence,
    }


def resolve_company(record: dict, *, client=None, limiter: RateLimiter | None = None,
                    search_fallback: bool = True, threshold: float = 0.88,
                    attempt_out: dict | None = None) -> dict | None:
    """Resolve and web-enrich one company, returning only accepted evidence."""
    limiter = limiter or RateLimiter()
    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    attempt = {
        "attempted_at": attempted_at,
        "resolver": "wikidata_p856+duckduckgo_html" if search_fallback else "wikidata_p856",
        "result": "unresolved",
    }
    if attempt_out is not None:
        attempt_out.update(attempt)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(12.0), headers={"User-Agent": USER_AGENT})
    try:
        candidates = wikidata_candidates(record, client, limiter)
        if search_fallback and not candidates:
            candidates = search_candidates(record, client, limiter)
        accepted = None
        for candidate in candidates[:3]:
            result = verify_candidate(record, candidate, client, limiter)
            if result and result["domain_confidence"] >= threshold:
                accepted = result
                break
        if not accepted:
            return None
        enriched = enrich_company({**record, "domain": accepted["domain"]}, client,
                                  before_request=limiter.wait)
        enriched["domain_confidence"] = accepted["domain_confidence"]
        enriched["domain_resolution"] = {
            "attempted_at": attempted_at,
            "resolver": accepted["provider"],
            "result": "resolved",
            "provider_id": accepted["provider_id"],
            "provider_name": accepted["provider_name"],
            "candidate_url": accepted["candidate_url"],
            "confidence": accepted["domain_confidence"],
            "evidence": accepted["evidence"],
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if attempt_out is not None:
            attempt_out.update(enriched["domain_resolution"])
        return enriched
    finally:
        if owns_client:
            client.close()
