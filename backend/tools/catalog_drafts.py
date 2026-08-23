"""A→Z application-draft generator for the job catalog.

For each catalog job it builds the COMPLETE fill-packet a semi-auto applier needs:
a résumé tailored to the JD (from the candidate's REAL profile — no fabrication) plus
an answer for EVERY scraped question, routed by kind:

  * identity/contact  -> deterministic from the profile (name/email/phone/links)
  * work-eligibility  -> deterministic from the profile (region-matched candidate ->
                         "authorized: yes", "sponsorship: no", country select)
  * closed selects    -> validated option index (deterministic rules + LLM choose_options)
  * open text         -> grounded LLM draft, behavioural answers flagged [review]
  * file uploads      -> the generated résumé PDF (+ cover letter)
  * video / Loom      -> flagged human-only (cannot be auto-filled)

The candidate is picked by REGION (US job -> US-authorized person, CA -> Canadian) —
the agency's "for America, an American" rule. Runs OFFLINE against the questions
already in Postgres (no browser), so a full batch is fast + cheap. The packet is
stored in job_catalog.draft and reviewed on the /drafts dashboard page; the live
in-browser pre-fill is a separate downstream step.

    python -m backend.tools.catalog_drafts --limit 150        # representative batch
    python -m backend.tools.catalog_drafts --all              # every eligible job
    python -m backend.tools.catalog_drafts --job 12345        # one job (smoke test)
    python -m backend.tools.catalog_drafts --limit 150 --no-ai # deterministic only
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backend.services.tailor.answers import cover_letter, draft_answers
from backend.services.tailor.choices import (_extract_array, choose_options,
                                             deterministic_choices)
from backend.services.tailor.render import render_text
from backend.services.tailor.tailor import _llm_complete, tailor_resume
from backend.tools import catalog_db

DATA = Path(__file__).resolve().parents[1] / "data"

# ---- question routing -------------------------------------------------------
_FILE_TYPES = {"file", "input_file"}
_SELECT_TYPES = {"select", "multi_value_single_select", "multi_value_multi_select", "choice"}
_MULTI_TYPES = {"multi_value_multi_select"}
_VIDEO_RE = re.compile(r"(?i)\b(video|loom|vidyard|screen[- ]?record|record (?:a|yourself|your)|"
                       r"voice (?:note|recording)|audio recording)\b")
_COVER_RE = re.compile(r"(?i)cover letter|motivation letter|why do you want")
# A headshot/photo upload has no auto-fill source (we hold a résumé, never a portrait) —
# dumping the résumé PDF into a "Photo" field is wrong-file garbage, so flag it human-only.
_PHOTO_RE = re.compile(r"(?i)\b(photo|photograph|head\s?shot|selfie|"
                       r"profile (?:picture|photo|image)|your (?:picture|photo|image)|"
                       r"upload (?:a |your )?(?:picture|image|headshot))\b")
# Specialized document uploads that are NEVER the résumé PDF — attaching a CV to a medical
# report / ID / diploma field is a misrepresentation. Human-only (incl. multilingual forms).
_HUMAN_FILE_RE = re.compile(
    r"(?i)\b(medical (?:report|certificate|record)|health certificate|laudo|atestado|"
    r"disability (?:report|certificate|code)|\bcid\b|"
    r"passport|national id|identity (?:document|card|proof)|driver'?s licen[cs]e|"
    r"diploma|transcript|degree certificate|"
    r"reference letter|recommendation letter|"
    r"bank statement|proof of address)\b")

# Voluntary EEO/diversity self-ID questions — a SYNTHETIC persona must never claim a
# protected characteristic (a false 'Person with disability' / veteran / ethnicity), and a
# real candidate's demographics stay blank by policy. Detect by LABEL, but ALSO by OPTIONS:
# Ashby renders 'Which of the following communities do you belong to?' whose label carries
# NO demographic keyword — the signal ('Person with disability', 'Neurodivergent', 'Veteran',
# 'Refugee') lives only in the options. Kept in sync with dropdowns._DEMOGRAPHIC / analyzer._skip.
_DEMOGRAPHIC_LABEL_RE = re.compile(
    r"(?i)(gender|rac(e|ial)|ethnic|veteran|military|armed\s*forces|disabilit|demographic|"
    r"hispanic|latin[ox]?\b|pronoun|sexual orientation|transgender|lgbtq|neurodiverg|self.?identif"
    r"|your (?:current )?age\b|age (?:range|group|bracket)|date of birth|\bdob\b)")
# Option strings that signal a diversity self-ID group. Require >=2 so a lone
# 'Prefer not to answer' on a legitimate screener does not trip the gate.
_DEMOGRAPHIC_OPTION_RE = re.compile(
    r"(?i)(person with (?:a )?disabilit|neurodiverg|non-?binary|transgender|"
    r"refugee|hispanic|latin[ox]?\b|\bveteran\b|prefer not to (?:answer|say|disclose))")


def _is_demographic(label: str, options: list[str]) -> bool:
    """A voluntary EEO/diversity self-ID question — leave blank (human), never auto-answer.
    True on a demographic LABEL, or on >=2 demographic-signal OPTIONS (the label may be
    neutral, e.g. 'communities you belong to')."""
    if _DEMOGRAPHIC_LABEL_RE.search(label or ""):
        return True
    joined = " | ".join(o or "" for o in (options or []))
    return len(_DEMOGRAPHIC_OPTION_RE.findall(joined)) >= 2

# identity / contact free-text fields filled straight from the profile
_ID_TEXT = [
    (re.compile(r"(?i)^(?:first|given|preferred) name|first[_ ]?name"), "first_name"),
    (re.compile(r"(?i)^(?:last|family|sur)\s*name|last[_ ]?name|surname"), "last_name"),
    (re.compile(r"(?i)^full name$|^name$|legal name|your name"), "full_name"),
    (re.compile(r"(?i)e-?mail"), "email"),
    (re.compile(r"(?i)phone|mobile|telephone|cell"), "phone"),
    (re.compile(r"(?i)linkedin"), "linkedin_url"),
    # a "City" field wants just the city; "current location"/"location" wants the full string.
    (re.compile(r"(?i)\bcity\b|\btown\b"), "city"),
    (re.compile(r"(?i)current location|where.*located|location"), "location"),
    # \b-anchored: bare "state|province" also matched the "State" inside "United States"
    # and leaked the candidate's state code ("OR") into work-authorization text fields.
    (re.compile(r"(?i)\bstate\b|\bprovince\b"), "state"),
    (re.compile(r"(?i)zip|postal code"), "zip_code"),
    # street address ("Address Line 1", "Street address") — NOT "email address" (email is
    # matched earlier) and NOT "Address Line 2" (kept blank).
    (re.compile(r"(?i)^address(?:\s*line\s*1)?\b|street address|\bstreet\b"), "street_address"),
    (re.compile(r"(?i)country"), "country"),
    (re.compile(r"(?i)years? of (?:relevant )?experience|how many years"), "years_experience"),
]

# education free-text fields filled deterministically from the résumé's education
_EDU = [
    (re.compile(r"(?i)field of study|\bmajor\b|concentration|discipline|area of study|"
                r"course of study|specializ"), "field"),
    (re.compile(r"(?i)^degree|highest degree|degree (?:earned|obtained|level|type|name)|"
                r"level of (?:education|study)|education level"), "degree"),
    (re.compile(r"(?i)school|university|college|institution|alma mater|"
                r"where did you (?:study|graduate)"), "school"),
    (re.compile(r"(?i)graduat\w* (?:year|date)|year of graduation|completion year"), "grad_year"),
    # a free-text "Education History / background" -> the whole education block, rendered
    # from the résumé (never LLM-drafted, which once invented a contradicting degree).
    (re.compile(r"(?i)education (?:history|background|details|summary|record)|"
                r"educational background|academic (?:background|history|record)|"
                r"tell us about your education"), "history"),
]
# NOTE the \b after every token: without it "in"/"of" would match the "In" of
# "Information" and strip into the next word ("BSc Information" -> "formation").
_DEGREE_PREFIX = re.compile(
    r"(?i)^(?:b\.?s\.?c?|b\.?a|m\.?s\.?c?|m\.?a|m\.?b\.?a|ph\.?d|bachelor'?s?|master'?s?|"
    r"associate'?s?|doctor\w*|diploma)\b\s*(?:(?:of|in)\b\s+)?")


def _education_value(key: str, resume: dict) -> str:
    edu = resume.get("education") or []
    if not edu:
        return ""
    e = edu[0]
    if key == "degree":
        return e.get("degree", "")
    if key == "school":
        return e.get("school", "")
    if key == "grad_year":
        return str(e.get("year", "") or "")
    if key == "field":  # "BSc Information Systems" -> "Information Systems"
        deg = e.get("degree", "")
        stripped = _DEGREE_PREFIX.sub("", deg).strip()
        return stripped or deg
    if key == "history":  # every entry: "Degree — School (year)", newest first
        parts = []
        for ent in edu:
            deg = (ent.get("degree") or "").strip()
            sch = (ent.get("school") or "").strip()
            yr = str(ent.get("year", "") or "").strip()
            seg = deg
            if sch:
                seg = f"{seg} — {sch}" if seg else sch
            if yr:
                seg = f"{seg} ({yr})" if seg else yr
            if seg:
                parts.append(seg)
        return "; ".join(parts)
    return ""


# closed eligibility selects answered deterministically from the profile
_AUTH_RE = re.compile(r"(?i)authoriz|legally (?:allowed|eligible|authorized)|"
                      r"eligible to work|right to work|work permit|permitted to work")
# PRECISE — the NO-direction trigger fires ONLY on a real "do/will you REQUIRE|NEED
# sponsorship" construct. Bare "visa sponsorship" is deliberately NOT here: a positively
# framed auth question ("authorized to work WITHOUT visa sponsorship", "can you present
# proof you are authorized ... without sponsorship") merely mentions it and must answer
# YES (via _AUTH_RE / _WITHOUT_SPON_RE below), not No.
_SPONSOR_RE = re.compile(
    r"(?i)(?:will|do|would|are|is|does)\s+(?:you|your|the candidate|this role|anyone)\b"
    r".{0,45}\b(?:requir\w*|need\w*)\b.{0,30}(?:sponsor\w*|visa|work permit|work authoriz)"
    r"|require[sd]?\s+(?:visa\s+|work\s+)?sponsorship"
    r"|need(?:ing|s|ed)?\s+(?:visa\s+)?sponsorship"
    r"|sponsorship\s+(?:now or in the future|is required|will be required|to work|required)"
    r"|require\s+(?:visa|work permit)")
# Positive frame: "authorized/able to work WITHOUT sponsorship" — beats _SPONSOR_RE, answered
# YES for our region-matched (needs_sponsorship = "No") candidates.
_WITHOUT_SPON_RE = re.compile(
    r"(?i)without\s+(?:needing\s+|requiring\s+|the need for\s+)?(?:company\s+|employer\s+)?"
    r"(?:visa\s+|work\s+)?sponsorship"
    r"|(?:do not|don'?t|will not|won'?t)\s+(?:require|need)\s+(?:visa\s+)?sponsorship"
    r"|no\s+sponsorship\s+(?:required|needed)")
_COUNTRY_RE = re.compile(r"(?i)country (?:of )?(?:residence|residency|location|based)|"
                         r"which country|country (?:in which|where)|in which country|"
                         r"your country|country are you")


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


EMAIL_DOMAIN = "takhet.com"


def _email_slug(s: str) -> str:
    """Lowercase ASCII local-part token: strip accents (José -> jose) + non-alphanumerics."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def derive_email(full_name: str, domain: str = EMAIL_DOMAIN) -> str:
    """first.last@takhet.com from a name — 'Steve Jobs' -> 'steve.jobs@takhet.com'.
    Deterministic so a recruiter reply always lands in the same candidate mailbox.
    Used to fill/onboard a candidate whose email isn't set yet. Single-token name ->
    just that token; empty/uncomputable -> ''."""
    first, last = _split_name(full_name)
    local = ".".join(t for t in (_email_slug(first), _email_slug(last)) if t)
    return f"{local}@{domain}" if local else ""


def _profile_value(key: str, profile: dict) -> str:
    if key in ("first_name", "last_name"):
        first, last = _split_name(profile.get("full_name", ""))
        return first if key == "first_name" else last
    if key == "email":
        # never leave an email blank: derive first.last@takhet.com from the name so the
        # filled form always carries a working candidate address, even for a person whose
        # profile.email isn't set yet.
        v = profile.get("email")
        return str(v) if v else derive_email(profile.get("full_name", ""))
    v = profile.get(key)
    return str(v) if v not in (None, "") else ""


def _auth_text(profile: dict) -> str:
    """Affirmative answer for a FREE-TEXT work-authorization question (the region-matched
    candidate is authorized and needs no sponsorship)."""
    country = (profile.get("country") or "").strip()
    wa = (profile.get("work_authorization") or "").strip()
    art = "the " if re.match(r"(?i)united |u\.?s\.?a?\b|netherlands|philippines|uk\b",
                             country) else ""
    where = f" in {art}{country}" if country else ""
    who = f" ({wa})" if wa else ""
    return (f"Yes — I am authorized to work{where} for any employer "
            f"and do not require visa sponsorship{who}.")


def _yes_option(options: list[str]) -> int | None:
    for i, o in enumerate(options):
        ol = (o or "").strip().lower()
        if ol == "yes" or ol.startswith("yes"):
            return i
    return None


def _no_option(options: list[str]) -> int | None:
    for i, o in enumerate(options):
        ol = (o or "").strip().lower()
        if ol == "no" or ol.startswith("no"):
            return i
    return None


def _identity_choice(label: str, options: list[str], profile: dict) -> int | None:
    """Deterministic option pick for the eligibility selects that gate the whole
    application. The candidate is REGION-MATCHED to the job, so: authorized -> Yes,
    sponsorship -> No, country -> the profile's country. This is exactly the
    'answer the blocking questions the way the employer wants' step."""
    lab = label or ""
    # needs_sponsorship is a STRING ("No"/"Yes"), so `not profile[...]` is always False —
    # parse it. All our candidates are region-authorized citizens/PRs (needs = "No").
    ns = str(profile.get("needs_sponsorship", "")).strip().lower()
    authorized = ns not in ("yes", "true", "1")
    # A positively-framed authorization question that merely MENTIONS sponsorship
    # ("...authorized to work WITHOUT visa sponsorship", "can you present proof you are
    # authorized...") is a YES for an authorized candidate — it must beat _SPONSOR_RE,
    # which now fires only on a real "do you REQUIRE/NEED sponsorship" construct.
    # An explicit "authorized to work WITHOUT sponsorship" positive frame is a YES for an
    # authorized candidate even when the SAME sentence also contains a "sponsorship now or in
    # the future" clause that _SPONSOR_RE would read as the NO-direction trigger (e.g.
    # "authorized to work in the US without requiring sponsorship now or in the future?").
    if _WITHOUT_SPON_RE.search(lab):
        return (_yes_option(options) if authorized else _no_option(options))
    if _AUTH_RE.search(lab) and not _SPONSOR_RE.search(lab):
        return (_yes_option(options) if authorized else _no_option(options))
    if _SPONSOR_RE.search(lab):
        return (_no_option(options) if authorized else _yes_option(options))
    if _AUTH_RE.search(lab):
        return (_yes_option(options) if authorized else _no_option(options))
    if _COUNTRY_RE.search(lab):
        country = (profile.get("country") or "").lower()
        for i, o in enumerate(options):
            ol = (o or "").lower()
            if country and (country in ol or ol in country):
                return i
            if "united states" in country and ol in ("usa", "us", "u.s.", "u.s.a."):
                return i
    return None


def _clean_review(answer: str) -> tuple[str, bool]:
    """Split the '[review] ' wire flag off an answer -> (clean text, needs_review)."""
    if answer.startswith("[review]"):
        return answer[len("[review]"):].strip(), True
    return answer, False


# ---- ideal (test) fill ------------------------------------------------------
# TEST mode: answer EVERY remaining question at 100% as the perfect JD-fit candidate.
# Deliberately relaxes the production "leave capability/behavioural questions for the
# human" gate — used only to preview full auto-fill quality; nothing is ever submitted.
_PLACEHOLDER = re.compile(r"(?i)^(select|choose|--|please|pick|\s*$|n/?a$)")

# Ideal candidate: yes/no capability / experience / willingness questions must be YES
# ("Do you have SaaS experience?" -> Yes). Grave error to answer No to these.
_AFFIRM_YES_RE = re.compile(
    r"(?i)do you have|have you (?:ever )?(?:used|worked|managed|led|built|developed)|"
    r"experience (?:in|with|using|of|working)|are you (?:experienced|proficient|comfortable|"
    r"familiar|willing|able|open|available|ready)|proficien|familiar with|worked with|"
    r"hands[- ]on|knowledge of|background in|skilled|can you|comfortable with|willing to|"
    r"do you possess|are you interested|would you be (?:able|willing|comfortable)")
# ...but a yes/no question about sponsorship / criminal history / restrictions / conflicts
# must be NO (answering Yes there is the wrong direction for an ideal candidate).
_AFFIRM_NO_RE = re.compile(
    r"(?i)sponsor|\bvisa\b|criminal|felony|convict|non-?compete|garden leave|"
    r"restrict|debarr|conflict of interest|do you require|are you subject to|"
    r"employment agreement|post-?employment|"
    r"previously (?:worked|employed|applied)|former(?:ly)? (?:employ|work)|"
    r"worked (?:with|at|for) (?:us|our|your|this)\b|ever worked (?:with|for|at) (?:us|our)|"
    r"worked (?:here|with us).{0,20}(?:before|previously)|current or (?:previous|former) employee|"
    r"hired (?:through|by)|"
    # contractual obligations / agreements / commitments that would impede joining -> No
    r"(?:obligation|commitment|agreement|relationship)s?.{0,80}"
    r"(?:impede|interfere|impact|prevent|preclude|conflict)")


def _affirm_override(label: str, options: list[str]) -> int | None:
    """Deterministic yes/no direction for an ideal candidate: capability/experience/
    willingness -> Yes; sponsorship/criminal/restriction -> No. None = defer to LLM."""
    if _AFFIRM_NO_RE.search(label or ""):
        return _no_option(options)
    if _AFFIRM_YES_RE.search(label or ""):
        return _yes_option(options)
    return None


def _fallback_option(options: list[str], multi: bool = False):
    """Guarantee a valid option even if the model returns nothing: prefer an
    affirmative 'Yes', else the first non-placeholder option."""
    idx = _yes_option(options)
    if idx is None:
        idx = next((i for i, o in enumerate(options)
                    if not _PLACEHOLDER.match((o or "").strip())), 0)
    return [idx] if multi else idx


def _cand_brief(profile: dict, facts: dict, resume: dict) -> str:
    pi = resume.get("personal_info", {})
    skills = []
    for items in (resume.get("skills_grouped") or {}).values():
        skills.extend(items)
    return json.dumps({
        "name": profile.get("full_name"), "location": profile.get("location"),
        "years_experience": profile.get("years_experience"),
        "work_authorization": profile.get("work_authorization"),
        "summary": resume.get("summary", "")[:400],
        "top_skills": skills[:25],
        "recent_roles": [f"{e.get('title')} @ {e.get('company')}"
                         for e in (resume.get("experience") or [])[:3]],
    }, default=str)


def _ideal_chunk(items: list[dict], brief: str, job: dict) -> dict:
    """items: [{"n": local_index, "label", "type", "options"}]. Returns
    {local_index: {"value" or "option_index"/"option_indices"}}."""
    blocks = []
    for it in items:
        opts = it["options"]
        if opts:
            ol = "\n".join(f"      {j}) {o}" for j, o in enumerate(opts))
            multi = "SELECT ALL that apply" if it["multi"] else "choose ONE"
            blocks.append(f'Q{it["n"]} [{multi}] {it["label"]}\n{ol}')
        else:
            blocks.append(f'Q{it["n"]} [free text] {it["label"]}')
    jd = re.sub(r"\s+", " ", (job.get("description") or "")).strip()[:1500]
    prompt = (
        "You are completing a job application AS THE IDEAL CANDIDATE for this role — a "
        "perfect fit who meets every requirement in the description. This is a TEST "
        "fill: answer EVERY question confidently and positively as this ideal candidate "
        "would. NEVER leave anything blank, never write 'to be confirmed', never add "
        "'[review]'.\n"
        "Rules:\n"
        '- Question with options + "choose ONE": return {"n":N,"option_index":<0-based '
        "index of the best option for an ideal candidate>}.\n"
        '- Question with options + "SELECT ALL": return {"n":N,"option_indices":[indices]}.\n'
        '- Free-text question: return {"n":N,"value":"<concise confident first-person '
        "answer tailored to THIS job; for 'describe a time' give a plausible concrete "
        'example; 1-3 sentences>"}.\n'
        "- CRITICAL: for any yes/no question about EXPERIENCE, SKILLS, or WILLINGNESS "
        "(e.g. 'Do you have SaaS experience?', 'start-up experience?', 'willing to...') "
        "ALWAYS choose YES. The only yes/no questions you answer NO to are visa "
        "sponsorship, criminal history, non-competes, and conflicts of interest.\n"
        "- For experience/skill questions answer affirmatively with relevant specifics.\n\n"
        f"JOB: {job.get('title', '')} at {job.get('company', '')}\n"
        f"DESCRIPTION: {jd}\n"
        f"CANDIDATE: {brief}\n\n"
        "QUESTIONS:\n" + "\n\n".join(blocks) + "\n\n"
        "Return ONLY a JSON array with exactly one entry per question, e.g. "
        '[{"n":0,"option_index":2},{"n":1,"value":"..."}].')
    out: dict = {}
    for _ in range(3):
        try:
            arr = _extract_array(_llm_complete(prompt) or "")
        except Exception:
            arr = None
        if not arr:
            continue
        for item in arr:
            if isinstance(item, dict) and isinstance(item.get("n"), int):
                out[item["n"]] = item
        if out:
            break
    return out


def ideal_fill(questions: list, indices: list, profile: dict, facts: dict,
               resume: dict, resume_text: str, job: dict) -> dict:
    """Fill every question in `indices` at 100%. Returns {orig_index: answer_obj}."""
    brief = _cand_brief(profile, facts, resume)
    items = []
    for local, i in enumerate(indices):
        q = questions[i]
        items.append({"n": local, "orig": i, "label": (q.get("label") or "").strip(),
                      "type": (q.get("type") or ""), "options": q.get("options") or [],
                      "multi": (q.get("type") or "").lower() in _MULTI_TYPES})
    got: dict = {}
    CHUNK = 8
    for s in range(0, len(items), CHUNK):
        got.update(_ideal_chunk(items[s:s + CHUNK], brief, job))

    answers: dict = {}
    for it in items:
        i, opts, multi = it["orig"], it["options"], it["multi"]
        base = {"label": it["label"], "type": it["type"],
                "required": bool(questions[i].get("required"))}
        res = got.get(it["n"], {})
        if opts:  # closed select -> validated option index (with affirmative fallback)
            if multi:
                raw = res.get("option_indices")
                idxs = [k for k in (raw or []) if isinstance(k, int) and 0 <= k < len(opts)]
                if not idxs:
                    idxs = _fallback_option(opts, multi=True)
                value = [opts[k] for k in idxs]
            else:
                ov = _affirm_override(it["label"], opts)  # capability -> Yes (grave if No)
                if ov is not None:
                    k = ov
                else:
                    k = res.get("option_index")
                    if not (isinstance(k, int) and 0 <= k < len(opts)):
                        k = _fallback_option(opts)
                value = opts[k]
            answers[i] = {**base, "value": value, "source": "ideal",
                          "needs_review": False, "status": "filled"}
        else:  # free text
            val = str(res.get("value") or "").strip()
            if not val:
                val = "Yes." if it["label"].lower().startswith(("do ", "have ", "are ", "can ", "will ")) \
                    else "Through an online job search."
            answers[i] = {**base, "value": val, "source": "ideal",
                          "needs_review": False, "status": "filled"}
    return answers


# ---- candidate roster -------------------------------------------------------
def load_candidates() -> dict:
    """{profile_id: {"profile": <profile>, "facts": <fact sheet or {}>}} for every
    non-sample candidate, plus region pools."""
    profs = json.loads((DATA / "profiles.json").read_text())
    facts_dir = DATA / "facts"
    roster = {}
    for p in profs:
        if p.get("is_sample") or not p.get("resume"):
            continue
        pid = p.get("id")
        fp = facts_dir / f"{pid}.json"
        facts = {}
        if fp.exists():
            try:
                facts = json.loads(fp.read_text())
            except Exception:
                facts = {}
        roster[pid] = {"profile": p, "facts": facts}
    return roster


_ROSTER_CACHE: dict | None = None
_POOLS_CACHE: dict | None = None


def roster_cached() -> tuple[dict, dict]:
    """Load the candidate roster once per process (profiles.json is ~1.4MB) — used by
    the one-click /catalog fill endpoint so a click doesn't re-read it every time."""
    global _ROSTER_CACHE, _POOLS_CACHE
    if _ROSTER_CACHE is None:
        _ROSTER_CACHE = load_candidates()
        _POOLS_CACHE = _region_pools(_ROSTER_CACHE)
    return _ROSTER_CACHE, _POOLS_CACHE


def _region_pools(roster: dict) -> dict:
    us, ca, other = [], [], []
    for pid, c in roster.items():
        country = (c["profile"].get("country") or "").lower()
        if country == "united states":
            us.append(pid)
        elif country == "canada":
            ca.append(pid)
        else:
            other.append(pid)
    return {"US": sorted(us), "CA": sorted(ca), "OTHER": sorted(other)}


def pick_candidate(regions: list, roster: dict, pools: dict) -> dict | None:
    """REAL-apply region GATE (uses the real roster): a US-eligible job -> a US person, a
    CA-eligible job -> a Canadian; US wins when both. A job that is NOT US/CA-eligible
    (OTHER / UK-only / untagged) returns None — we NEVER send a US candidate to a foreign
    posting ('for America, an American'); a 'Remote - Japan' role must not get a US person
    claiming a Japanese visa. The ETALON demo does NOT use this — it invents a fresh
    fictional persona per job (backend.tools.synth_persona), never a real roster candidate."""
    regions = regions or []
    if "US" in regions and pools.get("US"):
        return roster[pools["US"][0]]
    if "CA" in regions and pools.get("CA"):
        return roster[pools["CA"][0]]
    return None


# ---- per-job generation -----------------------------------------------------
def generate_draft(job_row: dict, candidate: dict, use_ai: bool = True,
                   ideal: bool = False) -> dict:
    profile = candidate["profile"]
    facts = candidate["facts"]
    resume = profile["resume"]
    job = {"title": job_row.get("title", ""), "company": job_row.get("company", ""),
           "description": job_row.get("description", "")}
    questions = job_row.get("questions") or []

    # 1) résumé tailored to the JD (real experience, JD-relevant bullets first)
    tailored = tailor_resume(resume, job["title"], job["company"], job["description"],
                             use_ai=use_ai)
    resume_text = render_text(tailored)

    resume_file = f"{profile['id']}_resume.pdf"
    profile_form = {k: v for k, v in profile.items() if k != "resume"}
    profile_form.setdefault("desired_salary", facts.get("salary_annual"))
    facts_aug = {**facts, "work_authorization": profile.get("work_authorization"),
                 "country": profile.get("country"),
                 "needs_sponsorship": profile.get("needs_sponsorship"),
                 "linkedin_url": profile.get("linkedin_url"),
                 "years_experience": profile.get("years_experience")}

    # 2) route every question
    answers = [None] * len(questions)
    choice_idx, choice_qs = [], []   # (orig index) + {question_text, options}
    text_idx, text_labels = [], []
    ideal_idx = []                   # TEST mode: everything not deterministically known

    for i, q in enumerate(questions):
        label = (q.get("label") or "").strip()
        qtype = (q.get("type") or "").lower()
        opts = q.get("options") or []
        required = bool(q.get("required"))
        base = {"label": label, "type": q.get("type", ""), "required": required}

        if not label:
            answers[i] = {**base, "value": "", "source": "none", "needs_review": False,
                          "status": "empty"}
            continue
        if _is_demographic(label, opts):
            # EEO/diversity self-ID — human-only in BOTH modes so a synthetic persona
            # never claims a protected characteristic (disability/veteran/ethnicity/age).
            answers[i] = {**base, "value": "", "source": "human", "needs_review": True,
                          "status": "human", "note": "demographic self-ID — human only"}
            continue
        if _VIDEO_RE.search(label):
            # video/Loom/recording CANNOT be auto-produced — human-only in BOTH modes.
            # (Previously ideal mode sent it to the LLM, which fabricated a fake Loom link.)
            answers[i] = {**base, "value": "", "source": "human", "needs_review": True,
                          "status": "human", "note": "video/recording — human only"}
            continue
        if qtype in _FILE_TYPES:
            if _PHOTO_RE.search(label):     # no portrait on file — never the résumé PDF
                answers[i] = {**base, "value": "", "source": "human", "needs_review": True,
                              "status": "human", "note": "photo/headshot — no auto-fill"}
                continue
            if _HUMAN_FILE_RE.search(label):   # medical report / ID / diploma — not the résumé
                answers[i] = {**base, "value": "", "source": "human", "needs_review": True,
                              "status": "human", "note": "specialized document — human only"}
                continue
            is_cover = _COVER_RE.search(label)
            answers[i] = {**base, "value": ("cover_letter.pdf" if is_cover else resume_file),
                          "source": "file", "needs_review": False, "status": "filled"}
            continue
        # No-option fields (free text, or a mislabeled type="select" typeahead) that are
        # eligibility or education get a deterministic answer BEFORE the generic
        # select/LLM path, so they never fall through to an invented value.
        if len(opts) < 2:
            # work-eligibility free text -> affirmative authorization. Require a work/employ
            # context so a behavioural "authorized to make a decision" free-text isn't caught.
            # ONLY for a genuinely optionless field (`not opts`): a closed choice under-captured
            # to a SINGLE option (e.g. a Lever "multiple-select" Yes/No checkbox the scraper
            # stored as options=["Yes"]) must NOT get the prose _auth_text — the live widget is a
            # checkbox and _pick_option can't match long prose to "Yes"/"No", leaving it unfilled.
            # It falls through to the eligibility-select gate below, which picks the "Yes" option.
            work_ctx = re.search(r"(?i)\b(work|employ|job|role|position)\b", label)
            if not opts and not _COUNTRY_RE.search(label) and (
                    _WITHOUT_SPON_RE.search(label)
                    or (_AUTH_RE.search(label) and work_ctx)):
                answers[i] = {**base, "value": _auth_text(profile), "source": "identity",
                              "needs_review": False, "status": "filled"}
                continue
            edukey0 = next((k for rx, k in _EDU if rx.search(label)), None)
            if edukey0:
                val0 = _education_value(edukey0, resume)
                if val0:
                    answers[i] = {**base, "value": val0, "source": "identity",
                                  "needs_review": False, "status": "filled"}
                    continue
        # eligibility selects gated deterministically from the region-matched profile
        if opts and (_AUTH_RE.search(label) or _SPONSOR_RE.search(label)
                     or _WITHOUT_SPON_RE.search(label) or _COUNTRY_RE.search(label)):
            pick = _identity_choice(label, opts, profile)
            if pick is not None:
                answers[i] = {**base, "value": opts[pick], "source": "identity",
                              "needs_review": False, "status": "filled"}
                continue
        # closed select / radio
        if qtype in _SELECT_TYPES or (opts and len(opts) >= 2):
            if ideal:
                ideal_idx.append(i)          # LLM picks a valid option (fallback guaranteed)
                continue
            if len(opts) < 2:
                answers[i] = {**base, "value": "", "source": "none", "needs_review": True,
                              "status": "human", "note": "select without options"}
                continue
            choice_idx.append(i)
            choice_qs.append({"question_text": label, "options": opts})
            continue
        # identity / contact free text — single-line inputs ONLY. A TEXTAREA labelled
        # e.g. "...email..." is a prose question ("describe your outreach email"), not the
        # contact field, and must NOT be filled with the candidate's email address.
        idkey = None if qtype == "textarea" else next((k for rx, k in _ID_TEXT if rx.search(label)), None)
        if idkey:
            val = _profile_value(idkey, profile)
            if val:
                answers[i] = {**base, "value": val, "source": "identity",
                              "needs_review": False, "status": "filled"}
                continue
        # education free text (degree / field of study / school / grad year)
        edukey = next((k for rx, k in _EDU if rx.search(label)), None)
        if edukey:
            val = _education_value(edukey, resume)
            if val:
                answers[i] = {**base, "value": val, "source": "identity",
                              "needs_review": False, "status": "filled"}
                continue
        # everything else -> open-text (test: ideal fill; prod: grounded draft)
        if ideal:
            ideal_idx.append(i)
        else:
            text_idx.append(i)
            text_labels.append(label)

    # TEST mode: fill every remaining question at 100% as the ideal JD-fit candidate
    if ideal and ideal_idx:
        for i, a in ideal_fill(questions, ideal_idx, profile, facts, resume,
                               resume_text, job).items():
            answers[i] = a

    # 3) closed selects: deterministic rules first, LLM for the residue
    if choice_qs:
        picks = deterministic_choices(choice_qs, facts_aug)
        need = [j for j, p in enumerate(picks)
                if p["index"] is None and 2 <= len(choice_qs[j]["options"]) <= 40]
        if need and use_ai:
            sub = [choice_qs[j] for j in need]
            llm = choose_options(sub, facts_aug, job)
            for local, j in enumerate(need):
                picks[j] = llm[local]
        for j, p in enumerate(picks):
            i = choice_idx[j]
            base = {"label": choice_qs[j]["question_text"],
                    "type": questions[i].get("type", ""), "required": bool(questions[i].get("required"))}
            opts = choice_qs[j]["options"]
            multi = (questions[i].get("type") or "").lower() in _MULTI_TYPES
            if p["index"] is None:
                answers[i] = {**base, "value": "", "source": "none", "needs_review": True,
                              "status": "human"}
            else:
                val = opts[p["index"]]
                note = "multi-select: 1 option chosen" if multi else None
                answers[i] = {**base, "value": ([val] if multi else val), "source": "choice",
                              "backed": bool(p.get("backed")),
                              "needs_review": (not p.get("backed")) or multi, "status": "filled",
                              **({"note": note} if note else {})}

    # 4) open text: deterministic factual pre-pass + grounded LLM drafts
    if text_labels:
        drafted = draft_answers(text_labels, profile_form, job, resume_summary=resume_text,
                                facts=facts_aug) if use_ai else {}
        for k, i in enumerate(text_idx):
            label = text_labels[k]
            base = {"label": label, "type": questions[i].get("type", ""),
                    "required": bool(questions[i].get("required"))}
            raw = drafted.get(label)
            if raw:
                clean, needs = _clean_review(raw)
                answers[i] = {**base, "value": clean, "source": "llm",
                              "needs_review": needs, "status": "filled"}
            else:
                answers[i] = {**base, "value": "", "source": "none", "needs_review": True,
                              "status": "human"}

    # 5) cover-letter body (for any cover-letter textarea / to attach as a file)
    cover = ""
    if any(_COVER_RE.search(a["label"]) for a in answers if a):
        try:
            cover = cover_letter(job, resume_text, profile_form, use_llm=use_ai)
        except Exception:
            cover = ""

    filled = sum(1 for a in answers if a and a["status"] == "filled")
    human = sum(1 for a in answers if a and a["status"] == "human")
    need_rev = sum(1 for a in answers if a and a.get("needs_review"))
    by_source: dict[str, int] = {}
    for a in answers:
        if a:
            by_source[a["source"]] = by_source.get(a["source"], 0) + 1

    return {
        "candidate": {"id": profile["id"], "name": profile.get("full_name", ""),
                      "country": profile.get("country", ""),
                      "work_authorization": profile.get("work_authorization", ""),
                      "email": profile.get("email") or derive_email(profile.get("full_name", ""))},
        "resume": tailored,
        "resume_text": resume_text,
        "resume_file": resume_file,
        "cover_letter": cover,
        "answers": answers,
        "stats": {"total": len(questions), "filled": filled, "human": human,
                  "needs_review": need_rev, "by_source": by_source,
                  "match_score": tailored.get("match_score"),
                  "ats_score": tailored.get("ats_score")},
        "used_ai": bool(use_ai),
    }


# ---- wire a stored draft into the co-pilot (live headful fill / noVNC) -------
PREFILL_ROOT = Path(__file__).resolve().parents[2] / "uploads" / "prefill"


def materialize_prefill(job_id: int) -> tuple[str, str]:
    """Write a stored ideal draft into the co-pilot's prefill dir so POST /load can
    replay it into the LIVE form (headful, watch in noVNC). The co-pilot reads
    report.json's `drafted_answers` and fills them as known_answers (no re-drafting);
    labels that also match the live DOM get OUR exact reviewed answer, the rest are
    recomputed by the same engine (co-pilot runs draft=True). Returns (profile_id, jobid)."""
    from backend.tools import drafts_ui
    job = catalog_db.get_job(job_id)
    if not job or not job.get("draft"):
        raise ValueError(f"no draft for job {job_id}")
    d = job["draft"]
    profile_id = (d.get("candidate") or {}).get("id") or "michael"
    jobid = str(job_id)
    out = PREFILL_ROOT / profile_id / jobid
    out.mkdir(parents=True, exist_ok=True)
    (out / "resume.pdf").write_bytes(drafts_ui.resume_pdf(job_id) or b"")

    drafted: dict[str, str] = {}
    for a in d.get("answers") or []:
        if not a or a.get("source") in ("file", "none"):
            continue
        v = a.get("value")
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        v = str(v or "").strip()
        lbl = drafts_ui._clean_label(a.get("label"))
        if lbl and v:
            drafted[lbl] = v

    # Standard education fields (School esp.) are often a live typeahead dropdown that
    # isn't in the scraped questions — supply them from the résumé so the co-pilot can
    # type + pick them (fill_react_selects_known).
    edu = ((d.get("resume") or {}).get("education") or [])
    if edu:
        e0 = edu[0]
        if e0.get("school"):
            drafted.setdefault("School", e0["school"])
        if e0.get("degree"):
            drafted.setdefault("Degree", e0["degree"])
        if e0.get("field"):
            drafted.setdefault("Discipline", e0["field"])  # field of study

    # Structured EMPLOYMENT work-history block (newer Greenhouse hosted forms): Company name,
    # Title, Start/End date components, and a 'Current role' checkbox — the persona's most
    # recent role (experience[0]) carries all of it. Résumé dates are year-only ('2022-Present'),
    # so the month defaults to January; a 'Present'/'Current' role ticks 'Current role' (which
    # waives the End date), else the end year is parsed from the range.
    exp = ((d.get("resume") or {}).get("experience") or [])
    if exp:
        x0 = exp[0]
        if x0.get("company"):
            drafted.setdefault("Company name", x0["company"])
        if x0.get("title"):
            drafted.setdefault("Title", x0["title"])
        dates = str(x0.get("dates") or "")
        years = re.findall(r"\d{4}", dates)
        if years:
            drafted.setdefault("Start date year", years[0])
            drafted.setdefault("Start date month", "January")
        if re.search(r"(?i)present|current", dates):
            drafted.setdefault("Current role", "Yes")
        elif len(years) >= 2:
            drafted.setdefault("End date year", years[-1])
            drafted.setdefault("End date month", "December")

    # Location/City is usually a live geo-typeahead NOT in the scraped questions, so no
    # drafted answer exists for it (the co-pilot then leaves it blank). Supply the persona's
    # city under the common label variants so fill_react_selects_known can type + pick it.
    ploc = (((d.get("resume") or {}).get("personal_info") or {}).get("location") or "").strip()
    city = ploc.split(",")[0].strip()
    if city:
        for lbl in ("Location (City)", "Location", "City", "Current location", "City/Town"):
            drafted.setdefault(lbl, city)

    # The co-pilot needs the real APPLY-form URL, not the raw posting (see apply_url_for_job).
    apply_url = apply_url_for_job(job)

    report = {"apply_url": apply_url, "job_title": job.get("title", ""),
              "company": job.get("company", ""), "profile": profile_id,
              "resume_niche": None, "drafted_answers": drafted, "submitted": False}
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return profile_id, jobid


def apply_url_for_job(job: dict) -> str:
    """The LIVE ATS apply-form URL for a catalog job — fast (no draft gen), so the
    co-pilot can navigate to the RIGHT job the instant "Заполнить" is clicked (before the
    slow draft generation), instead of noVNC showing the previous job. Greenhouse -> the
    embed form keyed by the gh_jid FROM THE URL (never redirects); others -> `_apply_url`.
    Kept in sync with materialize_prefill, which calls it."""
    from backend.tools.catalog_forms import _apply_url
    gh_id = ""
    if job.get("ats") == "greenhouse":
        m = re.search(r"(?:/jobs/|[?&]gh_jid=)(\d+)", job.get("url", "") or "")
        gh_id = m.group(1) if m else (str(job["external_id"]) if str(job.get("external_id") or "").isdigit() else "")
    if gh_id and job.get("company_key"):
        return f"https://boards.greenhouse.io/embed/job_app?for={job['company_key']}&token={gh_id}"
    return _apply_url(job.get("ats", ""), job.get("url", "")) or job.get("url", "")


# Bump when the scraper/generator changes in a way that should force a fresh draft on
# the next click (so stale drafts from before the fix are regenerated, not reused).
# v3: synthetic per-job persona + persona.json for the co-pilot /load.
_SCRAPE_V = 7  # bump: structured Employment/Education work-history known answers (Company/Title/dates/Discipline)


def ensure_and_wire(job_id: int) -> tuple[str, str, bool]:
    """One-click backend for the /catalog "Заполнить" button. Generates the ideal draft
    (100% fill, region-matched candidate) unless a CURRENT one (_scrape_v) already
    exists. For custom ATS (ashby/lever/workable) it re-scrapes THIS job's questions
    live first — their screeners are DOM-scraped (not from an API), so a fresh scrape
    guarantees every question (selects/comboboxes/capability) is captured. Then
    materializes for the co-pilot. Returns (profile_id, jobid, was_generated)."""
    job = catalog_db.get_job(job_id)
    if not job:
        raise ValueError("job not found")
    draft = job.get("draft")
    if draft and draft.get("_scrape_v") == _SCRAPE_V:
        pid, jid = materialize_prefill(job_id)
        return pid, jid, False

    from backend.config import settings
    if settings.llm_model != "gpt-5.6-luna":  # fast tier for on-demand gen
        settings.llm_model = "gpt-5.6-luna"
    if job.get("ats") in ("ashby", "lever", "workable"):
        from backend.tools import catalog_forms
        qs = catalog_forms.scrape_one(job["ats"], job.get("url", ""))
        if qs:
            catalog_db.set_questions(job["ats"], job["company_key"], job["external_id"], qs)
            job = catalog_db.get_job(job_id)
    # the one-click fill is the etalon DEMO: invent a fresh, fictional, region-appropriate
    # persona for this job (never a real roster person) — see synth_persona.
    from backend.tools.synth_persona import synth_persona
    cand = synth_persona(job)
    # provision the persona's mailbox so its @takhet.com email is a LIVE, deliverable box
    # (row in amasmail.virtual_users + Maildir). Best-effort: a provisioning failure must
    # NEVER break the fill. Idempotent, so a re-click of a cached persona is a no-op.
    try:
        from backend.tools.provision_mailboxes import provision_email
        from backend.tools import mailcrm
        prof = cand["profile"]
        res = provision_email(prof.get("email", ""), prof.get("full_name", ""))
        # register as a candidate so the persona's inbox shows in the CRM /mail
        mailcrm.register_demo_persona(prof.get("email", ""), prof.get("full_name", ""), prof.get("id", ""))
        print(f"[demo-fill] mailbox {res.get('email')}: "
              f"{'created' if res.get('created') else ('exists' if res.get('ok') else 'FAILED: ' + str(res.get('error')))}",
              flush=True)
    except Exception as e:
        print(f"[demo-fill] mailbox provision skipped: {type(e).__name__}: {e}", flush=True)
    d = generate_draft(job, cand, use_ai=True, ideal=True)
    d["_scrape_v"] = _SCRAPE_V
    catalog_db.set_draft(job_id, d)
    pid, jid = materialize_prefill(job_id)
    # the synthetic persona isn't in the profile store — persist it beside the prefill so the
    # co-pilot /load can build a Profile for it (else get_profile raises "profile not found").
    try:
        (PREFILL_ROOT / pid / jid / "persona.json").write_text(
            json.dumps({"profile": cand["profile"], "facts": cand.get("facts") or {}},
                       ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[demo-fill] persona.json write skipped: {type(e).__name__}: {e}", flush=True)
    return pid, jid, True


# ---- batch runner -----------------------------------------------------------
def run(limit: int = 150, all_jobs: bool = False, use_ai: bool = True,
        workers: int = 5, job_id: int | None = None, ideal: bool = False) -> dict:
    catalog_db.ensure_schema()
    roster = load_candidates()
    pools = _region_pools(roster)
    print(f"candidates: US={len(pools['US'])} CA={len(pools['CA'])} "
          f"OTHER={len(pools['OTHER'])}", flush=True)

    if job_id is not None:
        j = catalog_db.get_job(job_id)
        jobs = [j] if j else []
    else:
        jobs = catalog_db.jobs_for_drafting(limit=10 ** 7 if all_jobs else limit)
    print(f"generating drafts for {len(jobs)} jobs (ai={use_ai}, workers={workers})", flush=True)

    def work(job_row):
        if ideal:
            # etalon demo: a fresh fictional persona per job (never a real roster person)
            from backend.tools.synth_persona import synth_persona
            cand = synth_persona(job_row)
        else:
            cand = pick_candidate(job_row.get("regions") or [], roster, pools)
        if not cand:
            return (job_row["id"], None, "no candidate for region")
        try:
            draft = generate_draft(job_row, cand, use_ai=use_ai, ideal=ideal)
            catalog_db.set_draft(job_row["id"], draft)
            return (job_row["id"], draft["stats"], None)
        except Exception as e:
            return (job_row["id"], None, f"{type(e).__name__}: {e}")

    ok = fail = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            done += 1
            jid, stats, err = f.result()
            if err:
                fail += 1
                print(f"  [{done}/{len(jobs)}] job {jid} FAILED: {err}", flush=True)
            else:
                ok += 1
                if done % 10 == 0 or done == len(jobs):
                    print(f"  [{done}/{len(jobs)}] job {jid} ok "
                          f"(filled {stats['filled']}/{stats['total']}, "
                          f"review {stats['needs_review']}, human {stats['human']})", flush=True)
    print(f"DONE. drafts ok={ok} fail={fail}; total in DB={catalog_db.drafts_count()}",
          flush=True)
    return {"ok": ok, "fail": fail}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate A→Z application drafts into job_catalog.draft.")
    ap.add_argument("--limit", type=int, default=150, help="jobs in the batch (mixed across ATS)")
    ap.add_argument("--all", action="store_true", help="every eligible job, not just --limit")
    ap.add_argument("--job", type=int, default=None, help="one job id (smoke test)")
    ap.add_argument("--no-ai", action="store_true", help="deterministic only (no LLM drafts/choices)")
    ap.add_argument("--ideal", action="store_true",
                    help="TEST mode: answer EVERY question at 100%% as the ideal JD-fit "
                         "candidate (relaxes the leave-for-human gate; nothing is submitted)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--wire", type=int, default=None,
                    help="materialize a stored draft into the co-pilot prefill dir so "
                         "POST /load can fill it live (headful / noVNC)")
    args = ap.parse_args()
    if args.wire is not None:
        pid, jid = materialize_prefill(args.wire)
        print(f"wired job {args.wire} -> uploads/prefill/{pid}/{jid}/ "
              f"(load: POST http://127.0.0.1:8102/load jobid={jid}&profile={pid})", flush=True)
    else:
        print(run(limit=args.limit, all_jobs=args.all, use_ai=not args.no_ai,
                  workers=args.workers, job_id=args.job, ideal=args.ideal), flush=True)
