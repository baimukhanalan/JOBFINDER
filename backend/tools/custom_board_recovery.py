"""Fail-closed recovery of incomplete proprietary careers boards.

Only static, robots-permitted resources are read: the verified careers page,
same-origin sitemap documents/pages, JSON-LD, and external ATS links already
present in those documents.  The module never renders JavaScript, bypasses a
challenge, opens an application form, or submits anything.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools.company_enrichment import detect_ats, extract_links, public_http_url


MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "JobFinder-custom-board-recovery/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
MAX_REDIRECTS = 4
MAX_SITEMAPS = 4
MAX_SITEMAP_URLS = 100
MAX_PAGES = 12
BOARD_TIMEOUT_SECONDS = 30.0
CHALLENGE_RE = re.compile(
    r"\b(?:captcha|verify you are human|security challenge|access denied|"
    r"attention required|cloudflare ray id)\b", re.I)
JOB_PATH_RE = re.compile(r"(?:career|jobs?|opening|position|vacanc|requisition)", re.I)
SUPPORTED_RECOVERY_ATS = {
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday",
    "icims", "oracle", "successfactors", "eightfold",
}


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).lower(): str(value or "").lower() for key, value in attrs}
        if tag.lower() == "script" and "ld+json" in values.get("type", ""):
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(str(data))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            self.blocks.append("".join(self._parts))
            self._capture = False


def _json_urls(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _json_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_urls(item)
    elif isinstance(value, str) and value.startswith(("https://", "http://")):
        yield value


def jsonld_urls(page_html: str) -> list[str]:
    parser = _JsonLdParser()
    try:
        parser.feed(page_html or "")
    except Exception:
        return []
    urls: list[str] = []
    for block in parser.blocks:
        try:
            payload = json.loads(block)
        except (TypeError, ValueError):
            continue
        urls.extend(_json_urls(payload))
    return list(dict.fromkeys(urls))


def _origin(url: str) -> str:
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{port}"


def _raw_get(client: httpx.Client, url: str, *, resolve_dns: bool) -> httpx.Response | None:
    if not public_http_url(url, resolve_dns=resolve_dns):
        return None
    try:
        with client.stream("GET", url, follow_redirects=False) as streamed:
            chunks: list[bytes] = []
            size = 0
            for chunk in streamed.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    return None
                chunks.append(chunk)
            headers = httpx.Headers(streamed.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(streamed.status_code, headers=headers,
                                  content=b"".join(chunks), request=streamed.request)
    except (httpx.HTTPError, ValueError):
        return None


class RobotsGuard:
    def __init__(self, client: httpx.Client, *, resolve_dns: bool = True) -> None:
        self.client = client
        self.resolve_dns = resolve_dns
        self.cache: dict[str, RobotFileParser | None] = {}
        self.sitemaps: dict[str, list[str]] = {}

    def _load(self, url: str) -> RobotFileParser | None:
        origin = _origin(url)
        if origin in self.cache:
            return self.cache[origin]
        robots_url = origin + "/robots.txt"
        response = _raw_get(self.client, robots_url, resolve_dns=self.resolve_dns)
        if response is None:
            parser = None
        elif response.status_code == 404:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse([])
            self.sitemaps[origin] = []
        elif response.status_code == 200:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            self.sitemaps[origin] = list(parser.site_maps() or [])
        else:
            parser = None
        self.cache[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._load(url)
        return bool(parser and parser.can_fetch(USER_AGENT, url))

    def declared_sitemaps(self, url: str) -> list[str]:
        self._load(url)
        origin = _origin(url)
        return [value for value in self.sitemaps.get(origin, [])
                if _origin(value) == origin]


def _guarded_get(client: httpx.Client, guard: RobotsGuard, url: str,
                 *, deadline: float) -> tuple[httpx.Response | None, str | None]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() >= deadline:
            return None, "board time budget exceeded"
        if not guard.allowed(current):
            return None, "robots unavailable or disallowed"
        response = _raw_get(client, current, resolve_dns=guard.resolve_dns)
        if response is None:
            return None, "response unavailable or exceeded byte limit"
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                return None, "redirect without location"
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        if CHALLENGE_RE.search(response.text[:200_000]):
            return None, "challenge page detected"
        return response, None
    return None, "redirect limit exceeded"


def _sitemap_locations(xml_text: str) -> tuple[list[str], bool]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], False
    values = [str(node.text or "").strip() for node in root.iter()
              if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text]
    return list(dict.fromkeys(values)), root.tag.rsplit("}", 1)[-1].lower() == "sitemapindex"


def _detect_from_page(page_url: str, page_html: str) -> tuple[dict[str, str], dict[str, Any]]:
    links = [url for url, _text in extract_links(page_html, page_url)]
    structured = jsonld_urls(page_html)
    # Prefer explicit links/JSON-LD over an incidental raw script fragment.
    for source, values in (("external_link", links), ("json_ld", structured),
                           ("html_marker", [page_html])):
        detected = detect_ats(values)
        ats = str(detected.get("ats") or "").casefold()
        ats_url = str(detected.get("ats_url") or "")
        slug = company_db.normalize_ats_slug(ats, detected.get("ats_slug"))
        if (ats in SUPPORTED_RECOVERY_ATS and urlparse(ats_url).scheme == "https"
                and public_http_url(ats_url)
                and not (ats == "icims" and re.match(r"events?(?:-|$)", slug, re.I))):
            detected = {"ats": ats, "ats_slug": slug, "ats_url": ats_url}
            return detected, {"evidence_type": source, "page_url": page_url,
                              "ats_url": ats_url}
    return {"ats": "", "ats_slug": "", "ats_url": ""}, {}


def recover_record(record: Mapping[str, Any], *, client: httpx.Client,
                   resolve_dns: bool = True,
                   timeout_seconds: float = BOARD_TIMEOUT_SECONDS) -> dict[str, Any]:
    board_url = str(record.get("revalidation_url") or record.get("ats_url")
                    or record.get("careers_url") or "").strip()
    result = {"company_id": int(record["id"]), "status": "incomplete",
              "ats": "", "ats_slug": "", "ats_url": "", "evidence": {},
              "pages_fetched": 0, "sitemaps_fetched": 0, "errors": []}
    if not board_url:
        result["errors"].append("missing verified careers URL")
        return result
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    guard = RobotsGuard(client, resolve_dns=resolve_dns)
    board, error = _guarded_get(client, guard, board_url, deadline=deadline)
    if board is None:
        result["errors"].append(error or "careers page unavailable")
        return result
    result["pages_fetched"] += 1
    base_url = str(board.url)
    base_origin = _origin(base_url)
    detected, evidence = _detect_from_page(base_url, board.text)
    if detected.get("ats"):
        result.update(detected, evidence=evidence, status="recovered")
        return result

    sitemap_candidates = guard.declared_sitemaps(base_url) + [
        base_origin + "/sitemap.xml", base_origin + "/sitemap_index.xml",
        base_origin + "/jobs-sitemap.xml",
    ]
    page_urls: list[str] = []
    seen_sitemaps: set[str] = set()
    pending = list(dict.fromkeys(url for url in sitemap_candidates if _origin(url) == base_origin))
    while pending and len(seen_sitemaps) < MAX_SITEMAPS and time.monotonic() < deadline:
        sitemap_url = pending.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        response, _error = _guarded_get(client, guard, sitemap_url, deadline=deadline)
        if response is None:
            continue
        result["sitemaps_fetched"] += 1
        locations, is_index = _sitemap_locations(response.text)
        locations = [url for url in locations[:MAX_SITEMAP_URLS]
                     if _origin(url) == base_origin]
        if is_index:
            pending.extend(locations)
        else:
            page_urls.extend(url for url in locations if JOB_PATH_RE.search(url))

    for page_url in list(dict.fromkeys(page_urls))[:MAX_PAGES]:
        response, _error = _guarded_get(client, guard, page_url, deadline=deadline)
        if response is None:
            continue
        result["pages_fetched"] += 1
        detected, evidence = _detect_from_page(str(response.url), response.text)
        if detected.get("ats"):
            result.update(detected, evidence=evidence, status="recovered")
            return result
    result["errors"].append("no supported ATS evidence in permitted static resources")
    return result


def list_candidates(*, limit: int, revalidate: bool = False) -> list[dict[str, Any]]:
    with company_db._cur() as cur:
        if revalidate:
            cur.execute("""
              SELECT c.id,c.canonical_name,c.careers_url,c.ats,c.ats_slug,c.ats_url,
                c.provenance->'custom_board_recovery' AS recovery_evidence
              FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
              WHERE m.in_target_population AND m.domain_verified
                AND c.provenance ? 'custom_board_recovery'
              ORDER BY c.id LIMIT %s
            """, (int(limit),))
        else:
            cur.execute("""
              WITH latest AS (
                SELECT DISTINCT ON (company_id,source,source_board_id)
                  company_id,source,source_board_id,scan_complete,started_at
                FROM company_remote_job_scans
                ORDER BY company_id,source,source_board_id,started_at DESC,id DESC)
              SELECT c.id,c.canonical_name,c.careers_url,c.ats,c.ats_slug,c.ats_url,
                NULL::jsonb AS recovery_evidence
              FROM latest s JOIN company_discovery c ON c.id=s.company_id
              JOIN company_employer_master m ON m.company_id=c.id
              WHERE m.in_target_population AND m.domain_verified
                AND s.source='custom' AND s.scan_complete=FALSE
                AND lower(c.ats)='custom' AND c.ats_slug=s.source_board_id
              ORDER BY s.started_at,c.id LIMIT %s
            """, (int(limit),))
        rows = [dict(row) for row in cur.fetchall()]
    if revalidate:
        for row in rows:
            evidence = row.get("recovery_evidence")
            if isinstance(evidence, Mapping) and evidence.get("page_url"):
                row["revalidation_url"] = evidence["page_url"]
    return rows


def save_recovered(results: list[dict[str, Any]]) -> int:
    rows = [row for row in results if row.get("status") == "recovered"]
    if not rows:
        return 0
    values = []
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        ats = str(row["ats"]).casefold()
        slug = company_db.normalize_ats_slug(ats, row["ats_slug"])
        evidence = {**dict(row.get("evidence") or {}), "provider": "custom_board_recovery",
                    "observed_at": observed_at, "robots_respected": True,
                    "static_only": True, "ats": ats,
                    "ats_slug": slug, "ats_url": row["ats_url"]}
        values.append((ats, slug, row["ats_url"],
                       json.dumps(evidence), int(row["company_id"])))
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_discovery c SET ats=%s,ats_slug=%s,ats_url=%s,
            careers_confidence=GREATEST(COALESCE(careers_confidence,0),0.9),
            provenance=COALESCE(provenance,'{}'::jsonb)
              || jsonb_build_object('custom_board_recovery',%s::jsonb),updated_at=now()
          FROM company_employer_master m
          WHERE c.id=%s AND m.company_id=c.id AND m.in_target_population
            AND m.domain_verified AND lower(c.ats)='custom'
        """, values)
        return cur.rowcount


def save_revalidated(results: list[dict[str, Any]]) -> int:
    rows = [row for row in results if row.get("status") == "recovered"]
    if not rows:
        return 0
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = []
    for row in rows:
        evidence = {"revalidated_at": observed_at, "revalidation_result": "matched",
                    "revalidation_page_url": (row.get("evidence") or {}).get("page_url")}
        ats = str(row.get("ats") or "").casefold()
        slug = company_db.normalize_ats_slug(ats, row.get("ats_slug"))
        values.append((json.dumps(evidence), int(row["company_id"]), ats, slug))
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_discovery c SET
            provenance=jsonb_set(COALESCE(provenance,'{}'::jsonb),
              '{custom_board_recovery}',
              COALESCE(provenance->'custom_board_recovery','{}'::jsonb) || %s::jsonb),
            updated_at=now()
          FROM company_employer_master m
          WHERE c.id=%s AND m.company_id=c.id AND m.in_target_population
            AND m.domain_verified AND lower(c.ats)=%s AND c.ats_slug=%s
            AND c.provenance ? 'custom_board_recovery'
        """, values)
        return cur.rowcount


def recover_custom_boards(*, limit: int = 200, workers: int = 4,
                          apply: bool = False, revalidate: bool = False,
                          rows: list[dict[str, Any]] | None = None,
                          client_factory: Callable[[], httpx.Client] | None = None,
                          resolve_dns: bool = True) -> dict[str, Any]:
    if limit < 1 or not 1 <= workers <= 4:
        raise ValueError("limit must be positive and workers must be between 1 and 4")
    candidates = list(rows if rows is not None else
                      list_candidates(limit=limit, revalidate=revalidate))[:limit]
    factory = client_factory or (lambda: httpx.Client(
        timeout=httpx.Timeout(12.0), headers={"User-Agent": USER_AGENT}))

    def work(row: dict[str, Any]) -> dict[str, Any]:
        with factory() as client:
            result = recover_record(row, client=client, resolve_dns=resolve_dns)
        if revalidate and result.get("status") == "recovered":
            expected_ats = str(row.get("ats") or "").casefold()
            expected_slug = company_db.normalize_ats_slug(expected_ats, row.get("ats_slug"))
            actual_slug = company_db.normalize_ats_slug(result.get("ats"), result.get("ats_slug"))
            if result.get("ats") != expected_ats or actual_slug != expected_slug:
                result["status"] = "revalidation_failed"
                result["errors"].append("recovered ATS identity differs from stored evidence")
        return result

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(work, row) for row in candidates]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"status": "incomplete", "errors": [str(exc)],
                                "pages_fetched": 0, "sitemaps_fetched": 0})
    statuses = Counter(str(row.get("status") or "incomplete") for row in results)
    ats = Counter(str(row.get("ats") or "") for row in results if row.get("ats"))
    updated = (save_revalidated(results) if apply and revalidate else
               save_recovered(results) if apply else 0)
    return {"selected": len(candidates), "dry_run": not apply, "revalidate": revalidate,
            "recovered": statuses["recovered"], "incomplete": statuses["incomplete"],
            "revalidation_failed": statuses["revalidation_failed"], "updated": updated,
            "pages_fetched": sum(int(row.get("pages_fetched") or 0) for row in results),
            "sitemaps_fetched": sum(int(row.get("sitemaps_fetched") or 0) for row in results),
            "ats": dict(sorted(ats.items())),
            "errors": [error for row in results for error in row.get("errors") or []][:100],
            "proposals": [{key: row.get(key) for key in
                           ("company_id", "ats", "ats_slug", "ats_url", "evidence")}
                          for row in results if row.get("status") == "recovered"][:100]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--apply", action="store_true",
                        help="persist evidence-backed ATS changes; default is dry-run")
    parser.add_argument("--revalidate", action="store_true",
                        help="recheck stored recovery evidence without clearing it")
    args = parser.parse_args(argv)
    result = recover_custom_boards(limit=args.limit, workers=args.workers,
                                   apply=args.apply, revalidate=args.revalidate)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
