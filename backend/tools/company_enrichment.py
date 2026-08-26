"""Enrich independently sourced companies with careers-page and ATS signals.

This module deliberately starts from a company's official domain.  It does not read
job aggregators, ``targets.json`` or ``job_catalog``; reconciliation with the existing
catalog is a later storage concern.
"""
from __future__ import annotations

import argparse
import html
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
from urllib.parse import parse_qs, urljoin, urlparse

import httpx


_CAREER_WORDS = ("career", "careers", "jobs", "join-us", "joinus", "work-with-us")
_CAREER_PATH_ENDINGS = set(_CAREER_WORDS) | {
    "career-opportunities", "job-opportunities", "open-positions", "join-our-team",
}
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
        r"https?://([a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com"
        r"(?:/[a-z0-9_-]+){0,6}", re.I)),
    ("workday", re.compile(
        r"https?://([a-z0-9-]+)\.myworkdaysite\.com/(?:[a-z0-9_-]+)?", re.I)),
    ("oracle", re.compile(
        r"https?://([a-z0-9-]+)\.(?:fa\.)?[^/]*oraclecloud\.com/hcmUI/CandidateExperience"
        r"(?:/[a-z0-9_-]+){0,8}", re.I)),
    ("successfactors", re.compile(
        r"https?://([a-z0-9.-]+\.successfactors\.(?:com|eu))/(?:career|sfcareer|sfcareer/jobreqcareer)", re.I)),
)

# Live-audited official career entry points for the mandatory cohort. ``custom``
# means proprietary/vendor-fronted search without a safe named ATS tenant.
MANDATORY_CAREER_AUDIT: dict[str, dict[str, str]] = {
    "amazon.com": {"careers_url": "https://www.amazon.jobs/en/", "ats": "custom", "ats_slug": "amazonjobs", "ats_url": "https://www.amazon.jobs/en/", "platform_evidence": "official proprietary Amazon Jobs site"},
    "concentrix.com": {"careers_url": "https://jobs.concentrix.com/", "ats": "workday", "ats_slug": "cnx", "ats_url": "https://cnx.wd1.myworkdayjobs.com/external_global", "platform_evidence": "official page links Workday tenant cnx/site external_global"},
    "foundever.com": {"careers_url": "https://jobs.foundever.com/content/Find-a-Job/", "ats": "successfactors", "ats_slug": "jobs.foundever.com", "ats_url": "https://jobs.foundever.com/search/?q=&sortColumn=referencedate&sortDirection=desc", "platform_evidence": "official custom-domain SAP SuccessFactors RMK site"},
    "ttec.com": {"careers_url": "https://www.ttecjobs.com/en", "ats": "custom", "ats_slug": "ttecjobscom", "ats_url": "https://www.ttecjobs.com/en/search-results", "platform_evidence": "official Radancy experience; underlying ATS not asserted"},
    "teleperformance.com": {"careers_url": "https://www.tp.com/en-us/careers/job-opportunities/", "ats": "custom", "ats_slug": "tpcom", "ats_url": "https://www.tp.com/en-us/careers/job-opportunities/", "platform_evidence": "official TP job search experience"},
    "cvshealth.com": {"careers_url": "https://jobs.cvshealth.com/us/en", "ats": "custom", "ats_slug": "cvschlus", "ats_url": "https://jobs.cvshealth.com/us/en/search-results", "platform_evidence": "official Phenom tenant CVSCHLUS; underlying ATS not asserted"},
    "unitedhealthgroup.com": {"careers_url": "https://careers.unitedhealthgroup.com/search-jobs", "ats": "custom", "ats_slug": "careersunitedhealthgroupcom", "ats_url": "https://careers.unitedhealthgroup.com/search-jobs", "platform_evidence": "official Radancy search; underlying ATS not asserted"},
    "jpmorganchase.com": {"careers_url": "https://www.jpmorganchase.com/careers", "ats": "oracle", "ats_slug": "jpmc", "ats_url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions", "platform_evidence": "official page links Oracle tenant jpmc/site CX_1001"},
    "walmart.com": {"careers_url": "https://careers.walmart.com/us/en/home", "ats": "custom", "ats_slug": "careerswalmartcom", "ats_url": "https://careers.walmart.com/us/en/home", "platform_evidence": "official Walmart careers experience; underlying ATS not asserted"},
    "target.com": {"careers_url": "https://corporate.target.com/careers", "ats": "workday", "ats_slug": "target", "ats_url": "https://target.wd5.myworkdayjobs.com/targetcareers", "platform_evidence": "official page links Workday tenant target/site targetcareers"},
    "hilton.com": {"careers_url": "https://jobs.hilton.com/", "ats": "oracle", "ats_slug": "efet", "ats_url": "https://efet.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs", "platform_evidence": "official page links Oracle tenant efet/site CX_1"},
    "marriott.com": {"careers_url": "https://careers.marriott.com/", "ats": "oracle", "ats_slug": "ejwl", "ats_url": "https://ejwl.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/requisitions", "platform_evidence": "official page links Oracle tenant ejwl/site CX"},
    "progressive.com": {"careers_url": "https://careers.progressive.com/", "ats": "custom", "ats_slug": "careersprogressivecom", "ats_url": "https://careers.progressive.com/jobs/search/", "platform_evidence": "official careers search; underlying ATS not asserted"},
    "statefarm.com": {"careers_url": "https://jobs.statefarm.com/main/", "ats": "icims", "ats_slug": "careers-statefarm", "ats_url": "https://careers-statefarm.icims.com/jobs/search", "platform_evidence": "official site links main iCIMS tenant; event portal excluded"},
    "allstate.com": {"careers_url": "https://www.allstatecorporation.com/careers.aspx", "ats": "custom", "ats_slug": "allstatecorporationcom", "ats_url": "https://www.allstatecorporation.com/careers.aspx", "platform_evidence": "official Allstate Corporation careers page; underlying ATS not asserted"},
}


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


class _CareerPageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []
        self._capture = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"title", "h1", "h2"}:
            self._capture += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"title", "h1", "h2"} and self._capture:
            self._capture -= 1

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.values.append(str(data))


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
        parsed = urlparse(url)
        path_parts = [part.lower() for part in parsed.path.split("/") if part]
        host_parts = set((parsed.hostname or "").lower().split("."))
        label = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        explicit_label = bool(re.fullmatch(
            r"(?:(?:explore|find|open|our|search|see|view) )?"
            r"(?:careers?|jobs?|job opportunities|career opportunities)"
            r"(?: at .+)?", label)) or label in {"join our team", "work with us"}
        explicit_path = bool(path_parts and path_parts[-1] in _CAREER_PATH_ENDINGS)
        dedicated_host = bool(host_parts & {"career", "careers", "job", "jobs"})
        # Do not mistake a news/article title that merely contains "jobs" for a
        # careers landing page (for example an SEO article ending in "...-jobs").
        if not (explicit_label or explicit_path or dedicated_host):
            continue
        score = 3 * int(explicit_label) + 2 * int(explicit_path) + int(dedicated_host)
        ranked.append((score, url))
    ranked.sort(key=lambda item: (-item[0], len(item[1])))
    return [url for _, url in ranked]


def looks_like_career_page(url: str, html: str) -> bool:
    """Require a careers-specific URL or a careers-specific page heading/title."""
    parsed = urlparse(url)
    path_parts = [part.lower() for part in parsed.path.split("/") if part]
    host_parts = set((parsed.hostname or "").lower().split("."))
    if ((path_parts and path_parts[-1] in _CAREER_PATH_ENDINGS)
            or host_parts & {"career", "careers", "job", "jobs"}):
        return True
    parser = _CareerPageText()
    try:
        parser.feed(html or "")
    except Exception:
        return False
    heading = re.sub(r"[^a-z0-9]+", " ", " ".join(parser.values).lower())
    return bool(re.search(
        r"\b(careers?|job opportunities|career opportunities|open positions|join our team)\b",
        heading,
    ))


def detect_ats(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Detect a supported ATS from URLs or HTML fragments."""
    event_portal_fallback: dict[str, str] | None = None
    for value in values:
        for ats, pattern in _ATS_PATTERNS:
            match = pattern.search(value or "")
            if match:
                matched_url = match.group(0)
                for candidate in re.findall(r"https?://[^\s\"'<>]+", value or "", re.I):
                    if match.group(0) in html.unescape(candidate):
                        matched_url = html.unescape(candidate).rstrip(").,;")
                        break
                if not matched_url.lower().startswith("http"):
                    matched_url = f"https://{matched_url}"
                slug = match.group(1)
                parsed = urlparse(matched_url)
                query = parse_qs(parsed.query)
                if ats == "successfactors":
                    tenant = (query.get("company") or query.get("site") or [""])[0]
                    # careerN.successfactors.* is a shared data-center host, not a
                    # customer identity. Without company/site it is unsafe to persist.
                    if not tenant and re.fullmatch(
                            r"career\d+\.successfactors\.(?:com|eu)", slug, re.I):
                        continue
                    slug = tenant or slug
                elif ats == "eightfold":
                    customer_domain = (query.get("domain") or [""])[0]
                    if customer_domain:
                        slug = f"{slug}:{customer_domain}"
                    elif slug.casefold() == "app":
                        # app.eightfold.ai is shared by multiple customers.
                        continue
                result = {"ats": ats, "ats_slug": slug, "ats_url": matched_url}
                if ats == "icims" and re.match(r"events?(?:-|$)", slug, re.I):
                    # Event portals are partial inventories and frequently coexist
                    # with the employer's authoritative tenant on the same page.
                    event_portal_fallback = event_portal_fallback or result
                    continue
                return result
    return event_portal_fallback or {"ats": "", "ats_slug": "", "ats_url": ""}


def _custom_domain_successfactors(page_url: str, page_html: str) -> dict[str, str]:
    """Bind RMK to its official custom hostname, never to the shared CDN host."""
    body = (page_html or "").casefold()
    markers = ("rmkcdn.successfactors.com", "performancemanager4.successfactors.com")
    if not all(marker in body for marker in markers):
        return {"ats": "", "ats_slug": "", "ats_url": ""}
    host = (urlparse(page_url).hostname or "").casefold()
    if not host or host.endswith("successfactors.com"):
        return {"ats": "", "ats_slug": "", "ats_url": ""}
    return {"ats": "successfactors", "ats_slug": host, "ats_url": page_url}


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
                            # ``iter_bytes`` already decodes gzip/br. Reusing the
                            # original content-encoding header would make the new
                            # Response decode the body a second time and fail closed.
                            decoded_headers = httpx.Headers(streamed.headers)
                            decoded_headers.pop("content-encoding", None)
                            decoded_headers.pop("content-length", None)
                            response = httpx.Response(
                                streamed.status_code, headers=decoded_headers,
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
            candidate_url = str(response.url)
            links = extract_links(response.text, candidate_url)
            found = detect_ats([candidate_url] + [url for url, _ in links] + [response.text])
            if not found["ats"]:
                found = _custom_domain_successfactors(candidate_url, response.text)
            if not found["ats"] and not looks_like_career_page(candidate_url, response.text):
                continue
            careers_url = candidate_url
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


def apply_mandatory_career_audit() -> dict:
    """Atomically replace only careers/ATS fields for the 15 mandatory seeds."""
    from backend.tools import company_discovery_db as company_db

    source_ids = sorted(MANDATORY_CAREER_AUDIT)
    with company_db._cur(False) as cur:
        cur.execute(
            "SELECT id,source_external_id FROM company_discovery "
            "WHERE source='mandatory_employer' AND source_external_id=ANY(%s)",
            (source_ids,))
        rows = cur.fetchall()
        identities = {str(row[1]): int(row[0]) for row in rows}
        if set(identities) != set(source_ids) or len(rows) != len(source_ids):
            missing = sorted(set(source_ids) - set(identities))
            raise RuntimeError(f"mandatory careers preflight failed; missing={missing}")
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for source_id in source_ids:
            audit = MANDATORY_CAREER_AUDIT[source_id]
            evidence = json.dumps({"mandatory_careers_audit": {
                **audit, "observed_at": observed_at,
                "method": "official_careers_live_audit",
            }})
            cur.execute("""
              UPDATE company_discovery SET careers_url=%s,ats=%s,ats_slug=%s,ats_url=%s,
                careers_confidence=0.99,
                provenance=provenance || %s::jsonb,updated_at=now()
              WHERE id=%s AND source='mandatory_employer'
            """, (audit["careers_url"], audit["ats"], audit["ats_slug"],
                  audit["ats_url"], evidence, identities[source_id]))
            if cur.rowcount != 1:
                raise RuntimeError(f"mandatory careers update failed: {source_id}")
    named = sum(row["ats"] != "custom" for row in MANDATORY_CAREER_AUDIT.values())
    return {"selected": len(source_ids), "updated": len(source_ids),
            "careers": len(source_ids), "named_ats": named,
            "custom_experiences": len(source_ids) - named}


def verify_mandatory_official_site(source_external_id: str, *,
                                   client: httpx.Client | None = None) -> dict:
    """Add the independent live-site factor required for domain verification."""
    from backend.tools import company_discovery_db as company_db

    source_id = str(source_external_id or "").strip().casefold()
    if source_id not in MANDATORY_CAREER_AUDIT:
        raise ValueError("source_external_id is not in the mandatory cohort")
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.domain,c.trade_name,c.legal_name,m.candidate_domain
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE c.source='mandatory_employer' AND c.source_external_id=%s
        """, (source_id,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"mandatory employer not found: {source_id}")
    record = dict(row)
    domain = canonical_domain(record.get("domain"))
    if not domain or canonical_domain(record.get("candidate_domain")) != domain:
        raise RuntimeError("discovery and master candidate domains do not agree")

    owned = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(12.0), headers={
            "User-Agent": "JobFinder-company-discovery/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"})
    try:
        response = _get(client, official_url(domain))
    finally:
        if owned:
            client.close()
    if response is None or canonical_domain(str(response.url)) != domain:
        return {"source_external_id": source_id, "verified": False,
                "reason": "official homepage unavailable or cross-domain redirect"}
    page_key = re.sub(r"[^a-z0-9]+", "", html.unescape(response.text).casefold())
    names = [record.get("trade_name"), record.get("legal_name")]
    name_keys = [re.sub(r"[^a-z0-9]+", "", str(name).casefold())
                 for name in names if name]
    matched = next((name for name, key in zip(names, name_keys)
                    if len(key) >= 5 and key in page_key), None)
    if not matched:
        return {"source_external_id": source_id, "verified": False,
                "reason": "official homepage did not expose a matching employer name"}

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    evidence = {
        "provider": "official_site_identity",
        "class": "official_site_identity",
        "evidence_class": "independent_live_official_site",
        "assertion": "exact_domain_identity",
        "domain": domain, "homepage_url": str(response.url),
        "matched_name": matched, "observed_at": observed_at,
    }
    encoded_list = json.dumps([evidence])
    encoded_object = json.dumps({"official_site_identity": evidence})
    with company_db._cur(False) as cur:
        cur.execute("""
          UPDATE company_employer_master SET domain_verified=TRUE,
            identity_confidence=GREATEST(identity_confidence,0.99),
            domain_evidence=COALESCE((
              SELECT jsonb_agg(item) FROM jsonb_array_elements(domain_evidence) item
              WHERE item->>'provider'<>'official_site_identity'
            ),'[]'::jsonb) || %s::jsonb,
            qualification_evidence=qualification_evidence || %s::jsonb,
            last_verified_at=now(),updated_at=now()
          WHERE company_id=%s AND candidate_domain=%s
        """, (encoded_list, encoded_object, int(record["id"]), domain))
        if cur.rowcount != 1:
            raise RuntimeError("official site identity update lost its domain precondition")
        cur.execute("""
          UPDATE company_discovery SET provenance=provenance || %s::jsonb,updated_at=now()
          WHERE id=%s AND domain=%s
        """, (encoded_object, int(record["id"]), domain))
    return {"source_external_id": source_id, "verified": True,
            "domain": domain, "homepage_url": str(response.url),
            "matched_name": matched}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich company_discovery rows from their existing official domains")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--min-interval", type=float, default=0.1,
                        help="global delay between HTTP request starts")
    parser.add_argument("--retry-attempted", action="store_true",
                        help="retry rows already attempted by this web enricher")
    parser.add_argument("--mandatory-audit", action="store_true",
                        help="apply the live-audited careers/ATS identities for 15 mandatory seeds")
    parser.add_argument("--verify-official-site", metavar="SOURCE_EXTERNAL_ID",
                        help="record an independent bounded live-site identity factor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.limit < 1:
            raise ValueError("--limit must be at least 1")
        if args.verify_official_site:
            result = verify_mandatory_official_site(args.verify_official_site)
        elif args.mandatory_audit:
            result = apply_mandatory_career_audit()
        else:
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
