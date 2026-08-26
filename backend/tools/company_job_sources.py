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
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import httpx

from backend.tools.company_enrichment import _get, extract_links


SUPPORTED_ATS = (
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday",
    "icims", "oracle", "successfactors", "eightfold", "custom",
)
USER_AGENT = "JobFinder-company-jobs/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 500
MAX_PUBLIC_BOARD_PAGES = 500
MAX_PUBLIC_JOB_DETAILS = 5_000
PUBLIC_DETAIL_WORKERS = 4
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
            posted_at=info.get("startDate") or info.get("postedOn") or raw.get("postedOn"),
            updated_at=info.get("updatedOn"),
            compensation=info.get("compensation") or info.get("salary"),
            questions=[], questions_state="not_available", raw_payload=dict(raw),
        )
        row["locations"] = [item for item in dict.fromkeys([location] + extra_locations) if item]
        out.append(row)
    return out


def parse_oracle_jobs(details: list[Mapping[str, Any]], company_id: Any, slug: str,
                      *, public_base_url: str) -> list[dict]:
    out = []
    for raw in details:
        listing = raw.get("_listing") if isinstance(raw.get("_listing"), Mapping) else {}
        description_html = str(raw.get("ExternalDescriptionStr") or "")
        location = str(raw.get("PrimaryLocation") or listing.get("PrimaryLocation") or "")
        secondary = listing.get("secondaryLocations") or []
        secondary_names = [str(item.get("Name") or "") for item in secondary
                           if isinstance(item, Mapping) and item.get("Name")]
        workplace = str(raw.get("WorkplaceType") or listing.get("WorkplaceType") or "")
        if not _remote_confident(
            explicit_remote="remote" in workplace.casefold(), workplace=workplace,
            location=", ".join([location] + secondary_names),
            title=str(raw.get("Title") or listing.get("Title") or ""),
            description=html_to_text(description_html),
        ):
            continue
        job_id = raw.get("Id") or listing.get("Id")
        job_url = f"{public_base_url.rstrip('/')}/job/{quote(str(job_id), safe='')}"
        row = _base(
            ats="oracle", company_id=company_id, slug=slug, source_job_id=job_id,
            title=str(raw.get("Title") or listing.get("Title") or ""),
            department=str(raw.get("Category") or raw.get("JobFunction")
                           or listing.get("JobFamily") or ""),
            location=location,
            employment_type=str(raw.get("JobSchedule") or raw.get("RequisitionType") or ""),
            description_html=description_html, description_plain=html_to_text(description_html),
            apply_url=job_url, job_url=job_url,
            posted_at=raw.get("ExternalPostedStartDate") or listing.get("PostedDate"),
            updated_at=listing.get("PostingEndDate"), compensation=raw.get("Compensation"),
            questions=[], questions_state="not_available", raw_payload=dict(raw),
        )
        row["locations"] = [item for item in dict.fromkeys([location] + secondary_names) if item]
        out.append(row)
    return out


class _JsonLdExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).lower(): str(value or "").lower() for key, value in attrs}
        if tag.lower() == "script" and "ld+json" in values.get("type", ""):
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(str(data))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capture:
            self.blocks.append("".join(self._parts))
            self._capture = False
            self._parts = []


def _jsonld_job_objects(value: Any) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        kind = value.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(item).lower() == "jobposting" for item in kinds):
            out.append(value)
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in value:
                out.extend(_jsonld_job_objects(value[key]))
    elif isinstance(value, list):
        for item in value:
            out.extend(_jsonld_job_objects(item))
    return out


def parse_structured_html_jobs(page_html: str, company_id: Any, slug: str, *,
                               ats: str, page_url: str) -> list[dict]:
    parser = _JsonLdExtractor()
    try:
        parser.feed(page_html or "")
    except Exception:
        return []
    objects: list[Mapping[str, Any]] = []
    for block in parser.blocks:
        try:
            objects.extend(_jsonld_job_objects(json.loads(block)))
        except (ValueError, TypeError):
            continue
    out = []
    for job in objects:
        title = str(job.get("title") or job.get("name") or "")
        description_html = str(job.get("description") or "")
        location_type = str(job.get("jobLocationType") or "")
        locations = job.get("jobLocation") or []
        if not isinstance(locations, list):
            locations = [locations]
        location_parts = []
        for location in locations:
            address = (location or {}).get("address") if isinstance(location, Mapping) else {}
            if isinstance(address, Mapping):
                location_parts.append(", ".join(str(address.get(key) or "") for key in
                                                ("addressLocality", "addressRegion", "addressCountry")
                                                if address.get(key)))
        location = "; ".join(part for part in location_parts if part)
        explicit_remote = "telecommute" in location_type.casefold()
        if not _remote_confident(explicit_remote=explicit_remote, workplace=location_type,
                                 location=location, title=title,
                                 description=html_to_text(description_html)):
            continue
        identifier = job.get("identifier") or ""
        if isinstance(identifier, Mapping):
            identifier = identifier.get("value") or identifier.get("name") or ""
        job_url = str(job.get("url") or page_url)
        employment = job.get("employmentType") or ""
        if isinstance(employment, list):
            employment = ", ".join(str(item) for item in employment)
        out.append(_base(
            ats=ats, company_id=company_id, slug=slug,
            source_job_id=_stable_id(identifier, job_url), title=title,
            department=str(job.get("occupationalCategory") or ""), location=location,
            employment_type=str(employment), description_html=description_html,
            description_plain=html_to_text(description_html),
            apply_url=job_url, job_url=job_url, posted_at=job.get("datePosted"),
            updated_at=job.get("validThrough"), compensation=job.get("baseSalary"),
            questions=[], questions_state="not_available", raw_payload=dict(job),
        ))
    return out


def _likely_job_detail(ats: str, url: str, board_url: str) -> bool:
    parsed = urlparse(url)
    board = urlparse(board_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    if ats == "icims":
        return bool(re.search(r"/jobs?/\d+(?:/|$)", path))
    if ats == "oracle":
        return "/job/" in path or "jobid=" in parsed.query.lower()
    if ats == "successfactors":
        return ("jobreqcareer" in path or "jobid=" in parsed.query.lower()
                or bool(re.search(r"/job/(?:[^/]+/)+\d+/?$", path)))
    if ats == "eightfold":
        return "/careers/job/" in path or "/jobs?/" in path
    return (parsed.hostname == board.hostname and bool(re.search(r"/jobs?/[^/]+", path))
            and url.rstrip("/") != board_url.rstrip("/"))


class _JobPageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.headings: list[str] = []
        self._capture = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "meta" and values.get("property", "").lower() in {
                "og:title", "twitter:title"} and values.get("content"):
            self.headings.append(values["content"])
        if tag.lower() in {"title", "h1"}:
            self._capture = tag.lower()
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture == tag.lower():
            value = " ".join(" ".join(self._parts).split())
            if value:
                (self.title if tag.lower() == "title" else self.headings).append(value)
            self._capture = ""
            self._parts = []


def parse_public_job_detail(page_html: str, company_id: Any, slug: str, *,
                            ats: str, page_url: str) -> dict | None:
    parser = _JobPageText()
    try:
        parser.feed(page_html or "")
    except Exception:
        return None
    plain = html_to_text(page_html)
    title = next((value for value in parser.headings if len(value) >= 3), "")
    if not title:
        title = next((re.split(r"\s+[|–—-]\s+", value, maxsplit=1)[0]
                      for value in parser.title if value), "")
    location_match = re.search(
        r"(?:^|\n)(?:location|locations|work location|job location)\s*[:\n]\s*([^\n]{2,160})",
        plain, re.I,
    )
    location = location_match.group(1).strip() if location_match else ""
    if not title or not _remote_confident(
            location=location, title=title, description=plain):
        return None
    parsed = urlparse(page_url)
    id_match = re.search(r"/(\d+)(?:/|$)", parsed.path)
    if not id_match:
        id_match = re.search(r"(?:jobid|jobId|jobreqid)=([^&]+)", parsed.query, re.I)
    posted_match = re.search(
        r"(?:^|\n)(?:date posted|posted|publish start date)\s*[:\n]\s*([^\n]{4,60})",
        plain, re.I,
    )
    return _base(
        ats=ats, company_id=company_id, slug=slug,
        source_job_id=_stable_id(id_match.group(1) if id_match else "", page_url),
        title=title, department="", location=location, employment_type="",
        description_html=page_html, description_plain=plain,
        apply_url=page_url, job_url=page_url,
        posted_at=posted_match.group(1).strip() if posted_match else None,
        updated_at=None, compensation=None, questions=[],
        questions_state="not_available",
        raw_payload={"page_url": page_url, "html": page_html},
    )


def _public_job_detail_recognized(page_html: str) -> bool:
    """Distinguish a non-remote job page from an unparseable/challenge page."""
    plain = html_to_text(page_html)
    if re.search(r"\b(?:access denied|verify you are human|captcha|security challenge|"
                 r"temporarily unavailable)\b", plain, re.I):
        return False
    parser = _JobPageText()
    try:
        parser.feed(page_html or "")
    except Exception:
        return False
    return any(len(value.strip()) >= 3 for value in parser.headings + parser.title)


def parse_eightfold_positions(payload: Any, company_id: Any, slug: str, *,
                              careers_url: str = "") -> list[dict]:
    """Normalize the authenticated Eightfold position-list response."""
    if isinstance(payload, Mapping):
        rows = payload.get("positions") or payload.get("results") or payload.get("data") or []
        if isinstance(rows, Mapping):
            rows = rows.get("positions") or rows.get("results") or rows.get("items") or []
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    out = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        ats_data = raw.get("atsData") if isinstance(raw.get("atsData"), Mapping) else {}
        description_html = str(
            raw.get("jobDescriptionHtml") or raw.get("descriptionHtml")
            or raw.get("jobDescription") or raw.get("description") or "")
        description_plain = html_to_text(description_html)
        locations = raw.get("locations") or raw.get("location") or []
        if not isinstance(locations, list):
            locations = [locations]
        location_names = []
        for location in locations:
            if isinstance(location, Mapping):
                value = location.get("name") or location.get("displayName") \
                    or location.get("formattedAddress") or location.get("location")
            else:
                value = location
            if value:
                location_names.append(str(value))
        location = "; ".join(dict.fromkeys(location_names))
        workplace = str(raw.get("workplaceType") or raw.get("workplaceMode")
                        or raw.get("workLocationType") or "")
        explicit_remote = raw.get("isRemote") is True or raw.get("remote") is True
        title = str(raw.get("name") or raw.get("title") or raw.get("positionName") or "")
        if not _remote_confident(explicit_remote=explicit_remote, workplace=workplace,
                                 location=location, title=title,
                                 description=description_plain):
            continue
        job_id = raw.get("positionId") or raw.get("id") or raw.get("atsPositionId") \
            or ats_data.get("atsPositionId") or ats_data.get("jobId")
        job_url = str(raw.get("positionUrl") or raw.get("jobUrl") or raw.get("url")
                      or ats_data.get("jobUrl") or ats_data.get("applyUrl") or careers_url)
        apply_url = str(raw.get("applyUrl") or ats_data.get("applyUrl") or job_url)
        row = _base(
            ats="eightfold", company_id=company_id, slug=slug,
            source_job_id=_stable_id(job_id, job_url, apply_url), title=title,
            department=str(raw.get("department") or raw.get("jobFamily") or ""),
            location=location,
            employment_type=str(raw.get("employmentType") or raw.get("positionType") or ""),
            description_html=description_html, description_plain=description_plain,
            apply_url=apply_url, job_url=job_url,
            posted_at=raw.get("postedAt") or raw.get("createdAt") or raw.get("startDate"),
            updated_at=raw.get("updatedAt") or raw.get("lastModifiedAt"),
            compensation=raw.get("compensation") or raw.get("salary"), questions=[],
            questions_state="not_available", raw_payload=dict(raw),
        )
        row["locations"] = location_names
        out.append(row)
    return out


def _public_board_url(ats: str, ats_url: str) -> str:
    parsed = urlparse(ats_url)
    if ats == "icims":
        return urlunparse(parsed._replace(
            path="/jobs/search", query=urlencode({"ss": "1", "in_iframe": "1"}),
            fragment=""))
    if ats == "successfactors" and "successfactors." not in (parsed.hostname or ""):
        return urlunparse(parsed._replace(
            path="/search/",
            query=urlencode({"q": "", "sortColumn": "referencedate",
                             "sortDirection": "desc"}), fragment=""))
    return ats_url


def _fetch_public_html_board(ats: str, slug: str, company_id: Any,
                             ats_url: str | None, client: httpx.Client) -> JobFetchResult:
    if not ats_url:
        raise ValueError(f"{ats} requires a verified public ats_url or careers_url")
    first_url = _public_board_url(ats, ats_url)
    first_parsed = urlparse(first_url)
    board_host = (first_parsed.hostname or "").casefold()
    board_path = first_parsed.path.rstrip("/").casefold() or "/"
    base_board_query = parse_qs(first_parsed.query, keep_blank_values=True)
    pending = [first_url]
    seen_pages: set[str] = set()
    detail_urls: list[str] = []
    errors: list[str] = []
    portal_label = (urlparse(first_url).hostname or "").split(".", 1)[0].casefold()
    if ats == "icims" and re.match(r"events?(?:-|$)", portal_label):
        # Event/microsite portals expose only a filtered subset and therefore can
        # contribute jobs but can never authorize absence-based closures.
        errors.append("iCIMS event portal is not an authoritative full job inventory")
    zero_confirmed = False
    while pending and len(seen_pages) < MAX_PUBLIC_BOARD_PAGES:
        page_url = pending.pop(0)
        if page_url in seen_pages:
            continue
        page = _get(client, page_url)
        if page is None:
            errors.append(f"job board page unavailable: {page_url}")
            continue
        final_url = str(page.url)
        seen_pages.add(page_url)
        page_text = html_to_text(page.text)
        zero_confirmed = zero_confirmed or bool(re.search(
            r"\b(?:0 jobs?|no (?:open )?(?:jobs|positions) (?:found|available))\b",
            page_text, re.I))
        for url, text in extract_links(page.text, final_url):
            if _likely_job_detail(ats, url, final_url):
                detail_urls.append(url)
                continue
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            same_board = ((parsed.hostname or "").casefold() == board_host
                          and (parsed.path.rstrip("/").casefold() or "/") == board_path)
            is_page = same_board and ((ats == "icims" and "pr" in query) or (
                ats == "successfactors" and "startrow" in query))
            if is_page:
                cursor_key = "pr" if ats == "icims" else "startrow"
                cursor_value = str((query.get(cursor_key) or [""])[0]).strip()
                if cursor_value.isdigit():
                    canonical_query = {
                        key: list(values) for key, values in base_board_query.items()
                        if key != cursor_key
                    }
                    canonical_query[cursor_key] = [cursor_value]
                    page_url = urlunparse(first_parsed._replace(
                        query=urlencode(canonical_query, doseq=True), fragment=""))
                    if page_url not in seen_pages and page_url not in pending:
                        pending.append(page_url)
    if pending:
        errors.append(
            f"{ats} pagination exceeded {MAX_PUBLIC_BOARD_PAGES} public board pages")
    jobs = []
    unique_detail_urls = list(dict.fromkeys(detail_urls))
    if len(unique_detail_urls) > MAX_PUBLIC_JOB_DETAILS:
        errors.append(
            f"{ats} job details exceeded bounded limit of {MAX_PUBLIC_JOB_DETAILS}")
    def fetch_detail(url: str) -> tuple[dict[str, Any] | None, str | None]:
        detail = _get(client, url)
        if detail is None:
            return None, f"job detail unavailable: {url}"
        job = parse_public_job_detail(
            detail.text, company_id, slug, ats=ats, page_url=str(detail.url))
        if job:
            return job, None
        if not _public_job_detail_recognized(detail.text):
            return None, f"unparseable job detail: {url}"
        return None, None

    bounded_urls = unique_detail_urls[:MAX_PUBLIC_JOB_DETAILS]
    if isinstance(client, httpx.Client) and len(bounded_urls) > 20:
        with ThreadPoolExecutor(max_workers=PUBLIC_DETAIL_WORKERS) as executor:
            detail_results = executor.map(fetch_detail, bounded_urls)
            for job, error in detail_results:
                if job:
                    jobs.append(job)
                if error:
                    errors.append(error)
    else:
        for url in bounded_urls:
            job, error = fetch_detail(url)
            if job:
                jobs.append(job)
            if error:
                errors.append(error)
    if not detail_urls and not zero_confirmed:
        errors.append("no public job-detail links or authoritative zero result detected")
    unique = {(job["source_job_id"], job["job_url"]): job for job in jobs}
    return _result(unique.values(), errors=errors)


def _fetch_structured_html(ats: str, slug: str, company_id: Any, ats_url: str | None,
                           client: httpx.Client) -> JobFetchResult:
    board_url = str(ats_url or "").strip()
    if not board_url:
        raise ValueError(f"{ats} requires a verified public ats_url or careers_url")
    board = _get(client, board_url)
    if board is None:
        raise JobSourceError(f"{ats} public careers page is unavailable")
    jobs = parse_structured_html_jobs(board.text, company_id, slug, ats=ats,
                                      page_url=str(board.url))
    links = [url for url, _text in extract_links(board.text, str(board.url))
             if _likely_job_detail(ats, url, str(board.url))]
    errors: list[str] = []
    seen_urls = {job["job_url"] for job in jobs}
    for url in list(dict.fromkeys(links))[:MAX_PAGES]:
        if url in seen_urls:
            continue
        detail = _get(client, url)
        if detail is None:
            errors.append(f"job detail unavailable: {url}")
            continue
        parsed = parse_structured_html_jobs(detail.text, company_id, slug, ats=ats,
                                            page_url=str(detail.url))
        jobs.extend(parsed)
        seen_urls.update(job["job_url"] for job in parsed)
    if not jobs and not links:
        errors.append("no public structured job feed or job-detail links detected")
    unique = {(job["source_job_id"], job["job_url"]): job for job in jobs}
    return _result(unique.values(), errors=errors)


def _request_json(client: httpx.Client, url: str, *, retries: int = 3,
                  sleep: Callable[[float], None] = time.sleep, method: str = "GET",
                  json_body: Mapping[str, Any] | None = None,
                  headers: Mapping[str, str] | None = None) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            request_headers = {"User-Agent": USER_AGENT, **dict(headers or {})}
            response = client.request(method, url, json=json_body, follow_redirects=True,
                                      headers=request_headers)
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
        public_parts = [part for part in parts
                        if not re.fullmatch(r"[a-z]{2}-[a-z]{2}", part, re.I)]
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
            # Workday search is server-side full text. Every job we can accept must
            # expose a strong remote signal, so querying "remote" avoids downloading
            # thousands of unrelated requisition details before the strict gate.
            json_body={"appliedFacets": {}, "limit": limit, "offset": offset,
                       "searchText": "remote"},
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


def _oracle_context(ats_url: str | None) -> tuple[str, str, str]:
    raw = str(ats_url or "").strip()
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.hostname.endswith("oraclecloud.com"):
        raise ValueError("Oracle requires a verified oraclecloud.com CandidateExperience URL")
    match = re.search(r"/hcmUI/CandidateExperience/(?:[a-z]{2}/)?sites/([^/]+)", parsed.path, re.I)
    if not match:
        raise ValueError("Oracle ats_url must include the public site number")
    origin = f"https://{parsed.hostname}"
    site = match.group(1)
    public_base = f"{origin}/hcmUI/CandidateExperience/en/sites/{quote(site, safe='')}"
    return origin, site, public_base


def _fetch_oracle(slug: str, company_id: Any, ats_url: str | None,
                  client: httpx.Client, retries: int) -> JobFetchResult:
    origin, site, public_base = _oracle_context(ats_url)
    endpoint = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    initial = _request_json(
        client,
        f"{endpoint}?onlyData=true&expand=workplaceTypesFacet,requisitionList.secondaryLocations"
        f"&finder=findReqs;siteNumber={quote(site, safe='')},limit=1,offset=0",
        retries=retries,
    )
    container = (initial.get("items") or [{}])[0] if isinstance(initial, Mapping) else {}
    workplace_facets = container.get("workplaceTypesFacet") or []
    remote_ids = [str(item.get("Id") or "") for item in workplace_facets
                  if isinstance(item, Mapping) and "remote" in str(item.get("Name") or "").casefold()]
    # A complete provider facet declaring only on-site jobs is authoritative zero.
    if workplace_facets and not remote_ids:
        return _result([])
    selected = remote_ids[0] if remote_ids else ""
    listings: list[Mapping[str, Any]] = []
    offset = 0
    limit = 50
    for _page in range(MAX_PAGES):
        selected_arg = f",selectedWorkplaceTypesFacet={quote(selected, safe='')}" if selected else ""
        page = _request_json(
            client,
            f"{endpoint}?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={quote(site, safe='')},limit={limit},offset={offset}"
            f"{selected_arg}", retries=retries,
        )
        value = (page.get("items") or [{}])[0] if isinstance(page, Mapping) else {}
        rows = value.get("requisitionList") or []
        listings.extend(item for item in rows if isinstance(item, Mapping))
        total = int(value.get("TotalJobsCount") or len(listings))
        if not rows or len(listings) >= total:
            break
        offset += len(rows)
    else:
        raise JobSourceError(f"Oracle pagination exceeded {MAX_PAGES} pages")
    details = []
    errors = []
    detail_endpoint = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    for listing in listings:
        job_id = str(listing.get("Id") or "")
        if not job_id:
            errors.append("Oracle listing without Id")
            continue
        try:
            detail = _request_json(client, f"{detail_endpoint}/{quote(job_id, safe='')}?onlyData=true",
                                   retries=retries)
        except JobSourceError as exc:
            errors.append(f"job {job_id} detail: {exc}")
            continue
        if isinstance(detail, Mapping):
            details.append({**detail, "_listing": dict(listing)})
    return _result(parse_oracle_jobs(details, company_id, slug, public_base_url=public_base),
                   errors=errors)


def _fetch_eightfold(slug: str, company_id: Any, ats_url: str | None,
                     client: httpx.Client, retries: int,
                     token: str | None = None) -> JobFetchResult:
    """Use Eightfold's documented OAuth API; never treat missing auth as zero jobs."""
    token = str(token or os.getenv("EIGHTFOLD_API_TOKEN") or "").strip()
    if not token:
        return _result([], errors=[
            "Eightfold requires EIGHTFOLD_API_TOKEN or eightfold_token; "
            "public PCS X pages are not an authoritative job feed",
        ])
    api_base = str(os.getenv("EIGHTFOLD_API_BASE")
                   or "https://apiv2.eightfold.ai").rstrip("/")
    limit = 100
    start = 0
    positions: list[Mapping[str, Any]] = []
    errors: list[str] = []
    for _page in range(MAX_PAGES):
        url = (f"{api_base}/api/v2/core/positions?start={start}&limit={limit}"
               "&include=atsData")
        try:
            payload = _request_json(
                client, url, retries=retries,
                headers={"Authorization": f"Bearer {token}"})
        except JobSourceError as exc:
            errors.append(str(exc))
            break
        if isinstance(payload, Mapping):
            rows = payload.get("positions") or payload.get("results") or payload.get("data") or []
            if isinstance(rows, Mapping):
                rows = rows.get("positions") or rows.get("results") or rows.get("items") or []
        else:
            rows = payload
        if not isinstance(rows, list):
            errors.append("Eightfold position response has an unsupported shape")
            break
        positions.extend(item for item in rows if isinstance(item, Mapping))
        if len(rows) < limit:
            break
        start += len(rows)
    else:
        errors.append(f"Eightfold pagination exceeded {MAX_PAGES} pages")
    jobs = parse_eightfold_positions(
        positions, company_id, slug, careers_url=str(ats_url or ""))
    return _result(jobs, errors=errors)


def fetch_remote_jobs(ats: str, slug: str, company_id: Any = None, *,
                      client: httpx.Client | None = None, retries: int = 3,
                      ats_url: str | None = None,
                      eightfold_token: str | None = None) -> JobFetchResult:
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
        if ats_key == "workday":
            return _fetch_workday(slug, company_id, ats_url, client, retries)
        if ats_key == "oracle":
            return _fetch_oracle(slug, company_id, ats_url, client, retries)
        if ats_key in {"icims", "successfactors"}:
            return _fetch_public_html_board(ats_key, slug, company_id, ats_url, client)
        if ats_key == "eightfold":
            return _fetch_eightfold(
                slug, company_id, ats_url, client, retries, eightfold_token)
        return _fetch_structured_html(ats_key, slug, company_id, ats_url, client)
    finally:
        if owned:
            client.close()


__all__ = [
    "SUPPORTED_ATS", "JobSourceError", "JobFetchResult", "fetch_remote_jobs", "html_to_text",
    "normalize_greenhouse_question", "parse_greenhouse_jobs", "parse_lever_jobs",
    "parse_ashby_jobs", "parse_workable_jobs", "parse_smartrecruiters_jobs", "parse_workday_jobs",
    "parse_oracle_jobs", "parse_structured_html_jobs", "parse_public_job_detail",
    "parse_eightfold_positions",
]
