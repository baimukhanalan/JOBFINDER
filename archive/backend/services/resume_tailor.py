"""Tailor master CV to a specific job description.

Pipeline:
    cv_master.md + proof_points.md + Job  →  Claude  →  structured JSON
                                                              ↓
                                                       Jinja2 template
                                                              ↓
                                              Playwright (existing BrowserManager)
                                                              ↓
                                                  /uploads/resumes/{job_id}.pdf

The Claude prompt forbids fabrication. The validator flags any company /
certification / education entry that is NOT in the source files.
"""
import json
import logging
import re
from pathlib import Path

import anthropic
from jinja2 import Environment, FileSystemLoader, select_autoescape

from backend.applier.browser import BrowserManager
from backend.config import settings
from backend.models.job import Job

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CV_MASTER_PATH = PROJECT_ROOT / "data" / "cv_master.md"
PROOF_POINTS_PATH = PROJECT_ROOT / "data" / "proof_points.md"
TEMPLATE_DIR = PROJECT_ROOT / "backend" / "templates"
OUTPUT_DIR = PROJECT_ROOT / "uploads" / "resumes"


PROMPT = """You are tailoring a Customer Support professional's resume for a specific job application.

# SOURCE OF TRUTH

## MASTER_CV
{cv_master}

## PROOF_POINTS (extended evidence and metrics)
{proof_points}

# TARGET JOB

- **Title:** {job_title}
- **Company:** {job_company}
- **Description:**
{job_description}

# YOUR TASK

Generate a tailored resume by:
1. Selecting the most relevant experience and skills from MASTER_CV for this specific JD
2. Reordering bullets so most-relevant come first; drop bullets that don't help for this role
3. Rephrasing wording to match the JD's language (use their keywords where truthful)
4. Picking the appropriate summary length (short for compact roles, longer for senior)
5. Choosing the most fitting `preferred_title` from MASTER_CV's metadata
6. Selecting only the certifications and skills relevant to this JD (subset of MASTER_CV)

# CRITICAL RULES — NO FABRICATION

- Use ONLY facts that appear in MASTER_CV or PROOF_POINTS.
- DO NOT change company names, job titles, dates, education, or certification names.
- DO NOT invent metrics. If a number is not in the source, do not write a number.
- DO NOT claim familiarity with a tool, certification, or methodology that is not in MASTER_CV's skills/certifications sections.
- If a JD requirement is not present in MASTER_CV, leave it unaddressed. Better to omit than to fabricate.
- Rephrasing existing bullets is allowed and encouraged. Inventing new claims is forbidden.

# OUTPUT FORMAT

Return ONLY valid JSON, no commentary, no markdown fences. Schema:

{{
  "personal_info": {{
    "full_name": "...",
    "email": "...",
    "phone": "...",
    "location": "...",
    "linkedin": "..."
  }},
  "headline": "Customer Support Specialist",
  "summary": "...",
  "experience": [
    {{
      "company": "...",
      "title": "...",
      "dates": "...",
      "context": "...",
      "bullets": ["...", "..."]
    }}
  ],
  "skills_grouped": {{
    "Customer Support Platforms": ["..."],
    "Other Tools": ["..."]
  }},
  "certifications": ["..."],
  "education": [
    {{"degree": "...", "school": "...", "year": "..."}}
  ]
}}
"""


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    return json.loads(text)


def _extract_companies_from_master(cv_master: str) -> set[str]:
    """Pull company names from `### COMPANY — TITLE | DATES` headers."""
    pattern = r"^###\s+([^—\-—]+?)\s*[—\-—]"
    return {
        m.group(1).strip().strip("`<>").strip()
        for m in re.finditer(pattern, cv_master, re.MULTILINE)
    }


def _extract_certs_from_master(cv_master: str) -> set[str]:
    """Pull certification names from the Certifications section."""
    section_match = re.search(
        r"##\s*\d*\.?\s*Certifications\s*\n(.*?)(?=\n##\s|\Z)",
        cv_master,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return set()
    body = section_match.group(1)
    certs = set()
    for line in body.splitlines():
        m = re.match(r"^\s*-\s*\*\*([^*]+)\*\*", line)
        if m:
            certs.add(m.group(1).strip())
    return certs


def _validate_no_fabrication(structured: dict, cv_master: str) -> list[str]:
    """Return list of warnings for any output entries not present in source."""
    warnings: list[str] = []

    master_companies = _extract_companies_from_master(cv_master)
    master_companies_lower = {c.lower() for c in master_companies}

    for exp in structured.get("experience", []):
        c = (exp.get("company") or "").strip()
        if c and c.lower() not in master_companies_lower and not c.startswith("<"):
            warnings.append(f"company not in master: {c!r}")

    master_certs = _extract_certs_from_master(cv_master)
    master_certs_lower = {c.lower() for c in master_certs}
    for cert in structured.get("certifications", []) or []:
        cert_name = cert.split("—")[0].strip()
        if cert_name and cert_name.lower() not in master_certs_lower:
            warnings.append(f"cert not in master: {cert_name!r}")

    return warnings


def _cache_path(job: Job) -> Path:
    return OUTPUT_DIR / f"job_{job.id}.pdf"


async def _generate_structured(job: Job) -> dict:
    cv_master = _read_file(CV_MASTER_PATH)
    proof_points = _read_file(PROOF_POINTS_PATH)
    if not cv_master.strip():
        raise RuntimeError(f"cv_master.md is empty or missing: {CV_MASTER_PATH}")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    prompt = PROMPT.format(
        cv_master=cv_master,
        proof_points=proof_points or "(none)",
        job_title=job.title,
        job_company=job.company,
        job_description=(job.description or "(no description)")[:8000],
    )

    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text
    structured = _extract_json(raw)

    warnings = _validate_no_fabrication(structured, cv_master)
    if warnings:
        logger.warning("resume_tailor: %d fabrication warnings for job %d: %s",
                       len(warnings), job.id, warnings[:5])

    return structured


def _render_html(structured: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("cv.html")
    return template.render(**structured)


async def _html_to_pdf(html: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bm = await BrowserManager.get_instance()
    page = await bm.new_page()
    try:
        await page.set_content(html, wait_until="domcontentloaded")
        await page.emulate_media(media="print")
        await page.pdf(
            path=str(output_path),
            format="Letter",
            print_background=True,
            margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"},
        )
    finally:
        await page.close()


async def tailor_resume(job: Job, force: bool = False) -> Path:
    """Generate a JD-tailored resume PDF for the given job.

    Cached by job.id. Set force=True to regenerate.
    """
    cache = _cache_path(job)
    if cache.exists() and not force:
        return cache

    structured = await _generate_structured(job)
    html = _render_html(structured)
    await _html_to_pdf(html, cache)
    logger.info("Tailored resume generated: %s", cache)
    return cache
