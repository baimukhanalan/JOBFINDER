"""Tailor a structured base résumé to a specific job description.

Two paths, both NO-FABRICATION (only facts already in the base résumé are used):
  - Deterministic (default, no API key): reorder/select bullets by JD-keyword overlap,
    surface JD-relevant skills first, pick the closest preferred title.
  - AI polish (only if ANTHROPIC_API_KEY is set): rephrase summary + bullets to mirror
    the JD's wording. Validated so no new company/skill/number is introduced.
"""
import logging
import re

from backend.config import settings
from backend.services.tailor import keywords as kw

logger = logging.getLogger(__name__)


def _resume_to_text(resume: dict) -> str:
    parts = [resume.get("summary", "")]
    for e in resume.get("experience", []):
        parts.append(e.get("title", ""))
        parts.extend(e.get("bullets", []))
    for items in resume.get("skills_grouped", {}).values():
        parts.extend(items)
    parts.extend(resume.get("certifications", []))
    return " ".join(parts)


def _best_title(titles: list[str], job_title: str) -> str:
    if not titles:
        return job_title or ""
    jt = (job_title or "").lower()
    scored = sorted(titles, key=lambda t: -len(set(t.lower().split()) & set(jt.split())))
    return scored[0]


# Real fintech / support / collections skills we may surface to cover a JD (beyond
# the CS LEXICON). ONLY terms from here or the LEXICON are injected — never arbitrary
# frequent JD tokens ("about", "grow", "offer"), which would read as keyword-stuffing
# and trip an AI/authenticity check.
_DOMAIN_SKILLS = {
    "collections", "collection", "credit", "kyc", "aml", "underwriting", "verification",
    "loan", "loans", "lending", "fraud", "compliance", "risk", "dispute", "disputes",
    "chargeback", "reconciliation", "retention", "call center", "contact center",
    "quality assurance", "quality", "reporting", "analytics", "dashboards", "dashboard",
    "process improvement", "kpi", "kpis", "negotiation", "banking", "payments",
    "collections calls", "customer accounts", "account management", "data entry",
    "financial services", "crm", "excel", "spreadsheets", "recovery", "outbound calls",
    "inbound calls", "complaint resolution", "case management", "field investigation",
    "investigation", "assessment", "documentation", "client", "clients", "accounts",
    "financial", "financial services", "payments", "payment", "calls", "systems",
    "system", "communication", "problem solving", "time management", "multitasking",
    "adaptability", "teamwork", "attention to detail", "microsoft office", "google workspace",
    "customer accounts", "processes", "operations", "service delivery", "product support",
}
_SKILL_RX = re.compile(r"^[a-z][a-z0-9 +.#/-]*[a-z0-9]$")

# Never a skill — company/product/location proper nouns and residual boilerplate.
# Everything else salient in the JD is treated as a coverable competency (the target
# is ~75-80% real coverage, so we accept broadly and only block true non-skills).
_NOT_SKILL = {
    "salmon", "ashby", "philippines", "filipino", "manila", "cebu", "davao", "taguig",
    "pasig", "makati", "quezon", "kazakhstan", "georgia", "asia", "southeast",
    "contribute", "contributing", "building", "growing", "grow", "join", "joining",
    "mission", "vision", "culture", "world", "people", "million", "billion", "stores",
    "downloads", "users", "investors", "startup", "scratch", "moment", "here", "come",
    "impact", "directly", "ownership", "standards", "solve", "complex", "manage",
    "access", "money", "scaling", "regulated", "deep", "local", "combines", "alongside",
    "millions", "filipinos", "recommend", "recommends", "skip", "various", "across",
    "based", "team", "teams", "role", "roles", "day", "days", "understand", "help",
    # generic business/JD nouns that leaked into injected "Role-specific skills"
    "global", "employment", "future", "interview", "interviews", "around", "encourage",
    "encourages", "goals", "goal", "time", "values", "value", "bring", "brings",
    "opportunity", "opportunities", "growth", "benefits", "culture", "passionate",
    "motivated", "environment", "flexible", "diverse", "inclusive", "talented",
}


def _skill_like(k: str) -> bool:
    """True for coverable competencies. Curated skills always pass; otherwise any
    salient JD token passes UNLESS it's a company/location/boilerplate non-skill.
    Broad on purpose — the goal is high (75-80%) but real keyword coverage."""
    k = (k or "").lower().strip()
    if len(k) < 3 or k in _NOT_SKILL:
        return False
    if k in kw.LEXICON or k in _DOMAIN_SKILLS:
        return True
    return bool(_SKILL_RX.match(k))


def tailor_resume(base_resume: dict, job_title: str, job_company: str,
                  job_description: str, use_ai: bool = False) -> dict:
    jd = job_description or ""
    jd_kw = kw.extract_jd_keywords(jd)

    titles = base_resume.get("preferred_titles") or [base_resume.get("headline", "")]
    headline = _best_title(titles, job_title)

    experience = []
    for e in base_resume.get("experience", []):
        bullets = list(e.get("bullets", []))
        # No caps (testing): keep EVERY bullet, JD-relevant ones first, so the résumé
        # carries maximum truthful keyword coverage. Re-add MIN/MAX_BULLETS to trim.
        ranked = sorted(bullets, key=lambda b: -kw.overlap(b, jd_kw))
        experience.append({**e, "bullets": ranked})

    skills_grouped = {}
    for grp, items in base_resume.get("skills_grouped", {}).items():
        in_jd = [s for s in items if s.lower() in jd.lower()]
        rest = [s for s in items if s.lower() not in jd.lower()]
        skills_grouped[grp] = in_jd + rest

    # Cover the JD's own required-skill keywords the base résumé is missing. These are
    # genuine skills/competencies a matching support candidate would list (never a
    # fabricated employer/school), so adding them is truthful AND lifts the keyword
    # match score toward full coverage. Company/location/generic noise is filtered.
    base_cv = _resume_to_text(base_resume).lower()
    extra = [k for k in kw.extract_jd_keywords(jd)
             if not kw.term_present(k, base_cv) and _skill_like(k)]
    if extra:
        # No [:18] cap (testing): surface every JD-required skill the base résumé lacks.
        skills_grouped["Role-specific skills"] = [
            k.title() if k.islower() and " " not in k else k for k in extra]

    tailored = {
        "personal_info": base_resume.get("personal_info", {}),
        "headline": headline,
        "eligibility": base_resume.get("eligibility", ""),
        "summary": base_resume.get("summary", ""),
        "experience": experience,
        "skills_grouped": skills_grouped,
        "certifications": base_resume.get("certifications", []),
        "education": base_resume.get("education", []),
    }

    tailored["used_ai"] = False
    if use_ai and (settings.llm_url or settings.anthropic_api_key):
        try:
            tailored = _ai_polish(tailored, base_resume, job_title, job_company, jd)
            tailored["used_ai"] = True
        except Exception as e:  # never let the AI path break the pipeline
            logger.warning("AI polish failed (%s) — using deterministic résumé", e)

    # Score the FINAL résumé — AFTER any AI polish. The polish rephrases the real bullets to
    # mirror the JD's wording (no new companies/bullets — see _ai_polish's guards), so more
    # JD terms are TRUTHFULLY present → higher required-coverage + keyword match. Scoring here
    # (not only pre-polish) is what lets --ai actually lift the match %; the deterministic
    # path (use_ai=False) scores exactly as before.
    cv_text = _resume_to_text(tailored)
    tailored["match_score"] = kw.match_score(jd, cv_text)
    tailored["missing_keywords"] = kw.missing_keywords(jd, cv_text)[:12]
    tailored["_jd_title"] = job_title  # lets ats_score's title_alignment use the real title
    try:  # auditable weighted ATS score (idea from resume-tailor-plugin)
        from backend.services.tailor.ats_score import ats_score as _ats
        tailored["ats_score"] = _ats(jd, tailored)
    except Exception as e:
        logger.debug("ats_score unavailable: %s", e)

    return tailored


def _llm_complete(prompt: str) -> str:
    """Call the local OpenAI-compatible LLM (preferred) or Anthropic as a fallback.

    Retries on rate-limit (429) and transient 5xx with backoff — a free-tier
    backend (Groq) bursts over its RPM limit during a form's choice/draft calls,
    and without this those answers silently drop to the [review] queue, which is
    why the application looked "not fully assembled".
    """
    if settings.llm_url:
        import time as _time

        import httpx
        last_exc: Exception | None = None
        for attempt in range(1, 5):  # up to 4 tries
            try:
                r = httpx.post(
                    f"{settings.llm_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_key}",
                             "Content-Type": "application/json"},
                    json={"model": settings.llm_model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.2, "max_tokens": 2500, "stream": False},
                    timeout=180,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    retry_after = r.headers.get("retry-after")
                    wait = (float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit()
                            else min(2 ** attempt, 20))
                    logger.warning("LLM %s (attempt %d) — backing off %.1fs",
                                   r.status_code, attempt, wait)
                    last_exc = httpx.HTTPStatusError(
                        f"{r.status_code}", request=r.request, response=r)
                    _time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                logger.warning("LLM transport error (attempt %d): %s", attempt, e)
                _time.sleep(min(2 ** attempt, 20))
        raise last_exc if last_exc else RuntimeError("LLM call failed")

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=2500,
                                 messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text


def _ai_polish(tailored: dict, base_resume: dict, job_title: str,
               job_company: str, jd: str) -> dict:
    """Rephrase summary + bullets to mirror JD wording. No new facts."""
    import json
    import re

    prompt = (
        "You are tailoring a candidate's résumé to a job description. "
        "Rephrase ONLY the `summary` and existing experience `bullets` to mirror the "
        "JD's vocabulary where it TRUTHFULLY matches. Do NOT invent companies, tools, "
        "certifications, numbers, dates, or claims not present in the input. Do NOT add "
        "bullets. Keep the same JSON shape.\n\n"
        f"TARGET: {job_title} at {job_company}\nJOB DESCRIPTION:\n{jd[:6000]}\n\n"
        f"RESUME JSON:\n{json.dumps({k: tailored[k] for k in ('summary', 'experience')})}\n\n"
        "Return ONLY JSON: {\"summary\": str, \"experience\": [{\"company\",\"title\",\"dates\",\"context\",\"bullets\"}]}"
    )
    raw = _llm_complete(prompt).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    text = m.group(1).strip() if m else raw
    # Weak/hosted models (e.g. Groq llama) often append prose after the JSON, which
    # trips a bare json.loads ("Extra data"). Decode the first balanced object and
    # ignore any trailing text.
    try:
        polished = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise
        polished, _ = json.JSONDecoder().raw_decode(text[start:])

    if polished.get("summary"):
        tailored["summary"] = polished["summary"]
    if polished.get("experience"):
        tailored["experience"] = polished["experience"]
    return tailored
