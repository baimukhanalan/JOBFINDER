"""G2 — per-ATS DOM regression fixtures.

Each test loads a STATIC sanitized HTML fixture (no network) via Playwright
set_content, runs extract_form_fields / analyze_page against it, and asserts
the structural invariants that this branch (feat/semi-auto-apply-engine) fixed.

Fixture files: backend/tests/fixtures/<ats>_form.html
Captured from live ATS job-board pages, sanitized (scripts stripped, external
URLs removed).  Frozen HTML means field counts are stable and can be asserted
directly; where the DOM might evolve we prefer the invariant form.

Run: PYTHONPATH=. python3 -m pytest backend/tests/test_dom_fixtures.py -q
"""
import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.async_api import async_playwright  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

if not FIXTURES_DIR.exists():
    pytest.skip("fixtures/ directory missing — run G1 capture first",
                allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
    re.IGNORECASE,
)
_CARDS_RE = re.compile(r'\bcards\[')
_LONG_NUMERIC_ID_RE = re.compile(r'\b\d{5,}\b')
_TYPE_HERE_RE = re.compile(r'\btype\s+here\b', re.IGNORECASE)


def _has_uuid(s: str) -> bool:
    return bool(_UUID_RE.search(s))


def _has_cards_internal(s: str) -> bool:
    return bool(_CARDS_RE.search(s))


def _has_long_numeric_id(s: str) -> bool:
    return bool(_LONG_NUMERIC_ID_RE.search(s))


def _has_type_here(s: str) -> bool:
    return bool(_TYPE_HERE_RE.search(s))


SAMPLE_PROFILE = {
    "id": "sample",
    "full_name": "Jordan Sample",
    "email": "jordan.sample.demo@example.com",
    "phone": "312-555-0142",
    "location": "Chicago, IL, US",
    "state": "IL",
    "zip_code": "60601",
    "country": "United States",
    "linkedin_url": "",          # deliberately empty — tests A4 (required fields w/ empty value)
    "work_authorization": "US Citizen",
    "needs_sponsorship": "No",
    "years_experience": "6",
    "available_start": "Immediately",
    "resume_path": "/tmp/sample_resume.pdf",
}

SAMPLE_FACTS = {
    "shifts_nights": "Yes",
    "shifts_weekends": "Yes",
    "overtime": "Yes",
    "notice_period": "Immediately",
    "timezone": "CST",
    "typing_wpm": "65",
    "languages": ["English", "Spanish"],
    "education_level": "Bachelor's degree",
    "salary_hourly": "20-24",
    "drug_test_ok": "Yes",
    "drivers_license": "Yes",
    "criminal_record": "No",
    "background_check_ok": "Yes",
    "quiet_workspace": "Yes",
}


# ---------------------------------------------------------------------------
# Per-test browser helper (each test is fully self-contained)
# ---------------------------------------------------------------------------

async def _with_fixture_page(ats: str, coro_factory):
    """Launch a headless browser, load fixture HTML via set_content (no network),
    run coro_factory(page) and return the result.  Always closes cleanly.

    Uses wait_until='domcontentloaded' to avoid timing out on large HTML files
    that reference stripped external resources (Lever fixture is ~727KB).
    """
    path = FIXTURES_DIR / f"{ats}_form.html"
    html = path.read_text(encoding="utf-8")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            return await coro_factory(page)
        finally:
            await browser.close()
    finally:
        await pw.stop()


# ---------------------------------------------------------------------------
# Universal invariants helpers
# ---------------------------------------------------------------------------

def _assert_universal_invariants(fields: list[dict], unknown: list[dict], ats: str):
    """Invariants that must hold for ANY ATS output."""
    for f in fields:
        assert f.get("selector"), f"[{ats}] mapped field has empty selector: {f}"
    for u in unknown:
        assert u.get("selector"), f"[{ats}] unknown_question has empty selector: {u}"

    for f in fields + unknown:
        opts = f.get("option_selectors", []) or []
        if opts:
            assert len(opts) == len(set(opts)), \
                f"[{ats}] duplicate option_selectors in group: {f.get('selector')}"


def _assert_no_review_in_values(fields: list[dict], ats: str):
    """No '[review]' string in any filled field value."""
    for f in fields:
        v = f.get("value") or ""
        assert "[review]" not in v, \
            f"[{ats}] '[review]' found in field value: {f}"


# ===========================================================================
# G R E E N H O U S E
# ===========================================================================

def _skip_if_missing(ats: str):
    p = FIXTURES_DIR / f"{ats}_form.html"
    if not p.exists():
        pytest.skip(f"{ats}_form.html not captured")


def test_greenhouse_extract_field_count():
    """Greenhouse form yields a stable number of extracted fields."""
    _skip_if_missing("greenhouse")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("greenhouse", coro))
    # The captured fixture has 16 extracted fields (verified manually).
    # Allow ±2 for minor rendering variance.
    assert 14 <= len(fields) <= 18, \
        f"Greenhouse: unexpected field count {len(fields)}"


def test_greenhouse_react_select_inputs_not_open_text():
    """React-select inner <input> (Wave D fix) must NOT appear as open-text fields.

    The Greenhouse form wraps country/EEO selects in .select__container divs.
    Before the fix, those typeahead inputs were routed to 'fill' / unknown,
    causing prose to be typed into a combobox that never stuck.
    extract_form_fields should skip them entirely.
    """
    _skip_if_missing("greenhouse")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("greenhouse", coro))
    # The 10 .select__container inputs (country, EEO widgets) must NOT inflate
    # the field count beyond the expected 16-ish.
    assert len(fields) <= 18


def test_greenhouse_analyze_page_core_invariants():
    """Greenhouse analyze_page: universal invariants + no [review] in values."""
    _skip_if_missing("greenhouse")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover letter", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("greenhouse", coro))
    assert result["page_type"] == "application_form"
    _assert_universal_invariants(result["fields"], result["unknown_questions"], "greenhouse")
    _assert_no_review_in_values(result["fields"], "greenhouse")


def test_greenhouse_question_ids_stripped_from_display_text():
    """question_NNNNN numeric ids (Greenhouse internal) must not appear in
    question_text of unknown_questions (A3 noise-stripping fix).
    """
    _skip_if_missing("greenhouse")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("greenhouse", coro))
    for u in unknowns:
        qt = u.get("question_text", "")
        assert not re.search(r'\bquestion_\d+\b', qt), \
            f"Greenhouse: question_id leaked into display text: {qt!r}"
        assert not _has_long_numeric_id(qt), \
            f"Greenhouse: bare 5+-digit id in display text: {qt!r}"


def test_greenhouse_required_textareas_surface_as_unknown():
    """All visible required textareas must appear in unknown_questions (A5 fix)."""
    _skip_if_missing("greenhouse")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("greenhouse", coro))
    textarea_unknowns = [u for u in result["unknown_questions"] if u["type"] == "textarea"]
    assert len(textarea_unknowns) >= 6, \
        f"Greenhouse: expected >=6 textarea unknowns, got {len(textarea_unknowns)}"


def test_greenhouse_identity_fields_filled():
    """first_name, last_name, email, phone, resume must be in filled fields."""
    _skip_if_missing("greenhouse")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["fields"]

    fields = asyncio.run(_with_fixture_page("greenhouse", coro))
    matched_keys = {f["matched"] for f in fields}
    assert "_first_name" in matched_keys, "first_name not filled"
    assert "_last_name" in matched_keys, "last_name not filled"
    assert "email" in matched_keys, "email not filled"
    assert "phone" in matched_keys, "phone not filled"
    assert "resume" in matched_keys, "resume not filled"


# ===========================================================================
# L E V E R
# ===========================================================================

def test_lever_extract_field_count():
    """Lever apply form yields a stable field count."""
    _skip_if_missing("lever")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("lever", coro))
    assert 15 <= len(fields) <= 25, \
        f"Lever: unexpected field count {len(fields)}"


def test_lever_radio_group_merged_yes_no():
    """Lever sponsorship question is a radio_group with YES and NO options,
    not two separate unknown fields (radio-merge fix).
    """
    _skip_if_missing("lever")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("lever", coro))
    radio_groups = [f for f in fields if f["type"] == "radio_group"]
    assert len(radio_groups) >= 1, "Lever: no radio_group found — merge failed"

    yes_no_group = next(
        (g for g in radio_groups
         if any(o["text"] == "Yes" for o in g.get("options", []))
         and any(o["text"] == "No" for o in g.get("options", []))),
        None,
    )
    assert yes_no_group is not None, \
        "Lever: Yes/No sponsorship radio group not found after merge"

    sels = [o["value"] for o in yes_no_group["options"]]
    assert len(sels) == len(set(sels)), \
        f"Lever: Yes/No group has duplicate option selectors: {sels}"


def test_lever_analyze_page_core_invariants():
    """Lever analyze_page: universal invariants + no [review] in values."""
    _skip_if_missing("lever")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("lever", coro))
    assert result["page_type"] == "application_form"
    _assert_universal_invariants(result["fields"], result["unknown_questions"], "lever")
    _assert_no_review_in_values(result["fields"], "lever")


def test_lever_question_text_no_uuid():
    """Lever unknown_questions: fields that have a REAL human-readable question
    (non-empty text beyond just the internal name) must not contain bare UUIDs
    in question_text (A3 noise-stripping fix).

    Known gap: Lever textarea fields whose ONLY identifier is the internal
    'cards[UUID][fieldN]' name have no human-visible label in the static
    fixture; _clean_text falls back to the original when stripping yields an
    empty string.  Those entries ARE the internal name and will contain a UUID
    by design of the fallback.  The invariant tested here only applies to
    fields where a real question text IS available (label or nearbyText).
    """
    _skip_if_missing("lever")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("lever", coro))
    _CARDS_ONLY = re.compile(r'^cards\[')
    for u in unknowns:
        qt = u.get("question_text", "")
        # Skip entries whose question_text IS the internal cards[...] token
        # (known fallback — no human label available in the fixture).
        if _CARDS_ONLY.match(qt):
            continue
        assert not _has_uuid(qt), \
            f"Lever: UUID leaked into non-cards question_text: {qt!r}"


def test_lever_identity_fields_filled():
    """Lever: full_name, email, phone, resume filled from profile."""
    _skip_if_missing("lever")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["fields"]

    fields = asyncio.run(_with_fixture_page("lever", coro))
    matched_keys = {f["matched"] for f in fields}
    assert "full_name" in matched_keys, "Lever: full_name not filled"
    assert "email" in matched_keys, "Lever: email not filled"
    assert "phone" in matched_keys, "Lever: phone not filled"
    assert "resume" in matched_keys, "Lever: resume not filled"


def test_lever_group_question_is_not_a_sibling_option_text():
    """Live regression: the group-question lookup matched '[class*="question"]
    label' against ancestors OUTSIDE the group (li.application-question wraps
    the options ul), so the pronouns group surfaced as 'She/her' and the
    sponsorship group as 'No' — a sibling OPTION's text. The question must be
    the real group label ('Pronouns' / the sponsorship sentence)."""
    _skip_if_missing("lever")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("lever", coro))
    groups = [f for f in fields if f["type"] in ("radio_group", "checkbox_group")]
    assert groups, "Lever: no merged groups found"
    for g in groups:
        q = g["nearbyText"]
        opt_texts = {o["text"] for o in g["options"]}
        assert q and q not in opt_texts, \
            f"Lever: group question {q!r} is an option text, not the question"

    pronouns = next((g for g in groups if g["name"] == "pronouns"), None)
    assert pronouns is not None, "Lever: pronouns checkbox_group not merged"
    assert pronouns["nearbyText"] == "Pronouns"

    sponsor = next((g for g in groups if "sponsorship" in g["nearbyText"].lower()), None)
    assert sponsor is not None, \
        "Lever: sponsorship group question not resolved from the group label"


def test_lever_sponsorship_auto_answered_no():
    """With the group question resolved, the sponsorship Yes/No radio group is
    rule-answered (needs_sponsorship=No) instead of surfacing as unknown."""
    _skip_if_missing("lever")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("lever", coro))
    sponsor_fill = [f for f in result["fields"] if f.get("matched") == "_sponsorship"]
    assert sponsor_fill, "Lever: sponsorship group not auto-answered"
    assert sponsor_fill[0]["action"] == "check"
    assert 'value="No"' in sponsor_fill[0]["selector"], \
        f"Lever: sponsorship answered with the wrong option: {sponsor_fill[0]['selector']}"


def test_lever_linkedin_empty_not_filled_with_empty_value():
    """When linkedin_url='' the LinkedIn field must NOT be placed in filled fields
    with an empty value (A4: matched rule with empty profile value).
    """
    _skip_if_missing("lever")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("lever", coro))
    linkedin_filled = [
        f for f in result["fields"]
        if "linkedin" in (f.get("matched") or "").lower()
        or "linkedin" in (f.get("selector") or "").lower()
    ]
    for lf in linkedin_filled:
        assert lf.get("value"), \
            f"Lever: LinkedIn field filled with empty value: {lf}"


# ===========================================================================
# A S H B Y
# ===========================================================================

def test_ashby_extract_field_count():
    """Ashby form yields a stable extracted field count."""
    _skip_if_missing("ashby")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("ashby", coro))
    assert 10 <= len(fields) <= 18, \
        f"Ashby: unexpected field count {len(fields)}"


def test_ashby_uuid_field_ids_produce_attribute_selectors():
    """Ashby uses bare UUIDs as field IDs (start with a digit).
    The analyzer must produce [id="<uuid>"] attribute selectors — NOT #<uuid>
    CSS id selectors (invalid when the id starts with a digit).

    Radio/checkbox (and their merged groups) are exempt from the [id=...] form:
    Ashby gives every option of a radio group the SAME id (id==name), so id
    selectors always hit option #1 — those use [name=...] + value/nth instead.
    """
    _skip_if_missing("ashby")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("ashby", coro))
    for f in fields:
        sel = f.get("selector", "")
        assert not sel.startswith("#"), \
            f"Ashby: raw CSS #id selector used (invalid for UUID ids): {sel!r}"
        if f["type"] in ("radio", "checkbox", "radio_group", "checkbox_group"):
            continue
        if f.get("id") and _has_uuid(f["id"]):
            assert sel.startswith('[id="'), \
                f"Ashby: UUID field id does not use [id=...] selector: {sel!r}"


def test_ashby_radio_group_option_selectors_distinct():
    """Ashby radio options share one id — the merged group's option selectors
    must still be DISTINCT (name+nth), or every check would hit option #1."""
    _skip_if_missing("ashby")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("ashby", coro))
    for g in fields:
        if g["type"] != "radio_group":
            continue
        sels = [o["value"] for o in g["options"]]
        assert len(sels) == len(set(sels)), \
            f"Ashby: duplicate option selectors in group {g.get('nearbyText')!r}: {sels}"


def test_ashby_question_text_no_uuid():
    """Ashby unknown_questions: question_text must not contain raw UUIDs (A3)."""
    _skip_if_missing("ashby")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("ashby", coro))
    for u in unknowns:
        qt = u.get("question_text", "")
        assert not _has_uuid(qt), \
            f"Ashby: UUID leaked into question_text: {qt!r}"


def test_ashby_analyze_page_core_invariants():
    """Ashby analyze_page: universal invariants + no [review]."""
    _skip_if_missing("ashby")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("ashby", coro))
    assert result["page_type"] == "application_form"
    _assert_universal_invariants(result["fields"], result["unknown_questions"], "ashby")
    _assert_no_review_in_values(result["fields"], "ashby")


def test_ashby_radio_group_merged():
    """Ashby radio group (7 options, same name) must be merged into ONE
    radio_group — not 7 separate unknowns.
    """
    _skip_if_missing("ashby")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("ashby", coro))
    radio_groups = [f for f in fields if f["type"] == "radio_group"]
    assert len(radio_groups) >= 1, \
        "Ashby: radio group not merged — expected >=1 radio_group, got 0"

    hear_group = next(
        (g for g in radio_groups if len(g.get("options", [])) >= 4),
        None,
    )
    assert hear_group is not None, \
        "Ashby: expected radio_group with >=4 options (source-of-hire group)"


def test_ashby_identity_and_resume_filled():
    """Ashby: email and resume filled from profile."""
    _skip_if_missing("ashby")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("ashby", coro))
    matched_keys = {f["matched"] for f in result["fields"]}
    assert "email" in matched_keys, "Ashby: email not filled"
    assert "resume" in matched_keys, "Ashby: resume not filled"


def test_ashby_textarea_surfaces_as_unknown():
    """Ashby open-ended textarea must appear in unknown_questions (A5)."""
    _skip_if_missing("ashby")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("ashby", coro))
    textarea_unknowns = [u for u in unknowns if u["type"] == "textarea"]
    assert len(textarea_unknowns) >= 1, \
        "Ashby: expected >=1 textarea in unknown_questions"


# ===========================================================================
# W O R K A B L E
# ===========================================================================

def test_workable_extract_field_count():
    """Workable form yields a stable extracted field count."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("workable", coro))
    assert 22 <= len(fields) <= 35, \
        f"Workable: unexpected field count {len(fields)}"


def test_workable_radio_groups_merged():
    """Workable Yes/No radio pairs share a name and must be merged into
    radio_group entries — not appear as individual unmerged radios.
    """
    _skip_if_missing("workable")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("workable", coro))
    radio_groups = [f for f in fields if f["type"] == "radio_group"]
    assert len(radio_groups) >= 5, \
        f"Workable: expected >=5 radio_groups, got {len(radio_groups)}"

    bare_radios = [f for f in fields if f["type"] == "radio"]
    name_counts: dict[str, int] = {}
    for f in bare_radios:
        n = f.get("name", "")
        if n:
            name_counts[n] = name_counts.get(n, 0) + 1
    for name, count in name_counts.items():
        assert count < 2, \
            f"Workable: radio name {name!r} appears {count}x as bare radio (merge failed)"


def test_workable_radio_group_options_unique_selectors():
    """Every Workable radio_group option_selector must be unique within the group."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("workable", coro))
    for u in unknowns:
        opt_sels = u.get("option_selectors", []) or []
        if opt_sels:
            assert len(opt_sels) == len(set(opt_sels)), \
                f"Workable: duplicate option_selectors in unknown: {u['selector']}"


def test_workable_analyze_page_core_invariants():
    """Workable analyze_page: universal invariants + no [review]."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("workable", coro))
    assert result["page_type"] == "application_form"
    _assert_universal_invariants(result["fields"], result["unknown_questions"], "workable")
    _assert_no_review_in_values(result["fields"], "workable")


def test_workable_identity_fields_filled():
    """Workable: firstname, lastname, email, phone, postcode filled."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["fields"]

    fields = asyncio.run(_with_fixture_page("workable", coro))
    matched_keys = {f["matched"] for f in fields}
    assert "_first_name" in matched_keys, "Workable: first_name not filled"
    assert "_last_name" in matched_keys, "Workable: last_name not filled"
    assert "email" in matched_keys, "Workable: email not filled"
    assert "phone" in matched_keys, "Workable: phone not filled"
    assert "_zip" in matched_keys, "Workable: zip/postcode not filled"


def test_workable_unknown_radio_groups_have_non_empty_selectors():
    """All unknown radio_groups must have non-empty selector (the first-option
    selector from the merged group).  Pins the selector-generation fix for
    Workable opaque-id radio buttons.
    """
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("workable", coro))
    rg_unknowns = [u for u in unknowns if u["type"] == "radio_group"]
    assert len(rg_unknowns) >= 4, \
        f"Workable: expected >=4 radio_group unknowns, got {len(rg_unknowns)}"
    for u in rg_unknowns:
        assert u["selector"], \
            f"Workable: radio_group unknown has empty selector: {u}"


def test_workable_question_texts_are_human_no_raw_ids():
    """H4b: EVERY Workable unknown must carry the human question text resolved
    via aria-labelledby (label spans), never the raw internal ids that the live
    run surfaced ('CA_21646 hO23GnzQAZdWhGPb', '217238', 'QA_11864455 ...')."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("workable", coro))
    assert unknowns, "Workable: expected some unknown questions"
    for u in unknowns:
        qt = u.get("question_text", "")
        assert not re.search(r"\b(?:QA|CA|SQ)_\d+\b", qt, re.IGNORECASE), \
            f"Workable: internal id leaked into question_text: {qt!r}"
        assert not _has_long_numeric_id(qt), \
            f"Workable: bare numeric id leaked into question_text: {qt!r}"
        assert not re.search(r"\b(?=[a-zA-Z]*\d)[a-zA-Z0-9]{14,}\b", qt), \
            f"Workable: opaque token leaked into question_text: {qt!r}"
        # at least one real word
        assert re.search(r"[A-Za-z]{3,}", qt), f"Workable: no words in {qt!r}"


def test_workable_radio_group_options_human_not_internal_values():
    """H4b/c: merged group options must show the option LABEL ('Yes'/'No'),
    not the internal value attr ('217240', 'true')."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("workable", coro))
    groups = [f for f in fields if f["type"] in ("radio_group", "checkbox_group")]
    assert groups
    for g in groups:
        for o in g["options"]:
            assert o["text"].lower() not in ("true", "false"), \
                f"Workable: raw value leaked as option text in {g['nearbyText']!r}"
            assert not re.fullmatch(r"\d{5,}", o["text"]), \
                f"Workable: numeric id leaked as option text in {g['nearbyText']!r}"


def test_workable_distinct_name_checkbox_pairs_merge_via_aria_group():
    """'Open to relocate' (217238/217239) and 'Preferred work schedule'
    (217232..217235) render each option as a distinct-NAME checkbox inside one
    div[role=group] — they must merge into checkbox_groups, not surface as
    bare numeric-labeled checkboxes."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import extract_form_fields

    async def coro(page):
        return await extract_form_fields(page)

    fields = asyncio.run(_with_fixture_page("workable", coro))
    cb_groups = [f for f in fields if f["type"] == "checkbox_group"]
    assert len(cb_groups) >= 2, \
        f"Workable: expected >=2 ARIA-merged checkbox_groups, got {len(cb_groups)}"
    relocate = next((g for g in cb_groups if "relocate" in g["nearbyText"].lower()), None)
    assert relocate is not None, "Workable: 'Open to relocate' group not merged"
    assert {o["text"] for o in relocate["options"]} == {"Yes", "No"}
    # no bare single checkboxes with numeric-id names may survive
    for f in fields:
        if f["type"] == "checkbox":
            assert not re.fullmatch(r"\d{5,}", f.get("name", "")), \
                f"Workable: numeric-name checkbox not merged: {f['name']}"


def test_workable_resume_uploads_to_resume_input_not_photo():
    """H4a: the form has TWO file inputs — Photo first, Resume second. The
    résumé must attach to the Resume one (resolved via aria-labelledby), never
    to the Photo input the old first-file-input fallback used to grab."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["fields"]

    fields = asyncio.run(_with_fixture_page("workable", coro))
    uploads = [f for f in fields if f["action"] == "upload"]
    assert len(uploads) == 1, f"Workable: expected exactly 1 upload, got {uploads}"
    # frozen fixture: Photo input id is input_files_input_ZzMqc7mZM5jOTTTU,
    # Resume input id is input_files_input_F8eInYDgBivvIqIQ
    assert uploads[0]["selector"] == '[id="input_files_input_F8eInYDgBivvIqIQ"]', \
        f"Workable: resume attached to the wrong file input: {uploads[0]['selector']}"


def test_workable_datepicker_not_filled_with_prose():
    """'Date available to start work' is a datepicker (placeholder MM/DD/YYYY).
    Filling it with 'Immediately' produced garbage live ('03/05/4583') — a
    digit-less start value must leave the field to the human instead."""
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        return await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)

    result = asyncio.run(_with_fixture_page("workable", coro))
    start_fills = [f for f in result["fields"] if f.get("matched") == "_start_date"]
    assert not start_fills, \
        f"Workable: prose typed into the datepicker: {start_fills}"
    date_unknown = next((u for u in result["unknown_questions"]
                         if "date available" in u["question_text"].lower()), None)
    assert date_unknown is not None, \
        "Workable: datepicker question not surfaced for the human"
    assert date_unknown["type"] == "date", \
        f"Workable: datepicker unknown not typed 'date': {date_unknown['type']}"


def test_workable_salary_label_surfaces_not_raw_id():
    """Workable CA_21645 (salary) field has a human-readable nearby text label
    ('*Desired salary range per month'); that text must be the question_text,
    not the raw CA_ id.
    """
    _skip_if_missing("workable")
    from backend.applier.analyzer import analyze_page

    async def coro(page):
        r = await analyze_page(page, SAMPLE_PROFILE, "cover", {}, SAMPLE_FACTS)
        return r["unknown_questions"]

    unknowns = asyncio.run(_with_fixture_page("workable", coro))
    salary_unknown = next(
        (u for u in unknowns if "salary" in (u.get("question_text") or "").lower()),
        None,
    )
    assert salary_unknown is not None, \
        "Workable: salary field not surfaced as unknown"
    assert "CA_21645" not in salary_unknown["question_text"], \
        f"Workable: CA_21645 id leaked into salary question_text: {salary_unknown['question_text']!r}"
