from backend.scrapers.base import BaseScraper, RawJob

EXCLUDE_TITLES = [
    "senior", "sr.", "lead", "principal", "director", "head of",
    "vice president", "vp ", "architect", "staff ", "engineer", "developer",
    "scientist", "analyst", "consultant", "therapist", "physician",
    "nurse", "licensed", "attorney", "lawyer",
]


class RemoteOKScraper(BaseScraper):
    source = "remoteok"

    async def scrape(self) -> list[RawJob]:
        resp = await self.fetch("https://remoteok.com/api")
        resp.raise_for_status()
        data = resp.json()

        jobs = []
        for item in data:
            if not isinstance(item, dict) or "slug" not in item:
                continue

            position = (item.get("position", "") or "").lower()
            if any(ex in position for ex in EXCLUDE_TITLES):
                continue

            location = item.get("location", "Remote")
            loc_lower = (location or "").lower()
            if not self._is_us_or_canada(loc_lower, item):
                continue

            salary_min = self._parse_int(item.get("salary_min"))
            salary_max = self._parse_int(item.get("salary_max"))
            salary_text = None
            if salary_min and salary_max:
                salary_text = f"${salary_min:,} - ${salary_max:,}"

            tags = ", ".join(item.get("tags", [])[:10]) if item.get("tags") else None

            jobs.append(
                RawJob(
                    title=item.get("position", ""),
                    company=item.get("company", ""),
                    url=f"https://remoteok.com/remote-jobs/{item['slug']}",
                    salary_text=salary_text,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    location=location or "Remote",
                    country=self._detect_country(loc_lower),
                    description=item.get("description", ""),
                    tags=tags,
                )
            )

        return jobs

    def _is_us_or_canada(self, loc: str, item: dict) -> bool:
        us_terms = ["usa", "us", "united states", "north america", "worldwide", "anywhere"]
        ca_terms = ["canada", "ca"]
        remote_terms = ["remote", ""]
        all_terms = us_terms + ca_terms + remote_terms
        return any(term in loc for term in all_terms)

    def _detect_country(self, loc: str) -> str:
        if "canada" in loc or loc == "ca":
            return "CA"
        return "US"

    def _parse_int(self, val) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None
