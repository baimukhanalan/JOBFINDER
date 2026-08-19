import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.job import Job, JobSource
from backend.scrapers.base import BaseScraper, RawJob
from backend.scrapers.jobspy_scraper import scrape_jobspy
from backend.scrapers.remoteok import RemoteOKScraper
from backend.scrapers.weworkremotely import WeWorkRemotelyScraper

logger = logging.getLogger(__name__)

SOURCE_MAP = {
    "remoteok": JobSource.REMOTEOK,
    "weworkremotely": JobSource.WEWORKREMOTELY,
    "indeed": JobSource.INDEED,
    "linkedin": JobSource.LINKEDIN,
    "ziprecruiter": JobSource.ZIPRECRUITER,
    "glassdoor": JobSource.GLASSDOOR,
}

# Known equipment policies
EQUIPMENT_MAP = {
    "amazon": "corporate", "at&t": "corporate", "concentrix": "corporate",
    "creative force": "corporate", "cgi": "corporate", "marathon health": "corporate",
    "americor": "corporate", "geico": "corporate", "claimspro": "corporate",
    "penfed": "corporate", "ati physical": "corporate", "tp": "corporate",
    "teleperformance": "corporate",
    "aspire lifestyles": "byod", "10x team": "byod", "kastle": "byod",
    "alivi": "byod", "salesroads": "byod", "sutherland": "byod",
    "highlevel": "byod", "advanpro": "byod", "petabloc": "byod",
    "descript": "stipend", "testgorilla": "stipend",
}

# Known hiring speed
SPEED_MAP = {
    "amazon": "fast", "teleperformance": "fast", "tp": "fast",
    "concentrix": "fast", "aspire lifestyles": "fast", "americor": "fast",
    "alivi": "fast", "sutherland": "fast",
    "fliff": "medium", "marathon health": "medium", "penfed": "medium",
    "geico": "medium", "salesroads": "medium", "creative force": "medium",
    "cgi": "medium", "claimspro": "medium",
    "at&t": "slow",
}

# Physical/non-remote job titles to reject
REJECT_TITLES = [
    "produce clerk", "fresh cut", "shipping", "receiving clerk", "warehouse",
    "driver", "forklift", "janitor", "custodian", "cook", "chef", "cashier",
    "barista", "bartender", "sales advisor", "outside sales", "door to door",
    "on-site", "onsite", "in-office", "field service", "phlebotomist",
    "urgent care", "food service", "parts counter", "storeroom",
    "construction", "roofing", "mechanic", "plumber", "electrician",
    "welder", "fire sprinkler", "pest", "exterminator", "fitness",
    "personal trainer", "front desk", "concierge", "surgical",
    "cytogenetic", "medical assistant", "nurse", "territory sales",
    "solar sales", "store manager", "in-person", "printing",
    "reprographics", "vehicle integration", "customs brokerage",
    "housekeeper", "executive housekeeper", "materials handler",
    "ward clerk", "vehicle services", "poker room", "door-2-door",
    "door 2 door", "data center site", "loan originator",
    "insurance broker", "missing heir", "brand designer",
    "material coordinator", "secretary ii", "elections assistant",
    "temporary general clerk", "marble and granite", "animal care",
    "sales associate", "apartment locator", "territory account",
    "financial clearance", "care access representative",
    "75% travel", "hybrid", "administrative specialist",
    "office administrator", "pharmacy patient", "hr advisor",
    "operations system support", "mls® data entry",
    "medical call center", "claims clerk", "judicial assistant",
    "integration systems analysis", "payroll implementation",
    "accounts payable coordinator", "universal banker", "valve business",
    "donation services", "purchasing clerk", "workplace coordinator",
    "accounting specialist", "medical records technician",
    "medical support assistant", "patient safety observer",
    "enterprise account executive", "operations assistant",
    "service coordinator", "mobile associate", "mobile installation",
    "production worker", "production underwriter", "veterinarian",
    "maintenance tech", "security officer", "power systems sales",
    "relationship banker", "ehs specialist", "network administrator",
    "optometric assistant", "clinical secretary", "patient observation",
    "ambulatory coder", "medical office receptionist", "patient financial",
    "in-home sales", "personal banking", "automotive technician",
    "laundry gig", "retail sales and store", "sales intern",
    "intermodal csr", "luxury services associate", "adjunct faculty",
    "energy sales specialist", "infrastructure sales specialist",
    "industrial automation", "chronic care specialty sales",
    "implant direct regional", "insurance agent",
    "cabinet designer", "crop inputs sales", "modular homes",
    "counter sales", "aircraft fueler", "sterile processing",
    "special education teacher", "librarian", "accounting intern",
    "civilian pay technician", "geek squad", "retail banker",
    "inside sports sales", "registered dietician", "document controller",
    "station administrator", "caregiver assisted", "hvac install",
    "painting project", "judiciary clerk", "home cleaner",
    "audio video technician", "financial services entrepreneur",
    "in-store sales", "ticket agent", "parts customer service",
    "greek life sales", "retail sales representative",
    "delivery assurance", "sterile processing",
    "badge over only", "talent community", "pmhnp",
    # Batch 2 — physical / non-remote titles
    "receptionist", "bank teller", "personal banker", "deposit operations",
    "material handler", "cna", "certified nursing assistant", "lvn ",
    "walkthrough agent", "emissions inspector", "data center switchgear",
    "satellite communication technician", "print support specialist",
    "studio manager", "cad operator", "unit secretary", "monitor tech",
    "hotel reservations agent", "(d2d)", "door-to-door",
    "solar appointment setter", "hvac customer service",
    "industrial energy sales", "commercial tire sales",
    "medical device sales representative", "1099 medical device",
    "automotive business development", "batchman",
    "not remote",
    # Batch 3 — more physical / specialized
    "busperson", "ultrasound technologist", "kindergarten teacher",
    "teacher assistant", "inpatient coding", "cyber & tech risk",
    "endoscopy", "clinical specialist, pain", "spine - charleston",
    "charter sales", "kitchen and bath designer", "patient care coordinator",
    "hair stylist", "recruiting coordinator temporary",
    "billeting clerk", "operator in training", "direct sales representative",
    "outside/inside sales", "router - commercial",
]

# Physical companies to reject
REJECT_COMPANIES = [
    "walmart", "uchealth", "h&m", "planet fitness", "orkin",
    "srs distribution", "quality containers", "syneos health",
    "travel + leisure", "wyndham", "acme markets", "albertsons",
    "safeway", "shaw", "ferguson", "bilinski", "toromont",
    "breakthru beverage", "precision well", "moia",
    "fedex", "ryder system", "o'reilly auto", "nova hotels",
    "morgan auto", "atlantic hospitality", "culligan",
    "state farm", "green energy supplies", "mastercraft boating",
    "destination knot", "digiwerks", "elemental terra",
    "rei automated", "rise up kings", "dr lift spa",
    "qua nutrition", "bellota labs", "serverfarm",
    "akima", "art woods", "audacy", "cedar park group",
    "cowie construction", "defense holdings", "hennepin county",
    "hydro solution", "kognitive sales", "legrand",
    "hard roc academy", "coverpoint partners", "corpay",
    "sunny days vacation", "radar shop", "retirement solution",
    "voyage casselman", "willson international", "smile club",
    "lowe's", "mayfield paper", "michelli", "moffitt",
    "national dentex", "novartis", "petco", "primoris",
    "state of south carolina", "sigma relocation", "top turf",
    "unc health", "knipper health", "legacy hospice",
    "streeter group", "phsa", "primerica", "prlogistics",
    "protocall", "verdechem", "jack henry", "athena marble",
    "american roller", "chajinel", "spoton roofing",
    "12go asia", "alx africa", "financial times", "whatnot",
    "rws trainai", "machinify", "launchbrightly", "ping identity",
    "jmac lending", "aera technology",
    "jonview", "howard hanna",
    "therapeutic indulgence", "vermont judiciary", "bmo financial",
    "asap/netsource", "anix valve", "ameris bank", "cavalier inc",
    "flex dental", "goodwill", "greenlite lighting", "hitachi",
    "nis solutions", "ole mexican", "sms appliances",
    "thunder bay", "thurston elite", "valhalla performance",
    "fd thomas", "lendingtree", "variety insurance",
    "integrity marketing", "premier services", "cirrus fitness",
    "pac group", "us department of veterans",
    "st. marys medical", "ugenticai", "american modern insurance",
    # LinkedIn spam farms / MLM / aggregators
    "resonance cx", "careers in travel", "destination planners",
    "crossing hurdles", "home health focus", "lensa",
    "prism data", "mosaic talent", "carestream advisory",
    "allen cw group", "beaudry life", "walker financial",
    "marissa turner", "cyber focus ai", "orchestrate consulting",
    "work from home co", "remotehunter", "cherrie give",
    "peachtree va", "wiraa", "nousu", "jak travel",
    "system4 facility", "machine learning 1",
    "1840 staffing", "dunhill staffing", "mercor",
    # Physical / on-site
    "accentcare", "enterprise mobility", "declaration networks",
    "the realreal", "amuse-o-matic", "a. o. smith",
    "vail health", "ensemble health", "spacelabs",
    "dataone systems", "esquared electric", "fort bragg",
    "nc eagle auto", "td bank", "canon canada",
    "newfoundland and labrador health",
    # On-site / freelance platforms
    "penfed", "dataannotation",
    # Physical / on-site companies (batch 2)
    "valmont industries", "heartland co-op", "deep trekker",
    "bar jus", "sprout therapy", "capital remodeling", "transdev",
    "victaulic", "the master group", "busey bank", "alarm detection",
    "evans delivery", "e&m technologies", "miner, ltd", "leer group",
    "prisma health", "mott community college", "the home depot",
    "hobart canada", "medical college of wisconsin", "lifestance health",
    "blue cross blue shield", "feditc", "ennoble care", "golden customer care",
    "baycare health", "weed man", "edlen electrical", "bernalillo county",
    "t-mobile", "crothall healthcare", "castle group", "security mutual life",
    "cornerstone bank", "stanley black", "labcorp", "vanderbilt mortgage",
    "cambio property", "thompson machinery", "saint-gobain",
    "hero plumbing", "marriott", "zippertubing", "gn store nord",
    "blanchard valley", "coca-cola bottling", "hungerrush",
    "santa monica seafood", "poplin", "sylvite", "education & training resources",
    "amerivet", "compass group", "oerlikon", "envista holdings",
    "wakemed", "visionworks", "smart start", "innovage",
    # MLM / scam / aggregators (batch 2)
    "traveling with michaila", "luxury escape", "bilingual source",
    "flexpath travel", "majestic vacations", "ok media marketing",
    "pinnacle method", "jobs via dice", "careerscape", "gloura",
    "my reservation people", "ponkedout", "independent advisor representative",
    "ian graham agency", "kedziora business", "bb gets you moving",
    "key one capital",
    # Batch 3 — physical / on-site
    "klappenberger", "oscardo", "jostens", "new jersey courts",
    "cargill", "thomas, large", "advanced surgi-pharm", "leaf home",
    "bath & cabinet", "lansing sanitary", "osada construction",
    "trivantage", "brown & brown insurance", "cardinal homes",
    "centerstone", "specialized merchandising", "a+ outdoor",
    "david gooding", "university of new mexico", "fidelity investments",
    "allied osi", "benaa surfaces", "sentara", "henry ford health",
    "cityview audiology", "canadian nuclear", "yale university",
    "bridge appliances", "dawn foods", "us military treatment",
    "avenue living", "hunt military", "wayne bank", "tuff shed",
    "american vision windows", "loyal seal", "state employees credit union",
    "maids of bay area", "family health west", "green star exteriors",
    "senville", "belmont village", "ks audiovideo", "home care heating",
    "6 & fix", "commercial services company", "flexibleassembly",
    "wellspan health", "lp building solutions", "transcanada credit",
    "freeway insurance", "cowi", "celebright", "inland technologies",
    "premium waters", "automated system design", "custom building products",
    "city of chattanooga", "boston express bus", "kids dental brands",
    "women's health connecticut", "jitterbit", "nothum food",
    "ascendum machinery", "vital record control", "best buy",
    "purpose unlimited", "babco sales", "revspring",
    "strobert tree", "ace cooling", "uc san diego health",
    "mse advisors", "unitypoint health", "food bank for larimer",
    "state of illinois", "poway dermatology", "california institute of applied",
    "waste connections", "trigo adr", "nations roof",
    # Batch 3 — scam / MLM / aggregators
    "unified cx solutions", "ss staffing", "vlad cherchenko",
    "socialmind", "delco innovations", "pfs services",
    "leb insurance", "gigabrands", "legacy advocate",
    "gensales", "j and a management", "rescom products",
    "superior health coverage", "rylux studio", "ebm scholars",
    "senior health solutions", "halo connect", "careerxperts",
    "bokeh creatives", "container one",
    # Batch 4 — persistent re-adds
    "bold business", "seasoned recruitment", "dxc technology",
    "zudy", "s r international", "fba ", "peak6", "basis.ed",
    "clear results", "canadian addiction", "brightharbor",
    "paul bickford", "blitzscale broker", "quad film",
    "the harris group",
    # Batch 5 — on-site / non-remote
    "lockheed martin", "clinique podiatrique", "shuddl",
    "gale pacific", "tigertel",
    # Batch 6 — on-site / physical / scam
    "anheuser-busch", "great lakes wholesale", "stern therapy",
    "clifford power", "fleetwood bank", "frazier contracting",
    "avardis health", "prohome", "kaiser permanente",
    "aventiv technologies", "imcd", "capital management services",
    "carolina health specialists", "underdog roofing",
    "american 1 credit union", "vox media", "mayroom",
    "stratus building solutions", "dix performance",
    "national power management", "canadian north",
    "animal humane society", "whitten and lublin",
    "happy house services", "ipf sourcing", "texas republic bank",
    "westgate resorts", "diversified electric", "alpha power restoration",
    "kilgore college", "mueller landscape", "hallmark building supplies",
    "personal-touch home care", "manitoba liquor", "susi family",
    "ars/rescue rooter", "global imaging usa", "dap products",
    "hotwire communications", "inspiring physicians",
    "meridian trust", "central state supply", "winter sports inc",
    "alpine bank", "city of edmonton", "sentry",
    "first citizens bank", "east carolina university",
    "nationwide credit corporation", "cy-fair tire",
    "motiv medical", "proterial", "omni tax help",
    "mercury systems", "mid-america catastrophe",
    "allmark door", "nextplay jobs", "jersey mail systems",
    "ricoh", "corrigan moving", "monument grill",
    "toto usa", "kabafusion", "ies holdings", "ies communications",
    "mngi digestive", "farmers insurance", "wildbrain",
    "handyman connection", "producers xl", "frontstreet facility",
    "northern light health", "raventek", "easter seals",
    "staffeagle", "pod health", "aggreko",
    "a-1 heating", "spo management", "finch thornton",
    "service lane eadvisor", "minus33", "word & brown",
    "medkoder", "darcars", "cibc", "crescent electric",
    "sauk prairie", "711 materials", "casella waste",
    "aaron's", "builders firstsource", "superior solar",
    "children's national hospital", "witham family hotels",
    "cala sourcing", "summit state bank", "catawba county",
    "camis inc", "alabama one credit", "roers companies",
    "edfinancial", "abc home & commercial", "clarity partners",
    "central power systems", "dekra", "ramey-estep",
    "east coast lubricant",
    # Scam / MLM / spam
    "systems thinking & solutions", "silverlight research",
    "elite ceos", "eagru services", "create your life",
    "7 figure dojo", "mojo's hemp house", "talentify",
    "secrets of a scholar", "pad services",
    # Batch 7 — on-site / physical / non-remote
    "evergreen lawn care", "hilton", "franke", "reedy & company",
    "communitycare health", "everett public schools", "state of texas",
    "aaa i services", "key safety", "parallon", "hsb",
    "pitchbook", "centex foundation", "kao corporation",
    "capital air express", "perfectrx", "law offices of dean lloyd",
    "southwind", "robert half", "c. wright and associates",
    "ecm carpentry", "unm hospital", "flags.com",
    "galapagos conservancy", "check off your list",
    "billyard insurance", "brenkus team", "nabla",
    "air systems engineering", "tailored brands",
    "regal rexnord", "turner, wood", "herespa", "aimhire",
    "spacex", "nms",
    # Batch 7 — scam / MLM / spam
    "muslim marketing mastery", "pristine air solutions",
    "remote chess academy", "titan reconstruction",
    "hello sunshine travels", "wanderful excursions",
    "five star bath solutions", "extraordinary love",
    "envestnet institute", "novacomm", "virtual vantage",
    "sundayy", "latamcent", "levi zion", "syntopia ai",
    "livebuzz studio", "mercier consultancy",
    # Non-US/CA
    "cloudbeds", "fluxon", "getresponse",
]


def _detect_tags(company: str) -> tuple[str, str]:
    """Return (equipment, hiring_speed) for a company."""
    cl = company.lower().strip()
    eq = "unknown"
    speed = "unknown"
    for key, val in EQUIPMENT_MAP.items():
        if key in cl:
            eq = val
            break
    for key, val in SPEED_MAP.items():
        if key in cl:
            speed = val
            break
    return eq, speed


def _is_junk(title: str, company: str, location: str = "") -> bool:
    """Reject non-remote / physical jobs."""
    tl = title.lower()
    cl = company.lower().strip()
    ll = location.lower()
    if cl in ("nan", "") or not company.strip():
        return True
    if any(ord(c) > 0x3000 for c in cl):
        return True
    # Reject non-US/CA locations
    non_usca = ["india", "philippines", "stockholm", "london", "berlin",
                "amsterdam", "paris", "tokyo", "singapore", "australia",
                "nigeria", "kenya", "south africa", "brazil", "mexico",
                "argentina", "colombia", "pakistan", "bangladesh",
                "peru", "greece", "portugal", "manila", "ncr/manila"]
    if any(loc in ll for loc in non_usca):
        return True
    if any(r in cl for r in REJECT_COMPANIES):
        return True
    if any(r in tl for r in REJECT_TITLES):
        return True
    return False


async def run_all_scrapers(session: AsyncSession) -> int:
    total_new = 0

    # 1. RemoteOK (API — always works)
    try:
        scraper = RemoteOKScraper()
        raw_jobs = await scraper.scrape()
        new_count = await _save_jobs(session, raw_jobs, "remoteok")
        total_new += new_count
        logger.info(f"RemoteOK: {len(raw_jobs)} found, {new_count} new")
        await scraper.close()
    except Exception as e:
        logger.error(f"RemoteOK failed: {e}")

    # 2. WeWorkRemotely (100% remote jobs)
    try:
        scraper = WeWorkRemotelyScraper()
        raw_jobs = await scraper.scrape()
        new_count = await _save_jobs(session, raw_jobs, "weworkremotely")
        total_new += new_count
        logger.info(f"WeWorkRemotely: {len(raw_jobs)} found, {new_count} new")
        await scraper.close()
    except Exception as e:
        logger.error(f"WeWorkRemotely failed: {e}")

    # 3. JobSpy (Indeed + ZipRecruiter + Glassdoor + LinkedIn)
    try:
        raw_jobs = await scrape_jobspy()
        new_count = await _save_jobs(session, raw_jobs, "indeed")
        total_new += new_count
        logger.info(f"JobSpy (multi-source): {len(raw_jobs)} found, {new_count} new")
    except Exception as e:
        logger.error(f"JobSpy failed: {e}")

    return total_new


async def _save_jobs(session: AsyncSession, raw_jobs: list[RawJob], source: str) -> int:
    new_count = 0

    for raw in raw_jobs:
        if _is_junk(raw.title, raw.company, raw.location):
            continue

        # Dedup by URL
        existing = await session.execute(
            select(Job).where(Job.url == raw.url)
        )
        if existing.scalar_one_or_none():
            continue

        # Dedup by title + company (same job, different URL)
        existing2 = await session.execute(
            select(Job).where(Job.title == raw.title, Job.company == raw.company)
        )
        if existing2.scalar_one_or_none():
            continue

        eq, speed = _detect_tags(raw.company)

        job = Job(
            title=raw.title,
            company=raw.company,
            url=raw.url,
            salary_text=raw.salary_text,
            salary_min=raw.salary_min,
            salary_max=raw.salary_max,
            location=raw.location,
            country=raw.country,
            source=SOURCE_MAP.get(raw.source_site or source, JobSource.REMOTEOK),
            description=raw.description,
            tags=raw.tags,
            equipment=eq,
            hiring_speed=speed,
        )
        session.add(job)
        new_count += 1

    await session.commit()
    return new_count


if __name__ == "__main__":
    # Cron entry point: `python3 -m backend.scrapers.manager` — refresh the scraped pool.
    import asyncio

    from backend.models.database import async_session

    async def _main():
        async with async_session() as session:
            n = await run_all_scrapers(session)
            print(f"scrapers done: {n} new jobs")

    asyncio.run(_main())
