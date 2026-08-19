# Answer Coverage & Per-Person Résumés Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The apply engine answers ~every form question itself (deterministic bank → constrained LLM option choice → LLM text draft → `[review]` queue), with per-person fact sheets, per-person answer cache, and per-person etalon résumés — so the human only skims flagged answers and clicks Submit.

**Architecture:** Cascade. A per-person JSON fact sheet feeds (1) extended regex rules in `analyzer.py`, (2) a new constrained-choice module `choices.py` where the local LLM only picks an option index (validated), and (3) the open-text drafter `answers.py`. The answer cache becomes keyed `(profile, niche, question)`. Etalons move to one file per profile. The report counts answer sources and lists `[review]` items; unconfirmed review items block the opt-in auto-submit path.

**Tech Stack:** Python 3.12, Playwright (async), SQLite (stdlib `sqlite3`), pytest, local OpenAI-compatible LLM via `_llm_complete` in `backend/services/tailor/tailor.py`.

**Spec:** `docs/superpowers/specs/2026-06-11-answer-coverage-design.md`

**Test command (from repo root `/home/projects/jobfinder`):**
`PYTHONPATH=. python3 -m pytest backend/tests/ -q`

---

## File map

Create:
- `backend/profiles/facts.py` — fact-sheet loader
- `backend/data/facts/sample.json` — committed fake sample (mirrors `profiles.example.json` pattern)
- `backend/data/facts/michael.json` — real profile defaults (gitignored, user verifies values)
- `backend/services/tailor/choices.py` — constrained option choice
- `backend/tests/test_facts.py`, `test_analyzer_rules.py`, `test_radio_groups.py`, `test_choices.py`, `test_answers_v2.py`, `test_answer_cache.py`, `test_variants_per_profile.py`

Modify:
- `.gitignore` — ignore real fact sheets
- `backend/applier/analyzer.py` — rules v2, facts param, radio/checkbox group merging, criminal-record fix
- `backend/services/tailor/answers.py` — prompt v2, chunked drafting, cap 20
- `backend/answer_cache.py` — composite key `(profile, niche, key)`
- `backend/services/tailor/variants.py` — per-profile etalons
- `backend/applier/strategies/base.py` — wire cascade, sources, review items
- `backend/applier/runner.py` — pass facts/profile/niche, extend submit gate
- `backend/copilot.py`, `backend/dashboard_app.py`, `backend/apply_cli.py` — call sites
- `backend/data/etalons.json` → `backend/data/etalons/michael.json` (git mv)

---

### Task 1: Per-person fact sheets

**Files:**
- Create: `backend/profiles/facts.py`
- Create: `backend/data/facts/sample.json`
- Create: `backend/data/facts/michael.json` (gitignored)
- Create: `backend/tests/test_facts.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_facts.py`:

```python
"""Fact-sheet loader: per-person JSON answering facts for rules and prompts."""
import json

from backend.profiles import facts as facts_mod


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    assert facts_mod.load_facts("nobody") == {}


def test_loads_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    (tmp_path / "kate.json").write_text(json.dumps({"typing_wpm": "70"}), encoding="utf-8")
    assert facts_mod.load_facts("kate") == {"typing_wpm": "70"}


def test_garbage_or_non_dict_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1,2]", encoding="utf-8")
    assert facts_mod.load_facts("bad") == {}
    assert facts_mod.load_facts("list") == {}


def test_unsafe_profile_id_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    assert facts_mod.load_facts("../profiles") == {}


def test_sample_file_is_valid():
    # the committed sample must always load (it documents the schema)
    data = facts_mod.load_facts("sample")
    assert isinstance(data, dict) and data.get("background_check_ok") == "Yes"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_facts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.profiles.facts'`

- [ ] **Step 3: Write the implementation**

`backend/profiles/facts.py`:

```python
"""Per-person fact sheet: one JSON file per profile with screener-answering facts
(shifts, salary range, languages, tools, consents, ...). Single source of truth for
the deterministic rules in backend.applier.analyzer AND the LLM prompts in
backend.services.tailor.{choices,answers}.

Real people's files are gitignored; backend/data/facts/sample.json is a committed
fake that documents the schema. A missing/invalid file -> {} (the engine degrades
to pre-fact-sheet behavior, nothing crashes).
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FACTS_DIR = PROJECT_ROOT / "backend" / "data" / "facts"

_SAFE_ID = re.compile(r"^[a-z0-9_-]+$")


def load_facts(profile_id: str) -> dict:
    """Facts for one person, {} when absent/invalid. Keys are flat, values are
    strings or lists of strings (see backend/data/facts/sample.json)."""
    if not profile_id or not _SAFE_ID.match(profile_id):
        return {}
    path = FACTS_DIR / f"{profile_id}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("facts %s unreadable (%s) — ignoring", path.name, e)
        return {}
    return data if isinstance(data, dict) else {}
```

`backend/data/facts/sample.json` (fake values — documents the schema):

```json
{
  "shifts_nights": "Yes",
  "shifts_weekends": "Yes",
  "overtime": "Yes",
  "salary_hourly": "20-24",
  "salary_annual": "45000-55000",
  "notice_period": "Immediately",
  "languages": ["English", "Spanish"],
  "typing_wpm": "65",
  "tools": ["Zendesk", "Intercom", "Salesforce", "Slack", "Shopify"],
  "education_level": "Bachelor's degree",
  "state": "TX",
  "timezone": "CST",
  "equipment_ok": "Yes",
  "quiet_workspace": "Yes",
  "industries": ["saas", "ecommerce", "fintech"],
  "managed_people": "Yes",
  "drivers_license": "Yes",
  "drug_test_ok": "Yes",
  "background_check_ok": "Yes",
  "criminal_record": "No",
  "prior_employee": "No",
  "referral": "Online job search"
}
```

`backend/data/facts/michael.json`: copy `sample.json`, then set `state`/`timezone`/`salary_*`/`languages` from `backend/data/profiles.json` (michael entry) where present. Leave the rest at the defaults above — they match the engine's current hardcoded behavior. The user must review this file by hand afterwards (it answers consent questions for him).

Append to `.gitignore` under the PII block:

```
backend/data/facts/*
!backend/data/facts/sample.json
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_facts.py -q`
Expected: 5 passed

- [ ] **Step 5: Verify michael.json is NOT staged**

Run: `git add -A && git status --short | grep facts`
Expected: `backend/data/facts/sample.json` staged; `michael.json` absent from output.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(applier): per-person fact sheets for screener answers"
```

---

### Task 2: Analyzer rules v2 — fact-driven patterns + criminal-record fix

**Files:**
- Modify: `backend/applier/analyzer.py` (FIELD_PATTERNS ~line 16-73, `_resolve_value` ~line 130, `analyze_page` ~line 347)
- Modify: `backend/applier/strategies/base.py:40` (pass facts through — minimal stub here, full wiring in Task 8)
- Create: `backend/tests/test_analyzer_rules.py`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_analyzer_rules.py`:

```python
"""Rules v2: pure tests on _match_field/_resolve_value — no browser, no LLM."""
from backend.applier.analyzer import _match_field, _resolve_value

FACTS = {
    "shifts_nights": "Yes", "shifts_weekends": "Yes", "overtime": "Yes",
    "notice_period": "Two weeks", "timezone": "CST", "typing_wpm": "65",
    "languages": ["English", "Russian"], "education_level": "Bachelor's degree",
    "salary_hourly": "20-24", "drug_test_ok": "Yes", "drivers_license": "Yes",
    "criminal_record": "No", "background_check_ok": "Yes", "quiet_workspace": "Yes",
}


def _resolve(text: str, facts: dict = FACTS):
    m = _match_field(text)
    assert m is not None, f"no rule matched: {text!r}"
    return m[0], _resolve_value(m[0], {}, "", {}, facts)


def test_convicted_resolves_no_not_yes():
    key, val = _resolve("Have you ever been convicted of a felony?")
    assert key == "_fact:criminal_record" and val == "No"


def test_criminal_history_resolves_no():
    key, val = _resolve("Do you have a criminal history?")
    assert key == "_fact:criminal_record" and val == "No"


def test_background_check_consent_yes():
    key, val = _resolve("Are you willing to undergo a background check?")
    assert key == "_fact:background_check_ok" and val == "Yes"


def test_weekend_availability():
    key, val = _resolve("Are you available to work weekends?")
    assert key == "_fact:shifts_weekends" and val == "Yes"


def test_night_shift():
    key, val = _resolve("Can you work overnight shifts?")
    assert key == "_fact:shifts_nights" and val == "Yes"


def test_education_level():
    key, val = _resolve("What is your highest level of education?")
    assert key == "_fact:education_level" and val == "Bachelor's degree"


def test_languages_list_joined():
    key, val = _resolve("What languages do you speak fluently?")
    assert key == "_fact:languages" and val == "English, Russian"


def test_typing_speed():
    key, val = _resolve("What is your typing speed (WPM)?")
    assert val == "65"


def test_hourly_rate():
    key, val = _resolve("What is your expected hourly rate?")
    assert key == "_fact:salary_hourly" and val == "20-24"


def test_missing_fact_resolves_none():
    key, val = _resolve("Are you willing to take a drug test?", facts={})
    assert key == "_fact:drug_test_ok" and val is None


def test_foreign_work_auth_still_blocked():
    assert _match_field("Are you legally authorized to work in the UK?") is None


def test_open_ended_still_unmatched():
    assert _match_field("Describe a time you handled an angry customer") is None


def test_identity_rules_untouched():
    key, _ = _resolve("First Name")
    assert key == "_first_name"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_analyzer_rules.py -q`
Expected: FAIL — `_resolve_value` takes 4 args (no `facts`), criminal/night/etc. rules missing, and `Have you ever been convicted...` currently resolves via the old `criminal.?(history|record|check) -> _yes` rule.

- [ ] **Step 3: Implement rules v2**

In `backend/applier/analyzer.py`:

3a. In `FIELD_PATTERNS`, REPLACE the line

```python
    (r"(?i)(background.?check|criminal.?(history|record|check)|consent.*screen)", "_yes", "select_or_fill"),
```

with

```python
    (r"(?i)(convicted|conviction|felony|misdemeanor|criminal.?(history|record))", "_fact:criminal_record", "select_or_fill"),
    (r"(?i)(background.?check|consent.*screen)", "_fact:background_check_ok", "select_or_fill"),
```

3b. Insert a new fact-driven block into `FIELD_PATTERNS` right AFTER the work-auth/sponsorship pair (i.e. before the `# Location` block, so "Weekend availability" never falls through to the `availability -> _start_date` rule):

```python
    # --- fact-sheet driven (rules v2): values come from backend/data/facts/<profile>.json.
    # A missing fact resolves to None -> the question stays "unknown" and is handled by
    # the constrained-choice LLM (flagged [review]) or the human. Never guessed here.
    (r"(?i)(night.?shift|overnight|graveyard|work (at )?nights)", "_fact:shifts_nights", "select_or_fill"),
    (r"(?i)(weekend|saturday|sunday)", "_fact:shifts_weekends", "select_or_fill"),
    (r"(?i)(overtime|extra hours)", "_fact:overtime", "select_or_fill"),
    (r"(?i)(notice.?period|how (much|long) notice)", "_fact:notice_period", "fill"),
    (r"(?i)(time.?zone)", "_fact:timezone", "fill"),
    (r"(?i)(typing.?(speed|test)|\bwpm\b|words.?per.?minute)", "_fact:typing_wpm", "fill"),
    (r"(?i)(languages?.{0,30}(speak|fluent|proficien)|bilingual|multilingual)", "_fact:languages", "fill"),
    (r"(?i)(highest.{0,25}education|education.{0,12}level|highest.{0,12}degree)", "_fact:education_level", "select_or_fill"),
    (r"(?i)(hourly.?(rate|pay|wage)|pay.{0,10}per.?hour|rate.?per.?hour)", "_fact:salary_hourly", "fill"),
    (r"(?i)(drug.?(test|screen))", "_fact:drug_test_ok", "select_or_fill"),
    (r"(?i)(driver'?s?.?licen[sc]e)", "_fact:drivers_license", "select_or_fill"),
    (r"(?i)((quiet|dedicated|distraction.?free).{0,25}(work.?space|work.?area|home.?office))", "_fact:quiet_workspace", "select_or_fill"),
```

3c. Change `_resolve_value` signature and add fact resolution as the FIRST branch:

```python
def _resolve_value(key: str, profile: dict, cover_letter: str, known_answers: dict,
                   facts: dict | None = None) -> str | None:
    """Resolve a matched key to an actual value."""
    if key.startswith("_fact:"):
        v = (facts or {}).get(key[len("_fact:"):])
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        return str(v) if v not in (None, "") else None
    if key == "full_name":
        ...  # rest unchanged
```

3d. Change `analyze_page` to accept and pass facts:

```python
async def analyze_page(
    page: Page,
    profile: dict,
    cover_letter: str = "",
    known_answers: dict | None = None,
    facts: dict | None = None,
) -> dict:
```

and update its single `_resolve_value` call site:

```python
                value = _resolve_value(key, profile, cover_letter, known_answers, facts)
```

3e. In `backend/applier/strategies/base.py`, add the parameter to `prefill` (full wiring comes in Task 8; this keeps the call chain consistent now):

```python
    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None) -> dict:
```

and

```python
        analysis = await analyze_page(page, profile_form, cover_letter,
                                      known_answers or {}, facts or {})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_analyzer_rules.py backend/tests/ -q`
Expected: all pass (existing `test_pipeline.py` must stay green).

- [ ] **Step 5: Commit**

```bash
git add backend/applier/analyzer.py backend/applier/strategies/base.py backend/tests/test_analyzer_rules.py
git commit -m "feat(applier): fact-driven screener rules; fix criminal-history mis-answer"
```

---

### Task 3: Radio/checkbox group merging in the analyzer

Radio inputs arrive one element each with `options: []`, so closed radio questions are
currently unanswerable. Merge same-`name` radio/checkbox inputs into one logical
question with options, each option carrying the selector of its input.

**Files:**
- Modify: `backend/applier/analyzer.py` (`extract_form_fields` ~line 182-273, `analyze_page` unknown-handling ~line 374-499, `_match_select_option` ~line 513)
- Create: `backend/tests/test_radio_groups.py`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_radio_groups.py`:

```python
"""_merge_radio_groups: collapse same-name radio/checkbox inputs into one question."""
from backend.applier.analyzer import _merge_radio_groups


def _radio(name, sel, label="", nearby="", value=""):
    return {"selector": sel, "tag": "input", "type": "radio", "label": label,
            "ariaLabel": "", "placeholder": "", "name": name, "id": "",
            "title": "", "nearbyText": nearby, "required": True,
            "options": [], "value": value}


def test_merges_same_name_radios_into_group():
    fields = [
        _radio("shift", '[id="r1"]', label="Day", nearby="Which shift do you prefer?"),
        _radio("shift", '[id="r2"]', label="Night", nearby="Which shift do you prefer?"),
        {"selector": '[id="em"]', "tag": "input", "type": "email", "label": "Email",
         "ariaLabel": "", "placeholder": "", "name": "email", "id": "em", "title": "",
         "nearbyText": "", "required": True, "options": [], "value": ""},
    ]
    out = _merge_radio_groups(fields)
    groups = [f for f in out if f["type"] == "radio_group"]
    assert len(groups) == 1
    g = groups[0]
    assert g["label"] == "Which shift do you prefer?"
    assert [o["text"] for o in g["options"]] == ["Day", "Night"]
    assert [o["value"] for o in g["options"]] == ['[id="r1"]', '[id="r2"]']
    assert any(f["type"] == "email" for f in out)  # non-radios pass through


def test_option_text_falls_back_to_value_attr():
    fields = [_radio("q1", '[id="a"]', value="Yes"), _radio("q1", '[id="b"]', value="No")]
    g = _merge_radio_groups(fields)[0]
    assert [o["text"] for o in g["options"]] == ["Yes", "No"]


def test_single_unnamed_radio_passes_through():
    fields = [_radio("", '[id="solo"]', label="I agree")]
    out = _merge_radio_groups(fields)
    assert out[0]["type"] == "radio"


def test_checkbox_group_merged_too():
    fields = [
        {**_radio("days", '[id="c1"]', label="Mon"), "type": "checkbox"},
        {**_radio("days", '[id="c2"]', label="Tue"), "type": "checkbox"},
    ]
    g = _merge_radio_groups(fields)[0]
    assert g["type"] == "checkbox_group"
    assert [o["text"] for o in g["options"]] == ["Mon", "Tue"]


def test_single_checkbox_not_grouped():
    fields = [{**_radio("tos", '[id="t"]', label="I accept the terms"), "type": "checkbox"}]
    assert _merge_radio_groups(fields)[0]["type"] == "checkbox"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_radio_groups.py -q`
Expected: FAIL with `ImportError: cannot import name '_merge_radio_groups'`

- [ ] **Step 3: Implement merging**

In `backend/applier/analyzer.py`:

3a. Add after `_match_field` (module level):

```python
def _merge_radio_groups(fields: list[dict]) -> list[dict]:
    """Collapse radio (and multi-checkbox) inputs that share a `name` into ONE logical
    question with options, so the choice engine can answer them like a select.
    Option `value` holds the SELECTOR of that input — checking it answers the question.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    out: list[dict] = []
    for f in fields:
        if f["type"] in ("radio", "checkbox") and f["name"]:
            groups.setdefault((f["type"], f["name"]), []).append(f)
        else:
            out.append(f)
    for (ftype, _name), members in groups.items():
        if len(members) < 2:
            out.extend(members)
            continue
        options = []
        for m in members:
            text = (m["label"] or m["ariaLabel"] or m.get("value") or "").strip()
            options.append({"value": m["selector"], "text": text})
        question = next((m["nearbyText"] for m in members if m["nearbyText"]), "") \
            or members[0]["label"]
        out.append({**members[0],
                    "type": f"{ftype}_group",
                    "label": question,
                    "nearbyText": question,
                    "options": options})
    return out
```

3b. In `extract_form_fields`, capture the `value` attribute for radio/checkbox — change the field dict construction:

```python
                el_value = ""
                if el_type in ("radio", "checkbox"):
                    el_value = await el.get_attribute("value") or ""
```

and in the appended dict replace `"value": "",` with `"value": el_value,`.

3c. Still in `extract_form_fields`, make the nearby-text lookup run for radio/checkbox even when a `label[for]` was found (the label is the OPTION text; nearby gives the GROUP question). Change:

```python
                nearby = ""
                if not label:
```

to

```python
                nearby = ""
                if not label or el_type in ("radio", "checkbox"):
```

3d. Make `extract_form_fields` return merged fields — change its final line:

```python
    return _merge_radio_groups(fields)
```

3e. In `analyze_page`, handle the new group types. In the rule-matched branch, BEFORE the existing `if f["tag"] == "select" and f["options"]:` block, add:

```python
                if f["type"] in ("radio_group", "checkbox_group"):
                    opt = _pick_option(f["options"], value, key)
                    if opt:
                        fields.append({"selector": opt["value"], "action": "check",
                                       "value": "true", "matched": key})
                    else:
                        unknown.append(_unknown_entry(f, match_text))
                    continue
```

In the unmatched/`else` branch (currently `if f["required"] or f["tag"] == "select":`), change the condition to also surface groups:

```python
            if f["required"] or f["tag"] == "select" or f["type"] in ("radio_group", "checkbox_group"):
                unknown.append(_unknown_entry(f, match_text))
```

3f. Add the shared unknown-entry helper (module level, near `_match_select_option`) and use it for EVERY existing `unknown.append({...})` in `analyze_page` (there are four — replace all with `unknown.append(_unknown_entry(f, match_text))`):

```python
def _unknown_entry(f: dict, match_text: str) -> dict:
    return {
        "question_text": match_text[:200],
        "selector": f["selector"],
        "type": f["type"],
        "options": [o["text"] for o in f.get("options", [])],
        "option_selectors": [o.get("value", "") for o in f.get("options", [])]
        if f["type"] in ("radio_group", "checkbox_group") else [],
    }
```

3g. Refactor `_match_select_option` into an option-dict picker plus a thin text wrapper (the radio path needs the option's selector, the select path needs its text):

```python
def _pick_option(options: list[dict], desired: str, key: str) -> dict | None:
    """Find the best matching option dict for a desired value."""
    desired_lower = (desired or "").lower()

    if key in ("_work_auth", "_yes") or (key.startswith("_fact:") and desired_lower == "yes"):
        for o in options:
            if o["text"].lower() in ("yes", "yes, i am", "authorized", "yes - authorized"):
                return o
        for o in options:
            if "yes" in o["text"].lower():
                return o

    if key in ("_no", "_sponsorship") or (key.startswith("_fact:") and desired_lower == "no"):
        for o in options:
            if o["text"].lower().strip() == "no":
                return o
        for o in options:
            if o["text"].lower().strip().startswith("no"):
                return o

    if key == "_country":
        for o in options:
            t = o["text"].lower()
            if "united states" in t or t in ("us", "usa"):
                return o

    for o in options:
        if o["text"].lower().strip() == desired_lower:
            return o
    for o in options:
        if desired_lower and desired_lower in o["text"].lower():
            return o
    return None


def _match_select_option(options: list[dict], desired: str, key: str) -> str | None:
    opt = _pick_option(options, desired, key)
    return opt["text"] if opt else None
```

- [ ] **Step 4: Run all tests**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/applier/analyzer.py backend/tests/test_radio_groups.py
git commit -m "feat(applier): merge radio/checkbox groups into answerable questions"
```

---

### Task 4: Constrained option choice — `choices.py`

**Files:**
- Create: `backend/services/tailor/choices.py`
- Create: `backend/tests/test_choices.py`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_choices.py`:

```python
"""choose_options: the LLM only picks a validated option index — never free text."""
import json

import backend.services.tailor.choices as choices


QS = [
    {"question_text": "Which shift do you prefer?", "options": ["Day", "Night", "Either"]},
    {"question_text": "Do you have call-center experience?", "options": ["Yes", "No"]},
]

FACTS = {"shifts_nights": "Yes"}


def _patch(monkeypatch, replies):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(choices, "_llm_complete", fake)
    return calls


def test_valid_choices_applied(monkeypatch):
    _patch(monkeypatch, [json.dumps([
        {"q": 0, "choice": 2, "backed": True},
        {"q": 1, "choice": 0, "backed": False},
    ])])
    out = choices.choose_options(QS, FACTS, {"title": "CS Rep", "company": "Acme"})
    assert out == [{"index": 2, "backed": True}, {"index": 0, "backed": False}]


def test_out_of_range_choice_dropped(monkeypatch):
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 9, "backed": True},
                                     {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] is None and out[1]["index"] == 1


def test_garbage_retried_then_gives_up(monkeypatch):
    calls = _patch(monkeypatch, ["not json at all", "still garbage"])
    out = choices.choose_options(QS, FACTS, {})
    assert len(calls) == 2  # ATTEMPTS retries
    assert all(o["index"] is None for o in out)


def test_null_choice_left_for_human(monkeypatch):
    _patch(monkeypatch, [json.dumps([{"q": 0, "choice": None, "backed": False},
                                     {"q": 1, "choice": 1, "backed": True}])])
    out = choices.choose_options(QS, FACTS, {})
    assert out[0]["index"] is None and out[1]["index"] == 1


def test_too_many_options_skipped_without_llm(monkeypatch):
    calls = _patch(monkeypatch, ["[]"])
    big = [{"question_text": "Pick", "options": [str(i) for i in range(60)]}]
    out = choices.choose_options(big, {}, {})
    assert out == [{"index": None, "backed": False}] and calls == []


def test_prompt_contains_facts_and_question(monkeypatch):
    calls = _patch(monkeypatch, [json.dumps([{"q": 0, "choice": 0, "backed": True},
                                             {"q": 1, "choice": 0, "backed": True}])])
    choices.choose_options(QS, FACTS, {"title": "CS Rep", "company": "Acme"}, "bpo-voice-qa")
    p = calls[0]
    assert "shifts_nights" in p and "Which shift do you prefer?" in p and "bpo-voice-qa" in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_choices.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

`backend/services/tailor/choices.py`:

```python
"""Pick options for closed screener questions (selects / radio groups) the rules
didn't cover. The model only CHOOSES an option index based on the candidate's fact
sheet — it never writes free text, and every answer is validated to be a real option
index (anything else -> None -> the human). `backed=false` marks a choice not directly
supported by a fact: it is still filled, but flagged for the human to confirm.
"""
import json
import logging
import re

from backend.services.tailor.tailor import _llm_complete

logger = logging.getLogger(__name__)

CHUNK = 10        # questions per LLM call — small batches keep a weak model reliable
MAX_OPTIONS = 40  # huge dropdowns (countries etc.) are rule/human territory
ATTEMPTS = 2


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


def _parse(raw: str, questions: list[dict]) -> list[dict] | None:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    try:
        arr = json.loads(m.group(0) if m else raw)
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    out = [{"index": None, "backed": False} for _ in questions]
    for item in arr:
        if not isinstance(item, dict):
            continue
        qi, ch = item.get("q"), item.get("choice")
        if not isinstance(qi, int) or not 0 <= qi < len(questions):
            continue
        if isinstance(ch, int) and 0 <= ch < len(questions[qi]["options"]):
            out[qi] = {"index": ch, "backed": bool(item.get("backed"))}
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
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_choices.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add backend/services/tailor/choices.py backend/tests/test_choices.py
git commit -m "feat(applier): constrained option choice for closed screeners"
```

---

### Task 5: Open-text drafting v2 — `answers.py`

**Files:**
- Modify: `backend/services/tailor/answers.py` (full rewrite below)
- Create: `backend/tests/test_answers_v2.py`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_answers_v2.py`:

```python
"""draft_answers v2: profession-neutral prompt from facts+variant, chunked calls."""
import json

import re

import backend.services.tailor.answers as answers


def _patch(monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        # answer exactly the numbered questions present in this chunk's prompt
        qn = len(re.findall(r"(?m)^\d+\. ", prompt))
        return json.dumps([f"answer {i}" for i in range(qn)])

    monkeypatch.setattr(answers, "_llm_complete", fake)
    return calls


def test_chunks_of_eight(monkeypatch):
    calls = _patch(monkeypatch)
    qs = [f"Question number {i} about the role, long enough?" for i in range(20)]
    out = answers.draft_answers(qs, {"full_name": "Kate Doe"}, {"title": "CS Rep"},
                                facts={"typing_wpm": "70"}, niche_label="chat-email-async")
    assert len(calls) == 3  # 8 + 8 + 4
    assert len(out) == 20


def test_prompt_has_role_facts_niche_no_hardcoded_profession(monkeypatch):
    calls = _patch(monkeypatch)
    answers.draft_answers(["Why do you want to work here, in a few words?"],
                          {"full_name": "Kate Doe"},
                          {"title": "Night Auditor", "company": "Acme"},
                          facts={"typing_wpm": "70"}, niche_label="bpo-voice-qa")
    p = calls[0]
    assert "Night Auditor" in p and "Acme" in p
    assert "typing_wpm" in p and "bpo-voice-qa" in p
    assert "customer-support candidate" not in p


def test_cap_twenty(monkeypatch):
    calls = _patch(monkeypatch)
    qs = [f"Question number {i} about the role, long enough?" for i in range(30)]
    out = answers.draft_answers(qs, {}, {})
    assert len(out) == 20


def test_failed_chunk_skipped_not_fatal(monkeypatch):
    state = {"n": 0}

    def fake(prompt):
        state["n"] += 1
        if state["n"] <= answers.DRAFT_ATTEMPTS:  # first chunk fails all attempts
            return "garbage"
        return json.dumps(["ok"] * 8)

    monkeypatch.setattr(answers, "_llm_complete", fake)
    monkeypatch.setattr(answers.time, "sleep", lambda s: None)
    qs = [f"Question number {i} about the role, long enough?" for i in range(16)]
    out = answers.draft_answers(qs, {}, {})
    assert len(out) == 8  # second chunk still drafted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_answers_v2.py -q`
Expected: FAIL — `draft_answers` has no `facts`/`niche_label` params, cap is 8.

- [ ] **Step 3: Rewrite `backend/services/tailor/answers.py`**

```python
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

from backend.services.tailor.tailor import _llm_complete

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 20
CHUNK = 8
DRAFT_ATTEMPTS = 3  # retries per chunk so one transient failure doesn't blank a form


def _header(profile_form: dict, job: dict, resume_summary: str,
            facts: dict | None, niche_label: str) -> str:
    candidate = {k: v for k, v in profile_form.items() if not k.startswith("_") and v}
    candidate.update({k: v for k, v in (facts or {}).items() if v not in (None, "", [])})
    role = job.get("title") or "the role"
    company = job.get("company") or "the company"
    focus = f" The résumé being submitted is focused on: {niche_label}." if niche_label else ""
    return (
        f"You are drafting short job-application answers for a candidate applying to "
        f"{role} at {company}.{focus} Use ONLY the candidate facts below. Rules:\n"
        "- Use ONLY facts present in the profile/experience. Do NOT invent employers, "
        "dates, certifications, or specific numbers/metrics.\n"
        "- Each answer 1-3 sentences, professional, first person.\n"
        "- Write in the candidate's natural voice, as if filling the form himself. NEVER "
        "refer to 'my profile', 'the information provided', 'not listed', or that a fact "
        "is missing. If a detail isn't available, give a sensible professional default "
        "(e.g. 'How did you hear about this role?' -> 'Through an online job search'; a "
        "yes/no with no blockers -> answer plainly, e.g. 'No, none.').\n"
        "- For a question asking to 'describe a time' or 'give an example', write a "
        "realistic answer consistent with the experience and PREFIX it with '[review] ' "
        "so the human personalizes it with their real story.\n"
        "- For simple/factual questions, answer directly and briefly.\n\n"
        f"CANDIDATE FACTS: {json.dumps(candidate)}\n"
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
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            arr = json.loads(m.group(0) if m else raw)
            answers = {q: str(arr[i]).strip() for i, q in enumerate(questions)
                       if i < len(arr) and str(arr[i]).strip()}
            if answers:
                return answers
            last_err = "empty/unparseable response"
        except Exception as e:
            last_err = e
        if attempt < DRAFT_ATTEMPTS:
            logger.info("draft chunk attempt %d failed (%s) — retrying", attempt, last_err)
            time.sleep(2 * attempt)  # exponential backoff: 2s, 4s
    logger.warning("draft chunk failed after %d attempts (%s) — %d questions left "
                   "for the human", DRAFT_ATTEMPTS, last_err, len(questions))
    return {}


def draft_answers(questions: list[str], profile_form: dict, job: dict,
                  resume_summary: str = "", facts: dict | None = None,
                  niche_label: str = "") -> dict[str, str]:
    """Return {question: drafted_answer} for open-ended questions. Empty on failure."""
    questions = [q.strip() for q in questions if q and len(q.strip()) > 12][:MAX_QUESTIONS]
    if not questions:
        return {}
    header = _header(profile_form, job, resume_summary, facts, niche_label)
    out: dict[str, str] = {}
    for start in range(0, len(questions), CHUNK):
        out.update(_draft_chunk(questions[start:start + CHUNK], header))
    return out
```

- [ ] **Step 4: Run all tests**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/services/tailor/answers.py backend/tests/test_answers_v2.py
git commit -m "feat(applier): profession-aware chunked answer drafting (cap 20)"
```

---

### Task 6: Answer cache keyed (profile, niche, question)

**Files:**
- Modify: `backend/answer_cache.py` (full rewrite below)
- Modify: `backend/dashboard_app.py:296-323` (`/draft` endpoint)
- Create: `backend/tests/test_answer_cache.py`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_answer_cache.py`:

```python
"""Answer cache v2: keyed (profile, niche, question) — no cross-person reuse."""
import backend.answer_cache as ac


def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "DB_PATH", str(tmp_path / "cache.db"))


def test_profiles_do_not_share_answers(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q = "Why do you want to work at Acme?"
    ac.put_many({q: "Because I love helping Acme customers."}, "Acme",
                profile="michael", niche="bpo-voice-qa")
    assert ac.get_many([q], "Acme", profile="kate", niche="bpo-voice-qa") == {}
    got = ac.get_many([q], "Acme", profile="michael", niche="bpo-voice-qa")
    assert got == {q: "Because I love helping Acme customers."}


def test_niches_do_not_share_answers(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q = "How many years of experience do you have?"
    ac.put_many({q: "12+ years."}, "", profile="michael", niche="chat-email-async")
    assert ac.get_many([q], "", profile="michael", niche="bpo-voice-qa") == {}
    assert ac.get_many([q], "", profile="michael", niche="chat-email-async") == {q: "12+ years."}


def test_company_genericized_across_companies(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    q1 = "Why do you want to work at Acme?"
    ac.put_many({q1: "Acme's product impressed me."}, "Acme", profile="m", niche="")
    q2 = "Why do you want to work at Zapier?"
    got = ac.get_many([q2], "Zapier", profile="m", niche="")
    assert got == {q2: "Zapier's product impressed me."}


def test_stats(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    ac.put_many({"Question one is long enough?": "A."}, "", profile="m", niche="")
    s = ac.stats()
    assert s["cached_questions"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_answer_cache.py -q`
Expected: FAIL — `put_many() got an unexpected keyword argument 'profile'`

- [ ] **Step 3: Rewrite `backend/answer_cache.py`**

```python
"""SQLite cache of drafted answers, keyed by (profile, niche, normalized question).

Repetitive application questions recur across forms, so answers are cached and the
LLM is hit only for genuinely new questions. The key includes the applying PERSON and
the résumé NICHE: different people (and variants stating e.g. different years totals)
must never share an answer — five applicants submitting word-for-word identical texts
is exactly the volume problem this prevents. The company name is genericized to <co>
inside stored answers so one person's answer is reusable across companies.
"""
import os
import re
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "uploads", "answer_cache.db")
DB_PATH = os.path.abspath(DB_PATH)
_lock = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("DROP TABLE IF EXISTS answers")  # legacy schema (no profile column); it's a cache
    c.execute("""CREATE TABLE IF NOT EXISTS answers_v2 (
        profile TEXT NOT NULL,
        niche TEXT NOT NULL DEFAULT '',
        key TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        hits INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (profile, niche, key)
    )""")
    return c


def normalize(question: str, company: str = "") -> str:
    q = (question or "").lower().strip()
    if company:
        q = q.replace(company.lower().strip(), "<co>")
    # blank out common company-name leftovers like "at <Name>" / "join <Name>"
    q = re.sub(r"\b(at|join|with|for)\s+[a-z0-9][\w.&'-]+(\s+[a-z0-9][\w.&'-]+)?\b", r"\1 <co>", q)
    q = re.sub(r"[^a-z0-9<>]+", " ", q)        # drop punctuation
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _genericize(answer: str, company: str) -> str:
    """Replace the company name in a stored answer with <co> so the answer is
    reusable across companies ("I want to work at Acme" must never be served
    for a Zapier application)."""
    if not company:
        return answer
    return re.sub(re.escape(company.strip()), "<co>", answer, flags=re.IGNORECASE)


def _personalize(answer: str, company: str) -> str:
    return answer.replace("<co>", company.strip() if company else "your company")


def get_many(questions: list[str], company: str = "", *,
             profile: str = "default", niche: str = "") -> dict:
    """Return {question: answer} for this person's cached questions (bumps hit count)."""
    out = {}
    with _lock, _conn() as c:
        for q in questions:
            k = normalize(q, company)
            if not k:
                continue
            row = c.execute("SELECT answer FROM answers_v2 WHERE profile=? AND niche=? AND key=?",
                            (profile, niche, k)).fetchone()
            if row:
                out[q] = _personalize(row[0], company)
                c.execute("UPDATE answers_v2 SET hits=hits+1, updated_at=datetime('now') "
                          "WHERE profile=? AND niche=? AND key=?", (profile, niche, k))
    return out


def put_many(qa: dict, company: str = "", *,
             profile: str = "default", niche: str = "") -> None:
    """Store {question: answer} pairs (company name genericized to <co>)."""
    with _lock, _conn() as c:
        for q, a in qa.items():
            k = normalize(q, company)
            if not k or not a:
                continue
            c.execute(
                "INSERT INTO answers_v2(profile, niche, key, question, answer) VALUES(?,?,?,?,?) "
                "ON CONFLICT(profile, niche, key) DO UPDATE SET answer=excluded.answer, "
                "updated_at=datetime('now') WHERE excluded.answer != answers_v2.answer",
                (profile, niche, k, q, _genericize(a, company)))


def stats() -> dict:
    with _lock, _conn() as c:
        n = c.execute("SELECT COUNT(*), COALESCE(SUM(hits),0) FROM answers_v2").fetchone()
    return {"cached_questions": n[0], "cache_hits_served": n[1]}
```

- [ ] **Step 4: Update the dashboard `/draft` call site**

In `backend/dashboard_app.py` replace the body between `cached = ...` and `answers = ...` (lines 309-321) with:

```python
    niche = (payload.get("niche") or "").strip()
    cached = answer_cache.get_many(questions, company, profile="michael", niche=niche)
    misses = [q for q in questions if q not in cached]
    drafted = {}
    if misses:
        try:
            from backend.profiles.facts import load_facts
            from backend.services.tailor.answers import draft_answers
            form_facts, summary = _michael_form()
            drafted = draft_answers(misses, form_facts, {"title": title, "company": company},
                                    summary, facts=load_facts("michael"), niche_label=niche)
            if drafted:
                answer_cache.put_many(drafted, company, profile="michael", niche=niche)
        except Exception as e:  # never 500 the extension — return what we have
            drafted = {}
            print(f"[draft] LLM error: {e}")
```

- [ ] **Step 5: Run all tests**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/answer_cache.py backend/dashboard_app.py backend/tests/test_answer_cache.py
git commit -m "feat(applier): answer cache keyed per profile and niche"
```

---

### Task 7: Per-profile etalons

**Files:**
- Move: `backend/data/etalons.json` → `backend/data/etalons/michael.json`
- Modify: `backend/services/tailor/variants.py` (`_load_raw`, `list_niches`, `categorize`, `variant_for`)
- Modify: `backend/apply_cli.py:55-57,76`
- Create: `backend/tests/test_variants_per_profile.py`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_variants_per_profile.py`:

```python
"""Per-profile etalons: each person routes jobs to THEIR OWN résumé variants."""
import json

from backend.profiles.store import Profile
from backend.services.tailor import variants


def _etalon(key, label):
    return {"key": key, "label": label, "best_for": label,
            "resume": {"headline": label, "summary": f"{label} with 12+ years",
                       "experience": [{"company": "RealCo", "title": label,
                                       "bullets": ["Did support work"]}],
                       "skills": [{"group": "Core", "items": ["Zendesk"]}],
                       "certifications": [], "education": []}}


def _profile(pid):
    return Profile(id=pid, full_name="Kate Doe", email="k@x.com", phone="1")


def test_profiles_load_own_etalons(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    variants._load_raw.cache_clear()
    (tmp_path / "kate.json").write_text(
        json.dumps([_etalon("travel-hospitality", "Travel Support")]), encoding="utf-8")
    (tmp_path / "michael.json").write_text(
        json.dumps([_etalon("bpo-voice-qa", "Call Center QA")]), encoding="utf-8")

    key_k, resume_k = variants.variant_for({"title": "Travel Support Agent"}, _profile("kate"))
    assert key_k == "travel-hospitality"
    assert resume_k["personal_info"]["full_name"] == "Kate Doe"

    key_m, _ = variants.variant_for({"title": "Call Center QA Analyst"}, _profile("michael"))
    assert key_m == "bpo-voice-qa"


def test_profile_without_etalons_gets_none(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    variants._load_raw.cache_clear()
    key, resume = variants.variant_for({"title": "Support Agent"}, _profile("nobody"))
    assert key is None and resume is None


def test_list_niches_per_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    variants._load_raw.cache_clear()
    (tmp_path / "kate.json").write_text(json.dumps([_etalon("a", "A")]), encoding="utf-8")
    assert [n["key"] for n in variants.list_niches("kate")] == ["a"]
    assert variants.list_niches("nobody") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_variants_per_profile.py -q`
Expected: FAIL — `variants` has no `ETALONS_DIR`, `_load_raw` takes no args.

- [ ] **Step 3: Move the etalon file**

```bash
mkdir -p backend/data/etalons
git mv backend/data/etalons.json backend/data/etalons/michael.json
```

- [ ] **Step 4: Update `backend/services/tailor/variants.py`**

Replace the path constants and loader (lines 24-40):

```python
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ETALONS_DIR = PROJECT_ROOT / "backend" / "data" / "etalons"


@lru_cache(maxsize=16)
def _load_raw(profile_id: str) -> tuple:
    """One person's etalon set. No shared fallback: a profile without their own file
    gets NO variants (they must never apply with someone else's work history)."""
    path = ETALONS_DIR / f"{profile_id}.json"
    if not profile_id or not path.exists():
        logger.warning("no etalons for profile %r at %s — variants disabled", profile_id, path)
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(data) if isinstance(data, list) else ()


def list_niches(profile_id: str) -> list[dict]:
    """[{key, label, best_for}] for one profile's variants."""
    return [{"key": it.get("key"), "label": it.get("label", ""),
             "best_for": it.get("best_for", "")} for it in _load_raw(profile_id)]
```

Update `categorize` to take the profile (signature + first line):

```python
def categorize(job_title: str, job_description: str = "", profile_id: str = "") -> tuple[str | None, int]:
    ...
    items = _load_raw(profile_id)
```

Update `variant_for` (lines 140-152):

```python
def variant_for(job: dict, profile) -> tuple[str | None, dict | None]:
    """Pick the niche variant for a job and stamp it with the profile's identity.

    Returns (niche_key, base_resume) or (None, None) when this profile has no etalons.
    """
    key, score = categorize(job.get("title", ""), job.get("description", ""), profile.id)
    if not key:
        return None, None
    item = next((it for it in _load_raw(profile.id) if it.get("key") == key), None)
    if not item:
        return None, None
    logger.info("variant: %r -> %s (score=%s) [profile=%s]",
                job.get("title", ""), key, score, profile.id)
    return key, _adapt(item, profile)
```

- [ ] **Step 5: Update `backend/apply_cli.py` call sites**

Line 56-57:

```python
        if not list_niches(a.profile):
            ap.error(f"no résumé variants for profile {a.profile!r} "
                     f"(backend/data/etalons/{a.profile}.json missing)")
```

Line 76:

```python
            niche, score = categorize(j.get("title", ""), j.get("description", ""), a.profile)
```

- [ ] **Step 6: Check for leftover callers**

Run: `grep -rn "etalons.json\|list_niches(\|categorize(\|_load_raw(" backend --include="*.py" | grep -v tests/ | grep -v __pycache__`
Expected: every `list_niches`/`categorize`/`_load_raw` call passes a profile id; no reference to `backend/data/etalons.json` remains (the docstring at the top of `variants.py` mentions `etalons.json` — update it to `backend/data/etalons/<profile>.json`).

- [ ] **Step 7: Run all tests**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/ -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(applier): per-profile etalon resumes — no shared work history"
```

---

### Task 8: Wire the cascade into prefill + report sources + submit gate

**Files:**
- Modify: `backend/applier/strategies/base.py` (`prefill`, new `_fill_choice`)
- Modify: `backend/applier/runner.py` (`prefill_application`, `_is_submittable`)
- Modify: `backend/copilot.py:82-98` (`/load` prefill call)

No new unit test file: the cascade pieces are tested in Tasks 2-7; this task is wiring
(browser-bound, covered by the existing submit-guard tests + manual run in Task 9).
The submit gate change DOES get a test added to `test_pipeline.py`.

- [ ] **Step 1: Write the failing gate test**

Append to `backend/tests/test_pipeline.py`:

```python
def test_submit_gate_blocks_on_review_items():
    from backend.applier.runner import _is_submittable
    ok = {"page_type": "application_form", "unfilled": [], "review_items": []}
    assert _is_submittable(ok)
    assert not _is_submittable({**ok, "unfilled": ["q"]})
    assert not _is_submittable({**ok, "review_items": [{"question": "q", "answer": "[review] a"}]})
```

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_pipeline.py -q`
Expected: the new test FAILS (`_is_submittable` ignores `review_items`).

- [ ] **Step 2: Update `_is_submittable` in `backend/applier/runner.py`**

```python
def _is_submittable(fill_report: dict) -> bool:
    """A form may be auto-submitted ONLY if it's a real application form with no
    unanswered required questions AND no unconfirmed [review] answers — never
    half-submit or submit a guess the human hasn't seen."""
    return (fill_report.get("page_type") == "application_form"
            and not (fill_report.get("unfilled") or [])
            and not (fill_report.get("review_items") or []))
```

Run the test again — PASS.

- [ ] **Step 3: Rewrite `prefill` in `backend/applier/strategies/base.py`**

Replace the `prefill` method with:

```python
    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "") -> dict:
        # known_answers = answers already drafted by a prior batch — fills them directly
        # (no LLM call), so the co-pilot's "Fill" is instant instead of re-drafting (~30s).
        await self.open_form(page)
        await page.wait_for_timeout(1500)
        analysis = await analyze_page(page, profile_form, cover_letter,
                                      known_answers or {}, facts or {})
        page_type = analysis.get("page_type", "unknown")
        if page_type in ("login_required", "captcha", "expired"):
            return {
                "strategy": self.name, "page_type": page_type,
                "filled": 0, "failed": 0,
                "unfilled": [q.get("question_text", "") for q in analysis.get("unknown_questions", [])],
                "review_items": [], "answer_sources": {},
                "submit_selector": analysis.get("submit_selector"),
                "submitted": False,
                "note": f"stopped: page_type={page_type}",
            }
        success, fail = await fill_form(page, analysis)

        # Custom React-Select dropdowns (Greenhouse) the analyzer can't fill — deterministic
        # eligibility ones only (work-auth/sponsorship/country/18+/background/equipment).
        ds = await fill_react_selects(page)
        success += ds["filled"]

        unknown = analysis.get("unknown_questions", [])
        # `success` here = rule-based fills that actually took + react-select eligibility
        sources = {"rule": success,
                   "choice": 0, "choice_review": 0, "draft": 0, "draft_review": 0,
                   "human": 0}
        review_items: list[dict] = []
        drafted: dict = {}
        answered_idx: set[int] = set()

        if draft and job:
            company = (job or {}).get("company", "")

            # 1) Closed questions with options -> constrained LLM choice (validated index).
            closed = [(i, q) for i, q in enumerate(unknown)
                      if q.get("options") and q.get("type") in
                      ("select", "select-one", "radio_group", "checkbox_group")]
            if closed:
                from backend.services.tailor.choices import choose_options
                picks = choose_options(
                    [{"question_text": q["question_text"], "options": q["options"]}
                     for _, q in closed],
                    facts or {}, job, niche)
                for (i, q), pick in zip(closed, picks):
                    if pick["index"] is None:
                        continue
                    if not await self._fill_choice(page, q, pick["index"]):
                        continue
                    answered_idx.add(i)
                    success += 1
                    if pick["backed"]:
                        sources["choice"] += 1
                    else:
                        sources["choice_review"] += 1
                        review_items.append({"question": q["question_text"],
                                             "answer": q["options"][pick["index"]],
                                             "kind": "choice"})

            # 2) Open text questions -> per-person cache, then LLM draft.
            open_qs = [(i, q) for i, q in enumerate(unknown)
                       if q.get("type") in ("text", "textarea", "")
                       and len(q.get("question_text", "")) > 15]
            if open_qs:
                from backend import answer_cache
                from backend.services.tailor.answers import draft_answers
                q_texts = [q["question_text"] for _, q in open_qs]
                drafted = answer_cache.get_many(q_texts, company,
                                                profile=profile_id, niche=niche)
                missing = [t for t in q_texts if t not in drafted]
                if missing:
                    fresh = draft_answers(missing, profile_form, job, resume_summary,
                                          facts=facts, niche_label=niche)
                    if fresh:
                        answer_cache.put_many(fresh, company,
                                              profile=profile_id, niche=niche)
                        drafted.update(fresh)
                for i, q in open_qs:
                    ans = drafted.get(q.get("question_text", ""))
                    sel = q.get("selector")
                    if not (ans and sel):
                        continue
                    try:
                        await page.locator(sel).first.fill(ans, timeout=4000)
                    except Exception:
                        continue
                    answered_idx.add(i)
                    success += 1
                    sources["draft"] += 1
                    if ans.startswith("[review]"):
                        sources["draft_review"] += 1
                        review_items.append({"question": q["question_text"],
                                             "answer": ans, "kind": "draft"})

        unfilled = [q.get("question_text", "") for i, q in enumerate(unknown)
                    if i not in answered_idx]
        sources["human"] = len(unfilled)

        return {
            "strategy": self.name,
            "page_type": page_type,
            "filled": success,
            "failed": fail,
            "unfilled": unfilled,
            "drafted_answers": drafted,   # human reviews/edits these before submit
            "review_items": review_items,  # filled, but need a human eye before submit
            "answer_sources": sources,
            "dropdowns": ds["handled"],   # react-select eligibility dropdowns auto-answered
            "submit_selector": analysis.get("submit_selector"),
            "submitted": False,  # set True only by submit_form() on the opt-in path
        }

    async def _fill_choice(self, page: Page, q: dict, index: int) -> bool:
        """Apply a chosen option: select by label, or check the radio/checkbox input."""
        try:
            if q.get("type") in ("radio_group", "checkbox_group"):
                sels = q.get("option_selectors") or []
                if index >= len(sels) or not sels[index]:
                    return False
                await page.locator(sels[index]).first.check(timeout=4000)
            else:
                await page.locator(q["selector"]).first.select_option(
                    label=q["options"][index], timeout=4000)
            return True
        except Exception as e:
            logger.debug("choice fill failed for %r: %s",
                         q.get("question_text", "")[:40], e)
            return False
```

- [ ] **Step 4: Pass facts/profile/niche from `backend/applier/runner.py`**

Add the import:

```python
from backend.profiles.facts import load_facts
```

Replace the `strategy.prefill(...)` call (lines 130-132):

```python
            strategy = _pick_strategy(apply_url)
            fill_report = await strategy.prefill(
                page, form, str(resume_pdf),
                job=job, draft=draft_answers, resume_summary=render_text(tailored),
                facts=load_facts(profile.id), profile_id=profile.id, niche=niche or "")
```

- [ ] **Step 5: Update the co-pilot call site (`backend/copilot.py`)**

Add the import at the top (next to the other backend imports):

```python
from backend.profiles.facts import load_facts
```

Replace the `strat.prefill(...)` call (lines 94-98):

```python
            result = await strat.prefill(page, form, resume_pdf,
                                         job={"title": title, "company": company},
                                         draft=not known,  # only re-draft live if batch had none
                                         resume_summary=render_text(tailored),
                                         known_answers=known,
                                         facts=load_facts(profile),
                                         profile_id=profile, niche=niche or "")
```

- [ ] **Step 6: Run all tests**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/ -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/applier/strategies/base.py backend/applier/runner.py backend/copilot.py backend/tests/test_pipeline.py
git commit -m "feat(applier): wire answer cascade — sources report and review-gated submit"
```

---

### Task 9: Full verification

- [ ] **Step 1: Full test suite**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/ -q`
Expected: all pass, no warnings about collection errors.

- [ ] **Step 2: Branding grep (user-facing texts must not name the stack)**

Run: `grep -riE "\bclaude\b|\bgpt\b|anthropic|openai|\bllm\b|\b(ai|ии)\b" backend/applier/batch.py backend/copilot.py backend/dashboard_app.py | grep -viE "use_ai|--ai|a\.ai|_ai_polish|use_ai=|#|\"\"\"|anthropic_api_key|llm_url|llm_model|llm_key|from_llm"`
Expected: no user-visible strings (HTML/labels/report values) naming the stack. `[review]` labels and `answer_sources` keys are neutral. If anything surfaces in HTML — rephrase to "автоответ"/"черновик".

- [ ] **Step 3: Manual smoke run (real form, no submit)**

Run (pick any live greenhouse posting from the boards, headful optional):

```bash
PYTHONPATH=. python3 backend/apply_cli.py --profile michael --url "<greenhouse apply URL>" --draft
```

Expected in the printed report + `uploads/prefill/michael/<slug>/report.json`:
- `answer_sources` present with non-zero `rule`;
- closed screeners answered (`choice`/`choice_review` > 0 when the form has them);
- `review_items` lists every unbacked choice and `[review]` draft;
- `unfilled` ~empty on a standard form (success criterion: human only reviews and clicks Submit).

- [ ] **Step 4: Remind the user**

Tell the user to fill in real values in `backend/data/facts/michael.json` and to create
`backend/data/facts/<id>.json` + `backend/data/etalons/<id>.json` for each friend
before their first batch run.

- [ ] **Step 5: Push**

```bash
git push origin feat/semi-auto-apply-engine
```
