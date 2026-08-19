from bs4 import BeautifulSoup

from backend.scrapers.base import BaseScraper, RawJob

SEARCH_QUERIES = [
    "remote sales",
    "remote customer service",
    "remote support",
    "remote recruiter",
    "remote data entry",
    "remote account manager",
    "remote SDR",
    "remote virtual assistant",
]


class IndeedScraper(BaseScraper):
    source = "indeed"

    async def scrape(self) -> list[RawJob]:
        jobs = []

        for query in SEARCH_QUERIES:
            for country_code, base_url in [("US", "https://www.indeed.com"), ("CA", "https://ca.indeed.com")]:
                try:
                    url = f"{base_url}/jobs"
                    params = {
                        "q": query,
                        "l": "Remote",
                        "remotejob": "032b3046-06a3-4876-8dfd-474eb5e7ed11",
                        "fromage": "3",  # last 3 days
                    }
                    resp = await self.client.get(url, params=params)
                    resp.raise_for_status()
                except Exception:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("div.job_seen_beacon, div.jobsearch-ResultsList > div")

                for card in cards:
                    title_el = card.select_one("h2.jobTitle a, a.jcs-JobTitle")
                    company_el = card.select_one("[data-testid='company-name'], .companyName")
                    salary_el = card.select_one("[data-testid='attribute_snippet_testid'], .salary-snippet-container")
                    location_el = card.select_one("[data-testid='text-location'], .companyLocation")

                    if not title_el:
                        continue

                    title = title_el.get_text(strip=True)
                    company = company_el.get_text(strip=True) if company_el else ""
                    salary_text = salary_el.get_text(strip=True) if salary_el else None
                    location = location_el.get_text(strip=True) if location_el else "Remote"

                    href = title_el.get("href", "")
                    if href and not href.startswith("http"):
                        href = f"{base_url}{href}"

                    if not href:
                        continue

                    jobs.append(
                        RawJob(
                            title=title,
                            company=company,
                            url=href,
                            salary_text=salary_text,
                            location=location,
                            country=country_code,
                        )
                    )

        return jobs
