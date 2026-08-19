"""Deterministic JD keyword extraction + résumé match scoring (no API key needed).

Used to (a) rank which jobs to apply to, and (b) drive the deterministic tailor.
A curated customer-support / tooling lexicon is matched against the job description;
multi-word terms (e.g. "customer support", "live chat") are matched on word boundaries.
"""
import re

# Curated support / CX / tooling vocabulary. Multi-word entries matched as substrings.
LEXICON = [
    "customer support", "customer service", "customer experience", "customer success",
    "technical support", "tech support", "help desk", "helpdesk", "live chat",
    "chat support", "email support", "phone support", "inbound", "outbound",
    "tier 1", "tier 2", "tier 3", "escalation", "escalations", "troubleshooting",
    "ticketing", "tickets", "sla", "csat", "nps", "first response", "resolution",
    "knowledge base", "documentation", "onboarding", "de-escalation", "empathy",
    "zendesk", "intercom", "freshdesk", "salesforce", "service cloud", "hubspot",
    "help scout", "kustomer", "gorgias", "front", "jira", "confluence", "notion",
    "slack", "linear", "asana", "looker", "tableau", "postman", "devtools",
    "api", "sql", "html", "css", "json", "saas", "fintech", "b2b", "b2c",
    "ecommerce", "e-commerce", "billing", "refunds", "account management",
    "written communication", "remote", "work from home", "bilingual", "spanish",
]

# Words too generic to score on — never a salient JD token / "required skill".
# Shared with the ATS scorer (ats_score aliases this) so junk dies in one place.
_GENERIC = {
    "experience", "years", "year", "strong", "ability", "work", "working", "team",
    "role", "skills", "skill", "knowledge", "plus", "background", "responsibilities",
    "requirements", "qualifications", "company", "candidate", "position", "job",
    "hiring", "looking", "join", "help", "support",
}

# Common English words that are NOT job skills — keeps the frequent-token augmentation
# from emitting junk ("about", "grow", "offer", "closely") as if it were a keyword,
# which otherwise inflates the match denominator and pollutes injected skills.
_COMMON = set("""about above after again all also any because been before being below
between both but did do does doing down during each few more most other over own same
some such than then there these they those through too under until very was were what
when where which while who whom why would could may might into out off once how only get
got make made take taken use used using like even still back way new good great across
within per via etc example examples grow offer offering environment according closely
ensure ensuring provide providing including include includes needs need required require
look joining able able across day days week weeks month months full part date based office
onsite hybrid shift night day salary benefits companies well able across able
number numbers daily weekly monthly various additional relevant appropriate necessary
information detail details item items task tasks general specific overall multiple several
including ensure ensuring provide providing manner timely accurate accurately effectively
efficiently proper properly related regarding upon toward towards etc able""".split())

_STOP = set("""a an the and or for to of in on at with from by as is are be this that
your you we our their it its will can must should have has had not no yes role team
work working experience years year customer support help job apply please""".split()) | _GENERIC | _COMMON

# Compiled per-term boundary patterns, cached (extract runs per job in ranking loops).
_TERM_RES: dict[str, re.Pattern] = {}


def _norm(text: str) -> str:
    return (text or "").lower()


def term_present(term: str, text: str) -> bool:
    """True if `term` occurs in `text` (both lowercase) on word boundaries.

    Raw substring matching let 'chat' hit 'ChatGPT' and 'front' hit 'frontend',
    polluting scores and queueing wrong-discipline roles. Multi-word terms tolerate
    any whitespace run between words.
    """
    rx = _TERM_RES.get(term)
    if rx is None:
        parts = [re.escape(p) for p in term.lower().split()]
        rx = re.compile(r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])")
        _TERM_RES[term] = rx
    return bool(rx.search(text))


def extract_jd_keywords(jd_text: str, top: int = 25) -> list[str]:
    jd = _norm(jd_text)
    found: list[str] = []
    for term in LEXICON:
        if term_present(term, jd) and term not in found:
            found.append(term)
    # Augment with frequent salient single tokens not already covered
    tokens = re.findall(r"[a-z][a-z+#.\-]{2,}", jd)
    freq: dict[str, int] = {}
    for t in tokens:
        t = t.strip(".-")  # sentence punctuation glued to the token is not the token
        if t in _STOP or len(t) < 4:
            continue
        if any(t in term for term in found):
            continue
        freq[t] = freq.get(t, 0) + 1
    extra = sorted(freq, key=lambda k: -freq[k])
    for t in extra:
        if len(found) >= top:
            break
        found.append(t)
    return found[:top]


def match_score(jd_text: str, cv_text: str) -> int:
    """0-100 share of JD keywords present in the résumé text."""
    kws = extract_jd_keywords(jd_text)
    if not kws:
        return 0
    cv = _norm(cv_text)
    hit = sum(1 for k in kws if term_present(k, cv))
    return round(100 * hit / len(kws))


def missing_keywords(jd_text: str, cv_text: str) -> list[str]:
    cv = _norm(cv_text)
    return [k for k in extract_jd_keywords(jd_text) if not term_present(k, cv)]


def overlap(text: str, keywords: list[str]) -> int:
    t = _norm(text)
    return sum(1 for k in keywords if term_present(k, t))
