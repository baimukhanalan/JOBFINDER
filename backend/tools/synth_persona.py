"""Synthetic demo candidate generator for the ETALON fill.

When a human clicks "Заполнить"/generate on the /catalog demo, we do NOT use a real
roster candidate — we invent a fresh, entirely FICTIONAL applicant tailored to the job
(a random name, a derived @takhet.com email, a plausible résumé) so the demo shows a
complete fill without ever exposing a real person. Real applications still use the real
roster via catalog_drafts.pick_candidate (this module is demo-only).

The persona's NATIONALITY matches the JOB'S COUNTRY (parsed from the location, falling back
to the region tag, else Kazakhstan) so that work-authorization answers are truthful and
consistent: a US-based role gets an American ("authorized in the US: yes"), a Netherlands
role gets a Dutch person, a Tbilisi role a Georgian, etc. — never a Kazakhstani in Almaty
claiming US work authorization. Built by the local LLM with a deterministic fallback so a
demo click never fails.
"""
from __future__ import annotations

import json
import random
import re

from backend.services.tailor.tailor import _llm_complete
from backend.tools.catalog_drafts import derive_email

# --- resolve the job's country --------------------------------------------------
# location keyword -> canonical country (first match wins; order matters for overlaps)
_LOC_COUNTRY = [
    (re.compile(r"(?i)\b(united states|u\.?s\.?a\.?|u\.?s\.?\b|america|remote[- ]?us)\b"), "United States"),
    (re.compile(r"(?i)\b(canada|canadian)\b"), "Canada"),
    (re.compile(r"(?i)\b(united kingdom|u\.?k\.?\b|england|scotland|wales|london)\b"), "United Kingdom"),
    (re.compile(r"(?i)\b(netherlands|holland|amsterdam)\b"), "Netherlands"),
    (re.compile(r"(?i)\b(germany|deutschland|berlin|munich)\b"), "Germany"),
    (re.compile(r"(?i)\b(ireland|dublin)\b"), "Ireland"),
    (re.compile(r"(?i)\b(france|paris)\b"), "France"),
    (re.compile(r"(?i)\b(spain|madrid|barcelona)\b"), "Spain"),
    (re.compile(r"(?i)\b(portugal|lisbon)\b"), "Portugal"),
    (re.compile(r"(?i)\b(poland|warsaw|krakow)\b"), "Poland"),
    (re.compile(r"(?i)\b(brazil|brasil|sao paulo|são paulo)\b"), "Brazil"),
    (re.compile(r"(?i)\b(mexico|méxico)\b"), "Mexico"),
    (re.compile(r"(?i)\b(argentina|buenos aires)\b"), "Argentina"),
    (re.compile(r"(?i)\b(india|bengaluru|bangalore|mumbai|delhi)\b"), "India"),
    (re.compile(r"(?i)\b(australia|sydney|melbourne)\b"), "Australia"),
    (re.compile(r"(?i)\b(singapore)\b"), "Singapore"),
    (re.compile(r"(?i)\b(ukraine|kyiv|kiev|lviv)\b"), "Ukraine"),
    (re.compile(r"(?i)\b(tbilisi)\b|\bgeorgia\b(?!,?\s*(?:us|usa|united states|atlanta))"), "Georgia"),
    (re.compile(r"(?i)\b(kazakhstan|almaty|astana|nur[- ]?sultan)\b"), "Kazakhstan"),
]
_REGION_COUNTRY = {"US": "United States", "CA": "Canada", "UK": "United Kingdom"}
_DEFAULT_COUNTRY = "Kazakhstan"       # rest-of-world default (the agency's own market)

_CITIZEN = {"United States": "U.S. Citizen", "United Kingdom": "British Citizen",
            "Canada": "Canadian Citizen"}

# deterministic fallback name banks + cities (only used if the LLM is unavailable)
_NAMES = {
    "United States": (["James", "Michael", "Emily", "Olivia", "Daniel", "Grace", "Ethan", "Ava"],
                      ["Carter", "Bennett", "Foster", "Hayes", "Brooks", "Parker", "Ellis"]),
    "Canada": (["Liam", "Owen", "Charlotte", "Nathan", "Zoe", "Juliette", "Aiden"],
               ["Tremblay", "Gagnon", "Roy", "Clarke", "MacKenzie", "Fortin", "Lavoie"]),
    "United Kingdom": (["Oliver", "Harry", "Amelia", "Isla", "George", "Freya", "Jack"],
                       ["Walker", "Wright", "Hughes", "Hall", "Green", "Baker", "Clarke"]),
    "Kazakhstan": (["Arman", "Dias", "Aibek", "Timur", "Dana", "Aigerim", "Nurlan", "Alina"],
                   ["Serikuly", "Zhaksybek", "Toleubek", "Amirkhan", "Beisenov", "Yesenov"]),
}
_GENERIC_NAMES = (["Alex", "Maria", "Daniel", "Sofia", "Adrian", "Elena", "Lucas", "Nina"],
                  ["Novak", "Silva", "Kovac", "Costa", "Popov", "Moreau", "Duarte"])
_CITIES = {
    "United States": ["Austin, TX", "Denver, CO", "Columbus, OH", "Seattle, WA"],
    "Canada": ["Toronto, ON", "Vancouver, BC", "Ottawa, ON", "Calgary, AB"],
    "United Kingdom": ["London", "Manchester", "Bristol", "Leeds"],
    "Kazakhstan": ["Almaty", "Astana", "Shymkent", "Karaganda"],
}


def _country_of(job: dict) -> str:
    loc = job.get("location") or ""
    for rx, country in _LOC_COUNTRY:
        if rx.search(loc):
            return country
    regions = job.get("regions") or []
    for tag in ("US", "CA", "UK"):
        if tag in regions:
            return _REGION_COUNTRY[tag]
    return _DEFAULT_COUNTRY


def _citizen(country: str) -> str:
    return _CITIZEN.get(country, f"{country} Citizen")


def _extract_obj(text: str) -> dict | None:
    """First balanced {...} JSON object in an LLM reply (tolerates surrounding prose)."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _fictional_phone() -> str:
    """A reserved-fiction (555-01xx) number so the persona can never be submitted as real."""
    return f"+1 ({random.randint(200, 989)}) 555-0{random.randint(100, 199)}"


def _llm_persona(job: dict, country: str) -> dict | None:
    title = job.get("title", "")
    company = job.get("company", "")
    desc = re.sub(r"\s+", " ", (job.get("description") or "")).strip()[:1200]
    prompt = (
        f"Invent a REALISTIC but entirely FICTIONAL job applicant who is a citizen of and "
        f"resides in {country}, for the role below. This is synthetic demo data — do NOT use "
        f"any real, famous, or celebrity name; make up an ordinary {country} person with a "
        f"name and a city typical of {country}. Tailor the experience to the role.\n"
        f"Return ONLY a JSON object, no prose:\n"
        '{"full_name":"<common given + family name for ' + country + '>",'
        '"city":"<a city in ' + country + '>",'
        '"street_address":"<a plausible street address, number + street>",'
        '"phone":"<a ' + country + '-format phone number>",'
        '"years_experience":<int 4-12>,'
        '"headline":"<one-line professional headline>",'
        '"summary":"<2-sentence first-person professional summary>",'
        '"experience":[{"company":"<company>","title":"<title>","dates":"<e.g. 2021-Present>",'
        '"bullets":["<achievement>","<achievement>"]}],'
        '"education":[{"degree":"<e.g. BSc Computer Science>","school":"<university in ' + country + '>",'
        '"field":"<field>","year":"<YYYY>"}],'
        '"skills":["<skill>", "..."]}\n'
        f"Give 3 experience entries (most recent first) and 12-16 skills.\n\n"
        f"ROLE: {title} at {company}\nDESCRIPTION: {desc}")
    for _ in range(2):
        try:
            obj = _extract_obj(_llm_complete(prompt) or "")
        except Exception:
            obj = None
        if obj and obj.get("full_name") and obj.get("experience"):
            return obj
    return None


def _fallback_persona(job: dict, country: str) -> dict:
    first_bank, last_bank = _NAMES.get(country, _GENERIC_NAMES)
    cities = _CITIES.get(country, [country])
    title = job.get("title") or "Specialist"
    return {
        "full_name": f"{random.choice(first_bank)} {random.choice(last_bank)}",
        "city": random.choice(cities),
        "street_address": f"{random.randint(10, 990)} Main Street",
        "years_experience": random.randint(5, 10),
        "headline": title,
        "summary": (f"Experienced {title} with a track record of delivering results in "
                    f"remote, cross-functional teams."),
        "experience": [
            {"company": "Remote Solutions Inc.", "title": title, "dates": "2021-Present",
             "bullets": [f"Owned {title.lower()} workstreams for a distributed team.",
                         "Improved core delivery metrics through process ownership."]},
            {"company": "Global Services Ltd.", "title": f"Junior {title}", "dates": "2018-2021",
             "bullets": ["Supported day-to-day operations and customer outcomes."]},
        ],
        "education": [{"degree": "BSc Business Administration", "school": "State University",
                       "field": "Business", "year": "2017"}],
        "skills": ["communication", "remote collaboration", "problem solving",
                   "project management", "stakeholder management", "process improvement",
                   "data analysis", "documentation", "prioritization", "time management"],
    }


def _build_candidate(raw: dict, country: str, job: dict) -> dict:
    job_title = job.get("title", "") if job else (raw.get("headline") or "")
    name = str(raw.get("full_name") or "").strip()
    city = str(raw.get("city") or (_CITIES.get(country) or [country])[0]).strip().split(",")[0].strip()
    street = str(raw.get("street_address") or "").strip()
    try:
        yoe = int(raw.get("years_experience"))
    except (TypeError, ValueError):
        yoe = random.randint(5, 10)
    skills = [s for s in (raw.get("skills") or []) if isinstance(s, str)]
    exp = []
    for e in (raw.get("experience") or []):
        if isinstance(e, dict):
            exp.append({"company": e.get("company", ""), "title": e.get("title", ""),
                        "dates": e.get("dates", ""), "context": "",
                        "bullets": [b for b in (e.get("bullets") or []) if isinstance(b, str)]})
    edu = []
    for e in (raw.get("education") or []):
        if isinstance(e, dict):
            edu.append({"degree": e.get("degree", ""), "school": e.get("school", ""),
                        "field": e.get("field", ""), "year": str(e.get("year", "") or "")})
    email = derive_email(name)
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "candidate"
    pid = f"demo_{slug}"
    loc = f"{city}, {country}"
    phone = str(raw.get("phone") or "").strip() or _fictional_phone()
    resume = {
        "personal_info": {"name": name, "email": email, "phone": phone, "location": loc,
                          "address": street},
        "preferred_titles": [job_title], "headline": str(raw.get("headline") or job_title),
        "summary": str(raw.get("summary") or ""), "experience": exp,
        "skills_grouped": {"Skills": skills} if skills else {},
        "certifications": [], "education": edu,
    }
    profile = {
        "id": pid, "full_name": name, "email": email, "phone": phone,
        "location": loc, "city": city, "street_address": street, "country": country,
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "work_authorization": _citizen(country), "needs_sponsorship": "No",
        "years_experience": yoe, "is_synthetic": True, "is_sample": True, "resume": resume,
    }
    facts = {"salary_annual": None, "english_level": "Fluent",
             "education_level": "Bachelor's" if edu else "", "tools": skills[:10]}
    return {"profile": profile, "facts": facts}


def synth_persona(job: dict) -> dict:
    """A fresh, fictional demo candidate whose nationality matches the job's country
    (never a real roster person). LLM-authored with a deterministic fallback."""
    country = _country_of(job)
    raw = _llm_persona(job, country)
    if not (raw and str(raw.get("full_name") or "").strip()):
        raw = _fallback_persona(job, country)
    return _build_candidate(raw, country, job)
