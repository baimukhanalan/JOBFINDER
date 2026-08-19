"""Grow the company universe from remote-job aggregators.

Remotive / Jobicy / Himalayas list remote customer-support openings but link to
their own pages, not the ATS — so the valuable signal is the COMPANY. For every
company seen hiring remote support we guess likely ATS slugs from the name and
probe each ATS's public job-board API. Validated slugs are cached to
backend/data/discovered_slugs.json, which boards.collect() merges into its
company universe — every future batch then fetches those companies' live
openings straight from the ATS (fresh postings, correct apply URLs).

Probing is hundreds of HTTP calls -> run via `apply_cli --discover` on a weekly
cron, never in the batch path. The cache only grows (slugs are kept once seen);
a company with zero openings today is exactly the one we want to re-check daily.
"""
import asyncio
import json
import logging
import re

import httpx

from backend.applier.boards import (_DISCOVERED_PATH, _FETCHERS, _SLUG_RE, _SLUG_SKIP, _UA,
                                    DEFAULT_KEYWORDS)

logger = logging.getLogger(__name__)

_SUPPORT_WORDS = [k for k in DEFAULT_KEYWORDS]

# Aggregator endpoints that return remote customer-support listings as JSON.
_REMOTIVE = "https://remotive.com/api/remote-jobs?category=customer-support"
_JOBICY = "https://jobicy.com/api/v2/remote-jobs?count=100&industry=supporting"
_HIMALAYAS = "https://himalayas.app/jobs/api?limit=200&offset={off}"
_HIMALAYAS_PAGES = 5  # most-recent 1000 postings, filtered to support titles


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
            _mine_url(j.get("url", ""))
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


async def refresh_discovered_slugs() -> dict:
    """Pull aggregators, probe slug guesses, merge into the cached slug file."""
    known = {ats: set(slugs) for ats, slugs in load().items()}
    for ats in _FETCHERS:
        known.setdefault(ats, set())

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        names, direct = await _aggregator_companies(client)
        for ats, slugs in direct.items():
            known[ats] |= slugs

        sem = asyncio.Semaphore(8)
        tasks: list[tuple[str, str, asyncio.Task]] = []
        for name in sorted(names):
            for slug in _slug_candidates(name):
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

    out = {ats: sorted(slugs) for ats, slugs in known.items()}
    with open(_DISCOVERED_PATH, "w") as f:
        json.dump(out, f, indent=1)
    stats = {"companies_seen": len(names), "probes": len(tasks), "new_validated": validated,
             "total_cached": {ats: len(s) for ats, s in out.items()}}
    logger.info("slug discovery: %s", stats)
    return stats
