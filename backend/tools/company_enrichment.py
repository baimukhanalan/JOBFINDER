"""Enrich independently sourced companies with careers-page and ATS signals.

This module deliberately starts from a company's official domain.  It does not read
job aggregators, ``targets.json`` or ``job_catalog``; reconciliation with the existing
catalog is a later storage concern.
"""
from __future__ import annotations

import re
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


_CAREER_WORDS = ("career", "careers", "jobs", "join-us", "joinus", "work-with-us")
_FALLBACK_PATHS = ("/careers", "/jobs", "/about/careers")
_MAX_HTML_BYTES = 2 * 1024 * 1024
_ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(
        r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
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


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    current = url
    resolve_dns = isinstance(client, httpx.Client)
    for _ in range(6):
        if not public_http_url(current, resolve_dns=resolve_dns):
            return None
        try:
            response = client.get(current, follow_redirects=False)
        except (httpx.HTTPError, ValueError):
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        content_type = response.headers.get("content-type", "text/html")
        if (response.status_code < 400 and "text/html" in content_type
                and len((response.text or "").encode("utf-8")) <= _MAX_HTML_BYTES):
            return response
        return None
    return None


def enrich_company(record: dict, client: httpx.Client | None = None) -> dict:
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

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": "JobFinder-company-discovery/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"},
        )
    try:
        home = _get(client, official_url(domain))
        if home is None:
            return out
        home_url = str(home.url)
        home_links = extract_links(home.text, home_url)
        detected = detect_ats([url for url, _ in home_links] + [home.text])
        candidates = career_candidates(home.text, home_url)
        if not candidates:
            candidates = [urljoin(home_url, path) for path in _FALLBACK_PATHS]

        careers_url = ""
        for candidate in candidates[:3]:
            response = _get(client, candidate)
            if response is None:
                continue
            careers_url = str(response.url)
            links = extract_links(response.text, careers_url)
            found = detect_ats([careers_url] + [url for url, _ in links] + [response.text])
            if found["ats"]:
                detected = found
                break
        out["careers_url"] = careers_url or (candidates[0] if candidates else "")
        if detected["ats"]:
            out.update(detected)
        out["domain_confidence"] = 0.9
        out["careers_confidence"] = 0.95 if careers_url else 0.45
        return out
    finally:
        if owns_client:
            client.close()
