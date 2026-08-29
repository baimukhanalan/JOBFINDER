"""Deterministic role-category classifier for catalog job titles.

Same shape as `applier/regions.py`: a pure function run at collect time (and in a
backfill pass) that maps a job title (+ department as a tiebreaker) to one of a
fixed set of functional categories, with a `source` tag. Ordered keyword rules,
FIRST match wins — the order is what resolves the overlaps.

The ordering encodes the boundaries the classification fleet actually drew on the
live catalog (its labels are the regression fixture this is tuned against):
  * a "Sales Engineer"/"Support Engineer" is Sales/Support, not Engineering
    (checked before Engineering);
  * an "ML/AI/Data Engineer" is Engineering (any *engineer* title), while
    Data & ML is the SCIENTIST/ANALYST function (Data Scientist, Data Analyst,
    Research Scientist, Analytics) — Engineering is checked before Data & ML;
  * "Design Engineer"/"UX Engineer" is Engineering, so Design requires an actual
    designer/UX role and is checked after Engineering;
  * "Sales Operations" is Operations, so the Sales rule excludes "... operations".

A title the rules don't recognize falls to ``Other`` with source ``unknown`` (an
optional LLM residue pass can refine it). No network, no brand/stack strings.
"""
from __future__ import annotations

import re

CATEGORIES = [
    "Sales / GTM", "Customer Support & Success", "Engineering", "Data & ML",
    "Product", "Design", "Marketing & Comms", "People & Recruiting",
    "Finance & Accounting", "Operations", "Legal & Compliance",
    "Executive / Leadership", "Other",
]

# (pattern, category) — evaluated top to bottom, first hit wins. Order matters.
_RULES: list[tuple[str, str]] = [
    # 1) C-suite / founders first, so "Chief X Officer" is Executive not its function
    (r"\bchief \w+( \w+)? officer\b|\b(ceo|cfo|cto|coo|cmo|cro|cpo|ciso|cio|cco|chro)\b"
     r"|\bco[- ]?founder\b|\bfounder\b", "Executive / Leadership"),
    # 2) People & Recruiting ("Recruiter, Sales" / "Technical Recruiter" are People)
    (r"\brecruit|talent acquisition|\btalent\b|\bsourcer\b|people operations|people ops"
     r"|human resources|\bhr\b|\bhrbp\b|employee relations|learning (and|&) development"
     r"|\bl&d\b|compensation (and|&) benefits|\bbenefits\b|workforce|people (business )?partner|\bpeople\b|total rewards|\bhris\b|\bc&b\b",
     "People & Recruiting"),
    # 3) Customer Support & Success (post-sale relationship roles incl. account managers)
    (r"customer (success|support|service|experience|care)|client (success|services|experience)"
     r"|technical support|member services|provider services|\bpatient\b|care coordinator"
     r"|care navigator|\bonboarding\b|implementation|help ?desk|service desk|contact center"
     r"|call center|\bcsm\b|technical account manager|account manager|customer solution"
     r"|deployment strategist|support (specialist|engineer|analyst|representative|rep|advocate|associate)|professional services|engagement manager|support agent",
     "Customer Support & Success"),
    # 4) Sales / GTM (but "... operations" is Operations, excluded via lookahead)
    (r"account executive|account director|business development|\bsdr\b|\bbdr\b"
     r"|\bsales\b(?! operations)|partnerships?|partner manager|\bchannel\b|\bgtm\b|go-to-market"
     r"|client partner|solutions? (engineer|consultant|architect)|pre-?sales|\balliance"
     r"|account management|\bpipeline\b|\bterritory\b|inside sales|field sales|enterprise sales"
     r"|\bquota\b|\bseller\b", "Sales / GTM"),
    # 5) Engineering — any *engineer* title (incl. ML/AI/Data/Security engineers)
    (r"\bengineer\b|\bengineering\b|developer|\bdevops\b|\bsre\b|site reliability|infrastructure"
     r"|backend|front-?end|full-?stack|mobile (engineer|developer)|\bios\b|\bandroid\b|\bqa\b"
     r"|\bsdet\b|firmware|embedded|\bsoftware\b|architect|programmer|platform engineer|information technology|information security|offensive security|vulnerability|\bsalesforce\b|technical staff|\bqe\b",
     "Engineering"),
    # 6) Data & ML — the scientist / analyst / data-science function (non-engineer)
    (r"data scientist|data analyst|machine learning|\bml scientist\b|research scientist"
     r"|applied scientist|decision scientist|data science|business intelligence|\banalytics\b"
     r"|quantitative (analyst|research)|\bstatistician\b|econometric|\bmle\b|biostatistic|bioinformatic|applied ai", "Data & ML"),
    # 7) Product (contiguous "product <role>", so "Product Marketing" falls through)
    (r"\bproduct (manager|owner|management|lead|analyst|ops|operations|director)\b"
     r"|\bgroup product\b|technical product manager|\bcpo\b|head of product", "Product"),
    # 8) Design (engineers already claimed above)
    (r"\bdesigner\b|\bux\b|\bui\b|user experience|user interface|graphic design|visual design"
     r"|\bcreative\b|illustrat|motion design", "Design"),
    # 9) Marketing & Comms
    (r"product marketing|\bmarketing\b|demand gen|\bseo\b|\bsem\b|communications?|\bcomms\b"
     r"|public relations|\bpr\b|social media|paid (social|media|ads|search)|media buyer"
     r"|programmatic|field marketer|performance marketing|lifecycle marketing|\bcommunity\b"
     r"|copywriter|editorial|\bcontent\b|brand (manager|strateg|marketing|lead|director)"
     r"|\bpmm\b|influencer|\baffiliate\b|\bevents?\b|\bgrowth\b|audience development|e-?commerce", "Marketing & Comms"),
    # 10) Operations
    (r"revenue operations|sales operations|business operations|marketing operations|\brevops\b"
     r"|\bbiz ops\b|\boperations\b|program manager|project manager|\bpmo\b|supply chain"
     r"|\blogistics\b|\bprocurement\b|chief of staff|\bfulfillment\b|\bworkplace\b|\bfacilities\b"
     r"|strategy (and|&) operations|\benablement\b|administrative|office manager|business analyst"
     r"|\bportfolio\b|\bpricing\b|category manager|\bvendor\b|\bdispatch\b|district manager|quality (assurance|control)|strategic initiatives|\btrading\b", "Operations"),
    # 11) Finance & Accounting
    (r"\bfp&a\b|\bfinanc|accounting|accountant|\bcontroller\b|\bpayroll\b|\btreasury\b|\btax\b"
     r"|\baudit|bookkeep|accounts (payable|receivable)|\bbilling\b|underwrit|actuar|revenue cycle|\btrader\b|finops|incentive compensation|revenue compensation|\bsec\b (reporting|analyst)",
     "Finance & Accounting"),
    # 12) Legal & Compliance
    (r"\blegal\b|\bcounsel\b|\battorney\b|\bparalegal\b|\bcompliance\b|\bregulatory\b|\bprivacy\b"
     r"|\bgovernance\b|\blitigation\b|\brisk\b|contracts? (manager|specialist|counsel)|\blawyer\b|\baml\b|money laundering|sanctions|surveillance|pharmacovigilance|\blicensing\b|public policy|government affairs",
     "Legal & Compliance"),
    # 13) Leftover leadership -> Executive (a function-specific VP/Head was caught above)
    (r"general manager|managing director|\bpresident\b|vice president|\bvp\b|head of"
     r"|managing partner|executive director", "Executive / Leadership"),
]

_COMPILED = [(re.compile(p, re.I), cat) for p, cat in _RULES]


def _match(text: str) -> str | None:
    if not text:
        return None
    t = f" {text.lower()} "
    for rx, cat in _COMPILED:
        if rx.search(t):
            return cat
    return None


def classify_role(title: str | None, department: str | None = "") -> tuple[str, str]:
    """Return (category, source). source='rule' on a title or department hit,
    else ('Other', 'unknown')."""
    hit = _match(title or "")
    if hit:
        return hit, "rule"
    hit = _match(department or "")
    if hit:
        return hit, "rule"
    return "Other", "unknown"
