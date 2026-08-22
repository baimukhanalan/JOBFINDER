"""Pick options for closed screener questions (selects / radio groups) the rules
didn't cover. The model only CHOOSES an option index based on the candidate's fact
sheet — it never writes free text, and every answer is validated to be a real option
index (anything else -> None -> the human). `backed=false` marks a choice not directly
supported by a fact: it is still filled, but flagged for the human to confirm.

Known limitation: `backed` is the model's own claim and is not independently
verified — hostile text embedded in a job form could coax the model into
overstating it. Option indexes are always validated, and in semi-auto mode a
human reviews the filled form before submitting, so the blast radius is a
missing review flag, not a wrong-option fill.
"""
import json
import logging
import re

from backend.services.tailor.tailor import _llm_complete

logger = logging.getLogger(__name__)

CHUNK = 10        # questions per LLM call — small batches keep a weak model reliable
MAX_OPTIONS = 40  # huge dropdowns (countries etc.) are rule/human territory
ATTEMPTS = 2

# Eligibility radios a weak model routinely fumbles (it picked "Not ready to come
# to the Philippines" for a local candidate). When a fact states willingness, pick
# the most affirmative option deterministically and mark it backed.
_ELIGIBILITY_RE = re.compile(
    r"(?i)relocat|willing to (?:travel|work|come)|business trip|open to (?:relocation|travel)"
    r"|work(?:ing)? (?:onsite|on-site|from (?:the )?office|in office)|come to the")
_WILLING_FACT_KEYS = ("willing_to_relocate", "willing_onsite", "open_to_travel", "relocation")
_NEG = re.compile(r"(?i)\bnot\b|\bno\b|unable|cannot|can't|won'?t|decline|remote only")
_POS = re.compile(r"(?i)\byes\b|ready|willing|open|available|any (?:shift|location)")


def _eligibility_pick(question_text: str, options: list[str], facts: dict) -> int | None:
    """If the question is an eligibility one AND a fact says the candidate is willing,
    return the index of the most affirmative option. Otherwise None (defer to the model)."""
    if not _ELIGIBILITY_RE.search(question_text or ""):
        return None
    willing = any(str(facts.get(k, "")).strip().lower() in ("yes", "true", "1")
                  for k in _WILLING_FACT_KEYS)
    if not willing:
        return None
    best_i, best_score = None, -10
    for i, opt in enumerate(options):
        score = 0
        if _POS.search(opt):
            score += 2
        if _NEG.search(opt):
            score -= 3
        if re.search(r"(?i)\bonly\b", opt):
            score -= 1  # "business trips only" is weaker than a full yes
        if i == 0:
            score += 0.5  # forms usually list the strongest yes first
        if score > best_score:
            best_i, best_score = i, score
    return best_i if best_score > 0 else None


# --- Referral / "how did you hear about us" radios -----------------------------
_REFERRAL_RE = re.compile(
    r"(?i)hear about|how did you (?:find|learn)|where did you (?:find|hear)"
    r"|find (?:our|this|the) (?:job|posting|position|role|opening)|source of|referr")
# facts.referral keyword -> option keywords (first match wins). Deliberately avoids
# options that demand a follow-up "please specify" text field.
_REFERRAL_MAP = [
    (re.compile(r"(?i)linkedin"),                         re.compile(r"(?i)linkedin")),
    (re.compile(r"(?i)friend|colleague|word of mouth|referr"),
     re.compile(r"(?i)friend|colleague|referr")),
    (re.compile(r"(?i)career|company|official|website|site|corporate"),
     re.compile(r"(?i)career|company|website|site")),
    (re.compile(r"(?i)board|indeed|glassdoor|search|google|online|internet"),
     re.compile(r"(?i)board|indeed|glassdoor|search")),
]
_SPECIFY_RE = re.compile(r"(?i)\*|please specify|specify\)")


def _referral_pick(question_text: str, options: list[str], facts: dict) -> int | None:
    """Map the candidate's `referral` fact to the best hear-about option, skipping any
    option that would open a 'please specify' text field. Falls back to a company /
    job-board option so the question is answered rather than left blank."""
    if not _REFERRAL_RE.search(question_text or ""):
        return None
    referral = str(facts.get("referral", "")).strip()
    usable = [(i, o) for i, o in enumerate(options) if not _SPECIFY_RE.search(o or "")]
    if not usable:
        return None
    if referral:
        for src_re, opt_re in _REFERRAL_MAP:
            if src_re.search(referral):
                for i, o in usable:
                    if opt_re.search(o):
                        return i
    # No fact match -> a safe, plausible default (company/career page, then job board).
    for pat in (re.compile(r"(?i)career|company|website|site"),
                re.compile(r"(?i)board|indeed|glassdoor|search")):
        for i, o in usable:
            if pat.search(o):
                return i
    return usable[0][0]


# --- Plain yes/no suitability & agreement (affirmative default) -----------------
# Fires ONLY on availability/agreement/suitability questions — never capability or
# experience ones ("do you have X experience"), which must not be auto-answered.
_SUIT_RE = re.compile(
    r"(?i)does (?:it|this|that) (?:suit|work for)\b"
    r"|works? for you\b|suit you\b"
    r"|are you (?:ok|okay|fine|comfortable|available|willing)\b"
    r"|are you fine with\b"
    r"|can you (?:work|commit|attend|start|join|be available|make)\b"
    r"|do you agree\b|is (?:this|that) (?:ok|okay|acceptable|fine)\b")


def _yesno_pick(question_text: str, options: list[str]) -> int | None:
    """Affirmative default ("Yes") for a plain yes/no suitability/agreement question
    (e.g. 'core hours are 12-6 GMT+8, does it suit you?'). Only fires on a two-option
    yes/no group. Returned unbacked -> the human still sees it flagged."""
    if not _SUIT_RE.search(question_text or ""):
        return None
    norm = [(o or "").strip().lower() for o in options]
    if len(norm) != 2 or not ({"yes", "no"} <= set(norm)):
        return None
    for i, o in enumerate(norm):
        if o == "yes" or o.startswith("yes"):
            return i
    return None


# Capability / experience yes-no questions ("Do you have SaaS experience?"). An ideal
# candidate answers YES — but a "yes" here is UNBACKED (no fact proves it), so it stays
# review-flagged (the human's [review] safety gate). This mirrors the generator's
# _affirm_override so the co-pilot's own draft pass can't flip it back to No/null.
_CAPABILITY_RE = re.compile(
    r"(?i)do you have .{0,30}experience|are you experienced|have you (?:ever )?(?:used|worked)"
    r"|proficien\w* (?:in|with)|familiar with|comfortable (?:using|with)|hands[- ]on"
    r"|\d+\+? ?years? (?:of )?experience|experience (?:in|with|using)|background in"
    r"|knowledge of|do you have (?:a |an )?(?:degree|background|start-?up|saas)")
# ...but NOT the negative-direction yes/no (sponsorship/criminal/restrictions) and NOT
# prior-relationship-with-THIS-employer questions (a fresh applicant answers those No).
_CAPABILITY_NO_RE = re.compile(
    r"(?i)sponsor|\bvisa\b|criminal|felony|convict|non-?compete|restrict|conflict of interest"
    r"|are you subject to|employment agreement|post-?employment"
    r"|previously (?:worked|employed|applied)|former(?:ly)? (?:employ|work)"
    r"|worked (?:with|at|for) (?:us|our|your|this)\b|ever worked (?:with|for|at) (?:us|our)"
    r"|worked (?:here|with us).{0,20}(?:before|previously)|current or (?:previous|former) employee"
    r"|hired (?:through|by)")


def _capability_pick(question_text: str, options: list[str]) -> int | None:
    """Yes for a capability/experience 2-option yes-no question; None otherwise."""
    q = question_text or ""
    if _CAPABILITY_NO_RE.search(q) or not _CAPABILITY_RE.search(q):
        return None
    norm = [(o or "").strip().lower() for o in options]
    if len(norm) != 2 or not ({"yes", "no"} <= {n[:3] if n in ("yes", "no") else n for n in norm}):
        # accept a clean Yes/No pair only
        if not (len(norm) == 2 and any(n.startswith("yes") for n in norm)
                and any(n.startswith("no") for n in norm)):
            return None
    for i, o in enumerate(norm):
        if o == "yes" or o.startswith("yes"):
            return i
    return None


# --- English-level self-assessment ----------------------------------------------
_ENGLISH_RE = re.compile(
    r"(?i)english (?:level|proficiency|language level)|level of english"
    r"|proficiency in english|how (?:good|proficient).{0,20}english")
_CEFR_RE = re.compile(r"(?i)\b([abc][12])\b")
# Default when the candidate has no explicit english_level fact: B2 is "comfortable in
# meetings and discussions" — a professional, non-native claim, flagged for review.
_ENGLISH_DEFAULT = re.compile(r"(?i)\bb2\b|upper-intermediate")


def _language_pick(question_text: str, options: list[str],
                   facts: dict) -> tuple[int | None, bool]:
    """Pick an English-level option. From facts.english_level when present (backed);
    otherwise a B2 professional default (unbacked -> review). Returns (index, backed)."""
    if not _ENGLISH_RE.search(question_text or ""):
        return None, False
    level = str(facts.get("english_level", "")).strip()
    if level:
        m = _CEFR_RE.search(level)
        if m:  # match by CEFR code (B2, C1, ...) first — most robust
            code = m.group(1).lower()
            for i, o in enumerate(options):
                if code in (o or "").lower().replace("/", " "):
                    return i, True
        for i, o in enumerate(options):  # else substring match on the level text
            if level.lower() in (o or "").lower() or (o or "").lower() in level.lower():
                return i, True
    for i, o in enumerate(options):  # unbacked professional default
        if _ENGLISH_DEFAULT.search(o or ""):
            return i, False
    return (len(options) // 2, False) if options else (None, False)


def deterministic_choices(questions: list[dict], facts: dict) -> list[dict]:
    """LLM-FREE option picks for the standard closed screeners, so an application form
    fills to 'only Submit remains' without any model call: eligibility/relocation
    (willingness fact), hear-about (referral fact), English level, and plain yes/no
    suitability. Returns one {"index": int|None, "backed": bool} per question, same
    order; index=None -> defer to the LLM/human. Backed picks are fact-supported;
    unbacked ones still fill the field but are flagged for the human to confirm."""
    out: list[dict] = []
    for q in questions:
        qt = q.get("question_text", "")
        opts = q.get("options", []) or []
        idx = _eligibility_pick(qt, opts, facts)
        backed = idx is not None
        if idx is None:
            idx = _referral_pick(qt, opts, facts)
            if idx is not None:
                backed = bool(str(facts.get("referral", "")).strip())
        if idx is None:
            idx, backed = _language_pick(qt, opts, facts)
        if idx is None:
            idx = _yesno_pick(qt, opts)  # unbacked affirmative default
        if idx is None:
            idx = _capability_pick(qt, opts)  # capability -> Yes, UNBACKED (review)
        out.append({"index": idx, "backed": bool(backed) if idx is not None else False})
    return out


def _prompt(questions: list[dict], facts: dict, job: dict, niche_label: str) -> str:
    blocks = []
    for i, q in enumerate(questions):
        opts = "\n".join(f"   {j}. {o}" for j, o in enumerate(q["options"]))
        blocks.append(f"Q{i}: {q['question_text']}\n{opts}")
    return (
        "You are filling a job application form for a real candidate. For each question "
        "below choose exactly ONE option, using ONLY the candidate facts.\n"
        "Rules:\n"
        '- Return one entry per question: {"q": <question number>, "choice": <0-based '
        'option index or null>, "backed": true|false}.\n'
        '- "backed" is true only when a candidate fact directly supports the choice; '
        "false when it is a sensible professional default (available, flexible, agrees "
        "to standard policies).\n"
        "- NEVER pick an option that contradicts the facts. If every option would "
        "contradict them, or the question needs information you don't have (a name, a "
        "date, an ID number), use null.\n\n"
        f"CANDIDATE FACTS: {json.dumps(facts)}\n"
        f"JOB: {job.get('title', '')} at {job.get('company', '')}"
        + (f" (résumé focus: {niche_label})" if niche_label else "") + "\n\n"
        + "\n\n".join(blocks) + "\n\n"
        'Return ONLY a JSON array like [{"q":0,"choice":2,"backed":true}, ...] with one '
        "entry per question."
    )


def _extract_array(raw: str) -> list | None:
    """First valid JSON array in the text — tolerant of prose/fences around and
    after it (weak models love to add notes containing more brackets)."""
    start = raw.find("[")
    while start != -1:
        try:
            arr, _ = json.JSONDecoder().raw_decode(raw[start:])
            if isinstance(arr, list):
                return arr
        except Exception:
            pass
        start = raw.find("[", start + 1)
    return None


def _parse(raw: str, questions: list[dict]) -> list[dict] | None:
    arr = _extract_array(raw or "")
    if arr is None:
        return None
    out = [{"index": None, "backed": False} for _ in questions]
    for item in arr:
        if not isinstance(item, dict):
            continue
        # duplicate "q" entries: last one wins (the model's final decision)
        qi, ch = item.get("q"), item.get("choice")
        if not isinstance(qi, int) or isinstance(qi, bool) or not 0 <= qi < len(questions):
            continue
        if isinstance(ch, int) and not isinstance(ch, bool) and 0 <= ch < len(questions[qi]["options"]):
            out[qi] = {"index": ch, "backed": bool(item.get("backed"))}
    # a reply where NOTHING validated is indistinguishable from garbage — let the
    # caller retry (truncated/mangled replies are common with a weak model)
    if not any(o["index"] is not None for o in out):
        return None
    return out


def choose_options(questions: list[dict], facts: dict, job: dict,
                   niche_label: str = "") -> list[dict]:
    """questions: [{"question_text": str, "options": [str, ...]}, ...]
    Returns one {"index": int|None, "backed": bool} per question, same order.
    index=None -> leave the question for the human."""
    results = [{"index": None, "backed": False} for _ in questions]
    askable = [i for i, q in enumerate(questions)
               if q.get("question_text") and 2 <= len(q.get("options", [])) <= MAX_OPTIONS]
    for start in range(0, len(askable), CHUNK):
        idxs = askable[start:start + CHUNK]
        subset = [questions[i] for i in idxs]
        parsed = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                parsed = _parse(_llm_complete(_prompt(subset, facts, job, niche_label)), subset)
            except Exception as e:
                logger.info("choose_options attempt %d failed: %s", attempt, e)
                parsed = None
            if parsed is not None:
                break
        if parsed is None:
            logger.warning("choose_options: %d questions left for the human", len(subset))
            continue
        for local_i, global_i in enumerate(idxs):
            results[global_i] = parsed[local_i]
    # Deterministic override for eligibility questions backed by a willingness fact —
    # runs regardless of what the model chose (it fumbles these).
    for i, q in enumerate(questions):
        pick = _eligibility_pick(q.get("question_text", ""), q.get("options", []), facts)
        if pick is not None:
            results[i] = {"index": pick, "backed": True}
    # Capability/experience yes-no -> Yes (ideal candidate), UNBACKED so it stays
    # review-flagged; forces the affirmative even if the weak LLM picked No/null.
    for i, q in enumerate(questions):
        cap = _capability_pick(q.get("question_text", ""), q.get("options", []))
        if cap is not None:
            results[i] = {"index": cap, "backed": False}
    return results
