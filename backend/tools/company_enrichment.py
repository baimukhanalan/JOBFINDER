"""Enrich independently sourced companies with careers-page and ATS signals.

This module deliberately starts from a company's official domain.  It does not read
job aggregators, ``targets.json`` or ``job_catalog``; reconciliation with the existing
catalog is a later storage concern.
"""
from __future__ import annotations

import argparse
import json
import re
import ipaddress
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx


_CAREER_WORDS = ("career", "careers", "jobs", "join-us", "joinus", "work-with-us")
_FALLBACK_PATHS = ("/careers", "/jobs", "/about/careers")
_MAX_HTML_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5
_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(
        r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
    ("eightfold", re.compile(r"https?://([a-z0-9-]+)\.eightfold\.ai/(?:careers|jobs)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([a-z0-9_-]+)", re.I)),
    ("icims", re.compile(r"https?://([a-z0-9-]+)\.icims\.com/(?:jobs|connect)", re.I)),
    ("workday", re.compile(
        r"https?://([a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com/(?:[a-z0-9_-]+)?", re.I)),
    ("workday", re.compile(
        r"https?://([a-z0-9-]+)\.myworkdaysite\.com/(?:[a-z0-9_-]+)?", re.I)),
    ("oracle", re.compile(
        r"https?://([a-z0-9-]+)\.(?:fa\.)?[^/]*oraclecloud\.com/hcmUI/CandidateExperience", re.I)),
    ("successfactors", re.compile(
        r"https?://([a-z0-9.-]+\.successfactors\.(?:com|eu))/(?:career|sfcareer|sfcareer/jobreqcareer)", re.I)),
)


class _RequestLimiter:
    """Share one request-start budget across all enrichment workers."""

    def __init__(self, min_interval: float) -> None:
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


def canonical_domain(value: str | None) -> str:
    """Return a lowercase registrable-looking host without a leading ``www``."""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def official_url(value: str | None) -> str:
    domain = canonical_domain(value)
    return f"https://{domain}/" if domain else ""


def public_http_url(url: str, resolve_dns: bool = False) -> bool:
    """Reject URLs that could target local/private services (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False

    def public_ip(value: str) -> bool:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return True
        return bool(ip.is_global)

    if not public_ip(host):
        return False
    if resolve_dns:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
        except OSError:
            return False
        if not addresses or any(not public_ip(address) for address in addresses):
            return False
    return True


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = ""
            self._text = []


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    try:
        parser.feed(html or "")
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in parser.links:
        url = urljoin(base_url, href).split("#", 1)[0]
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append((url, text))
    return out


def career_candidates(html: str, base_url: str) -> list[str]:
    """Rank likely careers links found on the official homepage."""
    ranked: list[tuple[int, str]] = []
    for url, text in extract_links(html, base_url):
        blob = f"{url} {text}".lower()
        score = sum(3 if word in text.lower() else 1 for word in _CAREER_WORDS if word in blob)
        if score:
            ranked.append((score, url))
    ranked.sort(key=lambda item: (-item[0], len(item[1])))
    return [url for _, url in ranked]


def detect_ats(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Detect a supported ATS from URLs or HTML fragments."""
    for value in values:
        for ats, pattern in _ATS_PATTERNS:
            match = pattern.search(value or "")
            if match:
                matched_url = match.group(0)
                if not matched_url.lower().startswith("http"):
                    matched_url = f"https://{matched_url}"
                return {"ats": ats, "ats_slug": match.group(1), "ats_url": matched_url}
    return {"ats": "", "ats_slug": "", "ats_url": ""}


def _get(client: httpx.Client, url: str, *, retries: int = 2,
         before_request=None) -> httpx.Response | None:
    """Fetch bounded HTML while validating every redirect target.

    DNS checks are enabled for the real client. Test doubles deliberately skip DNS so
    fixtures can use reserved ``.test`` names without weakening production guards.
    """
    current = url
    resolve_dns = isinstance(client, httpx.Client)
    redirects = 0
    while redirects <= _MAX_REDIRECTS:
        if not public_http_url(current, resolve_dns=resolve_dns):
            return None
        response = None
        for attempt in range(max(0, int(retries)) + 1):
            try:
                if before_request is not None:
                    before_request()
                if isinstance(client, httpx.Client):
                    # Stream real network responses so the 2 MB limit is enforced
                    # before an untrusted server can fill memory with a huge body.
                    with client.stream("GET", current, follow_redirects=False) as streamed:
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in streamed.iter_bytes():
                            size += len(chunk)
                            if size > _MAX_HTML_BYTES:
                                response = None
                                break
                            chunks.append(chunk)
                        else:
                            response = httpx.Response(
                                streamed.status_code, headers=streamed.headers,
                                content=b"".join(chunks), request=streamed.request,
                                extensions=streamed.extensions)
                    if response is None:
                        return None
                else:
                    response = client.get(current, follow_redirects=False)
            except (httpx.HTTPError, ValueError):
                if attempt >= retries:
                    return None
                time.sleep(0.15 * (2 ** attempt))
                continue
            if response.status_code not in _RETRY_STATUSES or attempt >= retries:
                break
            time.sleep(0.15 * (2 ** attempt))
        if response is None:
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            redirects += 1
            continue
        content_type = response.headers.get("content-type", "text/html")
        try:
            declared_size = int(response.headers.get("content-length", "0"))
        except ValueError:
            declared_size = 0
        actual_size = len((response.text or "").encode("utf-8"))
        if (200 <= response.status_code < 400 and "text/html" in content_type.lower()
                and declared_size <= _MAX_HTML_BYTES and actual_size <= _MAX_HTML_BYTES):
            return response
        return None
    return None


def enrich_company(record: dict, client: httpx.Client | None = None,
                   before_request=None) -> dict:
    """Return a copy enriched from at most one homepage and three careers requests."""
    out = dict(record)
    domain = canonical_domain(out.get("domain"))
    out["domain"] = domain
    out.setdefault("careers_url", "")
    out.setdefault("ats", "")
    out.setdefault("ats_slug", "")
    out.setdefault("ats_url", "")
    if not domain:
        return out

    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    evidence = {
        "attempted_at": attempted_at,
        "method": "official_domain_html",
        "input_domain": domain,
        "homepage_url": None,
        "careers_url": None,
        "ats_url": None,
        "result": "homepage_unavailable",
    }

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": "JobFinder-company-discovery/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"},
        )
    try:
        home = _get(client, official_url(domain), before_request=before_request)
        if home is None:
            provenance = dict(out.get("provenance") or {})
            provenance["web_enrichment"] = evidence
            out["provenance"] = provenance
            return out
        home_url = str(home.url)
        evidence["homepage_url"] = home_url
        home_links = extract_links(home.text, home_url)
        existing_careers = str(out.get("careers_url") or "")
        detected = detect_ats(
            [existing_careers, str(out.get("ats_url") or "")]
            + [url for url, _ in home_links] + [home.text])
        candidates = ([existing_careers] if public_http_url(existing_careers) else [])
        candidates += [url for url in career_candidates(home.text, home_url)
                       if url not in candidates]
        if not candidates:
            candidates = [urljoin(home_url, path) for path in _FALLBACK_PATHS]

        careers_url = ""
        for candidate in candidates[:3]:
            response = _get(client, candidate, before_request=before_request)
            if response is None:
                continue
            careers_url = str(response.url)
            links = extract_links(response.text, careers_url)
            found = detect_ats([careers_url] + [url for url, _ in links] + [response.text])
            if found["ats"]:
                detected = found
                break
        if careers_url:
            out["careers_url"] = careers_url
            evidence["careers_url"] = careers_url
        if detected["ats"]:
            out.update(detected)
            evidence["ats_url"] = detected["ats_url"]
        out["domain_confidence"] = max(float(out.get("domain_confidence") or 0), 0.9)
        if careers_url:
            out["careers_confidence"] = max(float(out.get("careers_confidence") or 0), 0.95)
        evidence["result"] = "ats_found" if detected["ats"] else (
            "careers_found" if careers_url else "no_careers_found")
        provenance = dict(out.get("provenance") or {})
        provenance["web_enrichment"] = evidence
        out["provenance"] = provenance
        return out
    finally:
        if owns_client:
            client.close()


def enrich_database(*, limit: int = 1000, workers: int = 4,
                    retry_attempted: bool = False,
                    min_interval: float = 0.1,
                    client_factory: Callable[[], httpx.Client] | None = None) -> dict:
    """Enrich a bounded DB batch that already has an official domain."""
    from backend.tools import company_discovery_db as company_db

    rows = company_db.list_enrichment_candidates(
        limit=limit, retry_attempted=retry_attempted)
    if not rows:
        return {"selected": 0, "updated": 0, **company_db.enrichment_counts()}
    limiter = _RequestLimiter(min_interval)

    def work(row: dict) -> dict:
        if client_factory is None:
            return enrich_company(row, before_request=limiter.wait)
        client = client_factory()
        try:
            return enrich_company(row, client, before_request=limiter.wait)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    completed: list[dict] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
        futures = {pool.submit(work, row): row["id"] for row in rows}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                errors += 1
                continue
            result["id"] = futures[future]
            completed.append(result)
    updated = company_db.update_enrichment_results(completed)
    result = {"selected": len(rows), "updated": updated,
              **company_db.enrichment_counts()}
    if errors:
        result["errors"] = errors
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich company_discovery rows from their existing official domains")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--min-interval", type=float, default=0.1,
                        help="global delay between HTTP request starts")
    parser.add_argument("--retry-attempted", action="store_true",
                        help="retry rows already attempted by this web enricher")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.limit < 1:
            raise ValueError("--limit must be at least 1")
        result = enrich_database(limit=args.limit, workers=args.workers,
                                 retry_attempted=args.retry_attempted,
                                 min_interval=args.min_interval)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
