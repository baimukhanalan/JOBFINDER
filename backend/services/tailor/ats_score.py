"""Auditable, deterministic ATS score for a structured résumé vs. a job description.

No external dependencies, no LLM. Every sub-score is exposed in ``breakdown`` so a
human can see exactly *why* the number is what it is (idea borrowed from the
resume-tailor-plugin project: github.com/olegvg/resume-tailor-plugin).

Weighted formula (sums to 1.0)::

    score = 0.45 * required_skills_coverage
          + 0.25 * keyword_match
          + 0.15 * title_alignment
          + 0.15 * section_completeness

- ``required_skills_coverage`` — share of the JD's *required* skills found in the
  résumé. Required skills are terms that appear >= 2x in the JD or that show up in a
  "requirements" / "must have" context.
- ``keyword_match`` — share of all extracted JD keywords present in the résumé.
- ``title_alignment`` — overlap between the JD title/headline and the résumé's
  preferred titles + headline.
- ``section_completeness`` — how many of the standard ATS sections the résumé fills
  (summary, experience, skills, education, contact info).
"""
import re

from backend.services.tailor import keywords as kw
from backend.services.tailor import render

# Sub-score weights — must sum to 1.0.
W_REQUIRED = 0.45
W_KEYWORDS = 0.25
W_TITLE = 0.15
W_SECTIONS = 0.15

# Lines/sentences that signal a hard requirement rather than a "nice to have".
# "what makes you a strong fit" is Salmon's own must-have section (present in ~half
# the JDs) — its keywords are exactly what the candidate is scored on, so it counts.
_REQ_CONTEXT = re.compile(
    r"(require|required|must[\s-]?have|must:|qualifications|you have|you'?ll need|"
    r"minimum|at least|proficien|essential|mandatory|strong fit|makes you a strong)",
    re.IGNORECASE,
)
# Words we never treat as a "required skill" on their own (too generic to score on).
# Single source of truth lives in the keyword extractor (merged into its _STOP too).
_GENERIC = kw._GENERIC


def _norm(text: str) -> str:
    return (text or "").lower()


def _resume_text(resume_dict: dict) -> str:
    """Canonical plain-text view of the résumé (same one an ATS would parse)."""
    try:
        return _norm(render.render_text(resume_dict))
    except Exception:
        # Defensive fallback if the dict is missing fields render_text expects.
        parts = [resume_dict.get("summary", ""), resume_dict.get("headline", "")]
        for e in resume_dict.get("experience", []) or []:
            parts.append(e.get("title", ""))
            parts.extend(e.get("bullets", []) or [])
        for items in (resume_dict.get("skills_grouped", {}) or {}).values():
            parts.extend(items or [])
        parts.extend(resume_dict.get("certifications", []) or [])
        return _norm(" ".join(str(p) for p in parts))


def _split_required(jd_text: str, keywords: list[str]) -> list[str]:
    """Derive the JD's *required* skills from the extracted keyword list.

    A keyword is "required" if it appears >= 2 times anywhere in the JD, OR if it
    appears on/near a line that carries requirement-signalling language
    ("must have", "requirements", "proficient in", ...).
    """
    jd = _norm(jd_text)
    # Lines that establish a "requirements" context.
    req_lines = [ln for ln in re.split(r"[\n\r]+", jd) if _REQ_CONTEXT.search(ln)]
    req_blob = " ".join(req_lines)

    required: list[str] = []
    for term in keywords:
        t = _norm(term)
        if t in _GENERIC or len(t) < 3:
            continue
        # Count non-overlapping occurrences of the term in the whole JD.
        occurrences = len(re.findall(re.escape(t), jd))
        in_req_context = kw.term_present(t, req_blob)
        if occurrences >= 2 or in_req_context:
            if t not in required:
                required.append(t)
    # If nothing qualified (very short JD), fall back to the top keywords so the
    # 0.45-weighted component is never silently zero.
    if not required:
        required = [_norm(k) for k in keywords[:8] if _norm(k) not in _GENERIC]
    return required


def _coverage(terms: list[str], haystack: str) -> tuple[float, list[str], list[str]]:
    """Return (share_present, present_terms, missing_terms)."""
    if not terms:
        return 0.0, [], []
    present, missing = [], []
    for t in terms:
        (present if kw.term_present(t, haystack) else missing).append(t)
    return len(present) / len(terms), present, missing


def _title_alignment(jd_title: str, resume_dict: dict) -> float:
    """Word-overlap between the JD title and the résumé's titles/headline."""
    jt = set(re.findall(r"[a-z]+", _norm(jd_title)))
    jt -= _GENERIC
    if not jt:
        return 0.0
    cand = list(resume_dict.get("preferred_titles") or [])
    if resume_dict.get("headline"):
        cand.append(resume_dict["headline"])
    for e in resume_dict.get("experience", []) or []:
        if e.get("title"):
            cand.append(e["title"])
    best = 0.0
    for title in cand:
        words = set(re.findall(r"[a-z]+", _norm(title)))
        if not words:
            continue
        best = max(best, len(jt & words) / len(jt))
    return best


def _section_completeness(resume_dict: dict) -> tuple[float, list[str]]:
    """Fraction of standard ATS sections that are populated (and which are missing)."""
    checks = {
        "summary": bool(resume_dict.get("summary")),
        "experience": bool(resume_dict.get("experience")),
        "skills": bool(resume_dict.get("skills_grouped") or resume_dict.get("skills")),
        "education": bool(resume_dict.get("education")),
        "contact": bool(
            (resume_dict.get("personal_info") or {}).get("email")
            or (resume_dict.get("personal_info") or {}).get("phone")
        ),
    }
    present = sum(1 for ok in checks.values() if ok)
    missing = [name for name, ok in checks.items() if not ok]
    return present / len(checks), missing


def ats_score(jd_text: str, resume_dict: dict) -> dict:
    """Compute an auditable 0-100 ATS score for ``resume_dict`` against ``jd_text``.

    Returns a dict::

        {
          "score": int,                 # 0-100, weighted total
          "required_coverage": float,   # 0-1, share of required JD skills present
          "breakdown": {                # every sub-score + its weighted contribution
            "required_skills_coverage": {...},
            "keyword_match": {...},
            "title_alignment": {...},
            "section_completeness": {...},
          },
          "missing": [..],              # required skills NOT found in the résumé
        }
    """
    jd_text = jd_text or ""
    cv = _resume_text(resume_dict)

    # 1. JD keywords + required-skill subset.
    all_keywords = kw.extract_jd_keywords(jd_text)
    required = _split_required(jd_text, all_keywords)

    req_cov, req_present, req_missing = _coverage(required, cv)
    kw_cov, _, kw_missing = _coverage([_norm(k) for k in all_keywords], cv)
    title_score = _title_alignment(
        resume_dict.get("_jd_title") or _jd_title(jd_text), resume_dict
    )
    section_score, section_missing = _section_completeness(resume_dict)

    components = (
        (W_REQUIRED, req_cov),
        (W_KEYWORDS, kw_cov),
        (W_TITLE, title_score),
        (W_SECTIONS, section_score),
    )
    total = sum(weight * value for weight, value in components)
    score = max(0, min(100, round(total * 100)))

    breakdown = {
        "required_skills_coverage": {
            "weight": W_REQUIRED,
            "value": round(req_cov, 4),
            "contribution": round(W_REQUIRED * req_cov * 100, 2),
            "present": req_present,
            "missing": req_missing,
            "required_terms": required,
        },
        "keyword_match": {
            "weight": W_KEYWORDS,
            "value": round(kw_cov, 4),
            "contribution": round(W_KEYWORDS * kw_cov * 100, 2),
            "total_keywords": len(all_keywords),
            "missing": kw_missing,
        },
        "title_alignment": {
            "weight": W_TITLE,
            "value": round(title_score, 4),
            "contribution": round(W_TITLE * title_score * 100, 2),
        },
        "section_completeness": {
            "weight": W_SECTIONS,
            "value": round(section_score, 4),
            "contribution": round(W_SECTIONS * section_score * 100, 2),
            "missing_sections": section_missing,
        },
    }

    return {
        "score": score,
        "required_coverage": round(req_cov, 4),
        "breakdown": breakdown,
        "missing": req_missing,
    }


def _jd_title(jd_text: str) -> str:
    """Best-effort guess of the role title from a free-form JD (first salient line)."""
    for line in re.split(r"[\n\r]+", jd_text or ""):
        line = line.strip()
        if 3 <= len(line) <= 80 and re.search(r"[A-Za-z]", line):
            return line
    return ""
