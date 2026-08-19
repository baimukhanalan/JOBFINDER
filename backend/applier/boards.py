"""Fetch live job postings from ATS public APIs, so we can go roster -> real openings.

Greenhouse / Lever / Ashby expose public JSON job-board APIs (no auth). Each fetcher
returns a list of {title, company, description, apply_url} dicts the runner understands.
"""
import html
import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

from backend.data import roster
from backend.services.tailor import keywords as kw

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36"}


def _strip_html(s: str) -> str:
    """HTML -> plain text, escaped-HTML-safe.

    Greenhouse ships `content` HTML-ESCAPED, so unescape must run BEFORE the tag
    strip — stripping first left `&lt;strong&gt;` intact and the later unescape
    turned it into literal tags whose names ("strong", "nbsp", "class") became
    keyword tokens in 63/148 reports. Second unescape resolves entities the first
    pass exposed (`&amp;nbsp;` -> `&nbsp;` -> space); then collapse whitespace.
    """
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _slug_from_url(url: str, ats: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else ""


async def fetch_greenhouse(token: str, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", headers=_UA)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        out.append({"title": j.get("title", ""), "company": token,
                    "description": _strip_html(j.get("content", ""))[:4000],
                    "apply_url": j.get("absolute_url", "")})
    return out


async def fetch_lever(company: str, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"https://api.lever.co/v0/postings/{company}?mode=json", headers=_UA)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        desc = j.get("descriptionPlain") or _strip_html(j.get("description", ""))
        out.append({"title": j.get("text", ""), "company": company,
                    "description": (desc or "")[:4000],
                    "apply_url": j.get("hostedUrl") or j.get("applyUrl", "")})
    return out


async def fetch_ashby(org: str, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false",
        headers=_UA)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        desc = _strip_html(j.get("descriptionHtml", "")) or j.get("descriptionPlain", "")
        out.append({"title": j.get("title", ""), "company": org,
                    "description": (desc or "")[:4000],
                    "apply_url": j.get("applyUrl") or j.get("jobUrl", "")})
    return out


async def fetch_workable(slug: str, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", headers=_UA)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        if j.get("telecommuting"):
            loc = f"Remote — {loc}" if loc else "Remote"
        out.append({"title": j.get("title", ""), "company": slug,
                    "description": _strip_html(j.get("description", ""))[:4000],
                    "apply_url": j.get("application_url") or j.get("url", ""),
                    "location": loc})
    return out


_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
             "workable": fetch_workable}


async def fetch_entry(entry: dict, client: httpx.AsyncClient) -> list[dict]:
    ats = entry.get("ats")
    fetcher = _FETCHERS.get(ats)
    if not fetcher:
        return []
    try:
        slug = _slug_from_url(entry.get("apply_url", ""), ats)
        if not slug:
            return []
        result = await fetcher(slug, client)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.warning("board fetch failed for %s (%s): %s", entry.get("name"), ats, e)
        return []


DEFAULT_KEYWORDS = ["support", "customer", "happiness", "customer success", "customer service",
                    "customer care", "customer experience", "chat", "help desk", "client success"]

# Title markers that disqualify a role from the "entry/mid remote customer support" target.
_FIT_EXCLUDE = [
    # NOTE: "senior"/"sr"/"lead" are NOT excluded — the candidate is a senior CS person,
    # so senior/lead support roles ARE the fit (this was wrongly dropping good jobs).
    # NOTE: the support-engineer family (Support Engineer, Technical Support Engineer,
    # IT Support, Help Desk, Customer Success Engineer) is deliberately NOT excluded —
    # those are desired volume; only wrong-DISCIPLINE engineering titles are listed.
    "account executive", "sales", "business development",
    "principal", "director", "head of", "vp", "vice president",
    "manager", "architect", "intern", "recruiter", "designer",
    # engineering disciplines that slip in via "customer"/"support" in the title
    "software engineer", "analytics engineer", "data engineer", "solutions engineer",
    "sales engineer", "devops", "backend", "frontend", "full stack", "full-stack",
    "supportability engineer", "systems engineer", "systems support engineer",
    "site reliability", "sre", "platform engineer", "network engineer", "security engineer",
    "android", "ios", "machine learning", "ml engineer", "mobile engineer",
    "data scientist",
]
_NONREMOTE = ["hybrid", "on-site", "on site", "onsite", "in-office", "in office",
              "must relocate", "relocation required", "days a week", "days/week",
              "days per week", "commutable", "commuting distance"]
_REMOTE_HINTS = ["remote", "work from home", "wfh", "anywhere", "distributed", "home-based", "home based"]

# Non-US/Canada signals — the candidates are US (some CA) work-authorized only, so a role
# tied to another country/region (EMEA office, "authorized to work in Ireland", etc.) is a
# hard mis-fit. Catches the upstream cause of false work-auth / wrong-location answers.
_NON_US_MARKERS = [
    "emea", "apac", "anz", " uk ", "u.k.", "united kingdom", "ireland", "dublin", "cork",
    "london", "england", "scotland", "europe", "european", "germany", "france", "spain",
    "netherlands", "poland", "portugal", "romania", "india", "philippines", "pakistan",
    "singapore", "malaysia", "australia", "new zealand", "latam", "brazil", "mexico city",
    "authorized to work in ireland", "work in the uk", "reside in ireland", "eu work",
    "right to work in the uk", "based in europe",
]


def is_us_eligible(job: dict) -> bool:
    """False if the role is clearly tied to a non-US/Canada country/region."""
    blob = " ".join([job.get("title", ""), job.get("location", ""),
                     (job.get("description", "") or "")[:1200]]).lower()
    if any(m in blob for m in _NON_US_MARKERS):
        # ...unless it also explicitly anchors to the US/Canada (e.g. "US & EMEA")
        if not any(x in blob for x in ("united states", "u.s.", " usa", "us-based",
                                       "us only", "canada", "north america")):
            return False
    return True


# Places whose "<Place> Only" restriction makes a posting unwinnable for the roster
# (log evidence: "Remote - Mexico Only", Ontario-only reqs queued and burned).
# Single CA provinces are listed too: an "Ontario only" req is closed to US profiles.
_PLACE_ONLY = [
    "mexico", "latam", "latin america", "south america", "central america", "brazil",
    "argentina", "colombia", "chile", "peru", "costa rica", "europe", "emea", "apac",
    "uk", "united kingdom", "ireland", "germany", "france", "spain", "portugal",
    "poland", "romania", "netherlands", "india", "philippines", "pakistan", "africa",
    "australia", "new zealand", "singapore", "japan",
    "ontario", "quebec", "british columbia", "alberta",
]
_PLACES_ALT = "|".join(re.escape(p) for p in _PLACE_ONLY)
# "<place> ... only" / "only ... <place>" within one sentence fragment.
_PLACE_ONLY_RE = re.compile(
    rf"\b(?:{_PLACES_ALT})\b[^.\n]{{0,30}}\bonly\b|\bonly\b[^.\n]{{0,30}}\b(?:{_PLACES_ALT})\b")
_US_ANCHORS = ("united states", "u.s.", "usa", " us ", "us-", "canada", "north america")


def is_geo_eligible(job: dict) -> bool:
    """False when the posting hard-restricts to a non-US/CA place ("Mexico Only").

    Complements is_us_eligible: "Remote - Mexico Only" slipped past it because the
    marker list only knows "mexico city". An explicit "<Place> Only" is stronger than
    the US-anchor override — unless the US/Canada is named right AROUND the match:
    multi-country eligibility lists ("open in the United States and Mexico only")
    are winnable and common on remote boards. Anchors are tested in a window, not in
    m.group(0) — the "<place> ... only" alternative starts AT the place name, so a
    US mention just before it sits outside the match (same windowing pattern as
    is_language_eligible below).
    """
    blob = " ".join([job.get("title", ""), job.get("location", ""),
                     (job.get("description", "") or "")[:1500]]).lower()
    for m in _PLACE_ONLY_RE.finditer(blob):
        window = blob[max(0, m.start() - 40):m.end() + 40]
        if not any(a in window for a in _US_ANCHORS):
            return False
    return True


# Hard language requirements: fluent/native/bilingual next to a language the profile
# doesn't speak = unwinnable (bilingual-Spanish reqs kept landing in the queue).
_LANG_NAMES = (
    "spanish|french|german|portuguese|italian|dutch|japanese|korean|mandarin|"
    "cantonese|chinese|arabic|hebrew|polish|russian|turkish|vietnamese|thai|hindi|"
    "tagalog|indonesian|swedish|norwegian|danish|finnish|czech|greek|ukrainian|"
    "romanian|hungarian")
_LANG_REQ_RE = re.compile(
    rf"(?:fluent|fluency|native|bilingual)[^.\n]{{0,40}}\b({_LANG_NAMES})\b"
    rf"|\b({_LANG_NAMES})\b[^.\n]{{0,40}}(?:fluent|fluency|native|bilingual)")
# "Spanish fluency is a plus" is not a hard requirement.
_LANG_SOFTENERS = ("plus", "bonus", "nice to have", "preferred", "advantage")


def is_language_eligible(job: dict, languages: list[str] | None = None) -> bool:
    """False when the posting requires fluency in a language not in `languages`
    (the profile's facts `languages` list; defaults to English-only)."""
    known = {(lang or "").strip().lower() for lang in (languages or ["english"])}
    blob = " ".join([job.get("title", ""),
                     (job.get("description", "") or "")]).lower()
    for m in _LANG_REQ_RE.finditer(blob):
        lang = (m.group(1) or m.group(2) or "").lower()
        window = blob[max(0, m.start() - 30):m.end() + 40]
        if lang and lang not in known and not any(s in window for s in _LANG_SOFTENERS):
            return False
    return True


def _kw_match(text: str, keywords: list[str]) -> bool:
    """Word-boundary keyword test on lowercase text — raw substring matching queued
    an OpenAI Android role because 'chat' matched 'ChatGPT'."""
    return any(kw.term_present(k, text) for k in keywords)


def is_fit(job: dict) -> bool:
    """Entry/mid customer-support-style role (not sales/senior-mgmt/wrong discipline).

    Word-boundary matching on both lists: substrings let 'chat' hit 'ChatGPT' (queued)
    and 'sales' hit 'Salesforce' (wrongly dropped).
    """
    t = (job.get("title") or "").lower()
    if _kw_match(t, _FIT_EXCLUDE):
        return False
    return _kw_match(t, DEFAULT_KEYWORDS)


def is_remote(job: dict) -> bool:
    """Heuristic: explicitly remote and not flagged hybrid/onsite."""
    blob = " ".join([job.get("title", ""), job.get("location", ""),
                     (job.get("description", "") or "")[:1500]]).lower()
    if any(x in blob for x in _NONREMOTE) and "fully remote" not in blob:
        return False
    return any(x in blob for x in _REMOTE_HINTS)


# Hosts our per-ATS fill strategies can actually pre-fill (others need manual/Workday-iCIMS).
AUTOFILL_HOSTS = ("greenhouse", "lever.co", "ashbyhq", "workable")


def is_autofillable(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in AUTOFILL_HOSTS)


# Rank floor: postings scoring below this never enter the queue. The score is
# keywords.match_score of title+description against the support LEXICON, i.e. the
# share of the JD's own keywords that are customer-support vocabulary (0-100).
# Calibrated value was 25 (real support JDs land 28-80, wrong-discipline ones 4-22).
# DISABLED (0) for testing: no posting is dropped on fit — every job enters the queue
# and gets a per-vacancy tailored résumé. Restore the floor with MIN_SCORE = 25.
MIN_SCORE = 0
_SUPPORT_VOCAB = " . ".join(kw.LEXICON)


def score_job(job: dict) -> int | None:
    """0-100 support-fit of a posting; None when there is no description to judge
    (a fabricated 0 would rank an unknown below known-bad — None sorts last, kept)."""
    desc = (job.get("description") or "").strip()
    if not desc:
        return None
    return kw.match_score(f"{job.get('title', '')}\n{desc}", _SUPPORT_VOCAB)


def rank_jobs(jobs: list[dict]) -> list[dict]:
    """Annotate each job with 'score', drop scored ones below MIN_SCORE, best first.

    Description-less jobs keep score=None and sort AFTER every scored job — unknown
    is worse than any passing score, but not proof of a mis-fit, so they stay.
    """
    for j in jobs:
        j["score"] = score_job(j)
    kept = [j for j in jobs if j["score"] is None or j["score"] >= MIN_SCORE]
    kept.sort(key=lambda j: -1 if j["score"] is None else j["score"], reverse=True)
    return kept


async def collect_from_db(limit: int = 50, keywords: list[str] | None = None,
                          remote_only: bool = True, fit_only: bool = True,
                          autofillable_only: bool = True, only_new: bool = True,
                          languages: list[str] | None = None) -> list[dict]:
    """Pull openings already scraped into the DB and keep the ones we can act on.

    Filters out LinkedIn (ban risk per strategy) and, by default, anything not on an
    ATS our fill strategies support. Every candidate row is considered, then ranked
    (rank_jobs: support-fit score, MIN_SCORE floor, best first) BEFORE the limit cut.
    Returns the same {title, company, description, apply_url} dicts the runner
    understands, each annotated with 'score'.
    """
    from sqlalchemy import select

    from backend.models.database import async_session
    from backend.models.job import ApplicationStatus, Job, JobSource

    keywords = keywords or DEFAULT_KEYWORDS
    async with async_session() as session:
        stmt = select(Job).where(Job.source != JobSource.LINKEDIN)
        if only_new:
            stmt = stmt.where(Job.status == ApplicationStatus.NEW)
        rows = (await session.execute(stmt)).scalars().all()

    blocked = blocked_slugs()
    found: list[dict] = []
    for j in rows:
        apply_url = j.apply_url or j.url or ""
        if not apply_url:
            continue
        if autofillable_only and not is_autofillable(apply_url):
            continue
        if _url_slug(apply_url) in blocked:
            continue
        # Scraped descriptions carry the same (escaped) HTML as the live APIs.
        job = {"title": j.title or "", "company": j.company or "",
               "description": _strip_html(j.description or ""), "apply_url": apply_url,
               "location": j.location or ""}
        t = job["title"].lower()
        if keywords and not _kw_match(t, keywords):
            continue
        if fit_only and not is_fit(job):
            continue
        if remote_only and not is_remote(job):
            continue
        if not is_us_eligible(job) or not is_geo_eligible(job):
            continue
        if not is_language_eligible(job, languages):
            continue
        found.append(job)
    return rank_jobs(found)[:limit]


_ATS_BASE = {"greenhouse": "https://boards.greenhouse.io/{}",
             "lever": "https://jobs.lever.co/{}",
             "ashby": "https://jobs.ashbyhq.com/{}",
             "workable": "https://apply.workable.com/{}"}

# Curated companies known to hire remote CS on these ATSes (validated: API returns jobs).
# The market only has ~20-40 open remote-CS roles across all of them at any moment, so this
# is breadth to CATCH roles as they open (with the daily refresh), not a path to thousands.
SEED_COMPANIES = {
    "greenhouse": ["automatticcareers", "gitlab", "twilio", "calyxo", "remotecom", "stripe",
                   "hubspot", "webflow", "coinbase", "brex", "gusto", "samsara", "cloudflare",
                   "asana", "dropbox", "doordash", "instacart", "robinhood", "plaid", "affirm",
                   "databricks", "datadog", "mongodb", "elastic", "zendesk", "lattice", "faire",
                   "whatnot", "gopuff", "betterup", "beyondfinance", "iterable", "keepersecurity",
                   "amplemarket", "pairteam", "benevity", "airbnb", "squarespace", "grammarly",
                   "discord", "reddit"],
    "lever": ["toptal", "gohighlevel", "greenlight", "moonpay", "fliff", "bloom", "voodoo",
              "attentive", "ramp", "plaid", "spotify", "netflix"],
    "ashby": ["deel", "1password", "close", "zapier", "jerry.ai", "clickup", "vanta", "ramp",
              "linear", "posthog", "mercury", "runway", "replit", "census", "hex", "watershed",
              "peek", "kastle", "flock", "openstore", "supabase", "alma", "gem", "notion"],
}

_SLUG_RE = {
    "greenhouse": r"greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9_-]+)",
    "lever": r"lever\.co/([a-z0-9_-]+)",
    "ashby": r"ashbyhq\.com/([a-z0-9_.-]+)",
    "workable": r"apply\.workable\.com/([a-z0-9_-]+)",
}
_SLUG_SKIP = {"embed", "job-boards", "boards", "j", "api"}

# Ghost-job aggregators masquerading as employers on real ATSes — never fetch/queue.
AGGREGATOR_EXCLUDE = {"nogigiddy"}

_INTEL_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "employer_intel.json"))


def _url_slug(url: str) -> str:
    """ATS company slug from any known apply URL ('' if not a known ATS)."""
    u = (url or "").lower()
    for rx in _SLUG_RE.values():
        m = re.search(rx, u)
        if m and m.group(1) not in _SLUG_SKIP:
            return m.group(1)
    return ""


def blocked_slugs() -> set[str]:
    """Slugs we must not queue: aggregator junk + employers whose intel entry
    (backend/data/employer_intel.json) says hiring_remote_cs_now == "no" (e.g. Boldr)."""
    blocked = set(AGGREGATOR_EXCLUDE)
    try:
        with open(_INTEL_PATH) as f:
            entries = json.load(f)
    except Exception:
        return blocked
    for e in entries:
        if str(e.get("hiring_remote_cs_now", "")).strip().lower() == "no":
            slug = _url_slug(e.get("apply_url", ""))
            if slug:
                blocked.add(slug)
    return blocked


async def discover_ats_slugs_from_db() -> dict[str, set[str]]:
    """Mine the scraped DB for company slugs on Greenhouse/Lever/Ashby.

    Scraped posting URLs go stale, but the *company* keeps hiring — so we harvest the
    slugs and re-fetch each company's CURRENT openings via its ATS API (fresh + correct
    apply URLs). Turns a stale job pool into a live company list.
    """
    import re

    from backend.models.database import async_session
    from backend.models.job import Job, JobSource
    from sqlalchemy import select

    out: dict[str, set[str]] = {"greenhouse": set(), "lever": set(), "ashby": set(), "workable": set()}
    async with async_session() as session:
        rows = (await session.execute(
            select(Job.apply_url, Job.url).where(Job.source != JobSource.LINKEDIN))).all()
    for apply_url, url in rows:
        u = (apply_url or url or "").lower()
        for ats, host in (("greenhouse", "greenhouse.io"), ("lever", "lever.co"),
                          ("ashby", "ashbyhq.com"), ("workable", "apply.workable.com")):
            if host not in u:
                continue
            m = re.search(_SLUG_RE[ats], u)
            if m and m.group(1) not in _SLUG_SKIP:
                out[ats].add(m.group(1))
    return out


_DISCOVERED_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "discovered_slugs.json"))


def load_discovered_slugs() -> dict[str, list[str]]:
    """Slugs validated by `apply_cli --discover` (backend/applier/discovery.py)."""
    try:
        with open(_DISCOVERED_PATH) as f:
            d = json.load(f)
        return {k: list(v) for k, v in d.items() if k in _FETCHERS}
    except Exception:
        return {}


async def collect(atses: list[str] | None = None, keywords: list[str] | None = None,
                  limit: int = 12, remote_only: bool = True, fit_only: bool = True,
                  include_discovered: bool = True, max_per_company: int = 6,
                  languages: list[str] | None = None) -> list[dict]:
    """Fetch live openings from the roster's ATS companies (plus, by default, companies
    discovered in the scraped DB), keeping only remote + customer-support-fit roles.

    Each kept job is scored (score_job) and the MIN_SCORE floor is applied inline so
    junk never eats a `limit` slot; the result is returned best-score first (jobs with
    no description get score=None and go last). Fetching still stops once `limit`
    passing jobs are gathered — ranking reorders what was collected, it does not keep
    hitting every remaining board.

    `max_per_company` caps how many roles one company can contribute so a single
    high-volume employer (e.g. Twilio's many Support Engineer reqs) can't flood the queue.
    """
    atses = atses or list(_FETCHERS)
    keywords = keywords or DEFAULT_KEYWORDS
    entries = [e for e in roster.all_entries() if e.get("ats") in atses]

    if include_discovered:
        have = {(e.get("ats"), _slug_from_url(e.get("apply_url", ""), e.get("ats", ""))) for e in entries}

        def _add(ats: str, sg: str):
            if ats in atses and ats in _ATS_BASE and (ats, sg) not in have:
                entries.append({"ats": ats, "name": sg, "apply_url": _ATS_BASE[ats].format(sg)})
                have.add((ats, sg))

        for ats, slugs in SEED_COMPANIES.items():  # curated breadth
            for sg in slugs:
                _add(ats, sg)
        for ats, slugs in load_discovered_slugs().items():  # aggregator-discovered (cached)
            for sg in slugs:
                _add(ats, sg)
        try:
            discovered = await discover_ats_slugs_from_db()  # + whatever's in the scraped DB
            for ats, slugs in discovered.items():
                for sg in slugs:
                    _add(ats, sg)
        except Exception as e:
            logger.warning("slug discovery failed (%s)", e)

    blocked = blocked_slugs()
    entries = [e for e in entries
               if _slug_from_url(e.get("apply_url", ""), e.get("ats", "")) not in blocked]

    found: list[dict] = []
    per_company: dict[str, int] = {}
    seen: set[tuple] = set()
    async with httpx.AsyncClient(timeout=25) as client:
        for entry in entries:
            if len(found) >= limit:
                break
            jobs = await fetch_entry(entry, client)
            for j in jobs:
                if not j.get("apply_url"):
                    continue
                t = (j.get("title") or "").lower()
                if not _kw_match(t, keywords):
                    continue
                if fit_only and not is_fit(j):
                    continue
                if remote_only and not is_remote(j):
                    continue
                if not is_us_eligible(j) or not is_geo_eligible(j):
                    continue
                if not is_language_eligible(j, languages):
                    continue
                j["score"] = score_job(j)
                if j["score"] is not None and j["score"] < MIN_SCORE:
                    continue
                comp = (j.get("company") or "").lower()
                if (comp, t) in seen:  # exact duplicate title at the same company
                    continue
                if per_company.get(comp, 0) >= max_per_company:
                    continue
                seen.add((comp, t))
                per_company[comp] = per_company.get(comp, 0) + 1
                found.append(j)
                if len(found) >= limit:
                    break
    found.sort(key=lambda j: -1 if j["score"] is None else j["score"], reverse=True)
    return found
