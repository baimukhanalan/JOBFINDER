"""Résumé variants: route a job to one of the niche résumé variants in etalons/<profile>.json.

Each entry in backend/data/etalons/<profile>.json is one niche-tailored résumé *body*
(headline / summary / experience / skills / certs / education). The personal
identity (name, email, phone, location) is NOT stored there — it is injected from
the applying person's Profile at render time.

  categorize(title, description, profile_id) -> (niche_key, score)   pick the best-fit niche
  variant_for(job, profile)                  -> (niche_key, base_resume in tailor/render shape)

Classification is deterministic (keyword overlap, no API key) so it is fast,
free, and explainable.
"""
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from backend.services.tailor import keywords as kw

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ETALONS_DIR = PROJECT_ROOT / "backend" / "data" / "etalons"


_SAFE_ID = re.compile(r"^[a-z0-9_-]+$")


@lru_cache(maxsize=32)
def _load_raw_cached(profile_id: str, mtime: float) -> tuple:
    # mtime is part of the cache key: editing a person's etalons file invalidates
    # their entry without restarting long-lived processes (dashboard/copilot).
    path = ETALONS_DIR / f"{profile_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(data) if isinstance(data, list) else ()


def _load_raw(profile_id: str) -> tuple:
    """One person's etalon set. No shared fallback: a profile without their own file
    gets NO variants (they must never apply with someone else's work history)."""
    if not profile_id or not _SAFE_ID.match(profile_id):
        logger.warning("invalid profile_id %r — variants disabled", profile_id)
        return ()
    path = ETALONS_DIR / f"{profile_id}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        logger.debug("no etalons file for %r — variants disabled", profile_id)
        return ()
    try:
        return _load_raw_cached(profile_id, mtime)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("etalons %s unreadable (%s) — variants disabled", path.name, e)
        return ()


def list_niches(profile_id: str) -> list[dict]:
    """[{key, label, best_for}] for one profile's variants."""
    return [{"key": it.get("key"), "label": it.get("label", ""),
             "best_for": it.get("best_for", "")} for it in _load_raw(profile_id)]


def _skills_grouped(resume: dict) -> dict:
    """etalon `skills: [{group, items}]` -> render's `skills_grouped: {group: items}`."""
    out: dict[str, list] = {}
    for grp in resume.get("skills", []) or []:
        name = grp.get("group") or grp.get("name") or "Skills"
        out[name] = list(grp.get("items", []) or [])
    if not out and isinstance(resume.get("skills_grouped"), dict):
        out = dict(resume["skills_grouped"])
    return out


def _variant_text(item: dict) -> str:
    """All the niche-signalling text of one variant, for keyword matching."""
    r = item.get("resume", {})
    parts = [item.get("label", ""), item.get("best_for", ""),
             r.get("headline", ""), r.get("summary", "")]
    for e in r.get("experience", []):
        parts.append(e.get("title", ""))
        parts.append(e.get("company", ""))
    for items in _skills_grouped(r).values():
        parts.extend(items)
    parts.extend(r.get("certifications", []))
    return " ".join(parts)


_TITLE_BOOST = 3  # weight for a title word that hits a niche's own label/best_for


def _title_words(job_title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", (job_title or "").lower())
            if t not in kw._STOP}


def categorize(job_title: str, job_description: str = "", profile_id: str = "") -> tuple[str | None, int]:
    """Return (niche_key, score) of the best-fit résumé variant for a job.

    Two signals: (1) how many of the job's keywords appear anywhere in the
    variant, and (2) a stronger boost when a word from the job *title* hits the
    niche's own label/best_for — the canonical name of the niche.
    """
    items = _load_raw(profile_id)
    if not items:
        return None, 0
    jd_kw = kw.extract_jd_keywords(f"{job_title} {job_title} {job_title} {job_description}")
    title_words = _title_words(job_title)
    best_key, best_score = None, -1
    for it in items:
        score = kw.overlap(_variant_text(it), jd_kw)
        label_blob = f"{it.get('label','')} {it.get('best_for','')}".lower()
        score += _TITLE_BOOST * sum(1 for w in title_words if w in label_blob)
        if score > best_score:
            best_key, best_score = it.get("key"), score
    return best_key, max(best_score, 0)


def variant_years(resume: dict) -> str | None:
    """Years of experience stated in an etalon's headline/summary (they differ per niche).

    Used to keep the form's 'years of experience' field consistent with the résumé being
    submitted — otherwise one application shows e.g. resume "12+ years" but form "15".
    """
    for text in (resume.get("headline", ""), resume.get("summary", "")[:200]):
        m = re.search(r"(\d{1,2})\+?\s*(?:years|yrs)", text, re.I)
        if m:
            return m.group(1)
    return None


def _adapt(item: dict, profile) -> dict:
    """etalon body + the profile's real identity -> a base résumé tailor/render accept."""
    r = item.get("resume", {})
    pi = {
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "linkedin": profile.linkedin_url,
    }
    prof_resume = profile.resume or {}
    eligibility = prof_resume.get("eligibility") or profile.work_authorization or ""
    preferred = prof_resume.get("preferred_titles") or []
    if r.get("headline"):
        preferred = [r["headline"], *preferred]
    return {
        "personal_info": pi,
        "preferred_titles": preferred,
        "headline": r.get("headline", ""),
        "eligibility": eligibility,
        "summary": r.get("summary", ""),
        "experience": r.get("experience", []),
        "skills_grouped": _skills_grouped(r),
        "certifications": r.get("certifications", []),
        "education": r.get("education", []),
        "years_experience": variant_years(r) or "",
    }


def variant_for(job: dict, profile) -> tuple[str | None, dict | None]:
    """Pick the niche variant for a job and stamp it with the profile's identity.

    Returns (niche_key, base_resume) or (None, None) when this profile has no etalons.
    """
    key, score = categorize(job.get("title", ""), job.get("description", ""), profile.id)
    if not key:
        return None, None
    item = next((it for it in _load_raw(profile.id) if it.get("key") == key), None)
    if not item:
        return None, None
    logger.info("variant: %r -> %s (score=%s) [profile=%s]",
                job.get("title", ""), key, score, profile.id)
    return key, _adapt(item, profile)
