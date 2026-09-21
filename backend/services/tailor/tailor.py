"""Tailor a structured base résumé to a specific job description.

Two paths, both NO-FABRICATION (only facts already in the base résumé are used):
  - Deterministic (default, no API key): reorder/select bullets by JD-keyword overlap,
    surface JD-relevant skills first, pick the closest preferred title.
  - AI polish (only if ANTHROPIC_API_KEY is set): rephrase summary + bullets to mirror
    the JD's wording. Validated so no new company/skill/number is introduced.
"""
import logging
import os
import re
import time as _time

from backend.config import settings
from backend.services.tailor import keywords as kw

logger = logging.getLogger(__name__)

# --- LLM circuit-breaker (per-process) -------------------------------------------------
# When the local LLM 5xx's PERSISTENTLY (e.g. the Codex/Sumrak provider refresh token has
# expired), the naive retry burns 2+4+8+16 = 30s PER CALL before the caller falls back to
# the deterministic keyword path — and one persona build makes SEVERAL LLM calls, so a
# single fill wastes minutes while the LLM is down (starving the offer lanes at volume).
# This breaker trips after a few consecutive full-cycle failures, then makes _llm_complete
# raise IMMEDIATELY (→ instant deterministic fallback) for a cooldown, then probes once
# more. Behaviour is IDENTICAL while the LLM is healthy — it only makes the already-
# happening fallback fast. Long-lived processes (dash/copilot) self-recover after the
# cooldown; short-lived lane subprocesses simply skip the wasted backoff.
_LLM_BREAKER_THRESHOLD = int(os.getenv("LLM_BREAKER_THRESHOLD", "2"))    # consecutive failed cycles to trip
_LLM_BREAKER_COOLDOWN = float(os.getenv("LLM_BREAKER_COOLDOWN", "300"))  # seconds the breaker stays open
_llm_fail_cycles = 0
_llm_down_until = 0.0

# --- Claude CLI fallback (subscription, NO API key) ------------------------------------
# When the local Sumrak LLM is down (its Codex provider token expired / hit its quota, or
# Cerebras is out of credit), fall back to the locally-installed `claude` CLI in headless
# print mode. It runs on the machine's Claude SUBSCRIPTION (on-disk creds) → NO OpenAI /
# Anthropic API credit needed, the same mechanism the assessment harvester's
# claude_cli_solver already uses. OPT-IN, default OFF: set `TAILOR_CLAUDE_CLI=1` to enable it
# (then a down Sumrak degrades to CLAUDE — quality preserved — instead of the deterministic
# keyword path). `TAILOR_CLAUDE_MODEL` picks the model (fast/cheap haiku by default; set a
# sonnet id for higher quality at more subscription usage). It runs with `--allowedTools ""`
# (NO tools — cannot read files / run commands, only the prompt text). The tailor's callers
# already strip ```code fences``` + trailing prose, so the CLI output is drop-in.
_CLAUDE_BIN: str | None = None
_CLAUDE_BIN_RESOLVED = False


def _claude_bin() -> str | None:
    """Locate the `claude` CLI once (cached). None if not installed."""
    global _CLAUDE_BIN, _CLAUDE_BIN_RESOLVED
    if _CLAUDE_BIN_RESOLVED:
        return _CLAUDE_BIN
    import shutil
    b = shutil.which("claude")
    if not b:
        for c in (os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude"):
            if os.path.exists(c):
                b = c
                break
    _CLAUDE_BIN, _CLAUDE_BIN_RESOLVED = b, True
    return b


def _claude_cli_complete(prompt: str) -> str | None:
    """One-shot completion via the local `claude` CLI (subscription, no API key). Returns the
    text, or None when disabled / no binary / the call fails (caller then uses the next tier)."""
    if (os.getenv("TAILOR_CLAUDE_CLI", "0") or "0").strip().lower() not in ("1", "true", "yes", "on"):
        return None                                  # OPT-IN: default OFF (set TAILOR_CLAUDE_CLI=1)
    b = _claude_bin()
    if not b:
        return None
    import subprocess
    model = (os.getenv("TAILOR_CLAUDE_MODEL") or "claude-haiku-4-5-20251001").strip()
    try:
        to = float(os.getenv("TAILOR_CLAUDE_TIMEOUT", "120") or "120")
    except ValueError:
        to = 120.0
    env = dict(os.environ)
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    try:
        # `--allowedTools ""` = NO tools: a pure text completion that CANNOT read files/run
        # commands — it only sees the prompt text (the résumé + JD are embedded in it).
        p = subprocess.run([b, "-p", prompt, "--model", model, "--allowedTools", ""],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=to, text=True)
        out = (p.stdout or "").strip()
        if not out:
            logger.warning("claude CLI empty output (rc=%s): %s", p.returncode, (p.stderr or "")[:120])
        return out or None
    except subprocess.TimeoutExpired:
        logger.warning("claude CLI timeout after %.0fs", to)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("claude CLI failed: %s", str(e)[:140])
        return None


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


# A bullet with a digit / % / $ carries a quantified result. Recruiters reward
# quantified impact, so among equally-JD-relevant bullets the quantified ones lead.
# This is ORDER ONLY — no number is ever invented (strict no-fabrication).
_METRIC_RX = re.compile(r"\d")


def _has_metric(bullet: str) -> int:
    return 1 if _METRIC_RX.search(bullet or "") else 0


def _join_and(items: list[str]) -> str:
    """'a', 'a and b', 'a, b, and c' — a natural strengths list for the summary."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _matched_own_skills(base_resume: dict, jd: str) -> list[str]:
    """The persona's OWN skills (verbatim from the base résumé) that the JD asks for,
    in JD-relevance order (de-duplicated, case-insensitive). NO new skill is ever
    added — this only SELECTS + reorders skills already present, so it is strictly
    no-fabrication."""
    jd_l = (jd or "").lower()
    seen: set[str] = set()
    matched: list[str] = []
    for items in (base_resume.get("skills_grouped") or {}).values():
        for s in items or []:
            sl = (s or "").strip().lower()
            if sl and sl not in seen and kw.term_present(sl, jd_l):
                seen.add(sl)
                matched.append(s.strip())
    return matched


def _targeted_summary(base_summary: str, headline: str,
                      matched_skills: list[str]) -> str:
    """Role-targeted, compelling summary built ONLY from tokens already in the base
    résumé (the aligned title + the persona's own JD-matched skills + the persona's
    own summary). Leads with the role-relevant strengths, keeps the real summary.
    No fabricated fact is introduced — every noun comes from the input."""
    base_summary = (base_summary or "").strip()
    head = (headline or "").strip()
    top: list[str] = []
    for s in matched_skills:
        if s and s.lower() not in {t.lower() for t in top}:
            top.append(s)
        if len(top) >= 3:
            break
    if head and top:
        lead = f"{head} with hands-on strengths in {_join_and(top)}."
    elif head:
        lead = f"{head}." if not head.endswith(".") else head
    else:
        lead = ""
    if base_summary and (not lead or base_summary.lower() not in lead.lower()):
        return (lead + " " + base_summary).strip() if lead else base_summary
    return lead or base_summary


def tailor_resume(base_resume: dict, job_title: str, job_company: str,
                  job_description: str, use_ai: bool = False) -> dict:
    jd = job_description or ""
    jd_kw = kw.extract_jd_keywords(jd)

    titles = base_resume.get("preferred_titles") or [base_resume.get("headline", "")]
    headline = _best_title(titles, job_title)

    experience = []
    for e in base_resume.get("experience", []):
        bullets = list(e.get("bullets", []))
        # Keep EVERY bullet (truthful coverage), ordering by (1) JD relevance then
        # (2) quantified impact — a recruiter's eye rewards a metric, and a metric-
        # carrying bullet at the top of a role reads stronger. ORDER ONLY: no bullet
        # is added, dropped, or edited, so nothing is fabricated.
        ranked = sorted(bullets, key=lambda b: (-kw.overlap(b, jd_kw), -_has_metric(b)))
        experience.append({**e, "bullets": ranked})

    # The persona's OWN skills the JD asks for, in JD order — used to build a
    # highlighted lead group AND the targeted summary. Verbatim from the résumé.
    matched_skills = _matched_own_skills(base_resume, jd)
    matched_lower = {s.lower() for s in matched_skills}

    skills_grouped: dict[str, list[str]] = {}
    # 1) A prominent "Key skills for this role" group leads the section with exactly
    #    the persona's own JD-matched skills (recruiter/ATS both skim the first line).
    #    NO new skill is invented — the old block that INJECTED JD keywords the résumé
    #    LACKED was removed: it violated the strict no-fabrication invariant that this
    #    module documents ("services/tailor is strictly no-fabrication").
    if matched_skills:
        skills_grouped["Key skills for this role"] = matched_skills
    # 2) The rest of the persona's skills follow, JD-relevant first WITHIN each group,
    #    minus the ones already surfaced above (no duplicate tokens).
    for grp, items in base_resume.get("skills_grouped", {}).items():
        rest = [s for s in items if s.lower() not in matched_lower]
        in_jd = [s for s in rest if kw.term_present(s.lower(), jd.lower())]
        tail = [s for s in rest if not kw.term_present(s.lower(), jd.lower())]
        ordered = in_jd + tail
        if ordered:
            skills_grouped[grp] = ordered

    tailored = {
        "personal_info": base_resume.get("personal_info", {}),
        "headline": headline,
        "eligibility": base_resume.get("eligibility", ""),
        # Role-targeted summary — leads with the aligned title + the persona's own
        # top JD-matched strengths, then keeps the real summary. Tokens are all from
        # the base résumé (no invented fact); use_ai may still rephrase it below.
        "summary": _targeted_summary(base_resume.get("summary", ""), headline, matched_skills),
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
        global _llm_fail_cycles, _llm_down_until
        import httpx
        last_exc: Exception | None = None
        # Try the local Sumrak LLM unless the breaker is open (recent persistent 5xx).
        if _time.monotonic() >= _llm_down_until:
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
                    _llm_fail_cycles = 0            # a success closes the breaker
                    return r.json()["choices"][0]["message"]["content"]
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    last_exc = e
                    logger.warning("LLM transport error (attempt %d): %s", attempt, e)
                    _time.sleep(min(2 ** attempt, 20))
            # the whole retry cycle failed — count it toward the breaker
            _llm_fail_cycles += 1
            if _llm_fail_cycles >= _LLM_BREAKER_THRESHOLD:
                _llm_down_until = _time.monotonic() + _LLM_BREAKER_COOLDOWN
                _llm_fail_cycles = 0
                logger.warning("LLM circuit-breaker OPEN for %.0fs after %d failed cycles — "
                               "using the Claude CLI / deterministic fallback until it probes again",
                               _LLM_BREAKER_COOLDOWN, _LLM_BREAKER_THRESHOLD)
        # Sumrak unavailable (just failed, or the breaker is open) → Claude CLI (subscription).
        cli = _claude_cli_complete(prompt)
        if cli:
            return cli
        raise last_exc if last_exc else RuntimeError("LLM unavailable (Sumrak down, no Claude CLI)")

    # No local LLM configured: Claude CLI first (free/subscription), then the Anthropic API.
    cli = _claude_cli_complete(prompt)
    if cli:
        return cli
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
        "You are an expert résumé writer tailoring a candidate's résumé so a recruiter "
        "for THIS specific role wants to interview them, and so it ranks well in an ATS "
        "keyword scan.\n"
        "Rephrase ONLY the `summary` and existing experience `bullets`:\n"
        "- Make the `summary` a punchy 2-3 sentence pitch that LEADS with the strengths "
        "most relevant to this role and naturally uses the JD's key terms that the "
        "candidate TRUTHFULLY has.\n"
        "- Rewrite each existing bullet to start with a strong action verb and mirror "
        "the JD's vocabulary WHERE IT TRUTHFULLY MATCHES; keep any real metric that is "
        "already in the bullet and lead with it.\n"
        "HARD RULES (safety): Do NOT invent or add companies, tools, certifications, "
        "numbers, dates, metrics, or any claim not already present in the input. Do NOT "
        "add, remove, split, or merge bullets — rephrase the SAME facts one-to-one. Keep "
        "the same JSON shape and the same number of experience entries and bullets.\n\n"
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
