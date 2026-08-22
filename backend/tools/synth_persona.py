"""Synthetic demo candidate generator for the ETALON fill.

When a human clicks "Заполнить"/generate on the /catalog demo, we do NOT use a real
roster candidate — we invent a fresh, entirely FICTIONAL applicant tailored to the job
(a region-appropriate random name, a derived @takhet.com email, and a plausible résumé)
so the demo shows a complete fill without ever exposing a real person. Real applications
still use the real roster via catalog_drafts.pick_candidate (this module is demo-only).

Region → country: a US-eligible job gets an American, CA gets a Canadian, everything
else (OTHER / UK-only / untagged — e.g. Salmon in Tbilisi) gets a Kazakhstani (the
agency's own market). The persona is built by the local LLM with a deterministic
name-bank + template résumé fallback so a click never fails.
"""
from __future__ import annotations

import json
import random
import re

from backend.services.tailor.tailor import _llm_complete
from backend.tools.catalog_drafts import derive_email

# region -> the country we synthesize for the demo
_REGIONS = {
    "US": {"country": "United States", "auth": "U.S. Citizen",
           "cities": ["Austin, TX", "Denver, CO", "Columbus, OH", "Raleigh, NC",
                      "Seattle, WA", "Nashville, TN"], "phone": "+1 (512) 555-0{n:03d}"},
    "CA": {"country": "Canada", "auth": "Canadian Citizen",
           "cities": ["Toronto, ON", "Vancouver, BC", "Ottawa, ON", "Calgary, AB",
                      "Montreal, QC"], "phone": "+1 (416) 555-0{n:03d}"},
    "KZ": {"country": "Kazakhstan", "auth": "Kazakhstan Citizen",
           "cities": ["Almaty", "Astana", "Shymkent", "Karaganda", "Aktobe"],
           "phone": "+7 (7{a:02d}) 555-0{n:03d}"},
}

# deterministic fallback name banks (fictional, common given/family names per market)
_NAMES = {
    "US": (["James", "Michael", "David", "Emily", "Olivia", "Sophia", "Daniel", "Grace",
            "Ethan", "Ava", "Noah", "Chloe"],
           ["Carter", "Bennett", "Foster", "Hayes", "Reed", "Brooks", "Parker", "Morgan",
            "Sullivan", "Ellis"]),
    "CA": (["Liam", "Owen", "Charlotte", "Amelie", "Nathan", "Zoe", "Xavier", "Juliette",
            "Aiden", "Camille"],
           ["Tremblay", "Gagnon", "Roy", "Bouchard", "Clarke", "MacKenzie", "Fortin",
            "Bergeron", "Cormier", "Lavoie"]),
    "KZ": (["Arman", "Dias", "Aibek", "Timur", "Yerlan", "Dana", "Aigerim", "Gulnaz",
            "Nurlan", "Alina", "Bekzat", "Zhanel"],
           ["Serikuly", "Zhaksybek", "Toleubek", "Nurlanov", "Sagyndyk", "Amirkhan",
            "Beisenov", "Kairat", "Yesenov", "Abenuly"]),
}


def _region_of(job: dict) -> str:
    regions = job.get("regions") or []
    if "US" in regions:
        return "US"
    if "CA" in regions:
        return "CA"
    return "KZ"          # OTHER / UK-only / untagged -> rest-of-world demo persona


def _extract_obj(text: str) -> dict | None:
    """First balanced {...} JSON object in an LLM reply (tolerates surrounding prose)."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _fictional_phone(region: str) -> str:
    r = _REGIONS[region]
    return r["phone"].format(n=random.randint(0, 199), a=random.randint(10, 79))


def _llm_persona(job: dict, region: str) -> dict | None:
    cfg = _REGIONS[region]
    title = job.get("title", "")
    company = job.get("company", "")
    desc = re.sub(r"\s+", " ", (job.get("description") or "")).strip()[:1200]
    prompt = (
        f"Invent a REALISTIC but entirely FICTIONAL job applicant based in {cfg['country']} "
        f"for the role below. This is synthetic demo data — do NOT use any real, famous, or "
        f"celebrity name; make up an ordinary {cfg['country']} person. Tailor the experience "
        f"to the role.\n"
        f"Return ONLY a JSON object, no prose:\n"
        '{"full_name":"<common given + family name>",'
        '"city":"<City in ' + cfg["country"] + '>",'
        '"years_experience":<int 4-12>,'
        '"headline":"<one-line professional headline>",'
        '"summary":"<2-sentence first-person professional summary>",'
        '"experience":[{"company":"<company>","title":"<title>","dates":"<e.g. 2021–Present>",'
        '"bullets":["<achievement>","<achievement>"]}],'
        '"education":[{"degree":"<e.g. BSc Computer Science>","school":"<university>",'
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


def _fallback_persona(job: dict, region: str) -> dict:
    cfg = _REGIONS[region]
    first_bank, last_bank = _NAMES[region]
    title = job.get("title") or "Specialist"
    name = f"{random.choice(first_bank)} {random.choice(last_bank)}"
    return {
        "full_name": name,
        "city": random.choice(cfg["cities"]),
        "years_experience": random.randint(5, 10),
        "headline": title,
        "summary": (f"Experienced {title} with a track record of delivering results in "
                    f"remote, cross-functional teams."),
        "experience": [
            {"company": "Remote Solutions Inc.", "title": title, "dates": "2021–Present",
             "bullets": [f"Owned {title.lower()} workstreams for a distributed team.",
                         "Improved core delivery metrics through process ownership."]},
            {"company": "Global Services Ltd.", "title": f"Junior {title}",
             "dates": "2018–2021",
             "bullets": ["Supported day-to-day operations and customer outcomes."]},
        ],
        "education": [{"degree": "BSc Business Administration",
                       "school": "State University", "field": "Business", "year": "2017"}],
        "skills": ["communication", "remote collaboration", "problem solving",
                   "project management", "stakeholder management", "process improvement",
                   "data analysis", "customer success", "documentation", "prioritization",
                   "cross-functional teamwork", "time management"],
    }


def _build_candidate(raw: dict, region: str, job: dict) -> dict:
    cfg = _REGIONS[region]
    job_title = job.get("title", "") if job else (raw.get("headline") or "")
    name = str(raw.get("full_name") or "").strip()
    city = str(raw.get("city") or random.choice(cfg["cities"])).strip()
    yoe = raw.get("years_experience")
    try:
        yoe = int(yoe)
    except (TypeError, ValueError):
        yoe = random.randint(5, 10)
    skills = [s for s in (raw.get("skills") or []) if isinstance(s, str)]
    exp = []
    for e in (raw.get("experience") or []):
        if not isinstance(e, dict):
            continue
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
    pid = f"demo_{region.lower()}_{slug}"
    # the LLM sometimes already puts the country in the city ("Ottawa, Canada") — don't
    # append it twice.
    loc = city if cfg["country"].lower() in city.lower() else f"{city}, {cfg['country']}"
    phone = _fictional_phone(region)
    resume = {
        "personal_info": {"name": name, "email": email, "phone": phone, "location": loc},
        "preferred_titles": [job_title],
        "headline": str(raw.get("headline") or job_title),
        "summary": str(raw.get("summary") or ""),
        "experience": exp,
        "skills_grouped": {"Skills": skills} if skills else {},
        "certifications": [],
        "education": edu,
    }
    profile = {
        "id": pid, "full_name": name, "email": email, "phone": phone,
        "location": loc, "city": city.split(",")[0].strip(),
        "country": cfg["country"], "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "work_authorization": cfg["auth"], "needs_sponsorship": "No",
        "years_experience": yoe, "is_synthetic": True, "is_sample": True,
        "resume": resume,
    }
    edu_level = "Bachelor's" if edu else ""
    facts = {"salary_annual": None, "english_level": "Fluent",
             "education_level": edu_level,
             "tools": skills[:10]}
    return {"profile": profile, "facts": facts}


def synth_persona(job: dict) -> dict:
    """A fresh, fictional demo candidate tailored to `job` (never a real roster person).
    LLM-authored with a deterministic fallback so a demo click never fails."""
    region = _region_of(job)
    raw = _llm_persona(job, region)
    if not (raw and str(raw.get("full_name") or "").strip()):
        raw = _fallback_persona(job, region)
    return _build_candidate(raw, region, job)
