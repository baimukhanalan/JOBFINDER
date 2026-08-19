"""Generate plausible *fictional* test profiles, now ROLE-MATCHED to Salmon's roles.

For TESTING the prefill pipeline. The engine fills a form up to the Submit button;
a human reviews and submits. Identity (name / phone / city / email) is synthesised
per country; the résumé BODY (skills, stack, tools, experience, education, certs)
comes from a real, JD-grounded ROLE ARCHETYPE in `backend/data/archetypes.json`, so
a customer-support candidate is never sent to an iOS role — the profile genuinely
belongs to the role family, and the honest ATS score reflects that.

Archetypes were designed from the live Salmon JDs (Requirements / "What makes you a
strong fit" sections). Employers and universities are REAL (Kazakhstan + foreign);
we never let anything invent a company or school.

Country-parameterised: default "kz" (Kazakhstan). Pass country="ph" for Philippines.
Role family is chosen from `role_title` (mapped to its family) or drawn weighted by
how many online Salmon roles each family covers.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHETYPE_FILE = ROOT / "backend" / "data" / "archetypes.json"

# --- role-family archetypes (JD-grounded) ---------------------------------------
_ARCH_LIST = json.loads(ARCHETYPE_FILE.read_text())
ARCHETYPES: dict[str, dict] = {a["family"]: a for a in _ARCH_LIST}

# Map any Salmon job title -> the archetype family that genuinely covers it.
# Order matters: first match wins (specific before generic).
_FAMILY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("qa", re.compile(r"\b(qa|aqa|test automation|automation engineer|fullstack qa|sdet)\b", re.I)),
    ("mobile-dev", re.compile(r"\b(ios|android|flutter|kotlin|mobile|swift)\b", re.I)),
    ("data", re.compile(r"\b(data analyst|data engineer|data scien\w*|scoring|analytics|machine learning|\bml\b|\bbi\b)\b", re.I)),
    ("analyst-ba", re.compile(r"\b(business analyst|system analyst|bpmn|technical product manager \(system)\b", re.I)),
    ("infra-sec", re.compile(r"\b(cloud|devops|database|cloudops|security|solutions architect|sre|platform)\b", re.I)),
    ("product", re.compile(r"\b(product manager|program manager|product owner)\b", re.I)),
    # fin-risk: SPECIFIC risk/finance phrases only. Bare "risk"/"financial"/"tax" used to
    # swallow unrelated titles — "Senior Software Engineer, …Third Party Risk" (an eng role)
    # and "Senior Accountant, Tax" (accounting, no family here) — routing them to fin-risk
    # where no candidate fits, wasting a slot. Now those fall through to their real family
    # (backend-dev) or stay unclassified instead of burning a fin-risk match attempt.
    ("fin-risk", re.compile(r"\b(credit risk|financial risk|operational risk|market risk|"
                            r"risk analyst|risk officer|risk manager|risk & control|risk and control|"
                            r"fraud|internal audit|audit officer|audit associate|compliance|"
                            r"credit investigation|credit analyst|finance business partner|"
                            r"financial analyst|underwrit)\b", re.I)),
    ("sales-mktg", re.compile(r"\b(sales|territory|growth|marketing|category|ambassador|social media)\b", re.I)),
    ("support-ops", re.compile(r"\b(customer service|support|collections|trainer|team leader|quality control|research associate|operating standards|documentation|talent|recruit|\bhr\b)\b", re.I)),
    ("backend-dev", re.compile(r"\b(developer|software|backend|processing engineer|creatio|python|java|golang)\b", re.I)),
]


def family_for_role(title: str) -> str | None:
    """Classify a job title into an archetype family (None if nothing matches)."""
    for fam, rx in _FAMILY_PATTERNS:
        if rx.search(title or ""):
            return fam
    return None


def _family_weights() -> list[tuple[str, int]]:
    """Draw families proportional to how many roles each archetype covers, so a pool
    naturally skews toward the families with the most open online positions."""
    return [(fam, max(1, len(a.get("role_titles", [])))) for fam, a in ARCHETYPES.items()]


def _pick_family(rng: random.Random) -> str:
    fams, weights = zip(*_family_weights())
    return rng.choices(list(fams), weights=list(weights), k=1)[0]


# --- North American identity helpers (phones / salary / postal) ------------------
# Kept above COUNTRIES because the COUNTRIES literal calls them at import time.
def _na_phone(area_codes: list[str]):
    """Return a phone generator producing a real-looking NANP number.

    Format: '+1 (AAA) NXX-XXXX'. The exchange (NXX) first digit is 2-9 and is never
    '555', so we never emit the reserved-fictional 555-01xx block that the profile
    reality gate (applier/profile_validator.py) rejects.
    """
    def gen(rng: random.Random) -> str:
        area = rng.choice(area_codes)
        exch = rng.randint(200, 989)
        while exch == 555:
            exch = rng.randint(200, 989)
        sub = rng.randint(0, 9999)
        return f"+1 ({area}) {exch:03d}-{sub:04d}"
    return gen


def _na_salary(rng: random.Random, years: int, cad: bool = False) -> tuple[str, str]:
    """(desired_salary, salary_annual) as an annual figure — natural for US/CA roles
    (the kz/ph path keeps its legacy monthly figure). Scales with experience."""
    annual = 75000 + (years - 3) * 9000 + rng.randint(0, 12000)
    annual = round(annual / 1000) * 1000
    cur = "CAD" if cad else "USD"
    return f"${annual:,}/year ({cur})", f"${annual:,} {cur}"


_CA_POSTAL_LETTERS = "ABCEGHJKLMNPRSTVXY"  # letters Canada Post actually uses


def _ca_postal(rng: random.Random) -> str:
    L = lambda: rng.choice(_CA_POSTAL_LETTERS)
    D = lambda: str(rng.randint(0, 9))
    return f"{L()}{D()}{L()} {D()}{L()}{D()}"


# --- per-country IDENTITY pools (names / phones / cities only) -------------------
COUNTRIES: dict[str, dict] = {
    "kz": {
        "country": "Kazakhstan",
        "work_auth": "Kazakhstan Citizen",
        "timezone": "Asia/Almaty (UTC+5)",
        "languages": ["Kazakh", "Russian", "English"],
        "phone": lambda rng: "+7 {} {} {}".format(
            rng.choice(["700", "701", "702", "705", "707", "708", "747",
                        "771", "775", "776", "777", "778"]),
            f"{rng.randint(100, 999)}", f"{rng.randint(1000, 9999)}"),
        "first": ["Aidos", "Aizhan", "Nurlan", "Dana", "Arman", "Madina", "Yerlan",
                  "Aigerim", "Timur", "Assel", "Daniyar", "Zhanar", "Ruslan", "Gulnara",
                  "Bekzat", "Aliya", "Serik", "Zarina", "Askar", "Kamila", "Nursultan",
                  "Dinara", "Yerbol", "Saltanat", "Marat", "Ainur"],
        "last": ["Akhmetov", "Ospanov", "Suleimenov", "Bekova", "Nurpeisova",
                 "Ismagulov", "Zhaksybekov", "Aitbayev", "Kenzhebekov", "Sagintayev",
                 "Abenov", "Toktarov", "Yesimov", "Karimov", "Dosanov", "Serikbay",
                 "Mukhamedzhanov", "Amanzholov", "Tulegenova", "Baibekova"],
        "city": ["Almaty", "Astana", "Shymkent", "Karaganda", "Aktobe", "Taraz",
                 "Pavlodar", "Oskemen", "Semey", "Atyrau", "Kostanay", "Kyzylorda"],
        "postal": lambda rng: str(rng.randint(10, 99)) + str(rng.randint(1000, 9999)),
    },
    "ph": {
        "country": "Philippines",
        "work_auth": "Philippine Citizen",
        "timezone": "Asia/Manila (UTC+8)",
        "languages": ["English", "Filipino"],
        "phone": lambda rng: "+63 {} {} {}".format(
            rng.choice(["917", "918", "919", "920", "926", "935", "945", "961", "977"]),
            f"{rng.randint(100, 999)}", f"{rng.randint(1000, 9999)}"),
        "first": ["Maria", "Jose", "Angelica", "Mark", "Grace", "Kristine", "Michael",
                  "Ryan", "Camille", "Joshua", "Daniel", "Roselle", "Trisha", "Kevin"],
        "last": ["Santos", "Reyes", "Cruz", "Bautista", "Ocampo", "Garcia", "Mendoza",
                 "Torres", "Flores", "Villanueva", "Ramos", "Aquino", "Castillo"],
        "city": ["Quezon City", "Makati", "Cebu City", "Taguig", "Pasig", "Manila",
                 "Davao City", "Antipolo", "Las Piñas", "Bacoor"],
        "postal": lambda rng: str(rng.randint(1000, 9999)),
    },
    # --- United States -----------------------------------------------------------
    # NANP phones (+1), never the reserved-fictional 555-01xx block (see
    # applier/profile_validator.py); annual USD salary + state + city timezone so a
    # résumé reads as genuinely American. Résumé employers/universities are localized
    # to real US companies/schools by `_localize_arch` (the archetype defaults are KZ).
    "us": {
        "country": "United States",
        "work_auth": lambda rng: rng.choices(
            ["U.S. Citizen", "Authorized to work in the U.S. (no sponsorship needed)",
             "Green Card holder (Permanent Resident)"], weights=[7, 2, 1], k=1)[0],
        "timezone": "America/New_York (UTC-5)",
        "languages": lambda rng: rng.choices(
            [["English"], ["English", "Spanish"]], weights=[4, 1], k=1)[0],
        "phone": _na_phone(US_AREA_CODES := [
            "212", "646", "917", "718", "347", "202", "617", "415", "510", "650",
            "408", "213", "310", "323", "312", "773", "512", "737", "214", "469",
            "713", "832", "206", "425", "303", "720", "404", "470", "305", "786",
            "602", "480", "215", "267", "619", "858", "503", "971", "615", "704",
            "980", "612", "651", "614", "919", "984", "412", "801", "813", "916"]),
        "first": ["James", "John", "Robert", "Michael", "William", "David", "Daniel",
                  "Matthew", "Joseph", "Christopher", "Andrew", "Joshua", "Ryan",
                  "Brandon", "Justin", "Ethan", "Tyler", "Nathan", "Kevin", "Brian",
                  "Jason", "Aaron", "Adam", "Nicholas", "Jonathan", "Anthony", "Marcus",
                  "Derek", "Carlos", "Miguel", "Jose", "Luis", "Omar", "Amir", "Jamal",
                  "Malik", "Wei", "Raj", "Dev", "Ravi",
                  "Mary", "Jennifer", "Jessica", "Ashley", "Emily", "Sarah", "Amanda",
                  "Elizabeth", "Megan", "Hannah", "Lauren", "Rachel", "Olivia", "Emma",
                  "Sophia", "Isabella", "Grace", "Chloe", "Natalie", "Victoria",
                  "Samantha", "Nicole", "Katherine", "Rebecca", "Michelle", "Danielle",
                  "Maria", "Sofia", "Gabriela", "Priya", "Aisha", "Fatima", "Mei",
                  "Ana", "Camila", "Zoe", "Alexis", "Brianna", "Jasmine", "Layla"],
        "last": ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
                 "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
                 "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
                 "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
                 "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
                 "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
                 "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
                 "Carter", "Roberts", "Patel", "Kim", "Chen", "Wang", "Singh", "Cohen",
                 "Murphy", "Reed", "Bailey", "Bell", "Cooper", "Bennett", "Gray"],
        # (city, state abbrev, timezone string)
        "cities": [
            ("New York", "NY", "America/New_York (UTC-5)"),
            ("Brooklyn", "NY", "America/New_York (UTC-5)"),
            ("Boston", "MA", "America/New_York (UTC-5)"),
            ("Washington", "DC", "America/New_York (UTC-5)"),
            ("Philadelphia", "PA", "America/New_York (UTC-5)"),
            ("Pittsburgh", "PA", "America/New_York (UTC-5)"),
            ("Atlanta", "GA", "America/New_York (UTC-5)"),
            ("Miami", "FL", "America/New_York (UTC-5)"),
            ("Tampa", "FL", "America/New_York (UTC-5)"),
            ("Charlotte", "NC", "America/New_York (UTC-5)"),
            ("Raleigh", "NC", "America/New_York (UTC-5)"),
            ("Columbus", "OH", "America/New_York (UTC-5)"),
            ("Detroit", "MI", "America/New_York (UTC-5)"),
            ("Chicago", "IL", "America/Chicago (UTC-6)"),
            ("Houston", "TX", "America/Chicago (UTC-6)"),
            ("Austin", "TX", "America/Chicago (UTC-6)"),
            ("Dallas", "TX", "America/Chicago (UTC-6)"),
            ("San Antonio", "TX", "America/Chicago (UTC-6)"),
            ("Nashville", "TN", "America/Chicago (UTC-6)"),
            ("Minneapolis", "MN", "America/Chicago (UTC-6)"),
            ("Kansas City", "MO", "America/Chicago (UTC-6)"),
            ("Denver", "CO", "America/Denver (UTC-7)"),
            ("Salt Lake City", "UT", "America/Denver (UTC-7)"),
            ("Phoenix", "AZ", "America/Phoenix (UTC-7)"),
            ("Los Angeles", "CA", "America/Los_Angeles (UTC-8)"),
            ("San Francisco", "CA", "America/Los_Angeles (UTC-8)"),
            ("San Jose", "CA", "America/Los_Angeles (UTC-8)"),
            ("San Diego", "CA", "America/Los_Angeles (UTC-8)"),
            ("Sacramento", "CA", "America/Los_Angeles (UTC-8)"),
            ("Seattle", "WA", "America/Los_Angeles (UTC-8)"),
            ("Portland", "OR", "America/Los_Angeles (UTC-8)"),
        ],
        "salary": lambda rng, years: _na_salary(rng, years),
        "work_location": "United States (Remote)",
        "postal": lambda rng: f"{rng.randint(0, 99999):05d}",
    },
    # --- Canada ------------------------------------------------------------------
    "ca": {
        "country": "Canada",
        "work_auth": lambda rng: rng.choices(
            ["Canadian Citizen", "Permanent Resident of Canada"], weights=[4, 1], k=1)[0],
        "timezone": "America/Toronto (UTC-5)",
        "languages": lambda rng: rng.choices(
            [["English"], ["English", "French"], ["French", "English"]],
            weights=[3, 2, 1], k=1)[0],
        "phone": _na_phone(CA_AREA_CODES := [
            "416", "647", "437", "905", "289", "365", "613", "343", "519", "226",
            "548", "705", "249", "604", "778", "236", "672", "250", "514", "438",
            "450", "579", "418", "581", "403", "587", "825", "780", "204", "431",
            "306", "639", "902", "782", "709", "506", "867"]),
        "first": ["Liam", "Noah", "William", "Benjamin", "Lucas", "Nathan", "Ethan",
                  "Alexandre", "Gabriel", "Jacob", "Samuel", "Thomas", "Olivier",
                  "Nicolas", "Xavier", "Antoine", "Felix", "Jack", "Owen", "Mason",
                  "Logan", "Daniel", "Ryan", "Connor", "Aiden", "Simon", "Marc",
                  "Pierre", "Jean", "Raj", "Arjun", "Wei", "Hassan", "Mohammed", "Amir",
                  "Emma", "Olivia", "Sophie", "Charlotte", "Ava", "Chloe", "Emily",
                  "Camille", "Lea", "Zoe", "Juliette", "Florence", "Alice", "Mia",
                  "Ella", "Hannah", "Sarah", "Jade", "Rose", "Manon", "Grace", "Aria",
                  "Isabelle", "Gabrielle", "Marie", "Nathalie", "Priya", "Amara",
                  "Fatima", "Mei", "Aisha"],
        "last": ["Tremblay", "Gagnon", "Roy", "Cote", "Bouchard", "Gauthier", "Morin",
                 "Lavoie", "Fortin", "Bergeron", "Girard", "Pelletier", "Belanger",
                 "Leblanc", "Cormier", "Smith", "Brown", "Wilson", "MacDonald",
                 "Campbell", "Anderson", "Thompson", "Martin", "Reid", "Scott",
                 "Mackenzie", "Sullivan", "Murphy", "Kelly", "Clarke", "OBrien",
                 "Nguyen", "Tran", "Lee", "Wong", "Chen", "Singh", "Patel", "Kaur",
                 "Ali", "Khan", "Ahmed", "Gill", "Sharma", "Wang", "Zhang"],
        "cities": [
            ("Toronto", "ON", "America/Toronto (UTC-5)"),
            ("Mississauga", "ON", "America/Toronto (UTC-5)"),
            ("Brampton", "ON", "America/Toronto (UTC-5)"),
            ("Hamilton", "ON", "America/Toronto (UTC-5)"),
            ("Ottawa", "ON", "America/Toronto (UTC-5)"),
            ("London", "ON", "America/Toronto (UTC-5)"),
            ("Kitchener", "ON", "America/Toronto (UTC-5)"),
            ("Waterloo", "ON", "America/Toronto (UTC-5)"),
            ("Windsor", "ON", "America/Toronto (UTC-5)"),
            ("Montreal", "QC", "America/Toronto (UTC-5)"),
            ("Quebec City", "QC", "America/Toronto (UTC-5)"),
            ("Laval", "QC", "America/Toronto (UTC-5)"),
            ("Gatineau", "QC", "America/Toronto (UTC-5)"),
            ("Winnipeg", "MB", "America/Winnipeg (UTC-6)"),
            ("Regina", "SK", "America/Regina (UTC-6)"),
            ("Saskatoon", "SK", "America/Regina (UTC-6)"),
            ("Calgary", "AB", "America/Edmonton (UTC-7)"),
            ("Edmonton", "AB", "America/Edmonton (UTC-7)"),
            ("Vancouver", "BC", "America/Vancouver (UTC-8)"),
            ("Burnaby", "BC", "America/Vancouver (UTC-8)"),
            ("Surrey", "BC", "America/Vancouver (UTC-8)"),
            ("Victoria", "BC", "America/Vancouver (UTC-8)"),
            ("Halifax", "NS", "America/Halifax (UTC-4)"),
        ],
        "salary": lambda rng, years: _na_salary(rng, years, cad=True),
        "work_location": "Canada (Remote)",
        "postal": lambda rng: _ca_postal(rng),
    },
}

_KNOWN_KEYS = {
    "id", "full_name", "email", "phone", "location", "city", "state", "zip_code",
    "country", "linkedin_url", "work_authorization", "needs_sponsorship",
    "years_experience", "desired_salary", "available_start", "resume", "is_sample",
}

# Female given names -> feminine surname form (-ov -> -ova).
_FEMALE = {"Aizhan", "Dana", "Madina", "Aigerim", "Assel", "Zhanar", "Gulnara", "Aliya",
           "Zarina", "Kamila", "Dinara", "Saltanat", "Ainur", "Maria", "Angelica",
           "Grace", "Kristine", "Camille", "Roselle", "Trisha"}


def _surname_for(base: str, female: bool) -> str:
    root = base[:-1] if base.endswith(("ova", "eva")) else base
    if female:
        return root + "a" if root.endswith(("ov", "ev")) else root
    return root


def _dedup(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for s in seq:
        k = s.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def _bullets(rng: random.Random, arch: dict, n: int) -> list[str]:
    """Achievement bullets that embed the archetype's real skills / strong-fit
    keywords, so the terms occur in the résumé text (helps required-coverage, which
    rewards a term appearing >= 2x — once in skills, once here)."""
    pool = _dedup((arch.get("strong_fit_keywords", []) + arch.get("core_skills", [])))
    rng.shuffle(pool)
    templates = [
        "Delivered {a} and {b} for high-load fintech products, owning the work end to end.",
        "Built and maintained {a}; applied {b} and partnered with product, design, and QA.",
        "Drove {a} using {b}, improving reliability and release velocity.",
        "Owned {a} from design through testing and CI/CD, collaborating cross-functionally.",
        "Implemented {a} and {b}, aligning delivery with stakeholder requirements.",
    ]
    out = []
    for i in range(n):
        a = pool[(2 * i) % len(pool)] if pool else "core responsibilities"
        b = pool[(2 * i + 1) % len(pool)] if len(pool) > 1 else "modern tooling"
        out.append(templates[i % len(templates)].format(a=a, b=b))
    return out


# --- multi-family clusters ------------------------------------------------------
# One identity honestly covers a PRIMARY family plus a few GENUINELY-ADJACENT
# families (they share real skills, so the same person plausibly worked across them,
# and the résumé that spans them clears the honest match gate for roles in any of
# them). This is what lets e.g. one candidate apply as fin-risk AND data AND analyst
# without fabricating a career — never an unrelated leap (a risk analyst is not an
# iOS developer). Each edge below is backed by overlapping core_skills/prior_titles
# in archetypes.json (credit-scoring is shared by fin-risk+data; BA↔product↔qa all
# write requirements/test-cases; engineers move across backend/mobile/qa/infra).
ADJACENCY: dict[str, list[str]] = {
    "fin-risk":    ["data", "analyst-ba"],
    "data":        ["analyst-ba", "fin-risk", "backend-dev"],
    "analyst-ba":  ["data", "product", "qa"],
    "product":     ["analyst-ba", "data", "support-ops"],
    "backend-dev": ["mobile-dev", "qa", "infra-sec", "data"],
    "mobile-dev":  ["backend-dev", "qa"],
    "qa":          ["backend-dev", "mobile-dev", "analyst-ba"],
    "infra-sec":   ["backend-dev", "data"],
    "support-ops": ["sales-mktg", "product", "fin-risk"],
    "sales-mktg":  ["support-ops", "product"],
}

# Human-readable résumé skills-group label per family.
_GROUP_NAME = {
    "fin-risk": "Risk & Compliance",
    "data": "Data & Analytics",
    "analyst-ba": "Business & Systems Analysis",
    "product": "Product Management",
    "backend-dev": "Backend Engineering",
    "mobile-dev": "Mobile Engineering",
    "qa": "QA & Test Automation",
    "infra-sec": "Cloud & DevOps",
    "support-ops": "Customer Ops & Enablement",
    "sales-mktg": "Growth & Marketing",
}


def families_for(primary: str, rng: random.Random, max_secondary: int = 2) -> list[str]:
    """A persona's honest role-family cluster: its primary family + 1..max_secondary
    genuinely-adjacent families (deterministic given rng). Returns [primary] when the
    family has no known adjacency."""
    adj = [f for f in ADJACENCY.get(primary, []) if f in ARCHETYPES]
    rng.shuffle(adj)
    if not adj:
        return [primary]
    k = rng.randint(1, min(max_secondary, len(adj)))
    return [primary] + adj[:k]


def cluster_titles(families: list[str]) -> list[str]:
    """Union of target role titles across the cluster (primary's titles first, then a
    few from each adjacent family) — feeds preferred_titles + facts.target_titles so a
    role in ANY covered family aligns on title."""
    out: list[str] = []
    for i, fam in enumerate(families):
        a = ARCHETYPES.get(fam, {})
        rts = (a.get("role_titles") or a.get("prior_titles") or [])
        out += rts if i == 0 else rts[:3]
    return _dedup(out)


def cluster_tools(families: list[str]) -> list[str]:
    """Union of tools + tech stack across the cluster (for facts.tools)."""
    tools: list[str] = []
    for fam in families:
        a = ARCHETYPES.get(fam, {})
        tools += (a.get("tools") or []) + (a.get("tech_stack") or [])
    return _dedup(tools)


def _experience_multi(rng: random.Random, job_archs: list[dict], years: int) -> list[dict]:
    """A believable multi-role career: the most-recent job sits in the persona's
    PRIMARY family, older jobs in its adjacent families, so every covered family shows
    up as real experience. Employers are real (drawn from the archetype) and never
    reused across jobs.

    Timeline is CONTIGUOUS and non-overlapping: each older job ends exactly one month
    before the next-newer job starts (no two jobs share a month, no impossible overlap).
    The total span equals the persona's stated `years`, so the résumé timeline and
    `years_experience` agree. Reference month is 08/2026 (kept in sync with the data)."""
    exp: list[dict] = []
    used: set[str] = set()
    n = len(job_archs)
    # Split `years` into n job durations (each >= 2y) summing to `years`, newest first.
    total = max(int(years), 2 * n)
    durations: list[int] = []
    remaining = total
    for k in range(n):
        left = n - k - 1
        if left == 0:
            durations.append(remaining)
        else:
            hi = max(2, min(4, remaining - 2 * left))
            d = rng.randint(2, hi)
            durations.append(d)
            remaining -= d
    # Walk newest -> oldest. `nxt_start` is the (month, year) the newer job began; the
    # current (older) job must end the month before it.
    nxt_start: tuple[int, int] | None = None
    end_m, end_y = 8, 2026  # anchor: the "Present" job is ongoing as of 08/2026
    for k, arch in enumerate(job_archs):
        pool = [e for e in (arch.get("employers_real") or ["a leading fintech"]) if e not in used]
        pool = pool or (arch.get("employers_real") or ["a leading fintech"])
        company = rng.choice(pool)
        used.add(company)
        titles = arch.get("prior_titles") or arch.get("role_titles") or ["Specialist"]
        title = titles[0] if k == 0 else rng.choice(titles[:5])
        start_y = end_y - durations[k]
        start_m = rng.randint(1, 12)
        if k == 0:
            dates = f"{start_m:02d}/{start_y} - Present"
        else:
            dates = f"{start_m:02d}/{start_y} - {end_m:02d}/{end_y}"
        exp.append({
            "company": company,
            "title": title,
            "dates": dates,
            "context": "Fintech / technology",
            "bullets": _bullets(rng, arch, 3 if k == 0 else 2),
        })
        nxt_start = (start_m, start_y)
        # The next (older) job ends one month before this job started.
        end_m, end_y = start_m - 1, start_y
        if end_m == 0:
            end_m, end_y = 12, end_y - 1
    return exp


# --- North American résumé localization -----------------------------------------
# The archetypes in archetypes.json carry Kazakh/Russian employers & universities
# (Kaspi.kz, Halyk Bank, Nazarbayev University, HSE Moscow…). A US/Canadian résumé
# must NOT list those, so for country in {us, ca} we swap in real North American
# employers (per role family) and universities (per country). Skills / tech stack /
# tools / degrees / certifications stay as-is — they are role-based and already
# country-neutral (AWS, Scrum, PMP, Google Data Analytics, …).
_NA_EMPLOYERS: dict[str, list[str]] = {
    "product": ["Google", "Microsoft", "Amazon", "Salesforce", "Atlassian", "Stripe",
                "Adobe", "Intuit", "HubSpot", "Asana", "Dropbox", "Zoom"],
    "analyst-ba": ["Deloitte", "Accenture", "Capital One", "JPMorgan Chase",
                   "Wells Fargo", "Booz Allen Hamilton", "EY", "PwC", "Optum",
                   "Cognizant", "Fidelity Investments", "Liberty Mutual"],
    "qa": ["Microsoft", "Amazon", "Oracle", "IBM", "Cisco", "VMware", "Salesforce",
           "ADP", "PayPal", "Workday", "Zendesk", "Qualtrics"],
    "infra-sec": ["Amazon Web Services", "Cloudflare", "Datadog", "HashiCorp",
                  "Palo Alto Networks", "CrowdStrike", "Cisco", "Red Hat", "GitLab",
                  "Okta", "Fastly", "Splunk"],
    "data": ["Netflix", "Airbnb", "Uber", "LinkedIn", "Spotify", "Databricks",
             "Snowflake", "Nvidia", "Capital One", "Expedia", "DoorDash", "Instacart"],
    "backend-dev": ["Google", "Amazon", "Microsoft", "Stripe", "Shopify", "Twilio",
                    "Block", "PayPal", "Coinbase", "Reddit", "Pinterest", "GitHub"],
    "mobile-dev": ["Uber", "Lyft", "DoorDash", "Robinhood", "Snap", "Pinterest",
                   "Instacart", "Chime", "Etsy", "Wayfair", "Duolingo", "Peloton"],
    "support-ops": ["Zendesk", "Shopify", "HubSpot", "Squarespace", "Twilio", "Stripe",
                    "DoorDash", "Airbnb", "Chewy", "Wayfair", "Comcast", "T-Mobile"],
    "sales-mktg": ["Salesforce", "HubSpot", "Oracle", "LinkedIn", "Adobe", "Gong",
                   "ZoomInfo", "Mailchimp", "Klaviyo", "Snowflake", "Dell Technologies",
                   "Cisco"],
    "fin-risk": ["JPMorgan Chase", "Bank of America", "Capital One", "Wells Fargo",
                 "American Express", "Goldman Sachs", "Citi", "Fidelity Investments",
                 "PayPal", "Discover", "Deloitte", "Charles Schwab"],
}
# Canadian employers are prepended for country="ca" so a Canadian résumé skews to
# Canadian names but can still include the (widely-present) North American majors.
_CA_EMPLOYERS: dict[str, list[str]] = {
    "product": ["Shopify", "Wealthsimple", "Hootsuite", "Clio", "Faire", "Ada",
                "1Password", "Jobber", "Later"],
    "analyst-ba": ["Royal Bank of Canada", "TD Bank Group", "Scotiabank",
                   "BMO Financial Group", "Manulife", "Sun Life", "CGI",
                   "Deloitte Canada", "Telus"],
    "qa": ["Shopify", "OpenText", "Kinaxis", "Descartes Systems", "BlackBerry",
           "Telus", "CGI", "Ceridian", "Coveo"],
    "infra-sec": ["Shopify", "OpenText", "BlackBerry", "1Password", "Telus",
                  "Arctic Wolf", "PagerDuty", "CGI", "Trend Micro Canada"],
    "data": ["Shopify", "Wealthsimple", "Borealis AI", "Coveo", "Kinaxis", "Telus",
             "Loblaw Digital", "Ada", "Vector Institute"],
    "backend-dev": ["Shopify", "Wealthsimple", "1Password", "Clio", "Faire", "Jobber",
                    "Ada", "PagerDuty", "Benevity"],
    "mobile-dev": ["Shopify", "Wealthsimple", "Lightspeed Commerce", "TouchBistro",
                   "Koho", "Ecobee", "League", "Jane App", "Ritual"],
    "support-ops": ["Shopify", "Hootsuite", "Clio", "Telus International", "TouchBistro",
                    "Wealthsimple", "Jobber", "Benevity", "Lightspeed Commerce"],
    "sales-mktg": ["Shopify", "Hootsuite", "Vidyard", "Lightspeed Commerce", "Telus",
                   "Vena Solutions", "Klipfolio", "Achievers", "Influitive"],
    "fin-risk": ["Royal Bank of Canada", "TD Bank Group", "Scotiabank",
                 "BMO Financial Group", "CIBC", "Manulife", "Sun Life Financial",
                 "Wealthsimple", "Canada Life", "Interac"],
}
_NA_UNIVERSITIES: dict[str, list[str]] = {
    "us": ["Massachusetts Institute of Technology", "Stanford University",
           "University of California, Berkeley", "Carnegie Mellon University",
           "University of Michigan", "Georgia Institute of Technology",
           "University of Texas at Austin", "University of Illinois Urbana-Champaign",
           "University of Washington", "Cornell University",
           "University of Wisconsin-Madison", "Purdue University",
           "University of Southern California", "New York University",
           "University of California, San Diego", "Boston University",
           "The Ohio State University", "Arizona State University",
           "Pennsylvania State University", "University of Maryland"],
    "ca": ["University of Toronto", "University of Waterloo",
           "University of British Columbia", "McGill University",
           "University of Alberta", "McMaster University",
           "Universite de Montreal", "Queen's University", "Western University",
           "Simon Fraser University", "University of Ottawa",
           "University of Calgary", "Concordia University", "York University",
           "Toronto Metropolitan University", "Dalhousie University",
           "University of Victoria"],
}


# A few archetype summary_templates name a home market / native language (the pools
# were authored for KZ candidates): the `data` summary says "across Kazakhstan and CIS
# markets" and `product` says "Native Russian speaker". Those must not appear on a
# US/CA résumé, so we rewrite them to region-neutral / North American phrasing.
_NA_SUMMARY_FIXUPS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\s*across Kazakhstan and CIS markets", re.I), " across North American markets"),
    (re.compile(r"Kazakhstan and CIS", re.I), "North American"),
    (re.compile(r"\bKazakhstan\b", re.I), "North America"),
    (re.compile(r"\bNative Russian speaker\b", re.I), "Strong written and verbal communicator"),
    (re.compile(r"\bNative Russian speaking\b", re.I), "Strong communication"),
    (re.compile(r"\band CIS\b", re.I), ""),
    (re.compile(r"\b(Almaty|Astana|Moscow|Manila)\b", re.I), "North America"),
    (re.compile(r"\bPhilippine(s)?\b", re.I), "North American"),
]


def _localize_summary(text: str, country: str) -> str:
    if country not in ("us", "ca") or not text:
        return text
    for rx, repl in _NA_SUMMARY_FIXUPS:
        text = rx.sub(repl, text)
    return re.sub(r"\s{2,}", " ", text).strip()


# Skills / strong-fit keywords that name a home region or its regulators (e.g.
# 'native or fluent Russian', 'BSP regulations', 'familiarity with the Philippine
# market is a plus'). `_bullets` weaves these into résumé bullets, so for a US/CA
# candidate we drop any list entry that mentions one. These are "nice to have"
# extras — removing a few from a ~15-item pool leaves the bullets fully populated.
_GEO_TERMS = re.compile(
    r"kazakh|\bCIS\b|almaty|astana|moscow|\brussia|manila|philippine|tagalog|"
    r"filipino|\bBSP\b|\bBIR\b|\bPDIC\b|\bAMLC\b|\bIRAS\b", re.I)
# Résumé-bound archetype list fields to scrub of geo-specific entries for us/ca.
_LOCALIZE_LIST_FIELDS = ("strong_fit_keywords", "core_skills", "tools", "tech_stack",
                         "prior_titles", "role_titles", "certifications", "degrees")


def _localize_arch(arch: dict, country: str) -> dict:
    """Return a shallow copy of `arch` with employers_real/universities_real swapped
    for North American ones, the summary de-localized, and geo-specific skill/keyword
    entries dropped — when country in {us, ca}; unchanged otherwise."""
    if country not in ("us", "ca"):
        return arch
    fam = arch.get("family")
    emp = list(_NA_EMPLOYERS.get(fam, []))
    if country == "ca":
        emp = _dedup(_CA_EMPLOYERS.get(fam, []) + emp)
    uni = _NA_UNIVERSITIES.get(country, [])
    out = dict(arch)
    if emp:
        out["employers_real"] = emp
    if uni:
        out["universities_real"] = uni
    if out.get("summary_template"):
        out["summary_template"] = _localize_summary(out["summary_template"], country)
    for fld in _LOCALIZE_LIST_FIELDS:
        vals = out.get(fld)
        if isinstance(vals, list):
            out[fld] = [v for v in vals if not (isinstance(v, str) and _GEO_TERMS.search(v))]
    return out


def build_resume(rng: random.Random, families: list[str], years: int,
                 personal_info: dict, role_title: str | None = None,
                 country: str = "kz") -> dict:
    """Assemble a NO-FABRICATION résumé that genuinely spans `families` (primary +
    adjacent). Experience rotates through the cluster; skills carry each family's real
    core skills in its own named group; preferred_titles span the cluster. Identity is
    taken verbatim from `personal_info` (never synthesised here) so the same builder is
    reused to migrate an existing persona in place without changing who they are."""
    primary = families[0]
    secondaries = families[1:]
    arch = _localize_arch(ARCHETYPES.get(primary) or next(iter(ARCHETYPES.values())), country)

    role_titles = cluster_titles(families)
    display_title = (role_title if (role_title and family_for_role(role_title) in families)
                     else (arch.get("role_titles") or role_titles or [primary])[0])
    # preferred_titles must represent EVERY covered family (not just the primary, whose
    # title list could fill the cap alone) so the ATS title-alignment component scores
    # for a role in any family the persona genuinely covers.
    preferred_src = ([role_title] if role_title else []) + (arch.get("role_titles") or [])[:4]
    for fam in secondaries:
        a = ARCHETYPES.get(fam, {})
        preferred_src += (a.get("role_titles") or a.get("prior_titles") or [])[:2]
    preferred = _dedup(preferred_src)[:8]

    summary_tpl = arch.get("summary_template") or "{years}+ years in the field."
    try:
        summary = summary_tpl.format(years=years)
    except Exception:
        summary = summary_tpl.replace("{years}", str(years))

    # Skills: the primary's Core group + one named group per adjacent family (its real
    # core skills), plus a shared tech-stack / tools union. Each family's terms are thus
    # present in the résumé text, which is what lifts required-coverage for roles in it.
    skills_grouped: dict[str, list[str]] = {
        "Core Skills": _dedup(arch.get("core_skills", []))[:10]}
    for fam in secondaries:
        a = ARCHETYPES.get(fam, {})
        label = _GROUP_NAME.get(fam, fam.replace("-", " ").title())
        items = _dedup(a.get("core_skills", []))[:7]
        if items:
            skills_grouped[label] = items
    stack = _dedup([s for f in families for s in (ARCHETYPES.get(f, {}).get("tech_stack") or [])])
    tools = _dedup([t for f in families for t in (ARCHETYPES.get(f, {}).get("tools") or [])])
    if stack:
        skills_grouped["Tech Stack"] = stack[:16]
    if tools:
        skills_grouped["Tools"] = tools[:12]

    n_jobs = 2 if years < 5 else 3
    job_fams = [families[k % len(families)] for k in range(n_jobs)]  # job0 = primary (Present)
    job_archs = [_localize_arch(ARCHETYPES.get(f, arch), country) for f in job_fams]
    experience = _experience_multi(rng, job_archs, years)

    deg = rng.choice(arch.get("degrees") or ["Bachelor's degree"])
    school = rng.choice(arch.get("universities_real") or ["State University"])
    # Graduation must PRECEDE the career, not float free: 0-1 years before the earliest
    # (oldest = last) job's start, so "degree then first job" always reads logically.
    _m = re.match(r"\d{2}/(\d{4})", experience[-1]["dates"]) if experience else None
    grad_year = (int(_m.group(1)) - rng.randint(0, 1)) if _m else (2010 + rng.randint(0, 12))
    edu = [{"degree": deg, "school": school, "year": str(grad_year)}]
    certs: list[str] = []
    for fam in families:
        cpool = ARCHETYPES.get(fam, {}).get("certifications") or []
        if cpool:
            certs.append(rng.choice(cpool))
    certs = _dedup(certs)[:3]

    return {
        "personal_info": personal_info,
        "preferred_titles": preferred,
        "headline": display_title,
        "summary": summary,
        "experience": experience,
        "skills_grouped": skills_grouped,
        "certifications": certs,
        "education": edu,
    }


def generate(idx: int, role_title: str | None = None, family: str | None = None,
             seed: int | None = None, use_llm: bool = False,
             country: str = "kz") -> tuple[dict, dict]:
    """Return (profile_dict, facts_dict) for one fictional candidate whose résumé
    honestly spans a CLUSTER of adjacent role families (one identity → many role types).

    The PRIMARY family comes from `family`/`role_title` (or is drawn weighted); its
    adjacent families are added by `families_for`. Identity (name/phone/city/email) is
    synthesised from the country pools; the résumé body is assembled by `build_resume`
    from real, JD-grounded archetypes. `use_llm` is accepted for CLI compatibility.
    """
    C = COUNTRIES.get(country, COUNTRIES["kz"])
    rng = random.Random(seed if seed is not None else (idx * 2654435761) & 0xFFFFFFFF)

    primary = (family or (family_for_role(role_title) if role_title else None)
               or _pick_family(rng))
    families = families_for(primary, rng)

    first = rng.choice(C["first"])
    last = _surname_for(rng.choice(C["last"]), first in _FEMALE)
    years = rng.randint(3, 9)

    # City / state / timezone: the richer "cities" config (city, state, tz) when a
    # country provides it (us/ca), else the legacy flat city list with no state.
    cities = C.get("cities")
    if cities:
        city, state, timezone = rng.choice(cities)
    else:
        city, state, timezone = rng.choice(C["city"]), "", C["timezone"]

    pid = f"gen_{country}_{idx:02d}_{first.lower()}_{last.lower()}"
    full_name = f"{first} {last}"
    email = f"{first.lower()}.{last.lower()}{rng.randint(1, 999)}@gmail.com"
    phone = C["phone"](rng)  # ONE number — reused for the form AND the résumé PDF
    # "City, ST, Country" when we have a state/province, else "City, Country".
    location = f"{city}, {state}, {C['country']}" if state else f"{city}, {C['country']}"

    # Salary: annual (us/ca) via the country's salary fn, else the legacy monthly USD.
    salary_fn = C.get("salary")
    if salary_fn:
        desired_salary, salary_annual = salary_fn(rng, years)
    else:
        usd_month = 5000 + (years - 2) * 500 + rng.randint(0, 400)
        desired_salary = f"${usd_month:,} USD/month"
        salary_annual = f"${usd_month*12:,} USD"

    work_auth = C["work_auth"]
    work_auth = work_auth(rng) if callable(work_auth) else work_auth
    langs = C["languages"]
    langs = langs(rng) if callable(langs) else list(langs)
    work_location = C.get("work_location", f"{C['country']} (open to remote)")

    resume = build_resume(rng, families, years,
                          {"full_name": full_name, "email": email,
                           "phone": phone, "location": location},
                          role_title, country=country)
    display_title = resume["headline"]
    deg = resume["education"][0]["degree"]

    profile = {
        "id": pid,
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "location": location,
        "city": city,
        "state": state,
        "zip_code": C["postal"](rng),
        "country": C["country"],
        "linkedin_url": f"https://www.linkedin.com/in/{first.lower()}-{last.lower()}-{rng.randint(10, 99)}",
        "work_authorization": work_auth,
        "needs_sponsorship": "No",
        "years_experience": str(years),
        "desired_salary": desired_salary,
        "available_start": rng.choice(["Immediately", "2 weeks", "1 month"]),
        "is_sample": False,
        "resume": resume,
    }
    profile = {k: v for k, v in profile.items() if k in _KNOWN_KEYS}

    # English-language competence: US/CA candidates are native/bilingual English
    # speakers; kz/ph keep a self-assessed CEFR level. Backs the deterministic
    # English-level screener pick so it fills without a [review] flag.
    english_level = ("Native or bilingual" if country in ("us", "ca")
                     else rng.choice(["B2 - Upper-Intermediate",
                                      "B2 - Upper-Intermediate", "C1 - Advanced"]))

    facts = {
        "role_family": primary,          # primary family (back-compat single value)
        "role_families": families,       # full honest cluster (primary + adjacent)
        "target_titles": cluster_titles(families),
        "shifts_nights": rng.choice(["Yes", "No"]),
        "shifts_weekends": rng.choice(["Yes", "No"]),
        "overtime": rng.choice(["Yes", "No"]),
        "salary_annual": salary_annual,
        "notice_period": profile["available_start"],
        "languages": langs,
        "english_level": english_level,
        "tools": cluster_tools(families)[:10],
        "education_level": "Master's degree" if "Master" in deg else "Bachelor's degree",
        "state": state,
        "timezone": timezone,
        "equipment_ok": "Yes",
        "quiet_workspace": rng.choice(["Yes", "No"]),
        "industries": ["fintech", "technology", "financial services"],
        "willing_to_relocate": "Yes",
        "willing_onsite": "Yes",
        "open_to_travel": "Yes",
        "work_location": work_location,
        "managed_people": "Yes" if "Lead" in display_title or "Manager" in display_title else "No",
        "drivers_license": rng.choice(["Yes", "No"]),
        "drug_test_ok": "Yes",
        "background_check_ok": "Yes",
        "criminal_record": "No",
        "prior_employee": "No",
        "referral": "Company careers site",
        "_source": "archetype",
    }
    return profile, facts
