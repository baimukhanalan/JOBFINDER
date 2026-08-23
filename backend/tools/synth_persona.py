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
import os
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
    (re.compile(r"(?i)\b(serbia|belgrade)\b"), "Serbia"),
    (re.compile(r"(?i)\b(turkey|t[uü]rkiye|istanbul)\b"), "Turkey"),
    (re.compile(r"(?i)\b(armenia|yerevan)\b"), "Armenia"),
    (re.compile(r"(?i)\b(azerbaijan|baku)\b"), "Azerbaijan"),
    (re.compile(r"(?i)\b(kyrgyzstan|bishkek)\b"), "Kyrgyzstan"),
    (re.compile(r"(?i)\b(uzbekistan|tashkent)\b"), "Uzbekistan"),
    (re.compile(r"(?i)\b(tajikistan|dushanbe)\b"), "Tajikistan"),
    (re.compile(r"(?i)\b(tbilisi)\b|\bgeorgia\b(?!,?\s*(?:us|usa|united states|atlanta))"), "Georgia"),
    (re.compile(r"(?i)\b(kazakhstan|almaty|astana|nur[- ]?sultan)\b"), "Kazakhstan"),
]
_REGION_COUNTRY = {"US": "United States", "CA": "Canada", "UK": "United Kingdom"}
_DEFAULT_COUNTRY = "Kazakhstan"       # rest-of-world default (the agency's own market)

_CITIZEN = {"United States": "U.S. Citizen", "United Kingdom": "British Citizen",
            "Canada": "Canadian Citizen"}

# Name banks per country. These are OUR source of names now (not just the LLM fallback):
# `_pick_name` chooses first+last from here, avoiding recently-used names, and pins that name
# into the LLM prompt — the local LLM is stateless and otherwise collapses to the same handful
# of "favourite" names on every fill. Banks are large so repeats are rare even before history.
_NAMES = {
    "United States": (
        ["James", "Michael", "Emily", "Olivia", "Daniel", "Grace", "Ethan", "Ava", "Noah",
         "Sophia", "Mason", "Chloe", "Logan", "Lily", "Jackson", "Hannah", "Aiden", "Zoe",
         "Caleb", "Nora", "Owen", "Ruby", "Henry", "Claire", "Nathan", "Violet", "Isaac",
         "Stella", "Evan", "Aubrey", "Julian", "Naomi", "Adrian", "Paige", "Miles", "Vivian"],
        ["Carter", "Bennett", "Foster", "Hayes", "Brooks", "Parker", "Ellis", "Reed", "Coleman",
         "Sullivan", "Fleming", "Dawson", "Harrington", "Blake", "Morrison", "Sherwood",
         "Whitaker", "Preston", "Underwood", "Hollis", "Barrett", "Cross", "Mercer", "Vaughn",
         "Ashby", "Lang", "Sutton", "Pierce", "Rowe", "Kirby", "Nash", "Chase", "Donovan"]),
    "Canada": (
        ["Liam", "Owen", "Charlotte", "Nathan", "Zoe", "Juliette", "Aiden", "Emma", "William",
         "Chloe", "Samuel", "Camille", "Felix", "Rose", "Thomas", "Alice", "Gabriel", "Beatrice",
         "Simon", "Margot", "Adam", "Laurence", "Elliot", "Sadie"],
        ["Tremblay", "Gagnon", "Roy", "Clarke", "MacKenzie", "Fortin", "Lavoie", "Bergeron",
         "Cote", "Girard", "Morin", "Belanger", "Cardinal", "Beaulieu", "Fraser", "Leclerc",
         "Boucher", "Gauthier", "Pelletier", "Caron", "Simard", "Fontaine"]),
    "United Kingdom": (
        ["Oliver", "Harry", "Amelia", "Isla", "George", "Freya", "Jack", "Poppy", "Charlie",
         "Evie", "Thomas", "Florence", "Arthur", "Ivy", "Alfie", "Maisie", "Edward", "Rosie",
         "Reuben", "Elsie", "Toby", "Martha", "Louis", "Bonnie"],
        ["Walker", "Wright", "Hughes", "Hall", "Green", "Baker", "Clarke", "Turner", "Hutchinson",
         "Pearce", "Whitfield", "Redmond", "Ashworth", "Bramwell", "Fairfax", "Holloway",
         "Winterbourne", "Marsh", "Pembroke", "Radcliffe", "Thornton", "Ainsley"]),
    "Kazakhstan": (
        ["Arman", "Dias", "Aibek", "Timur", "Dana", "Aigerim", "Nurlan", "Alina", "Yerlan",
         "Aizhan", "Bekzat", "Madina", "Ruslan", "Zhanar", "Sanzhar", "Gulnar", "Adil", "Assel",
         "Daniyar", "Aisulu", "Kairat", "Malika", "Yernar", "Saltanat"],
        ["Serikuly", "Zhaksybek", "Toleubek", "Amirkhan", "Beisenov", "Yesenov", "Nurpeisov",
         "Sagatov", "Iskakov", "Bekova", "Omarov", "Kassymova", "Zhumabek", "Tulegenov",
         "Abenov", "Dosanova", "Kaliyev", "Seitkali", "Baibek", "Nurlanuly"]),
}
_GENERIC_NAMES = (
    ["Alex", "Maria", "Daniel", "Sofia", "Adrian", "Elena", "Lucas", "Nina", "David", "Clara",
     "Marco", "Ines", "Victor", "Lena", "Theo", "Anna", "Leon", "Mira", "Felix", "Sara"],
    ["Novak", "Silva", "Kovac", "Costa", "Popov", "Moreau", "Duarte", "Weiss", "Ferrari",
     "Andersen", "Halvorsen", "Bauer", "Marin", "Vidal", "Horvat", "Lindqvist", "Rossi",
     "Fischer", "Nagy", "Sorensen"])
_CITIES = {
    "United States": ["Austin, TX", "Denver, CO", "Columbus, OH", "Seattle, WA"],
    "Canada": ["Toronto, ON", "Vancouver, BC", "Ottawa, ON", "Calgary, AB"],
    "United Kingdom": ["London", "Manchester", "Bristol", "Leeds"],
    "Kazakhstan": ["Almaty", "Astana", "Shymkent", "Karaganda"],
}


def _country_of(job: dict) -> str:
    """The country to synthesize a persona for. When the location names SEVERAL countries
    (e.g. Salmon's 'Kazakhstan; Kyrgyzstan'), the rule is: if Kazakhstan is one of them use
    Kazakhstan (the agency's own market); otherwise use the FIRST country as it appears in
    the location text. Falls back to the region tag, then Kazakhstan."""
    loc = job.get("location") or ""
    found = []  # (position_in_text, country)
    for rx, country in _LOC_COUNTRY:
        m = rx.search(loc)
        if m:
            found.append((m.start(), country))
    if found:
        if any(c == "Kazakhstan" for _, c in found):
            return "Kazakhstan"
        found.sort(key=lambda t: t[0])   # else the first country named in the location string
        return found[0][1]
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


# --- name history (the local LLM is stateless; we keep the memory it lacks) ------------------
# A tiny rolling log of recently-invented demo names so a fresh fill doesn't keep showing the
# same person. Best-effort + gitignored (demo data); any failure never blocks a fill.
_USED_NAMES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "demo_used_names.json")
_USED_MAX = 400  # remember this many recent names before rolling off the oldest


def _load_used() -> list:
    try:
        with open(_USED_NAMES_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _remember_name(name: str) -> None:
    if not name:
        return
    try:
        used = _load_used()
        used.append(name.lower())
        used = used[-_USED_MAX:]
        tmp = _USED_NAMES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(used, f)
        os.replace(tmp, _USED_NAMES_PATH)  # atomic — no torn file under concurrent clicks
    except Exception:
        pass


def _pick_name(country: str) -> str:
    """A random 'First Last' for the country, avoiding names used in the recent history so
    consecutive fills don't show the same person. Falls back to a plain random pick if the
    whole (small) space is exhausted."""
    first_bank, last_bank = _NAMES.get(country, _GENERIC_NAMES)
    used = set(_load_used())
    for _ in range(20):
        full = f"{random.choice(first_bank)} {random.choice(last_bank)}"
        if full.lower() not in used:
            return full
    return f"{random.choice(first_bank)} {random.choice(last_bank)}"


def _llm_persona(job: dict, country: str, name: str = "") -> dict | None:
    title = job.get("title", "")
    company = job.get("company", "")
    desc = re.sub(r"\s+", " ", (job.get("description") or "")).strip()[:1200]
    # Pin the name we already chose (see `_pick_name`) so the résumé is authored around it and
    # the model can't drift back to its handful of favourite names.
    name_line = (f"The applicant's name is EXACTLY '{name}'. Use it verbatim as full_name; do "
                 f"NOT invent a different name.\n" if name else "")
    full_name_slot = ('"<given + family name for ' + country + '>"') if not name else f'"{name}"'
    prompt = (
        f"Invent a REALISTIC but entirely FICTIONAL job applicant who is a citizen of and "
        f"resides in {country}, for the role below. This is synthetic demo data — do NOT use "
        f"any real, famous, or celebrity name; make up an ordinary {country} person with a "
        f"city typical of {country}. Tailor the experience to the role.\n" + name_line +
        f"Return ONLY a JSON object, no prose:\n"
        '{"full_name":' + full_name_slot + ','
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


def _postal(country: str) -> str:
    """A plausible, country-appropriate postal/ZIP for the invented persona so a required
    'Zip Code' field fills with a real value instead of the LLM's 'Not provided' non-answer."""
    c = country or ""
    if c == "United States":
        return f"{random.randint(1000, 99999):05d}"
    if c == "Canada":
        L, D = "ABCEGHJKLMNPRSTVXY", "0123456789"
        return (random.choice(L) + random.choice(D) + random.choice(L) + " "
                + random.choice(D) + random.choice(L) + random.choice(D))
    if c == "United Kingdom":
        L = "ABDEFGHJLNPQRSTUWXYZ"
        return (random.choice(L) + random.choice(L) + str(random.randint(1, 20))
                + " " + str(random.randint(1, 9)) + random.choice(L) + random.choice(L))
    return f"{random.randint(1000, 999999)}"


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
    # every DEMO persona gets a numeric suffix -> a UNIQUE mailbox even when the LLM invents
    # the same common name for two jobs (first.last573@takhet.com). Real onboarding via /setup
    # keeps the clean first.last@ (derive_email is unchanged) — the number is demo-only.
    num = random.randint(100, 9999)
    base = derive_email(name)
    if base:
        local, _, dom = base.partition("@")
        email = f"{local}{num}@{dom}"
    else:
        email = ""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "candidate"
    pid = f"demo_{slug}{num}"
    loc = f"{city}, {country}"
    phone = str(raw.get("phone") or "").strip() or _fictional_phone()
    zipc = str(raw.get("zip_code") or raw.get("postal_code") or "").strip() or _postal(country)
    resume = {
        "personal_info": {"name": name, "email": email, "phone": phone, "location": loc,
                          "address": street, "zip_code": zipc},
        "preferred_titles": [job_title], "headline": str(raw.get("headline") or job_title),
        "summary": str(raw.get("summary") or ""), "experience": exp,
        "skills_grouped": {"Skills": skills} if skills else {},
        "certifications": [], "education": edu,
    }
    profile = {
        "id": pid, "full_name": name, "email": email, "phone": phone,
        "location": loc, "city": city, "street_address": street, "country": country,
        "zip_code": zipc,
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
    name = _pick_name(country)                 # OUR choice, history-avoided (not the LLM's)
    raw = _llm_persona(job, country, name)
    if not (raw and str(raw.get("full_name") or "").strip()):
        raw = _fallback_persona(job, country)
    raw["full_name"] = name                     # force the diverse name in BOTH paths
    _remember_name(name)
    return _build_candidate(raw, country, job)
