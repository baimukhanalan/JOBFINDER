"""Verified target roster: fast-hire fully-remote US/CA customer-support & gig targets.

Each entry's apply_url was checked to be an official company/platform domain (not a
third-party reposting, never LinkedIn). Tiers are ordered by speed-to-start.
Used as the hardcoded watchlist the apply engine prioritises.
"""

# tier -> list of {name, channel, ats, apply_url, country, employment}
ROSTER: dict[str, list[dict]] = {
    "tier1_gig_fastest": [
        {"name": "DataAnnotation", "channel": "gig-signup", "ats": "proprietary", "apply_url": "https://app.dataannotation.tech/worker_signup", "country": "both", "employment": "1099"},
        {"name": "Prolific", "channel": "marketplace", "ats": "proprietary", "apply_url": "https://app.prolific.com/", "country": "both", "employment": "gig"},
        {"name": "Clickworker / UHRS", "channel": "marketplace", "ats": "proprietary", "apply_url": "https://workplace.clickworker.com/en/registration", "country": "both", "employment": "gig"},
        {"name": "User Interviews", "channel": "marketplace", "ats": "proprietary", "apply_url": "https://www.userinterviews.com/participants/signup", "country": "both", "employment": "gig"},
        {"name": "UserTesting", "channel": "gig-signup", "ats": "proprietary", "apply_url": "https://www.usertesting.com/get-paid-to-test", "country": "both", "employment": "gig"},
        {"name": "CrowdGen by Appen", "channel": "gig-signup", "ats": "proprietary", "apply_url": "https://connect.appen.com/qrp/core/sign-up", "country": "both", "employment": "1099"},
        {"name": "TELUS Digital AI", "channel": "gig-signup", "ats": "proprietary", "apply_url": "https://www.telusinternational.ai/cmp/signup", "country": "both", "employment": "1099"},
    ],
    "tier2_bpo_w2": [
        {"name": "Concentrix", "channel": "bpo-portal", "ats": "workday", "apply_url": "https://jobs.concentrix.com/job-search/", "country": "both", "employment": "W2"},
        {"name": "Foundever", "channel": "bpo-portal", "ats": "successfactors", "apply_url": "https://jobs.foundever.com/go/Work@Home-Jobs/9237700/", "country": "both", "employment": "W2"},
        {"name": "Teleperformance US", "channel": "bpo-portal", "ats": "icims", "apply_url": "https://careersus-teleperformance.icims.com/jobs/intro", "country": "US", "employment": "W2"},
        {"name": "Alorica", "channel": "bpo-portal", "ats": "oracle", "apply_url": "https://www.alorica.com/careers", "country": "US", "employment": "W2"},
        {"name": "Sutherland", "channel": "bpo-portal", "ats": "smartrecruiters", "apply_url": "https://www.jobs.sutherlandglobal.com/Home-office", "country": "both", "employment": "W2"},
        {"name": "TaskUs", "channel": "bpo-portal", "ats": "workday", "apply_url": "https://jobs.taskus.com/", "country": "both", "employment": "W2"},
        {"name": "Conduent", "channel": "bpo-portal", "ats": "phenom", "apply_url": "https://careers.conduent.com/us/en/c/customer-service-transaction-processing-jobs", "country": "US", "employment": "W2"},
        {"name": "ibex", "channel": "bpo-portal", "ats": "icims", "apply_url": "https://uscareers-ibex.icims.com/jobs/intro", "country": "US", "employment": "W2"},
        {"name": "Boldr", "channel": "bpo-portal", "ats": "workable", "apply_url": "https://apply.workable.com/boldr-1/", "country": "both", "employment": "W2"},
        {"name": "SupportNinja", "channel": "bpo-portal", "ats": "adp", "apply_url": "https://www.supportninja.com/careers", "country": "US", "employment": "W2"},
        {"name": "TTEC", "channel": "bpo-portal", "ats": "taleo", "apply_url": "https://www.ttecjobs.com/en/work-from-home", "country": "both", "employment": "W2"},
        {"name": "KellyConnect", "channel": "bpo-portal", "ats": "mykelly", "apply_url": "https://www.mykelly.com/find-jobs/", "country": "both", "employment": "W2"},
    ],
    "tier2b_brand_evergreen": [
        {"name": "Amazon Virtual Customer Service", "channel": "ats-company", "ats": "amazon", "apply_url": "https://hiring.amazon.com/job-opportunities/customer-service-jobs", "country": "US", "employment": "W2"},
        {"name": "U-Haul", "channel": "ats-company", "ats": "proprietary", "apply_url": "https://jobs.uhaul.com/OpenJobs", "country": "US", "employment": "W2"},
        {"name": "Williams-Sonoma WFH CSR", "channel": "ats-company", "ats": "applicantstack", "apply_url": "https://wsgc.applicantstack.com/x/openings", "country": "US", "employment": "W2"},
        {"name": "Hilton WFH Reservations", "channel": "ats-company", "ats": "oracle", "apply_url": "https://jobs.hilton.com/us/en/call-center-and-reservations-jobs", "country": "US", "employment": "W2"},
        {"name": "Marriott CEC", "channel": "ats-company", "ats": "proprietary", "apply_url": "https://careers.marriott.com/career-paths/corporate/customer-engagement-centers/", "country": "US", "employment": "W2"},
        {"name": "CVS / Aetna Remote Care", "channel": "ats-company", "ats": "proprietary", "apply_url": "https://jobs.cvshealth.com/us/en/customer-care", "country": "US", "employment": "W2"},
        {"name": "American Express Virtual Care", "channel": "ats-company", "ats": "eightfold", "apply_url": "https://www.americanexpress.com/en-us/careers/career-areas/customer-service/", "country": "US", "employment": "W2"},
        {"name": "Apple At Home Advisor", "channel": "ats-company", "ats": "apple", "apply_url": "https://jobs.apple.com/en-us/search?team=support-and-service-CRP-SUPP", "country": "US", "employment": "W2"},
        {"name": "Liveops", "channel": "gig-signup", "ats": "fountain", "apply_url": "https://join.liveops.com/apply-now/", "country": "US", "employment": "1099"},
    ],
    "tier3_remote_first": [
        {"name": "Automattic", "channel": "ats-company", "ats": "greenhouse", "apply_url": "https://job-boards.greenhouse.io/automatticcareers", "country": "both", "employment": "W2"},
        {"name": "Zapier", "channel": "ats-company", "ats": "ashby", "apply_url": "https://jobs.ashbyhq.com/zapier", "country": "both", "employment": "W2"},
        {"name": "GitLab", "channel": "ats-company", "ats": "greenhouse", "apply_url": "https://job-boards.greenhouse.io/gitlab", "country": "both", "employment": "W2"},
        {"name": "Toptal", "channel": "ats-company", "ats": "lever", "apply_url": "https://jobs.lever.co/toptal", "country": "both", "employment": "W2"},
        {"name": "Remote.com", "channel": "ats-company", "ats": "greenhouse", "apply_url": "https://job-boards.greenhouse.io/remotecom", "country": "both", "employment": "W2"},
        {"name": "Deel", "channel": "ats-company", "ats": "ashby", "apply_url": "https://jobs.ashbyhq.com/deel", "country": "both", "employment": "W2"},
        {"name": "1Password", "channel": "ats-company", "ats": "ashby", "apply_url": "https://jobs.ashbyhq.com/1password", "country": "both", "employment": "W2"},
        {"name": "Close", "channel": "ats-company", "ats": "ashby", "apply_url": "https://jobs.ashbyhq.com/close", "country": "US", "employment": "W2"},
    ],
    "tier4_flex_canada": [
        {"name": "Working Solutions", "channel": "gig-signup", "ats": "proprietary", "apply_url": "https://apply.workingsolutions.com/", "country": "both", "employment": "1099"},
        {"name": "NexRep", "channel": "marketplace", "ats": "proprietary", "apply_url": "https://nexrep.com/agents/", "country": "US", "employment": "1099"},
        {"name": "Chatdesk", "channel": "ats-company", "ats": "jazzhr", "apply_url": "https://www.chatdesk.com/expert-signup-form", "country": "US", "employment": "1099"},
        {"name": "ModSquad", "channel": "ats-company", "ats": "workday", "apply_url": "https://modsquad.wd5.myworkdayjobs.com/ModSquad_Contractor", "country": "both", "employment": "1099"},
        {"name": "Foundever Canada", "channel": "bpo-portal", "ats": "successfactors", "apply_url": "https://jobs.foundever.com/go/Work-at-Home-Canada/9237500/", "country": "CA", "employment": "W2"},
        {"name": "Concentrix Canada", "channel": "bpo-portal", "ats": "workday", "apply_url": "https://jobs.concentrix.com/job-search/?country=Canada", "country": "CA", "employment": "W2"},
        {"name": "Nordia", "channel": "bpo-portal", "ats": "ultipro", "apply_url": "https://recruiting.ultipro.ca/NOR5102NODV/JobBoard/", "country": "CA", "employment": "W2"},
    ],
}

# Targets to AVOID (scam / pay-to-work / dead).
AVOID = [
    "Arise (pay-to-work; FTC $7M settlement)",
    "Outlier/Remotasks (2026 non-payment reports)",
    "The Chat Shop / Needle (rarely hiring)",
    "Indeed Apply / LinkedIn auto-submit (account ban risk)",
]


# Per-employer remote-hire eligibility (June 2026 audit). A GUIDE — always verify on the live
# posting, since BPO campaigns shift. excluded_us_states = states they do NOT hire remote CS from.
ELIGIBILITY = {
    # gig: every state, no exclusions
    "DataAnnotation": {"us": True, "ca": True, "excluded_us_states": [], "kind": "gig"},
    "Prolific": {"us": True, "ca": True, "excluded_us_states": [], "kind": "gig"},
    "Clickworker / UHRS": {"us": True, "ca": True, "excluded_us_states": [], "kind": "gig"},
    "User Interviews": {"us": True, "ca": True, "excluded_us_states": [], "kind": "gig"},
    # W2 BPO (known hard exclusions)
    "TTEC": {"us": True, "ca": True, "excluded_us_states": ["AK", "CA", "HI", "MT"], "kind": "bpo"},
    "Teleperformance US": {"us": True, "ca": False, "excluded_us_states": ["CA", "CO", "HI", "WA"], "kind": "bpo"},
    "Concentrix": {"us": True, "ca": True, "excluded_us_states": ["CA", "AK", "HI"], "kind": "bpo"},
    "Conduent": {"us": True, "ca": False, "excluded_us_states": ["AK", "CA", "HI", "MA", "IL", "MT"], "kind": "bpo"},
    "Alorica": {"us": True, "ca": True, "excluded_us_states": [], "kind": "bpo"},
    "Foundever": {"us": True, "ca": True, "excluded_us_states": [], "kind": "bpo"},
    "Sutherland": {"us": True, "ca": True, "excluded_us_states": [], "kind": "bpo"},
    "KellyConnect": {"us": True, "ca": False, "excluded_us_states": [], "kind": "bpo"},
    "U-Haul": {"us": True, "ca": True, "excluded_us_states": [], "kind": "bpo"},
    "Amazon Virtual Customer Service": {"us": True, "ca": False, "excluded_us_states": [], "kind": "bpo"},
    # 1099 flex
    "Working Solutions": {"us": True, "ca": True, "excluded_us_states": ["CA", "NY", "PA", "WA"], "kind": "1099"},
    "Liveops": {"us": True, "ca": False, "excluded_us_states": [], "kind": "1099"},
}


def eligible_for_state(state: str) -> list[str]:
    """Employers that hire remote CS from a 2-letter US state (a guide — verify on the posting)."""
    st = (state or "").upper().strip()
    out = []
    for name, e in ELIGIBILITY.items():
        if not e.get("us"):
            continue
        if st and st in [s.upper() for s in e.get("excluded_us_states", [])]:
            continue
        out.append(name)
    return out


def all_entries() -> list[dict]:
    out = []
    for tier, items in ROSTER.items():
        for e in items:
            out.append({**e, "tier": tier})
    return out


def by_ats(ats: str) -> list[dict]:
    return [e for e in all_entries() if e.get("ats") == ats]
