"""Grow the company universe from remote-job aggregators.

Remotive / Jobicy / Himalayas / We Work Remotely list remote openings but link to
their own pages, not the ATS — so the valuable signal is the COMPANY. For every
company seen hiring remote support we guess likely ATS slugs from the name and
probe each ATS's public job-board API. Validated slugs are cached to
backend/data/discovered_slugs.json, which boards.collect() merges into its
company universe — every future batch then fetches those companies' live
openings straight from the ATS (fresh postings, correct apply URLs).

Probing is hundreds of HTTP calls -> run via `apply_cli --discover` (or directly as
`python -m backend.applier.discovery`) on a weekly cron, never in the batch path.
The cache only grows (slugs are kept once seen); a company with zero openings today
is exactly the one we want to re-check daily.
"""
import asyncio
import json
import logging
import os
import re

import httpx
from bs4 import BeautifulSoup

from backend.applier.boards import (_DISCOVERED_PATH, _FETCHERS, _SLUG_RE, _SLUG_SKIP, _UA,
                                    DEFAULT_KEYWORDS, blocked_slugs)

logger = logging.getLogger(__name__)

_SUPPORT_WORDS = [k for k in DEFAULT_KEYWORDS]

# Aggregator endpoints that return remote customer-support listings as JSON.
# Remotive's category taxonomy uses the slug "customer-service" (verified against
# https://remotive.com/api/remote-jobs/categories) — the old "customer-support" slug
# does not exist and silently returned the unfiltered feed. Its free API is a ~15-job
# teaser now, kept only as a low-yield COMPANY-NAME source (its `url` is always a
# remotive.com landing page, so mining it for ATS slugs is wasted work — see below).
_REMOTIVE = "https://remotive.com/api/remote-jobs?category=customer-service"
_JOBICY = "https://jobicy.com/api/v2/remote-jobs?count=100&industry=supporting"
_HIMALAYAS = "https://himalayas.app/jobs/api?limit=200&offset={off}"
_HIMALAYAS_PAGES = 5  # most-recent 1000 postings, filtered to support titles

# We Work Remotely publishes OPEN category RSS feeds (no auth). HTML/job pages are
# Cloudflare+login-walled, so we consume RSS ONLY — never fetch a WWR job/apply page.
# The valuable signal is the COMPANY name (title = "Company: Role"); our nightly
# catalog_collector re-tags each resolved board's regions/open_anywhere itself, so
# WWR's own region/country is used only as a COARSE worldwide pre-filter, never as
# the authoritative eligibility verdict. Poll at most during the weekly discovery run.
_WWR_FEEDS = (
    "https://weworkremotely.com/remote-jobs.rss",  # master
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
    "https://weworkremotely.com/categories/remote-design-jobs.rss",
    "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
)


def _parse_wwr_feed(xml: str, seen_guids: set[str], *, worldwide_only: bool = True) -> set[str]:
    """Company names from one WWR RSS feed.

    title -> "Company: Role" (company = the part before the first colon). Items are
    deduped by <guid> across feeds (the master feed repeats every category). The
    <country>-empty COARSE pre-filter keeps globally-open postings ("Anywhere in the
    World" with no country pin) and drops region-pinned ones — but this is only a
    bias for which companies to probe, NOT an eligibility verdict (the collector
    re-tags every job on the resolved board). Never touches WWR job/apply pages.
    """
    names: set[str] = set()
    soup = BeautifulSoup(xml, "xml")
    for item in soup.find_all("item"):
        guid_el = item.find("guid")
        guid = guid_el.get_text(strip=True) if guid_el else ""
        if guid:
            if guid in seen_guids:
                continue
            seen_guids.add(guid)
        title_el = item.find("title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title or ":" not in title:
            continue
        if worldwide_only:
            country_el = item.find("country")
            country = country_el.get_text(strip=True) if country_el else ""
            if country:  # named country/countries = region-restricted; skip this item
                continue
        company = title.split(":", 1)[0].strip()
        if company:
            names.add(company)
    return names


async def _aggregator_companies(client: httpx.AsyncClient) -> tuple[set[str], dict[str, set[str]]]:
    """Return (company display names, direct ATS slugs found in aggregator URLs)."""
    names: set[str] = set()
    direct: dict[str, set[str]] = {ats: set() for ats in _FETCHERS}

    def _mine_url(u: str):
        u = (u or "").lower()
        for ats, rx in _SLUG_RE.items():
            m = re.search(rx, u)
            if m and m.group(1) not in _SLUG_SKIP:
                direct[ats].add(m.group(1))

    try:
        r = await client.get(_REMOTIVE, headers=_UA)
        for j in r.json().get("jobs", []):
            names.add(j.get("company_name", ""))
            # NB: no _mine_url here — a Remotive `url` is always a remotive.com landing
            # page, so it never yields an ATS slug (only wasted regex work).
    except Exception as e:
        logger.warning("remotive fetch failed: %s", e)

    try:
        r = await client.get(_JOBICY, headers=_UA)
        for j in r.json().get("jobs", []):
            names.add(j.get("companyName", ""))
            _mine_url(j.get("url", ""))
    except Exception as e:
        logger.warning("jobicy fetch failed: %s", e)

    for page in range(_HIMALAYAS_PAGES):
        try:
            r = await client.get(_HIMALAYAS.format(off=page * 200), headers=_UA)
            for j in r.json().get("jobs", []):
                title = (j.get("title") or "").lower()
                cats = " ".join(j.get("parentCategories") or []).lower()
                if not any(k in title or k in cats for k in _SUPPORT_WORDS):
                    continue
                names.add(j.get("companyName", ""))
                if j.get("companySlug"):
                    names.add(j["companySlug"])
                _mine_url(j.get("applicationLink", ""))
        except Exception as e:
            logger.warning("himalayas page %d failed: %s", page, e)
            break

    # We Work Remotely — RSS-only, company names only, its own failure-isolated block
    # so a dead feed loses nothing (grow-only/start-from-load invariant preserved).
    try:
        seen_guids: set[str] = set()
        for feed in _WWR_FEEDS:
            try:
                r = await client.get(feed, headers=_UA)
                if r.status_code != 200:
                    logger.warning("wwr feed %s -> HTTP %s", feed, r.status_code)
                    continue
                names |= _parse_wwr_feed(r.text, seen_guids)
            except Exception as e:
                logger.warning("wwr feed %s failed: %s", feed, e)
                continue
    except Exception as e:
        logger.warning("wwr block failed: %s", e)

    names.discard("")
    return names, direct


_NAME_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|labs|hq|technologies|technology|software"
    r"|app|ai|io|com|group|global|digital|solutions|health|team)\b\.?", re.I)


def _slug_candidates(name: str) -> list[str]:
    """'Beyond Finance, Inc.' -> ['beyondfinance', 'beyond-finance', 'beyond'] etc."""
    base = name.lower().strip()
    base = re.sub(r"[^a-z0-9 &-]+", "", base)
    stripped = _NAME_SUFFIXES.sub("", base).strip()
    words = [w for w in re.split(r"[\s&-]+", stripped) if w]
    if not words:
        return []
    cands = [
        "".join(words),          # beyondfinance
        "-".join(words),         # beyond-finance
    ]
    if len(words) > 1:
        cands.append(words[0])   # beyond  (last resort, often a different company)
    out, seen = [], set()
    for c in cands:
        if 2 < len(c) <= 40 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def _probe(ats: str, slug: str, client: httpx.AsyncClient,
                 sem: asyncio.Semaphore) -> bool:
    """True if the slug is a real board on this ATS (API answers with a job list)."""
    async with sem:
        try:
            jobs = await _FETCHERS[ats](slug, client)
            return isinstance(jobs, list) and len(jobs) > 0
        except Exception:
            return False


def load() -> dict[str, list[str]]:
    try:
        with open(_DISCOVERED_PATH) as f:
            return {k: sorted(set(v)) for k, v in json.load(f).items()}
    except Exception:
        return {}


# Company names we've already turned into probe candidates — a name that failed to
# resolve to any board isn't worth re-probing every weekly run (validated slugs
# already only grow, so a name that DID resolve is captured there). Best-effort +
# non-fatal, and a SEPARATE file from discovered_slugs.json (never a second writer to it).
_PROBED_NAMES_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "discovery_probed_names.json"))


def _load_probed_names() -> set[str]:
    try:
        with open(_PROBED_NAMES_PATH) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_probed_names(names: set[str]) -> None:
    try:
        with open(_PROBED_NAMES_PATH, "w") as f:
            json.dump(sorted(names), f, indent=1)
    except Exception as e:
        logger.warning("probed-names cache write failed: %s", e)


async def refresh_discovered_slugs() -> dict:
    """Pull aggregators, probe slug guesses, merge into the cached slug file."""
    known = {ats: set(slugs) for ats, slugs in load().items()}
    for ats in _FETCHERS:
        known.setdefault(ats, set())

    probed = _load_probed_names()
    newly_probed: set[str] = set()
    skipped_cached = 0

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        names, direct = await _aggregator_companies(client)
        for ats, slugs in direct.items():
            known[ats] |= slugs

        sem = asyncio.Semaphore(8)
        tasks: list[tuple[str, str, asyncio.Task]] = []
        for name in sorted(names):
            if name in probed:  # already turned into probes on a prior run — skip
                skipped_cached += 1
                continue
            cands = _slug_candidates(name)
            if not cands:
                continue
            newly_probed.add(name)  # mark considered so we don't re-probe next week
            for slug in cands:
                for ats in _FETCHERS:
                    if slug in known[ats]:
                        continue
                    tasks.append((ats, slug, asyncio.ensure_future(
                        _probe(ats, slug, client, sem))))
        validated = 0
        for ats, slug, t in tasks:
            if await t:
                known[ats].add(slug)
                validated += 1

    # Hardening: drop known-junk aggregator slugs (nogigiddy + employer_intel "no")
    # at WRITE time so they never enter the cache — the grow-only invariant's one
    # deliberate exception, and the choke point the collector's _slugs() path lacks.
    blocked = blocked_slugs()
    out = {ats: sorted(s for s in slugs if s not in blocked) for ats, slugs in known.items()}
    with open(_DISCOVERED_PATH, "w") as f:
        json.dump(out, f, indent=1)
    _save_probed_names(probed | newly_probed)
    stats = {"companies_seen": len(names), "probes": len(tasks), "new_validated": validated,
             "names_skipped_cached": skipped_cached,
             "total_cached": {ats: len(s) for ats, s in out.items()}}
    logger.info("slug discovery: %s", stats)
    return stats


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Refresh backend/data/discovered_slugs.json from remote-job aggregators "
                     "(Remotive/Jobicy/Himalayas/WeWorkRemotely) by probing likely ATS slugs. "
                     "Hundreds of HTTP calls; intended for a weekly cron, not the batch path.")
    ap.parse_args()
    print(asyncio.run(refresh_discovered_slugs()), flush=True)
