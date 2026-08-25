"""Fail-closed policy for the isolated company-remote application queue.

This module is pure: it reads no database, opens no browser and submits nothing.
It decides whether one *real* candidate may prepare an application and records the
human-review reasons that must remain visible before any live form is opened.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from backend.applier.profile_validator import validate_profile
from backend.applier.regions import classify_regions
from backend.services.tailor.ats_score import ats_score


_HUMAN_RE = re.compile(
    r"(?i)\b(gender|race|racial|ethnic|veteran|military|disabilit\w*|age|date of birth|"
    r"sexual orientation|pronoun|background check|consent|certif(?:y|ication)|"
    r"acknowledge|signature|salary|compensation|sponsorship|work authori[sz]ation|"
    r"video|audio|recording|passport|national id|medical|criminal)\b"
)
_NO_SPONSOR_RE = re.compile(
    r"(?i)(?:no|without)\s+(?:visa\s+)?sponsorship|not\s+(?:able|available)\s+to\s+sponsor"
)


def _profile_dict(profile: Any) -> dict:
    if isinstance(profile, dict):
        return dict(profile)
    return {key: getattr(profile, key) for key in (
        "id", "full_name", "email", "phone", "location", "city", "state",
        "country", "work_authorization", "needs_sponsorship", "resume", "mailbox",
        "is_sample", "is_synthetic",
    ) if hasattr(profile, key)}


def _region_for_country(country: str) -> str | None:
    value = re.sub(r"[^a-z]", "", (country or "").casefold())
    if value in {"us", "usa", "unitedstates", "unitedstatesofamerica"}:
        return "US"
    if value == "canada":
        return "CA"
    return None


def revalidation_hash(job: dict, profile: Any, facts: dict) -> str:
    p = _profile_dict(profile)
    payload = {
        "job_content_hash": job.get("content_hash"),
        "question_set_hash": job.get("question_set_hash"),
        "apply_url": job.get("apply_url"),
        "profile": {key: p.get(key) for key in sorted(p) if key != "resume"},
        "resume": p.get("resume") or {},
        "facts": facts or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def evaluate(job: dict, profile: Any, facts: dict, *, min_fit: float = 35.0) -> dict:
    """Return a complete, auditable decision. Any uncertainty blocks preparation."""
    p = _profile_dict(profile)
    blocking: list[str] = []
    review: list[str] = []

    if p.get("is_sample") or p.get("id") == "sample":
        blocking.append("sample profile is forbidden")
    if p.get("is_synthetic"):
        blocking.append("synthetic profile is forbidden")
    blocking.extend(validate_profile(p))
    if not p.get("resume"):
        blocking.append("real resume is missing")
    if not facts:
        blocking.append("candidate facts are missing")
    email = str(p.get("email") or "").casefold()
    if not (p.get("mailbox") or email.endswith("@takhet.com")):
        blocking.append("verified recruiter reply route is missing")

    url = str(job.get("apply_url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        blocking.append("apply URL is not verified HTTPS")
    if job.get("status") != "active" or job.get("remote_type") != "remote":
        blocking.append("job is not an active confirmed-remote posting")
    if job.get("questions_status") != "success":
        blocking.append("complete application questions are unavailable")

    region_job = {
        "title": job.get("title") or "",
        "location": job.get("location_raw") or job.get("location_normalized") or "",
        "description": job.get("description") or "",
    }
    regions = classify_regions(region_job)
    candidate_region = _region_for_country(str(p.get("country") or ""))
    if not regions:
        blocking.append("job geography is unknown")
    elif not candidate_region:
        blocking.append("candidate country is outside the supported US/CA policy")
    elif candidate_region not in regions:
        blocking.append(f"candidate region {candidate_region} is not eligible")

    needs_sponsorship = str(p.get("needs_sponsorship") or "").strip().casefold()
    if needs_sponsorship in {"yes", "true", "1"} and _NO_SPONSOR_RE.search(
            str(job.get("description") or "")):
        blocking.append("job forbids sponsorship required by candidate")

    fit_score = None
    if p.get("resume") and (job.get("description") or job.get("title")):
        try:
            score_input = dict(p["resume"])
            score_input["_jd_title"] = job.get("title") or ""
            score = ats_score(str(job.get("description") or ""), score_input)
            fit_score = float(score["score"])
            if fit_score < float(min_fit):
                blocking.append(f"honest fit score {fit_score:g} is below {float(min_fit):g}")
        except Exception as exc:
            blocking.append(f"fit scoring failed: {type(exc).__name__}")
    else:
        blocking.append("job description is missing")

    questions = job.get("questions") or []
    for question in questions:
        label = str(question.get("label") or "").strip()
        if not label:
            blocking.append("unlabelled application question exists")
        elif _HUMAN_RE.search(label):
            review.append(label)
    # Every external application remains a human decision even with zero sensitive fields.
    review.append("human approval is required before opening the live form")

    return {
        "allowed": not blocking,
        "blocking_reasons": list(dict.fromkeys(blocking)),
        "review_reasons": list(dict.fromkeys(review)),
        "regions": regions,
        "candidate_region": candidate_region,
        "fit_score": fit_score,
        "revalidation_hash": revalidation_hash(job, p, facts),
    }


__all__ = ["evaluate", "revalidation_hash"]
