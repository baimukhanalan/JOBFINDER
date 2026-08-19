"""Résumé-parser-only (anti-spam) pre-fill mode.

`strategy.prefill(..., resume_parser_only=True)` — or the RESUME_PARSER_ONLY env
switch — must feed the résumé to the ATS's OWN parser + attach the file, and type
NOTHING else. The machine-gun field-fill is the automation tell that trips ATS spam
detection; this mode avoids it and leaves the remaining required fields to the human.

Self-contained: loads inline HTML via Playwright set_content (no network / no fixture
file). Same asyncio.run-per-test style as test_dom_fixtures.py.

Run: PYTHONPATH=. python3 -m pytest backend/tests/test_resume_parser_only.py -q
"""
import asyncio
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.async_api import async_playwright  # noqa: E402

from backend.applier.strategies.base import GenericStrategy  # noqa: E402

_FORM_HTML = """
<!doctype html><html><body>
<form>
  <label>First name <input type="text" name="first_name" id="first_name"></label>
  <label>Email <input type="email" name="email" id="email"></label>
  <label>Phone <input type="tel" name="phone" id="phone"></label>
  <label>Resume/CV <input type="file" name="resume" id="resume"
                          accept=".pdf,.doc,.docx"></label>
  <label>Why do you want this role? Describe a time you led a project.
    <textarea name="q_why" id="q_why"></textarea></label>
  <button type="submit">Submit application</button>
</form>
</body></html>
"""

_FORM = {"first_name": "Jordan", "last_name": "Sample", "full_name": "Jordan Sample",
         "email": "jordan@example.com", "phone": "312-555-0142",
         "location": "Chicago, IL, US"}
_JOB = {"title": "Support Specialist", "company": "Acme", "description": "..."}


async def _run(prefill_kw: dict) -> dict:
    """Load the inline form, run GenericStrategy.prefill, return the report plus the
    live DOM values so callers can assert what was (not) typed."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4\n% fake resume\n")
        resume_pdf = f.name
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(_FORM_HTML, wait_until="domcontentloaded")
            form = dict(_FORM)
            form["resume_path"] = resume_pdf  # runner sets this for the full-fill path
            report = await GenericStrategy().prefill(
                page, form, resume_pdf, job=_JOB, **prefill_kw)
            dom = {
                "first_name": await page.eval_on_selector("#first_name", "e=>e.value"),
                "email": await page.eval_on_selector("#email", "e=>e.value"),
                "q_why": await page.eval_on_selector("#q_why", "e=>e.value"),
                "resume_files": await page.eval_on_selector("#resume", "e=>e.files.length"),
            }
            await browser.close()
            return {"report": report, "dom": dom}
    finally:
        Path(resume_pdf).unlink(missing_ok=True)


def test_parser_only_attaches_resume_but_types_nothing():
    out = asyncio.run(_run({"resume_parser_only": True}))
    rep, dom = out["report"], out["dom"]
    assert rep.get("mode") == "resume_parser_only"
    assert rep.get("resume_attached") is True
    assert dom["resume_files"] == 1, "the résumé must be attached"
    # The anti-spam invariant: NO programmatic typing into text / textarea fields.
    assert dom["first_name"] == ""
    assert dom["email"] == ""
    assert dom["q_why"] == ""


def test_full_mode_still_fills_and_attaches():
    """Regression guard: the default (non-parser-only) path is unchanged."""
    out = asyncio.run(_run({}))
    rep, dom = out["report"], out["dom"]
    assert rep.get("mode") != "resume_parser_only"
    assert dom["first_name"] == "Jordan", "full mode should type identity fields"
    assert dom["resume_files"] == 1, "full mode should still attach the résumé"


def test_env_switch_flips_mode_without_the_param(monkeypatch):
    monkeypatch.setenv("RESUME_PARSER_ONLY", "1")
    out = asyncio.run(_run({}))  # no param — the env switch must flip it
    rep, dom = out["report"], out["dom"]
    assert rep.get("mode") == "resume_parser_only"
    assert dom["first_name"] == "", "env parser-only must NOT type into fields"
    assert dom["resume_files"] == 1


def test_env_switch_off_by_default():
    out = asyncio.run(_run({}))
    assert out["report"].get("mode") != "resume_parser_only"
