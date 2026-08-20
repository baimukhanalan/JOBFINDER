"""Draft answers to open-ended application questions from the candidate's REAL profile.

The LLM drafts one CHUNK of questions per call (small batches keep the local model
reliable). Answers are grounded strictly in the profile + per-person fact sheet;
behavioral "describe a time" answers are prefixed with "[review]" so the human
personalizes them with their real story. The human reviews everything before Submit.
No invented employers / certifications / metrics.
"""
import json
import logging
import re
import time

from backend.services.tailor.choices import _extract_array
from backend.services.tailor.tailor import _llm_complete

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 20
CHUNK = 8
DRAFT_ATTEMPTS = 3  # retries per chunk so one transient failure doesn't blank a form

# Wire prefix that marks "a human must personalize this before Submit" (see
# strategies.base.strip_review). The drafting prompt ASKS the model to add it, but
# the local model is small and sometimes forgets — so we ALSO force it
# deterministically below. Defence in depth: the human's #1 promise is that he
# only reviews flagged answers, so a behavioural/specifics question must never
# slip through unflagged just because the model dropped the prefix.
_REVIEW = "[review] "
# NOTE: (?i) only — NOT (?ix): verbose mode would treat '#' as a comment and drop
# literal spaces, both of which this pattern relies on.
_NEEDS_REVIEW = re.compile(
    r"(?i)(?:"
    r"describe (?:a|an|the|your)\b.{0,40}?\b(?:time|situation|instance|example|experience|challenge|conflict)"
    r"|tell (?:us|me) about a time"
    r"|give (?:a |an )?(?:specific )?example"
    r"|share an experience"
    r"|walk (?:us|me) through a (?:time|situation|project)"
    r"|a time when (?:you|your)"
    r"|example of a time"
    r"|provide (?:a )?reference"
    r"|references? (?:with|contact)"
    r"|list\b.{0,20}?\breferences?\b"
    r"|certificat(?:e|ion) (?:number|id|#)"
    r"|licen[sc]e (?:number|#)"
    r"|link to your"
    r"|portfolio (?:link|url)"
    r"|\bgithub\b"
    r"|linkedin (?:url|profile)"
    r")"
)


def _force_review(question: str, answer: str) -> str:
    """Deterministic backstop for the '[review] ' flag. If the question demands the
    candidate's own story / verifiable specifics and the model didn't self-flag,
    prefix it so the human is guaranteed to personalize it before Submit."""
    if answer.startswith("[review]"):
        return answer
    if _NEEDS_REVIEW.search(question):
        return _REVIEW + answer
    return answer


def _header(profile_form: dict, job: dict, resume_summary: str,
            facts: dict | None, niche_label: str) -> str:
    candidate = {k: v for k, v in profile_form.items() if not k.startswith("_") and v}
    candidate.update({k: v for k, v in (facts or {}).items() if v not in (None, "", [])})
    role = job.get("title") or "the role"
    company = job.get("company") or "the company"
    focus = f" The résumé being submitted is focused on: {niche_label}." if niche_label else ""
    # Fit the answers to THIS specific posting: give the model the job description so
    # "why this role / why us / what interests you" answers reference the actual role,
    # its mission and requirements — not a generic template. Grounding rules still bind
    # (no invented facts); the JD only shapes tone/relevance, never adds candidate facts.
    jd = re.sub(r"\s+", " ", (job.get("description") or "")).strip()
    jd_block = (f"ROLE DESCRIPTION (tailor 'why this role/company/what interests you' "
                f"answers to fit this — reference its real focus, but invent NO personal "
                f"facts): {jd[:1200]}\n\n") if jd else ""
    return (
        f"You are drafting short job-application answers for a candidate applying to "
        f"{role} at {company}.{focus} Use ONLY the candidate facts below; tailor relevance "
        f"to the role description. Rules:\n"
        "- Use ONLY facts present in the profile/experience. Do NOT invent employers, "
        "dates, certifications, or specific numbers/metrics.\n"
        "- Each answer 1-3 sentences, professional, first person.\n"
        "- Write in the candidate's natural voice, as if filling the form himself. NEVER "
        "refer to 'my profile', 'the information provided', 'not listed', or that a fact "
        "is missing. If a detail isn't available, give a sensible professional default "
        "(e.g. 'How did you hear about this role?' -> 'Through an online job search'; a "
        "yes/no with no blockers -> answer plainly, e.g. 'No, none.').\n"
        "- For a question asking to 'describe a time' or 'give an example', OR a question "
        "that demands specific verifiable items you do not have (named references with "
        "contact info, links/usernames/IDs, certificates), write the best truthful answer "
        "and PREFIX it with '[review] ' so the human completes it with real specifics.\n"
        "- For simple/factual questions, answer directly and briefly.\n\n"
        + jd_block
        + f"CANDIDATE FACTS: {json.dumps(candidate, default=str)}\n"
        f"EXPERIENCE SUMMARY: {resume_summary[:1500]}\n\n"
    )


def _draft_chunk(questions: list[str], header: str) -> dict[str, str]:
    prompt = (
        header
        + "QUESTIONS:\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        + "\n\nReturn ONLY a JSON array of answer strings in the SAME ORDER as the "
          'questions, e.g. ["answer to 1", "answer to 2"].'
    )
    last_err = None
    for attempt in range(1, DRAFT_ATTEMPTS + 1):
        try:
            raw = _llm_complete(prompt)
            arr = _extract_array(raw or "")
            if arr is None:
                arr = []
            drafted = {q: str(arr[i]).strip() for i, q in enumerate(questions)
                       if i < len(arr) and str(arr[i]).strip()}
            if drafted:
                return drafted
            last_err = "empty/unparseable response"
        except Exception as e:
            last_err = e
        if attempt < DRAFT_ATTEMPTS:
            logger.info("draft chunk attempt %d failed (%s) — retrying", attempt, last_err)
            time.sleep(2 * attempt)  # growing backoff: 2s, 4s
    logger.warning("draft chunk failed after %d attempts (%s) — %d questions left "
                   "for the human", DRAFT_ATTEMPTS, last_err, len(questions))
    return {}


def _telegram_handle(profile_form: dict) -> str:
    return "@none"  # per request: Telegram is @none wherever a form asks for it


# "Soft" open questions: a grounded LLM draft is better than a template, so we
# prefer the model and only synthesize a deterministic fallback if it comes back
# empty (weak/rate-limited backend). These stay [review] — the human's own words.
_SOFT_RE = re.compile(
    r"(?i)cover letter|motivation letter|tell us about yourself|about yourself"
    r"|why (?:do|are|would|should).{0,40}?(?:interested|want|good fit|join|hire|"
    r"this role|this company|this position|work (?:here|with|for))")


def _letter_body(job: dict, resume_summary: str) -> str:
    role = job.get("title") or "the role"
    company = job.get("company") or "your company"
    first = (resume_summary.strip().split(". ")[0] if resume_summary else "").strip(". ")
    return (
        f"Dear {company} Hiring Team, I am excited to apply for the {role} position. "
        f"{first + '. ' if first else ''}I bring strong communication, issue-resolution, "
        "and CRM experience across support and financial services, and I would welcome "
        "the chance to contribute to your team. Thank you for your consideration.")


def _soft_fallback(q: str, profile_form: dict, job: dict, resume_summary: str) -> str:
    return _REVIEW + _letter_body(job, resume_summary)


def cover_letter(job: dict, resume_summary: str, profile_form: dict,
                 use_llm: bool = True) -> str:
    """A grounded cover-letter body to fill a form's cover-letter field VERBATIM
    (no [review] prefix — it goes straight into the textarea). LLM when available
    and asked, deterministic template otherwise. No fabricated facts."""
    if use_llm:
        name = profile_form.get("full_name", "")
        role = job.get("title") or "the role"
        company = job.get("company") or "the company"
        prompt = (
            f"Write a concise professional cover letter (110-150 words, one paragraph) "
            f"for {name} applying to {role} at {company}. Ground it ONLY in this "
            f"experience summary — invent no employers, dates, or metrics:\n"
            f"{resume_summary[:1200]}\n"
            "First person, warm but professional. Return ONLY the letter text.")
        try:
            txt = _llm_complete(prompt).strip()
            txt = re.sub(r"^```.*?\n|```$", "", txt).strip()
            if len(txt) > 80 and "as an ai" not in txt.lower():
                return txt
        except Exception as e:
            logger.info("cover-letter LLM draft failed (%s) — using template", e)
    return _letter_body(job, resume_summary)


def _deterministic_answer(q: str, profile_form: dict, job: dict,
                          facts: dict | None) -> str | None:
    """Fill HARD (factual/identity) open fields from the profile/facts WITHOUT the
    LLM — a weak or rate-limited model can never leave these blank. Soft/personal
    questions are handled by the LLM (with _soft_fallback), not here."""
    ql = q.lower()
    facts = facts or {}

    if "telegram" in ql:  # fixed @none per request — no review flag needed
        return _telegram_handle(profile_form)

    if ("hear about" in ql or "how did you" in ql
            or ("find" in ql and any(w in ql for w in ("posting", "job", "position", "us", "role")))
            or "where did you" in ql or "referr" in ql):
        return facts.get("referral") or "Through an online job search"

    if ("notice period" in ql or "when can you start" in ql or "earliest" in ql
            or ("available" in ql and "start" in ql) or "start date" in ql):
        return facts.get("notice_period") or profile_form.get("available_start") or "Immediately"

    if "salary" in ql or "compensation" in ql or "expected pay" in ql:
        return (profile_form.get("desired_salary") or facts.get("salary_annual")
                or "Open to discussion based on the role")

    if "linkedin" in ql:
        return profile_form.get("linkedin_url") or "N/A"
    if "portfolio" in ql or "website" in ql or "personal site" in ql or "github" in ql:
        return profile_form.get("linkedin_url") or "N/A"

    return None


# Identity/link questions the deterministic pass deliberately LEAVES for the human
# (or a mapped rule field): LinkedIn is intentionally kept blank on the form (see
# runner), and portfolio/github "N/A" adds no value — better an empty optional field.
_DET_SKIP = re.compile(r"(?i)linkedin|portfolio|\bgithub\b|personal (?:site|website)|website")


def deterministic_answers(questions: list[str], profile_form: dict, job: dict,
                          facts: dict | None = None) -> dict[str, str]:
    """The LLM-FREE subset of draft_answers: fill HARD factual/identity open fields
    (telegram, hear-about, notice period, salary) straight from the profile/facts —
    no model call, so these always land even when answer-drafting is off or the model
    is down. Link fields are skipped (left blank/human). Returns {question: answer},
    review-flagged where the wire contract requires it."""
    out: dict[str, str] = {}
    for q in questions:
        qs = (q or "").strip()
        if len(qs) <= 12 or _DET_SKIP.search(qs):
            continue
        det = _deterministic_answer(qs, profile_form, job, facts)
        if det:
            out[qs] = det
    return {q: _force_review(q, a) for q, a in out.items()}


def draft_answers(questions: list[str], profile_form: dict, job: dict,
                  resume_summary: str = "", facts: dict | None = None,
                  niche_label: str = "") -> dict[str, str]:
    """Return {question: drafted_answer} for open-ended questions. Empty on failure."""
    questions = [q.strip() for q in questions if q and len(q.strip()) > 12][:MAX_QUESTIONS]
    if not questions:
        return {}
    out: dict[str, str] = {}
    # 1) HARD factual pre-pass — never depends on the LLM being up.
    remaining: list[str] = []
    for q in questions:
        det = _deterministic_answer(q, profile_form, job, facts)
        if det:
            out[q] = det
        else:
            remaining.append(q)
    # 2) LLM drafts the rest (incl. soft/personal questions) in small chunks.
    if remaining:
        header = _header(profile_form, job, resume_summary, facts, niche_label)
        for start in range(0, len(remaining), CHUNK):
            out.update(_draft_chunk(remaining[start:start + CHUNK], header))
    # 3) Deterministic fallback for SOFT questions the LLM left blank — so a cover
    #    letter / "why do you want this" is never empty even if the model failed.
    for q in remaining:
        if q not in out and _SOFT_RE.search(q):
            out[q] = _soft_fallback(q, profile_form, job, resume_summary)
    return {q: _force_review(q, a) for q, a in out.items()}
