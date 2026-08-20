# Phase 1 — Remote-jobs pipeline foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `job_catalog` the single source of remote jobs, tag every job with the countries it is open to (US/CA/UK/OTHER), guarantee questions are collected, add Workable, and automate refresh.

**Architecture:** Add a pure-functional deterministic region classifier (`applier/regions.py`) mirroring `boards.py`'s blob heuristics, with an LLM fallback only on the ambiguous residue. Extend the existing `job_catalog` store (`tools/catalog_db.py`) with a `regions text[]` column and hook classification into the collector (`tools/catalog_collector.py`). Add Workable to the ATS normalizer and question scraper. Schedule the collector via cron.

**Tech Stack:** Python 3.12, psycopg2 (Postgres `jobfinder_crm` via `CRM_PG_DSN`), httpx (local Sumrak LLM), Playwright (question scraping), pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-remote-jobs-pipeline-and-oneclick-apply-design.md`

## Global Constraints

- Region codes are exactly `("US", "CA", "UK", "OTHER")` — no others.
- Multi-eligibility: a job is tagged with EVERY region it is open to (`North America` → `["US","CA"]`; worldwide/anywhere → all four).
- Classification is **deterministic-first**; the LLM (local Sumrak) is called ONLY on jobs the rules can't resolve, cached by normalized location, and unresolved → `region_source="unknown"` with empty `regions` (never guessed).
- Remote is a hard gate; the collector only stores remote jobs.
- DB access follows the existing `catalog_db` pattern: `CRM_PG_DSN`, `psycopg2` pool, `conn()`/`_cur()`, `ensure_schema()` applies DDL idempotently (no separate migration tool).
- Tests follow the existing style: pure-logic unit tests run as `PYTHONPATH=. python3 -m pytest backend/tests/test_X.py -q`; there is no `conftest.py` and no DB test harness — DB-touching changes are verified by a smoke script against the live `jobfinder_crm`.
- Commits: conventional (`type(scope): msg`), English, NO `Co-Authored-By`/assistant trailer.
- Do NOT re-add server-side auto-submit. Do NOT chase new company coverage beyond current sources + Workable.

---

### Task 1: Deterministic region classifier

**Files:**
- Create: `backend/applier/regions.py`
- Test: `backend/tests/test_regions.py`

**Interfaces:**
- Produces: `REGION_CODES: tuple[str,...]`; `classify_regions(job: dict) -> list[str]` (deterministic; returns a subset of `REGION_CODES` in canonical order, or `[]` when no rule matches).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_regions.py
"""Region classifier — pure logic, no network/DB. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_regions.py -q
"""
from backend.applier import regions


def _j(location="", title="", description=""):
    return {"title": title, "location": location, "description": description}


def test_us_only():
    assert regions.classify_regions(_j(location="Remote - US")) == ["US"]
    assert regions.classify_regions(_j(location="Remote (United States)")) == ["US"]


def test_canada():
    assert regions.classify_regions(_j(location="Remote, Canada")) == ["CA"]
    assert regions.classify_regions(_j(location="Toronto, Ontario")) == ["CA"]


def test_north_america_is_us_and_ca():
    assert regions.classify_regions(_j(location="Remote - North America")) == ["US", "CA"]
    assert regions.classify_regions(_j(description="Open to US & Canada")) == ["US", "CA"]


def test_uk():
    assert regions.classify_regions(_j(location="Remote - United Kingdom")) == ["UK"]
    assert regions.classify_regions(_j(location="London, England")) == ["UK"]


def test_worldwide_is_all():
    assert regions.classify_regions(_j(location="Remote - Worldwide")) == ["US", "CA", "UK", "OTHER"]
    assert regions.classify_regions(_j(location="Work from anywhere")) == ["US", "CA", "UK", "OTHER"]


def test_other_only():
    assert regions.classify_regions(_j(location="Remote - EMEA")) == ["OTHER"]
    assert regions.classify_regions(_j(location="Latin America (Remote)")) == ["OTHER"]


def test_multi_us_uk():
    assert regions.classify_regions(_j(location="Remote - US or UK")) == ["US", "UK"]


def test_false_positives_do_not_match_us():
    # "business"/"focus"/"customer" contain the substring "us" — must NOT tag US.
    assert regions.classify_regions(_j(title="Customer Success", description="our business focus")) == []


def test_join_us_in_description_does_not_match_us():
    # ubiquitous "join us"/"contact us" in descriptions must NOT tag US —
    # bare "us"/"uk" are honored only in title/location, never the description.
    j = _j(location="Remote", description="We would love for you to join us — contact us today!")
    assert regions.classify_regions(j) == []


def test_us_in_location_field_matches():
    assert regions.classify_regions(_j(location="US")) == ["US"]


def test_latin_america_is_not_us():
    assert regions.classify_regions(_j(location="Latin America")) == ["OTHER"]


def test_empty_when_no_signal():
    assert regions.classify_regions(_j(location="Remote")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_regions.py -q`
Expected: FAIL (ModuleNotFoundError: backend.applier.regions).

- [ ] **Step 3: Write minimal implementation**

```python
# backend/applier/regions.py
"""Classify which countries/regions a (remote) job is open to.

Deterministic-first, mirroring boards.py's blob pattern (lower-cased join of
title+location+description[:1500]). Returns a subset of REGION_CODES.
Multi-eligibility: North America -> US+CA; worldwide/anywhere -> all four.
Short/ambiguous tokens use word boundaries to avoid false positives
("business" must not match US).
"""
from __future__ import annotations
import re

REGION_CODES = ("US", "CA", "UK", "OTHER")

_WORLDWIDE_RE = re.compile(
    r"\b(worldwide|work from anywhere|remote anywhere|anywhere in the world|global(?:ly)?)\b")
_NA_RE = re.compile(
    r"\bnorth america\b|\bus\s*&\s*canada\b|\bus\s*/\s*canada\b|\bus and canada\b|\busa\s*/\s*canada\b")
_US_STRONG_RE = re.compile(
    r"\bunited states\b|\bu\.?s\.?a\b|\bu\.s\.\b|\bus[- ]based\b|\bus[- ]only\b"
    r"|\bremote\s*[-,(]\s*us\b")
_CA_RE = re.compile(
    r"\bcanada\b|\bcanadian\b|\bontario\b|\bquebec\b|\bbritish columbia\b|\balberta\b"
    r"|\btoronto\b|\bvancouver\b|\bmontreal\b")
_UK_STRONG_RE = re.compile(
    r"\bunited kingdom\b|\bengland\b|\bscotland\b|\bwales\b|\blondon\b|\bbritain\b|\bbritish\b")
# Short ambiguous tokens ("us","uk") — scanned ONLY over title+location (see _loc_blob),
# never the description, or "join us"/"contact us" would false-positive everywhere.
_US_LOC_RE = re.compile(r"\bus\b(?!\s*[/&]?\s*canada)")
_UK_LOC_RE = re.compile(r"\buk\b|\bu\.k\.\b")
_OTHER_RE = re.compile(
    r"\bemea\b|\bapac\b|\banz\b|\beurope(?:an)?\b|\blatam\b|\blatin america\b|\bsouth america\b"
    r"|\bindia\b|\bphilippines\b|\bpakistan\b|\bgermany\b|\bfrance\b|\bspain\b|\bnetherlands\b"
    r"|\bpoland\b|\bportugal\b|\bromania\b|\bireland\b|\baustralia\b|\bnew zealand\b"
    r"|\bsingapore\b|\bbrazil\b|\bmexico\b|\bargentina\b|\bcolombia\b|\bafrica\b|\bjapan\b")


def _blob(job: dict) -> str:
    """Full text for multi-word/unambiguous markers (incl. description head)."""
    return " ".join([
        job.get("title", "") or "",
        job.get("location", "") or "",
        (job.get("description", "") or "")[:1500],
    ]).lower()


def _loc_blob(job: dict) -> str:
    """Title+location only — the safe scope for short ambiguous tokens (us/uk)."""
    return " ".join([job.get("title", "") or "", job.get("location", "") or ""]).lower()


def classify_regions(job: dict) -> list[str]:
    """Deterministic region set; [] if no rule fires (caller may LLM-fallback)."""
    blob = _blob(job)
    if _WORLDWIDE_RE.search(blob):
        return list(REGION_CODES)
    loc = _loc_blob(job)
    found: set[str] = set()
    if _NA_RE.search(blob):
        found.update(("US", "CA"))
    if _US_STRONG_RE.search(blob) or _US_LOC_RE.search(loc):
        found.add("US")
    if _CA_RE.search(blob):
        found.add("CA")
    if _UK_STRONG_RE.search(blob) or _UK_LOC_RE.search(loc):
        found.add("UK")
    if _OTHER_RE.search(blob):
        found.add("OTHER")
    # multi-eligibility: keep every region that fired.
    return [c for c in REGION_CODES if c in found]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_regions.py -q`
Expected: PASS (12 passed, output pristine). The `_US_LOC_RE`/`_loc_blob` split is what keeps `test_join_us_in_description_does_not_match_us` green — if it fails, the bare `us` token is leaking into the description scan.

- [ ] **Step 5: Commit**

```bash
git add backend/applier/regions.py backend/tests/test_regions.py
git commit -m "feat(regions): deterministic US/CA/UK/OTHER classifier"
```

---

### Task 2: LLM fallback + source-tagged classifier

**Files:**
- Modify: `backend/applier/regions.py`
- Test: `backend/tests/test_regions.py`

**Interfaces:**
- Consumes: `classify_regions` (Task 1), `backend.config.settings` (`llm_url/llm_key/llm_model`).
- Produces: `classify_with_source(job: dict, use_llm: bool = True) -> tuple[list[str], str]` where source ∈ `{"rule","llm","unknown"}`; `_llm_regions(job: dict) -> list[str]` (internal).

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_regions.py

def test_source_rule_when_deterministic(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(regions, "_llm_regions", lambda job: called.__setitem__("n", called["n"] + 1) or [])
    out, src = regions.classify_with_source(_j(location="Remote - US"))
    assert out == ["US"] and src == "rule"
    assert called["n"] == 0  # LLM never called when rules resolve


def test_source_llm_on_residue(monkeypatch):
    monkeypatch.setattr(regions, "_llm_regions", lambda job: ["US", "CA"])
    out, src = regions.classify_with_source(_j(location="Remote"))
    assert out == ["US", "CA"] and src == "llm"


def test_source_unknown_when_llm_empty(monkeypatch):
    monkeypatch.setattr(regions, "_llm_regions", lambda job: [])
    out, src = regions.classify_with_source(_j(location="Remote"))
    assert out == [] and src == "unknown"


def test_llm_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(regions, "_llm_regions", lambda job: ["US"])
    out, src = regions.classify_with_source(_j(location="Remote"), use_llm=False)
    assert out == [] and src == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_regions.py -q`
Expected: FAIL (AttributeError: module has no attribute `classify_with_source`).

- [ ] **Step 3: Write minimal implementation**

Append to `backend/applier/regions.py`:

```python
def _llm_regions(job: dict) -> list[str]:
    """Ask the local Sumrak LLM to pick regions; returns a subset of REGION_CODES ([] on any failure)."""
    from backend.config import settings
    if not settings.llm_url:
        return []
    import json as _json
    import httpx
    prompt = (
        "You classify which regions a REMOTE job is open to. "
        "Reply with ONLY a JSON array using codes from US, CA, UK, OTHER "
        "(OTHER = open to some region but not US/CA/UK). Empty array if unclear.\n\n"
        f"Title: {job.get('title','')}\nLocation: {job.get('location','')}\n"
        f"Description: {(job.get('description','') or '')[:1200]}"
    )
    try:
        r = httpx.post(
            f"{settings.llm_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
            json={"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.0, "max_tokens": 40, "stream": False},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[.*?\]", text, re.S)
        raw = _json.loads(m.group(0)) if m else []
        return [c for c in REGION_CODES if c in {str(x).upper() for x in raw}]
    except Exception:
        return []


def classify_with_source(job: dict, use_llm: bool = True) -> tuple[list[str], str]:
    """Deterministic first; LLM only on the residue. Returns (regions, source)."""
    rule = classify_regions(job)
    if rule:
        return rule, "rule"
    if use_llm:
        llm = _llm_regions(job)
        if llm:
            return llm, "llm"
    return [], "unknown"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_regions.py -q`
Expected: PASS (16 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/applier/regions.py backend/tests/test_regions.py
git commit -m "feat(regions): LLM fallback on residue + source tagging"
```

---

### Task 3: `job_catalog` gets `regions` column + helpers

**Files:**
- Modify: `backend/tools/catalog_db.py` (`ensure_schema` ~73-100; `_UP_COLS` ~103; `upsert_jobs` ~103-130; add `set_regions`, `rows_missing_regions`, extend `counts`)

**Interfaces:**
- Produces: column `regions text[]` on `job_catalog`; `set_regions(ats, company_key, external_id, regions: list[str], source: str) -> int`; `rows_missing_regions(limit: int = 0) -> list[dict]`; `counts()` gains `by_region: dict[str,int]`.

- [ ] **Step 1: Add the column in `ensure_schema()`**

In `ensure_schema()`, after the `CREATE TABLE`/index block, add idempotent ALTERs and a GIN index:

```python
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS regions TEXT[]")
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS region_source TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_regions ON job_catalog USING GIN (regions)")
```

- [ ] **Step 2: Thread `regions`/`region_source` through `upsert_jobs`**

Update `_UP_COLS` to include the two new columns (append them so ordinal positions of existing cols are unchanged), and add them to the `ON CONFLICT ... DO UPDATE SET`:

```python
_UP_COLS = ("ats", "company_key", "company", "external_id", "title", "location",
            "department", "workplace", "is_remote", "url", "description",
            "description_html", "questions", "q_count", "regions", "region_source")
_QI = _UP_COLS.index("questions")
```

In the `DO UPDATE SET` string append (regions only overwrites when the incoming value is non-null):

```python
           "regions=COALESCE(EXCLUDED.regions, job_catalog.regions), "
           "region_source=COALESCE(EXCLUDED.region_source, job_catalog.region_source), "
```

(psycopg2 adapts a Python `list[str]` to a Postgres `text[]` automatically; rows without a `regions` key get `None` via `r.get(c)` and are left untouched by the COALESCE.)

- [ ] **Step 3: Add `set_regions`, `rows_missing_regions`, extend `counts`**

```python
def set_regions(ats: str, company_key: str, external_id: str, regions: list, source: str) -> int:
    with _cur(False) as cur:
        cur.execute("UPDATE job_catalog SET regions=%s, region_source=%s "
                    "WHERE ats=%s AND company_key=%s AND external_id=%s",
                    (regions, source, ats, company_key, external_id))
        return cur.rowcount


def rows_missing_regions(limit: int = 0) -> list:
    sql = ("SELECT ats, company_key, external_id, title, location, description "
           "FROM job_catalog WHERE regions IS NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]
```

Extend `counts()` to add a per-region breakdown:

```python
def counts() -> dict:
    with _cur(False) as cur:
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE is_remote), "
                    "COUNT(*) FILTER (WHERE q_count > 0) FROM job_catalog")
        t, rem, wq = cur.fetchone()
        by_region = {}
        for code in ("US", "CA", "UK", "OTHER"):
            cur.execute("SELECT COUNT(*) FROM job_catalog WHERE %s = ANY(regions)", (code,))
            by_region[code] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM job_catalog WHERE regions IS NULL")
        untagged = cur.fetchone()[0]
    return {"total": t, "remote": rem, "with_questions": wq,
            "by_region": by_region, "untagged": untagged}
```

- [ ] **Step 4: Smoke-verify against the live CRM DB**

Run (from repo root; there is no DB unit-test harness — this is the verification step):

```bash
PYTHONPATH=. python3 -c "
from backend.tools import catalog_db as db
db.ensure_schema()
with db._cur(False) as cur:
    cur.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='job_catalog' AND column_name IN ('regions','region_source')\")
    cols = sorted(r[0] for r in cur.fetchall())
print('cols:', cols)
assert cols == ['region_source','regions'], cols
n = db.set_regions('ashby','salmon-group', db.rows_missing_regions(1)[0]['external_id'] if db.rows_missing_regions(1) else 'x', ['US'], 'rule')
print('set_regions rowcount:', n)
print('counts:', db.counts())
print('OK')
"
```
Expected: prints `cols: ['region_source', 'regions']`, a `counts:` dict containing `by_region` and `untagged` keys, and `OK`.

- [ ] **Step 5: Commit**

```bash
git add backend/tools/catalog_db.py
git commit -m "feat(catalog): regions text[] column + set_regions/rows_missing_regions/region counts"
```

---

### Task 4: Classify on collect + backfill existing rows

**Files:**
- Modify: `backend/tools/catalog_collector.py` (`collect_board` ~63-84; add `backfill_regions`; CLI in `__main__`)

**Interfaces:**
- Consumes: `regions.classify_with_source` (Task 2), `catalog_db.rows_missing_regions`/`set_regions` (Task 3).
- Produces: every collected row carries `regions`/`region_source`; `backfill_regions(limit: int = 0, use_llm: bool = True) -> dict` (returns `catalog_db.counts()`-style summary).

- [ ] **Step 1: Classify inside `collect_board`**

At the top of `catalog_collector.py` add `from backend.applier.regions import classify_with_source`. In `collect_board`, right before `rows.append({...})`, build the dict into a variable `row`, then:

```python
        row["regions"], row["region_source"] = classify_with_source(row, use_llm=False)
        rows.append(row)
```
(Collection stays fast/offline — deterministic only during bulk collect; the LLM residue is handled by the backfill pass.)

- [ ] **Step 2: Add `backfill_regions`**

```python
def backfill_regions(limit: int = 0, use_llm: bool = True) -> dict:
    """Classify every row whose regions IS NULL. Deterministic first; LLM on residue."""
    from backend.applier.regions import classify_with_source
    catalog_db.ensure_schema()
    rows = catalog_db.rows_missing_regions(limit)
    done = 0
    for r in rows:
        regs, src = classify_with_source(r, use_llm=use_llm)
        # store [] (not NULL) so a resolved-empty row isn't re-processed forever
        catalog_db.set_regions(r["ats"], r["company_key"], r["external_id"], regs, src)
        done += 1
    return {"processed": done, **catalog_db.counts()}
```

- [ ] **Step 3: CLI hook**

In the `__main__` argparse block add a `--backfill-regions` flag that calls `backfill_regions(use_llm=not args.no_llm)` and prints the summary. Add a `--no-llm` flag (default off).

- [ ] **Step 4: Verify — deterministic backfill over the live catalog**

```bash
PYTHONPATH=. python3 -m backend.tools.catalog_collector --backfill-regions --no-llm
```
Expected: prints `processed: <~2845>` and a `by_region` breakdown with non-zero US/CA/UK/OTHER and a shrunken `untagged`. Then run once WITH the LLM on the residue:
```bash
PYTHONPATH=. python3 -m backend.tools.catalog_collector --backfill-regions
```
Expected: `untagged` drops further; `processed` = the remaining NULL rows only.

- [ ] **Step 5: Commit**

```bash
git add backend/tools/catalog_collector.py
git commit -m "feat(collector): tag regions on collect + backfill_regions pass"
```

---

### Task 5: Add Workable to the catalog collector

**Files:**
- Modify: `backend/applier/ats_boards.py` (`SUPPORTED` ~37; add `_fetch_workable`; register in `_FETCHERS`)
- Test: `backend/tests/test_ats_boards_workable.py`

**Interfaces:**
- Produces: `fetch_board("workable", slug)` returns the standard normalized shape (`title, applyUrl, jobUrl, workplaceType, isRemote, location, department, descriptionHtml, descriptionPlain`).

- [ ] **Step 1: Write the failing test** (normalizer only; monkeypatch httpx)

```python
# backend/tests/test_ats_boards_workable.py
"""Workable normalizer — no live network (httpx.get monkeypatched)."""
from backend.applier import ats_boards


class _Resp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


def test_workable_normalizes(monkeypatch):
    payload = {"jobs": [{
        "title": "Support Engineer", "shortcode": "ABC123",
        "url": "https://apply.workable.com/acme/j/ABC123/",
        "location": {"location_str": "Remote (US)"}, "telecommuting": True,
        "department": "Support", "description": "<p>Do support</p>",
    }]}
    monkeypatch.setattr(ats_boards.httpx, "get", lambda *a, **k: _Resp(payload))
    jobs = ats_boards.fetch_board("workable", "acme")
    assert jobs and jobs[0]["title"] == "Support Engineer"
    assert jobs[0]["isRemote"] is True
    assert jobs[0]["applyUrl"].endswith("/ABC123/")
    assert "US" in jobs[0]["location"]
    assert "workable" in ats_boards.SUPPORTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_ats_boards_workable.py -q`
Expected: FAIL (`fetch_board` raises ValueError unsupported ATS `workable`).

- [ ] **Step 3: Implement `_fetch_workable` + register**

Add `import httpx` if not present, add `"workable"` to `SUPPORTED`, and:

```python
def _fetch_workable(slug: str) -> list[dict]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location") or {}
        loc_str = loc.get("location_str") or loc.get("city") or ""
        remote = bool(j.get("telecommuting"))
        out.append({
            "title": j.get("title", ""),
            "applyUrl": j.get("url", ""),
            "jobUrl": j.get("url", ""),
            "workplaceType": "Remote" if remote else "OnSite",
            "isRemote": remote,
            "location": loc_str,
            "department": j.get("department", "") or "",
            "descriptionHtml": j.get("description", "") or "",
            "descriptionPlain": _html_to_text(j.get("description", "") or ""),
        })
    return out
```
Register it in `_FETCHERS` alongside the others (`"workable": _fetch_workable`). Reuse the module's existing HTML→text helper; if none exists, add a trivial `_html_to_text` using `re.sub(r"<[^>]+>", " ", h)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest backend/tests/test_ats_boards_workable.py -q`
Expected: PASS.

- [ ] **Step 5: Let the collector use Workable slugs**

In `catalog_collector._slugs()`, ensure Workable slugs from `discovered_slugs.json` are included now that `ats_boards.SUPPORTED` contains `workable` (the existing `SUPPORTED` gate will now admit them automatically — verify by running a dry collect for one known Workable slug and confirming rows appear). Then a live smoke:
```bash
PYTHONPATH=. python3 -m backend.tools.catalog_collector --ats workable --limit 1
PYTHONPATH=. python3 -c "from backend.tools import catalog_db as db; print({k:v for k,v in db.counts().items()})"
```
Expected: Workable rows present; `total` increased; new rows are remote-only.

- [ ] **Step 6: Commit**

```bash
git add backend/applier/ats_boards.py backend/tests/test_ats_boards_workable.py backend/tools/catalog_collector.py
git commit -m "feat(ats): add Workable board fetcher into the catalog"
```

---

### Task 6: Workable question scraping (best-effort)

**Files:**
- Modify: `backend/tools/catalog_forms.py` (`_apply_url` ~42-48; `run` default `ats_list`)

**Interfaces:**
- Consumes: `catalog_db.rows_missing_questions("workable")`, `catalog_db.set_questions`.
- Produces: `catalog_forms.run(ats_list=("ashby","lever","workable"), ...)` also scrapes Workable apply forms.

- [ ] **Step 1: Add the Workable case to `_apply_url`**

```python
    if ats == "workable":
        # Workable apply form is the posting URL itself (…/j/<code>/); no suffix.
        return url
```
Add `"workable"` to the default `ats_list` in `run()`.

- [ ] **Step 2: Verify on one live Workable posting**

```bash
PYTHONPATH=. python3 -c "
from backend.tools import catalog_forms, catalog_db
n = catalog_forms.run(ats_list=('workable',), limit=1)
print('scraped:', n)
"
```
Expected: prints `scraped: 0` or `1` without crashing (best-effort; Workable widgets may be iframe'd — if 0 across several, that's an accepted limitation per spec, not a failure). Confirm no exception.

- [ ] **Step 3: Commit**

```bash
git add backend/tools/catalog_forms.py
git commit -m "feat(catalog-forms): best-effort Workable question scraping"
```

---

### Task 7: Automate the collector + region backfill via cron

**Files:**
- Modify: user crontab (JOBFINDER section); Create: `docs/superpowers/plans/phase1-cron.txt` (the exact lines, committed for the record)

**Interfaces:** none (ops).

- [ ] **Step 1: Write the cron lines to a committed reference file**

Create `docs/superpowers/plans/phase1-cron.txt`:

```cron
# JobFinder (Alan) — catalog collect + region tag, daily 05:30
30 5 * * * cd /home/projects/JOBFINDER && /usr/bin/python3 -m backend.tools.catalog_collector --with-questions >> /home/projects/JOBFINDER/logs/catalog.log 2>&1
# region backfill (LLM residue), daily 06:15 (after collect)
15 6 * * * cd /home/projects/JOBFINDER && /usr/bin/python3 -m backend.tools.catalog_collector --backfill-regions >> /home/projects/JOBFINDER/logs/regions.log 2>&1
# company discovery refresh, weekly Sun 07:00 (modest, not aggressive)
0 7 * * 0 cd /home/projects/JOBFINDER && /usr/bin/python3 -m backend.applier.discovery >> /home/projects/JOBFINDER/logs/discovery.log 2>&1
```
(Verify the exact `catalog_collector` CLI flag names match what `__main__` accepts; adjust the file to the real flags before installing.)

- [ ] **Step 2: Install into crontab (append, don't replace)**

```bash
crontab -l > /tmp/ct.now
cat docs/superpowers/plans/phase1-cron.txt >> /tmp/ct.now
crontab /tmp/ct.now
crontab -l | grep -c catalog_collector   # expect 2
```

- [ ] **Step 3: Verify a manual run of each line succeeds**

```bash
mkdir -p /home/projects/JOBFINDER/logs
cd /home/projects/JOBFINDER && timeout 1800 /usr/bin/python3 -m backend.tools.catalog_collector --with-questions | tail -5
```
Expected: completes; `catalog_db.counts()` total ≥ prior. Confirm `logs/` written.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/phase1-cron.txt
git commit -m "chore(cron): schedule catalog collect + region backfill + weekly discovery"
```

---

## Self-Review

**Spec coverage (Workstream 1):** single store ✓ (Tasks 3–5 keep `job_catalog` the one store); remote hard gate ✓ (collect_board already filters; Task 5 keeps it); all questions ✓ (existing GH/Ashby/Lever + Task 6 Workable); country tags ✓ (Tasks 1–4); automation ✓ (Task 7); staleness — `last_seen` is maintained by `upsert_jobs`; the UI hiding by `last_seen` cutoff is a Phase 2 (UI) task and is intentionally deferred there, not lost.

**Placeholder scan:** every step carries real code or a real command. Task 6/Workable question scraping is explicitly "best-effort" per spec (not a placeholder — an accepted-limitation outcome is defined).

**Type consistency:** `classify_regions -> list[str]`; `classify_with_source -> (list[str], str)`; `set_regions(..., regions: list, source: str)`; `_UP_COLS` includes `regions, region_source`; `counts()` adds `by_region`/`untagged`. Names match across Tasks 1–4.

**Deferred to later phases (by design):** Workstream 2 (3-tab UI reading `job_catalog` + staleness hiding), Workstream 3 (delete dead modules), Workstream 4 (extension re-point + nginx + one-click). Each gets its own plan after Phase 1 lands.
