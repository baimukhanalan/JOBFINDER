import logging

from jobspy import scrape_jobs

from backend.config import settings
from backend.scrapers.base import RawJob

logger = logging.getLogger(__name__)

SEARCH_QUERIES = [
    "remote customer service representative",
    "remote call center agent",
    "remote chat support agent",
    "remote data entry clerk",
    "remote virtual assistant",
    "remote appointment setter",
    "remote telemarketing",
    "remote booking agent",
    "remote order processing",
    "work from home customer support no experience",
]

# Filter out senior/specialized/physical roles — we want entry level online mass hiring
EXCLUDE_TITLES = [
    "senior", "sr.", "lead", "principal", "director", "manager", "head of",
    "vice president", "vp ", "architect", "staff ", "engineer", "developer",
    "scientist", "analyst", "consultant", "strategist", "therapist",
    "physician", "nurse", "licensed", "attorney", "lawyer", "cpa",
    "construction", "warehouse", "driver", "mechanic", "plumber",
    "electrician", "welder", "forklift", "janitor", "custodian",
    "cook", "chef", "culinary", "cashier", "barista", "bartender",
    "hospital", "coder i", "coder ii", "auditor", "superintendent",
    "supervisor", "field ", "on-site", "onsite", "in-office",
]

# Must contain at least one of these to be relevant
INCLUDE_KEYWORDS = [
    "customer service", "customer support", "call center", "chat",
    "support rep", "support agent", "data entry", "virtual assistant",
    "appointment", "telemarketing", "sales rep", "booking",
    "order process", "admin assist", "receptionist", "helpdesk",
    "help desk", "tech support", "remote", "work from home",
    "bilingual", "inbound", "outbound",
]


async def scrape_jobspy() -> list[RawJob]:
    all_jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    use_proxy = bool(settings.proxy_url)  # disabled on first proxy failure

    for query in SEARCH_QUERIES:
        for country, country_code in [("USA", "US"), ("Canada", "CA")]:
            try:
                kwargs = {
                    # LinkedIn dropped: no-LinkedIn strategy, and its guest API
                    # errored on every run anyway (rows were consumed by nothing).
                    "site_name": ["indeed"],
                    "search_term": query,
                    "location": country,
                    "results_wanted": 25,
                    "is_remote": True,
                    "hours_old": 72,
                }
                if use_proxy:
                    kwargs["proxies"] = settings.proxy_url
                if country_code == "US":
                    kwargs["country_indeed"] = "USA"
                else:
                    kwargs["country_indeed"] = "Canada"

                try:
                    df = scrape_jobs(**kwargs)
                except Exception as proxy_err:
                    # If the proxy is unreachable, disable it and retry direct (once).
                    if use_proxy:
                        logger.warning(
                            "JobSpy proxy unreachable (%s) — disabling proxy, retrying direct",
                            type(proxy_err).__name__,
                        )
                        use_proxy = False
                        kwargs.pop("proxies", None)
                        df = scrape_jobs(**kwargs)
                    else:
                        raise

                for _, row in df.iterrows():
                    url = str(row.get("job_url", ""))
                    if not url or url in seen_urls:
                        continue

                    title = str(row.get("title", ""))
                    title_lower = title.lower()
                    desc_lower = str(row.get("description", "")).lower()[:500]

                    # Skip senior/specialized/physical roles
                    if any(ex in title_lower for ex in EXCLUDE_TITLES):
                        continue

                    # Must match at least one relevant keyword in title or description
                    if not any(kw in title_lower or kw in desc_lower for kw in INCLUDE_KEYWORDS):
                        continue

                    seen_urls.add(url)

                    salary_min = _safe_int(row.get("min_amount"))
                    salary_max = _safe_int(row.get("max_amount"))
                    salary_text = _format_salary(salary_min, salary_max)

                    # Detect source site from URL
                    source_site = "indeed"
                    if "ziprecruiter" in url:
                        source_site = "ziprecruiter"
                    elif "glassdoor" in url:
                        source_site = "glassdoor"
                    elif "linkedin" in url:
                        source_site = "linkedin"

                    all_jobs.append(
                        RawJob(
                            title=str(row.get("title", "")),
                            company=str(row.get("company", "")),
                            url=url,
                            salary_text=salary_text,
                            salary_min=salary_min,
                            salary_max=salary_max,
                            location=str(row.get("location", "Remote")),
                            country=country_code,
                            description=str(row.get("description", ""))[:5000],
                            tags=str(row.get("job_type", "")),
                            source_site=source_site,
                        )
                    )

                logger.info(f"JobSpy [{country}] '{query}': {len(df)} results")

            except Exception as e:
                logger.error(f"JobSpy [{country}] '{query}' failed: {e}")

    logger.info(f"JobSpy total: {len(all_jobs)} unique jobs")
    return all_jobs


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        v = int(float(val))
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _format_salary(min_val: int | None, max_val: int | None) -> str | None:
    if min_val and max_val:
        if min_val > 500:  # annual
            return f"${min_val:,} - ${max_val:,}"
        else:  # hourly
            return f"${min_val}/hr - ${max_val}/hr"
    if max_val:
        return f"Up to ${max_val:,}"
    if min_val:
        return f"From ${min_val:,}"
    return None
