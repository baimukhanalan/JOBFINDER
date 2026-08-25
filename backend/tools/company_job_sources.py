"""Independent public ATS connectors for company-discovery phase 2.

The only inputs are an ATS name, its public board slug and a company id.  This
module deliberately does not import the existing catalog, target lists or
aggregator discovery.  Parsers are kept pure so captured ATS responses can be
replayed in tests or re-normalized later from ``raw_payload``.

Only jobs with a *confident* remote signal are returned.  "Hybrid", on-site,
"remote possible" and otherwise ambiguous postings are intentionally excluded.
"""
from __future__ import annotations

import html
import hashlib
import re
import time
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlparse

import httpx


SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday")
USER_AGENT = "JobFinder-company-jobs/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 500
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_REMOTE_STRONG = re.compile(
    r"\b(?:fully|entirely|completely|100\s*%)\s+remote\b|"
    r"\bremote[- ](?:only|first|position|role|job)\b|"
    r"\bwork(?:ing)?\s+from\s+home\b|\btelecommut(?:e|ing)\b",
    re.I,
)
_REMOTE_LOCATION = re.compile(
    r"^(?:remote|remote\s*[-–—,(/]|(?:us|usa|united states|north america)[ -]+remote\b)|"
    r"\(\s*(?:fully\s+)?remote\s*\)",
    re.I,
)
_REMOTE_TITLE = re.compile(
    r"\(\s*(?:fully\s+|100\s*%\s*)?remote\s*\)|[-–—]\s*remote\s*$|\bremote position\b",
    re.I,
)
_NOT_REMOTE = re.compile(
    r"\bhybrid\b|\bon[- ]?site\b|\bin[- ]office\b|\boffice[- ]based\b|"
    r"\bremote\s+(?:option|optional|possible|eligible|available)\b",
    re.I,
)


class JobSourceError(RuntimeError):
    """A public ATS endpoint could not be read after bounded retries."""


class JobFetchResult(list):
    """List-compatible fetch result with an explicit closure-safety signal.

    Existing callers can continue to iterate/compare it as a normal list.  New
    collectors must check ``complete`` before treating absence as authoritative.
    """

    def __init__(self, jobs: Iterable[dict] = (), *, complete: bool = True,
                 errors: Iterable[str] = ()) -> None:
        super().__init__(jobs)
        self.complete = bool(complete)
        self.errors = [str(error) for error in errors if str(error).strip()]


def _result(jobs: Iterable[dict], *, errors: Iterable[str] = ()) -> JobFetchResult:
    errors = list(errors)
    return JobFetchResult(jobs, complete=not errors, errors=errors)


class _TextExtractor(HTMLParser):
    _BREAK_TAGS = {"br", "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value or ""))
    except Exception:
        return re.sub(r"<[^>]+>", " ", html.unescape(value or "")).strip()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _section(description_html: str, names: tuple[str, ...]) -> str:
    """Best-effort extraction that never replaces the preserved full JD."""
    labels = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"<h[1-6][^>]*>\s*(?:{labels})\s*</h[1-6]>(.*?)(?=<h[1-6][^>]*>|$)",
        html.unescape(description_html or ""), re.I | re.S,
    )
    return html_to_text(match.group(1)) if match else ""


def _joined(values: Any, key: str = "name") -> str:
    if not isinstance(values, list):
        return ""
    return ", ".join(
        str(item.get(key) if isinstance(item, Mapping) else item).strip()
        for item in values
        if (item.get(key) if isinstance(item, Mapping) else item)
    )


def _remote_confident(*, explicit_remote: bool = False, workplace: str = "",
                      location: str = "", title: str = "", description: str = "") -> bool:
    """Conservative remote gate: a negative mode always wins."""
    mode_blob = f"{workplace} {location} {title}".strip()
    if _NOT_REMOTE.search(mode_blob) or _NOT_REMOTE.search(description or ""):
        return False
    workplace_norm = re.sub(r"[^a-z]", "", workplace.lower())
    if explicit_remote or workplace_norm in {"remote", "fullyremote", "remotefirst"}:
        return True
    if _REMOTE_LOCATION.search(location.strip()) or _REMOTE_TITLE.search(title.strip()):
        return True
    # JD-only admission requires an especially strong phrase and no conflicting mode.
    return bool(_REMOTE_STRONG.search(description or ""))


def _stable_id(value: Any, *urls: str) -> str:
    direct = str(value or "").strip()
    if direct:
        return direct
    for url in urls:
        clean = str(url or "").split("?", 1)[0].rstrip("/")
        if clean:
            tail = clean.rsplit("/", 1)[-1]
            if tail.lower() not in {"apply", "application"}:
                return tail
    seed = "|".join(str(url or "") for url in urls)
    return hashlib.sha256(seed.encode()).hexdigest()[:24] if seed else ""


def _compensation(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        if isinstance(data, str) and data.strip():
            return {"min": None, "max": None, "currency": "", "interval": "", "text": data.strip()}
        return {"min": None, "max": None, "currency": "", "interval": "", "text": ""}

    def first(*keys: str) -> Any:
        for key in keys:
            if data.get(key) is not None:
                return data[key]
        return None

    return {
        "min": first("min", "minimum", "minValue", "minAmount", "salary_min"),
        "max": first("max", "maximum", "maxValue", "maxAmount", "salary_max"),
        "currency": str(first("currency", "currencyCode") or ""),
        "interval": str(first("interval", "period", "unit", "timeUnit") or ""),
        "text": str(first("text", "summary", "compensationTierSummary", "display") or ""),
    }


def _base(*, ats: str, company_id: Any, slug: str, source_job_id: Any, title: str,
          department: str, location: str, employment_type: str, description_html: str,
          description_plain: str, apply_url: str, job_url: str, posted_at: Any,
          updated_at: Any, compensation: Any, questions: list[dict], raw_payload: Any,
          questions_state: str = "not_available") -> dict:
    requirements = _section(description_html, ("requirements", "qualifications", "what you'll need", "you have"))
    benefits = _section(description_html, ("benefits", "perks", "what we offer", "our benefits"))
    comp = _compensation(compensation)
    return {
        "company_id": company_id,
        "ats": ats,
        "company_key": slug,
        "source": ats,
        "source_job_id": str(source_job_id or ""),
        "title": title or "",
        "department": department or "",
        "location_raw": location or "",
        "locations": [location] if location else [],
        "country": "",
        "state": "",
        "city": "",
        "remote_type": "remote",
        "is_remote": True,
        "employment_type": employment_type or "",
        "salary_min": comp["min"],
        "salary_max": comp["max"],
        "currency": comp["currency"],
        "salary_interval": comp["interval"],
        "compensation_text": comp["text"],
        "description": description_plain or html_to_text(description_html),
        "description_html": description_html or "",
        "requirements": requirements,
        "benefits": benefits,
        "apply_url": apply_url or job_url or "",
        "job_url": job_url or apply_url or "",
        "posted_at": posted_at,
        "updated_at": updated_at,
        "questions": questions,
        "question_count": len(questions),
        # ``available`` means the ATS explicitly supplied its complete public
        # questions field (which can legitimately be an empty list).
        "questions_state": questions_state,
        "raw_payload": raw_payload,
    }


def normalize_greenhouse_question(question: Mapping[str, Any]) -> dict[str, Any]:
    fields = []
    for field in question.get("fields") or []:
        fields.append({
            "name": field.get("name"),
            "label": field.get("label"),
            "type": field.get("type"),
            "required": field.get("required"),
            "values": [dict(value) if isinstance(value, Mapping) else value
                       for value in (field.get("values") or [])],
        })
    return {
        "id": question.get("id"),
        "label": html_to_text(str(question.get("label") or "")),
        "required": bool(question.get("required")),
        "fields": fields,
        "raw": dict(question),
    }


def _greenhouse_questions(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect every public GH question group, including compliance/demographic ones."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            if "label" in value and isinstance(value.get("fields"), list):
                marker = repr((value.get("id"), value.get("label"), value.get("fields")))
                if marker not in seen:
                    seen.add(marker)
                    item = normalize_greenhouse_question(value)
                    item["group"] = path
                    out.append(item)
                return
            for key, child in value.items():
                if key == "raw":
                    continue
                visit(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for child in value:
                visit(child, path)

    for key, value in detail.items():
        if key == "questions" or "question" in key or key in {"compliance", "demographic"}:
            visit(value, key)
    return out


def parse_greenhouse_jobs(board: Mapping[str, Any], company_id: Any, slug: str,
                          details: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict]:
    details = details or {}
    out = []
    for listing in board.get("jobs") or []:
        jid = str(listing.get("id") or "")
        detail = details.get(jid) or {}
        merged = {**listing, **detail}
        loc = str((merged.get("location") or {}).get("name") or "")
        offices = _joined(merged.get("offices"))
        description_html = html.unescape(str(merged.get("content") or ""))
        workplace = str(merged.get("workplace_type") or merged.get("workplaceType") or "")
        if not _remote_confident(
            explicit_remote=merged.get("is_remote") is True,
            workplace=workplace, location=f"{loc} {offices}".strip(),
            title=str(merged.get("title") or ""), description=html_to_text(description_html),
        ):
            continue
        questions = _greenhouse_questions(detail)
        pay = merged.get("pay_input_ranges") or merged.get("pay_ranges") or merged.get("compensation")
        if isinstance(pay, list):
            pay = pay[0] if pay else None
        out.append(_base(
            ats="greenhouse", company_id=company_id, slug=slug,
            source_job_id=_stable_id(jid, str(merged.get("absolute_url") or "")),
            title=str(merged.get("title") or ""), department=_joined(merged.get("departments")),
            location=loc, employment_type=str(merged.get("employment_type") or ""),
            description_html=description_html, description_plain=html_to_text(description_html),
            apply_url=str(merged.get("absolute_url") or ""), job_url=str(merged.get("absolute_url") or ""),
            posted_at=merged.get("first_published") or merged.get("created_at"),
            updated_at=merged.get("updated_at"), compensation=pay, questions=questions,
            questions_state="available" if detail else "not_fetched",
            raw_payload={"listing": dict(listing), "detail": dict(detail)},
        ))
    return out


def parse_lever_jobs(payload: Any, company_id: Any, slug: str) -> list[dict]:
    if not isinstance(payload, list):
        return []
    out = []
    for job in payload:
        cats = job.get("categories") or {}
        lists = job.get("lists") or []
        description_html = str(job.get("description") or job.get("descriptionBody") or "")
        section_html = "".join(
            f"<h3>{html.escape(str(item.get('text') or ''))}</h3>{item.get('content') or ''}"
            for item in lists if isinstance(item, Mapping)
        )
        additional = str(job.get("additional") or "")
        full_html = f"{description_html}{section_html}{additional}"
        location = str(cats.get("location") or "")
        workplace = str(job.get("workplaceType") or "")
        if not _remote_confident(explicit_remote=job.get("isRemote") is True,
                                 workplace=workplace, location=location,
                                 title=str(job.get("text") or ""), description=html_to_text(full_html)):
            continue
        row = _base(
            ats="lever", company_id=company_id, slug=slug,
            source_job_id=_stable_id(job.get("id"), str(job.get("hostedUrl") or ""), str(job.get("applyUrl") or "")),
            title=str(job.get("text") or ""),
            department=str(cats.get("department") or cats.get("team") or ""), location=location,
            employment_type=str(cats.get("commitment") or job.get("commitment") or ""),
            description_html=full_html,
            description_plain=str(job.get("descriptionPlain") or "") or html_to_text(full_html),
            apply_url=str(job.get("applyUrl") or ""), job_url=str(job.get("hostedUrl") or ""),
            posted_at=job.get("createdAt") or job.get("created_at"),
            updated_at=job.get("updatedAt") or job.get("updated_at"),
            compensation=job.get("salaryRange") or job.get("compensation"), questions=[],
            raw_payload=dict(job),
        )
        # Lever's named list blocks are more reliable than heading heuristics.
        for item in lists:
            name = str(item.get("text") or "").lower() if isinstance(item, Mapping) else ""
            value = html_to_text(str(item.get("content") or "")) if isinstance(item, Mapping) else ""
            if any(word in name for word in ("requirement", "qualification", "you have")):
                row["requirements"] = value
            if any(word in name for word in ("benefit", "perk", "we offer")):
                row["benefits"] = value
        out.append(row)
    return out


def parse_ashby_jobs(payload: Mapping[str, Any], company_id: Any, slug: str) -> list[dict]:
    out = []
    for job in payload.get("jobs") or []:
        description_html = str(job.get("descriptionHtml") or "")
        description_plain = str(job.get("descriptionPlain") or "") or html_to_text(description_html)
        location = str(job.get("location") or "")
        workplace = str(job.get("workplaceType") or "")
        if not _remote_confident(explicit_remote=job.get("isRemote") is True,
                                 workplace=workplace, location=location,
                                 title=str(job.get("title") or ""), description=description_plain):
            continue
        questions = job.get("applicationForm") or job.get("applicationFormQuestions") or []
        if not isinstance(questions, list):
            questions = [questions]
        row = _base(
            ats="ashby", company_id=company_id, slug=slug, source_job_id=job.get("id"),
            title=str(job.get("title") or ""), department=str(job.get("department") or ""),
            location=location, employment_type=str(job.get("employmentType") or job.get("employmentTypeLabel") or ""),
            description_html=description_html, description_plain=description_plain,
            apply_url=str(job.get("applyUrl") or job.get("jobUrl") or ""),
            job_url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
            posted_at=job.get("publishedAt") or job.get("publishedDate"),
            updated_at=job.get("updatedAt"),
            compensation=job.get("compensation") or job.get("compensationTierSummary"),
            questions=[dict(q) if isinstance(q, Mapping) else {"value": q} for q in questions],
            questions_state="available" if ("applicationForm" in job or "applicationFormQuestions" in job)
            else "not_available",
            raw_payload=dict(job),
        )
        row["source_job_id"] = _stable_id(job.get("id"), row["job_url"], row["apply_url"])
        extra_locations = job.get("secondaryLocations") or []
        if isinstance(extra_locations, list):
            row["locations"] = list(dict.fromkeys(
                [location] + [str(x.get("location") or x.get("name") or "") if isinstance(x, Mapping) else str(x)
                              for x in extra_locations]
            ))
            row["locations"] = [x for x in row["locations"] if x]
        out.append(row)
    return out


def parse_workable_jobs(payload: Mapping[str, Any], company_id: Any, slug: str) -> list[dict]:
    out = []
    for job in payload.get("jobs") or []:
        loc = job.get("location") or {}
        location = str(loc.get("location_str") or ", ".join(
            str(loc.get(k) or "") for k in ("city", "region", "country") if loc.get(k)
        ))
        description_html = str(job.get("description") or job.get("description_html") or "")
        workplace = str(job.get("workplace_type") or job.get("workplaceType") or "")
        if not _remote_confident(explicit_remote=job.get("telecommuting") is True or job.get("remote") is True,
                                 workplace=workplace, location=location,
                                 title=str(job.get("title") or ""), description=html_to_text(description_html)):
            continue
        questions = job.get("questions") or []
        if not isinstance(questions, list):
            questions = [questions]
        row = _base(
            ats="workable", company_id=company_id, slug=slug,
            source_job_id=_stable_id(job.get("shortcode") or job.get("id"),
                                     str(job.get("url") or job.get("application_url") or "")),
            title=str(job.get("title") or ""),
            department=str(job.get("department") or ""), location=location,
            employment_type=str(job.get("employment_type") or job.get("employmentType") or ""),
            description_html=description_html, description_plain=html_to_text(description_html),
            apply_url=str(job.get("url") or job.get("application_url") or ""),
            job_url=str(job.get("url") or ""), posted_at=job.get("published_on") or job.get("created_at"),
            updated_at=job.get("updated_at"), compensation=job.get("salary") or job.get("compensation"),
            questions=[dict(q) if isinstance(q, Mapping) else {"value": q} for q in questions],
            questions_state="available" if "questions" in job else "not_available",
            raw_payload=dict(job),
        )
        row.update({"country": str(loc.get("country") or ""),
                    "state": str(loc.get("region") or loc.get("state") or ""),
                    "city": str(loc.get("city") or "")})
        out.append(row)
    return out


def parse_smartrecruiters_jobs(payload: Any, company_id: Any, slug: str) -> list[dict]:
    """Normalize SmartRecruiters posting details (or list responses in tests/replays)."""
    jobs = payload.get("content") or payload.get("jobs") or [] if isinstance(payload, Mapping) else payload
    if not isinstance(jobs, list):
        return []
    out = []
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        location_data = job.get("location") or {}
        if not isinstance(location_data, Mapping):
            location_data = {}
        location = str(location_data.get("fullLocation") or ", ".join(
            str(location_data.get(key) or "")
            for key in ("city", "region", "country") if location_data.get(key)
        ))
        sections = ((job.get("jobAd") or {}).get("sections") or {})
        if not isinstance(sections, Mapping):
            sections = {}
        section_names = (
            "companyDescription", "jobDescription", "qualifications", "additionalInformation"
        )
        description_html = "".join(
            f"<h2>{html.escape(str((sections.get(name) or {}).get('title') or name))}</h2>"
            f"{(sections.get(name) or {}).get('text') or ''}"
            for name in section_names if isinstance(sections.get(name), Mapping)
        ) or str(job.get("description") or job.get("descriptionHtml") or "")
        workplace = str(job.get("workplaceMode") or job.get("workplaceType") or "")
        if not _remote_confident(
            explicit_remote=location_data.get("remote") is True or job.get("remote") is True,
            workplace=workplace, location=location, title=str(job.get("name") or job.get("title") or ""),
            description=html_to_text(description_html),
        ):
            continue
        department = job.get("department") or {}
        employment = job.get("typeOfEmployment") or {}
        department_label = (department.get("label") or department.get("name") or "") \
            if isinstance(department, Mapping) else department
        employment_label = (employment.get("label") or employment.get("name") or "") \
            if isinstance(employment, Mapping) else employment
        job_id = job.get("id") or job.get("uuid") or job.get("ref")
        public_url = str(job.get("ref") or job.get("publicUrl") or "")
        if not public_url.startswith(("http://", "https://")) and job_id:
            public_url = f"https://jobs.smartrecruiters.com/{quote(slug, safe='')}/{quote(str(job_id), safe='')}"
        row = _base(
            ats="smartrecruiters", company_id=company_id, slug=slug,
            source_job_id=_stable_id(job_id, public_url),
            title=str(job.get("name") or job.get("title") or ""),
            department=str(department_label or ""), location=location,
            employment_type=str(employment_label or ""),
            description_html=description_html, description_plain=html_to_text(description_html),
            apply_url=str(job.get("applyUrl") or public_url), job_url=public_url,
            posted_at=job.get("releasedDate") or job.get("createdOn"),
            updated_at=job.get("updatedOn"), compensation=job.get("compensation") or job.get("salary"),
            questions=[], questions_state="not_available", raw_payload=dict(job),
        )
        row.update({"country": str(location_data.get("country") or ""),
                    "state": str(location_data.get("region") or ""),
                    "city": str(location_data.get("city") or "")})
        qualifications = sections.get("qualifications") or {}
        additional = sections.get("additionalInformation") or {}
        if isinstance(qualifications, Mapping):
            row["requirements"] = html_to_text(str(qualifications.get("text") or ""))
        if isinstance(additional, Mapping):
            extra_text = html_to_text(str(additional.get("text") or ""))
            if re.search(r"\bbenefits?|perks?\b", str(additional.get("title") or ""), re.I):
                row["benefits"] = extra_text
        out.append(row)
    return out


def parse_workday_jobs(payload: Any, company_id: Any, slug: str, *,
                       public_base_url: str = "") -> list[dict]:
    """Normalize Workday CXS job details while preserving the complete response."""
    jobs = payload.get("jobs") or payload.get("jobPostings") or [] if isinstance(payload, Mapping) else payload
    if not isinstance(jobs, list):
        return []
    out = []
    for raw in jobs:
        if not isinstance(raw, Mapping):
            continue
        info = raw.get("jobPostingInfo") if isinstance(raw.get("jobPostingInfo"), Mapping) else raw
        description_html = str(info.get("jobDescription") or info.get("description") or "")
        location = str(info.get("location") or info.get("locationsText") or raw.get("locationsText") or "")
        additional = info.get("additionalLocations") or []
        if isinstance(additional, list):
            extra_locations = [str(item.get("location") or item.get("name") or "")
                               if isinstance(item, Mapping) else str(item) for item in additional]
        else:
            extra_locations = []
        workplace = str(info.get("workplaceType") or info.get("workplaceMode") or "")
        explicit_remote = info.get("remoteType") in {"Remote", "REMOTE", "remote"} or info.get("remote") is True
        all_locations = ", ".join([location] + extra_locations)
        if not _remote_confident(
            explicit_remote=explicit_remote, workplace=workplace, location=all_locations,
            title=str(info.get("title") or raw.get("title") or ""), description=html_to_text(description_html),
        ):
            continue
        external_path = str(info.get("externalPath") or raw.get("externalPath") or "")
        external_url = str(info.get("externalUrl") or raw.get("externalUrl") or "")
        if not external_url and public_base_url and external_path:
            external_url = f"{public_base_url.rstrip('/')}/{external_path.lstrip('/')}"
        employment = info.get("timeType") or info.get("workerType") or info.get("jobSchedule") or ""
        if isinstance(employment, Mapping):
            employment = employment.get("title") or employment.get("label") or employment.get("name") or ""
        row = _base(
            ats="workday", company_id=company_id, slug=slug,
            source_job_id=_stable_id(info.get("id") or info.get("jobReqId"), external_path, external_url),
            title=str(info.get("title") or raw.get("title") or ""),
            department=str(info.get("jobFamily") or info.get("jobFamilyGroup") or ""),
            location=location, employment_type=str(employment),
            description_html=description_html, description_plain=html_to_text(description_html),
            apply_url=external_url, job_url=external_url,
            posted_at=info.get("postedOn") or raw.get("postedOn"),
            updated_at=info.get("updatedOn"),
            compensation=info.get("compensation") or info.get("salary"),
            questions=[], questions_state="not_available", raw_payload=dict(raw),
        )
        row["locations"] = [item for item in dict.fromkeys([location] + extra_locations) if item]
        out.append(row)
    return out


def _request_json(client: httpx.Client, url: str, *, retries: int = 3,
                  sleep: Callable[[float], None] = time.sleep, method: str = "GET",
                  json_body: Mapping[str, Any] | None = None) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = client.request(method, url, json=json_body, follow_redirects=True,
                                      headers={"User-Agent": USER_AGENT})
            if response.status_code in _RETRYABLE_STATUS and attempt + 1 < max(1, retries):
                retry_after = response.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.5 * (2 ** attempt)
                sleep(min(delay, 5.0))
                continue
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise JobSourceError(f"ATS response exceeds {MAX_RESPONSE_BYTES} bytes")
            return response.json()
        except (httpx.TransportError, httpx.TimeoutException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                sleep(min(0.5 * (2 ** attempt), 5.0))
                continue
            break
        except httpx.HTTPStatusError as exc:
            raise JobSourceError(f"ATS request failed with HTTP {exc.response.status_code}") from exc
    raise JobSourceError("ATS request failed after bounded retries") from last_error


def _fetch_greenhouse(slug: str, company_id: Any, client: httpx.Client,
                      retries: int) -> JobFetchResult:
    safe = quote(slug, safe="")
    board = _request_json(client, f"https://boards-api.greenhouse.io/v1/boards/{safe}/jobs?content=true",
                          retries=retries)
    details: dict[str, Mapping[str, Any]] = {}
    errors: list[str] = []
    # Fetch questions only for listings that can plausibly pass the strict gate.
    for job in board.get("jobs") or []:
        loc = str((job.get("location") or {}).get("name") or "")
        desc = html_to_text(str(job.get("content") or ""))
        if not _remote_confident(explicit_remote=job.get("is_remote") is True,
                                 workplace=str(job.get("workplace_type") or ""), location=loc,
                                 title=str(job.get("title") or ""), description=desc):
            continue
        jid = str(job.get("id") or "")
        if not jid:
            errors.append("remote job without id cannot be detailed")
            continue
        try:
            detail = _request_json(
                client,
                f"https://boards-api.greenhouse.io/v1/boards/{safe}/jobs/{quote(jid, safe='')}?questions=true",
                retries=retries,
            )
        except JobSourceError as exc:
            # A single deleted/restricted job must not discard the rest of a board.
            errors.append(f"job {jid} detail: {exc}")
            continue
        if isinstance(detail, Mapping):
            details[jid] = detail
        else:
            errors.append(f"job {jid} detail: invalid response")
    return _result(parse_greenhouse_jobs(board, company_id, slug, details), errors=errors)


def _fetch_smartrecruiters(slug: str, company_id: Any, client: httpx.Client,
                           retries: int) -> JobFetchResult:
    safe = quote(slug, safe="")
    listings: list[Mapping[str, Any]] = []
    offset = 0
    limit = 100
    seen_page_ids: set[tuple[str, ...]] = set()
    for _page_number in range(MAX_PAGES):
        page = _request_json(
            client,
            f"https://api.smartrecruiters.com/v1/companies/{safe}/postings?limit={limit}&offset={offset}",
            retries=retries,
        )
        content = page.get("content") or [] if isinstance(page, Mapping) else []
        if not isinstance(content, list):
            raise JobSourceError("SmartRecruiters returned an invalid postings page")
        page_rows = [item for item in content if isinstance(item, Mapping)]
        page_ids = tuple(str(item.get("id") or item.get("uuid") or item.get("ref") or "")
                         for item in page_rows)
        if content and (not page_rows or page_ids in seen_page_ids):
            raise JobSourceError("SmartRecruiters pagination made no progress")
        seen_page_ids.add(page_ids)
        listings.extend(page_rows)
        total = int(page.get("totalFound") or page.get("total") or len(listings))
        if not content or len(listings) >= total:
            break
        next_offset = offset + len(content)
        if next_offset <= offset:
            raise JobSourceError("SmartRecruiters pagination made no progress")
        offset = next_offset
    else:
        raise JobSourceError(f"SmartRecruiters pagination exceeded {MAX_PAGES} pages")

    details: list[Mapping[str, Any]] = []
    errors: list[str] = []
    for listing in listings:
        job_id = str(listing.get("id") or listing.get("uuid") or "")
        if not job_id:
            errors.append("listing without id cannot be detailed")
            continue
        try:
            detail = _request_json(
                client,
                f"https://api.smartrecruiters.com/v1/companies/{safe}/postings/{quote(job_id, safe='')}",
                retries=retries,
            )
        except JobSourceError as exc:
            # List rows do not contain the complete JD. Never persist one as complete.
            errors.append(f"job {job_id} detail: {exc}")
            continue
        if isinstance(detail, Mapping):
            details.append({**listing, **detail})
        else:
            errors.append(f"job {job_id} detail: invalid response")
    return _result(parse_smartrecruiters_jobs(details, company_id, slug), errors=errors)


def _workday_context(slug: str, ats_url: str | None) -> tuple[str, str, str]:
    """Return CXS base, public job base and site id from a verified Workday URL."""
    raw_url = str(ats_url or "").strip()
    if not raw_url:
        raise ValueError("Workday requires ats_url with the company's public career site")
    parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    host = (parsed.hostname or "").lower()
    if not (host.endswith(".myworkdayjobs.com") or host.endswith(".myworkdaysite.com")):
        raise ValueError("Workday ats_url must use a public myworkdayjobs.com or myworkdaysite.com host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[:2] == ["wday", "cxs"]:
        tenant, site = parts[2], parts[3]
    else:
        public_parts = [part for part in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", part)]
        if "job" in public_parts:
            public_parts = public_parts[:public_parts.index("job")]
        site = public_parts[0] if public_parts else ""
        tenant = slug
    if not tenant or not site:
        raise ValueError("Workday ats_url must include the public career site name")
    origin = f"https://{host}"
    return f"{origin}/wday/cxs/{quote(tenant, safe='')}/{quote(site, safe='')}", \
        f"{origin}/{quote(site, safe='')}", site


def _fetch_workday(slug: str, company_id: Any, ats_url: str | None,
                   client: httpx.Client, retries: int) -> JobFetchResult:
    cxs_base, public_base, _site = _workday_context(slug, ats_url)
    listings: list[Mapping[str, Any]] = []
    offset = 0
    limit = 20
    seen_page_ids: set[tuple[str, ...]] = set()
    for _page_number in range(MAX_PAGES):
        page = _request_json(
            client, f"{cxs_base}/jobs", retries=retries, method="POST",
            json_body={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
        )
        content = page.get("jobPostings") or page.get("jobs") or [] if isinstance(page, Mapping) else []
        if not isinstance(content, list):
            raise JobSourceError("Workday returned an invalid jobs page")
        page_rows = [item for item in content if isinstance(item, Mapping)]
        page_ids = tuple(str(item.get("externalPath") or item.get("id") or "")
                         for item in page_rows)
        if content and (not page_rows or page_ids in seen_page_ids):
            raise JobSourceError("Workday pagination made no progress")
        seen_page_ids.add(page_ids)
        listings.extend(page_rows)
        total = int(page.get("total") or len(listings))
        if not content or len(listings) >= total:
            break
        next_offset = offset + len(content)
        if next_offset <= offset:
            raise JobSourceError("Workday pagination made no progress")
        offset = next_offset
    else:
        raise JobSourceError(f"Workday pagination exceeded {MAX_PAGES} pages")

    details: list[Mapping[str, Any]] = []
    errors: list[str] = []
    for listing in listings:
        external_path = str(listing.get("externalPath") or "")
        if not external_path:
            errors.append("listing without externalPath cannot be detailed")
            continue
        try:
            detail = _request_json(client, f"{cxs_base}/{external_path.lstrip('/')}", retries=retries)
        except JobSourceError as exc:
            # Workday list rows omit the full JD; retry it in the next scan instead.
            errors.append(f"job {external_path} detail: {exc}")
            continue
        if isinstance(detail, Mapping):
            # The list's externalPath is sometimes omitted from jobPostingInfo.
            info = detail.get("jobPostingInfo")
            if isinstance(info, Mapping):
                detail = {**detail, "jobPostingInfo": {**listing, **info, "externalPath": external_path}}
            else:
                detail = {**listing, **detail, "externalPath": external_path}
            details.append(detail)
        else:
            errors.append(f"job {external_path} detail: invalid response")
    return _result(
        parse_workday_jobs(details, company_id, slug, public_base_url=public_base),
        errors=errors,
    )


def fetch_remote_jobs(ats: str, slug: str, company_id: Any = None, *,
                      client: httpx.Client | None = None, retries: int = 3,
                      ats_url: str | None = None) -> JobFetchResult:
    """Fetch remote jobs plus whether the board was complete enough for closures."""
    ats_key = (ats or "").strip().lower()
    slug = (slug or "").strip()
    if ats_key not in SUPPORTED_ATS:
        raise ValueError(f"unsupported ATS {ats!r}; expected one of {', '.join(SUPPORTED_ATS)}")
    if not slug:
        raise ValueError("ATS slug is required")
    owned = client is None
    if client is None:
        client = httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        safe = quote(slug, safe="")
        if ats_key == "greenhouse":
            return _fetch_greenhouse(slug, company_id, client, retries)
        if ats_key == "lever":
            payload = _request_json(client, f"https://api.lever.co/v0/postings/{safe}?mode=json", retries=retries)
            return _result(parse_lever_jobs(payload, company_id, slug))
        if ats_key == "ashby":
            payload = _request_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{safe}?includeCompensation=true",
                                    retries=retries)
            return _result(parse_ashby_jobs(payload, company_id, slug))
        if ats_key == "workable":
            payload = _request_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{safe}?details=true",
                                    retries=retries)
            return _result(parse_workable_jobs(payload, company_id, slug))
        if ats_key == "smartrecruiters":
            return _fetch_smartrecruiters(slug, company_id, client, retries)
        return _fetch_workday(slug, company_id, ats_url, client, retries)
    finally:
        if owned:
            client.close()


__all__ = [
    "SUPPORTED_ATS", "JobSourceError", "JobFetchResult", "fetch_remote_jobs", "html_to_text",
    "normalize_greenhouse_question", "parse_greenhouse_jobs", "parse_lever_jobs",
    "parse_ashby_jobs", "parse_workable_jobs", "parse_smartrecruiters_jobs", "parse_workday_jobs",
]
