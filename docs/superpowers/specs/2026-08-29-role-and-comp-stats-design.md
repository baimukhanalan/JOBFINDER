# Role classification + posted-comp on `/stats`

**Date:** 2026-08-29
**Status:** approved (design), executing

## Goal

Add a new "По ролям" (by-role) cut to the `/stats` dashboard answering the
owner's question **«на какие роли нас приглашали больше всего»** (which role
categories invite us to interview most), and show the **posted pay range** per
role. Classification + comp must also happen **automatically at collect time**
for every future job, immediately, with no external API call and no stack/brand
strings — exactly the pattern `applier/regions.py` already uses for regions.

## Data reality (measured 2026-08-29)

- `job_catalog`: **6243 live rows**, **4415 distinct live titles** (4721 incl.
  dead). `department` populated on **98.7%** of rows — a strong second signal.
- Salary: **62%** of live descriptions carry `$`+number; **~3974 (64%)** have an
  extractable comp signal. Dominant format: `Base Pay/Salary/Compensation Range
  $110,000 — $160,000 USD`; also `... per year: $195,000 - 255,000`.
- **False positives exist** ("$781M in funding", "deal size $100k+") → extract
  only the range anchored to a comp keyword, never any `$` figure.
- Postings almost always state a **base range** (US pay-transparency law). True
  total comp (base+bonus+equity) as one number is rare; `OTE` = 0 matches. So we
  store and label the **posted range honestly** ("вилка по вакансии"), not
  "total compensation". Where a posting states OTE/total explicitly, capture it.
- Mail outcomes today: interview=196, offer=1, action_needed=51, rejection=353,
  ack=3046. "Приглашали" = **interview + offer** (offer implies an invite);
  `action_needed` shown separately as a softer signal.

## Definitions

- **Role category** — a property of the job **title** (+ department). Stable per
  title, so classified on the ~4.7k distinct titles and mapped back to all rows.
  Taxonomy (13, last = catch-all): `Sales / GTM`, `Customer Support & Success`,
  `Engineering`, `Data & ML`, `Product`, `Design`, `Marketing & Comms`,
  `People & Recruiting`, `Finance & Accounting`, `Operations`,
  `Legal & Compliance`, `Executive / Leadership`, `Other`.
  The agent fleet may propose additions/renames; owner approves before backfill.
- **Posted comp** — a property of the **row** (same title at two companies pays
  differently), so extracted per row on the ~3974 salary-bearing rows. Stored as
  `comp_min`, `comp_max` (annualized USD integers), `comp_currency`. Hourly rates
  are annualized (`× 2080`); a single stated figure sets both min=max.
- **`*_source`** ∈ `{rule, llm, agent, unknown}` — provenance, like `region_source`.

## Architecture (mirrors `regions.py`)

### New deterministic modules (immediate, offline, no brand strings)
- `backend/applier/role_category.py`
  - `classify_role(title, department) -> (category, source)` — ordered keyword
    rules over the normalized title, department as tiebreaker. `source="rule"` on
    a hit, `("Other","unknown")` on miss (left for an optional LLM residue pass).
  - Rules are **tuned to reproduce the agent gold set** (target ≥90% agreement on
    the labeled titles); the gold set is the regression fixture.
- `backend/applier/comp_extract.py`
  - `extract_comp(description) -> {min,max,currency,source}` — anchored regex:
    a comp keyword (`salary|base pay|compensation|pay range|per year`) within a
    small window of a `$X[-–—]$Y` (or `$X`) pattern; annualize hourly; reject
    figures adjacent to funding/deal words (`funding|raised|valuation|deal size|
    revenue|ARR|market`). `source="rule"` / `("",unknown)` on miss.
  - Also tuned against the agent gold set (numeric tolerance ±$2k on the midpoint).

Both are **pure functions, unit-tested with no network** (`test_role_category.py`,
`test_comp_extract.py`), gold set checked in under `backend/tests/data/`.

### Schema (`catalog_db.ensure_schema`, `ALTER TABLE ADD COLUMN IF NOT EXISTS`)
`role_category TEXT`, `role_source TEXT`, `comp_min INT`, `comp_max INT`,
`comp_currency TEXT`, `comp_source TEXT`. Add to `_UP_COLS` + the upsert
`DO UPDATE SET` with `COALESCE(EXCLUDED.x, job_catalog.x)` so a later
description-only refresh never wipes them. New `catalog_db` helpers mirroring
`set_regions`/`rows_missing_regions`: `set_role`, `set_comp`, `rows_missing_role`,
`rows_missing_comp`.

### Collect-time wiring (`catalog_collector.collect_board`)
Right after `classify_with_source(row)`: `row["role_category"], row["role_source"]
= classify_role(row["title"], row.get("department"))` and
`row.update(extract_comp(row["description"]))`. Plus backfill entrypoints
`--backfill-roles` / `--backfill-comp` (deterministic-first, LLM residue optional)
mirroring `--backfill-regions`, for the nightly cron / one-shot repair.

### One-time backfill — the sonnet agent fleet (authorized)
A `Workflow` fan-out (Phase 2 below). Agents produce the **authoritative gold
labels** used to (a) backfill the DB directly and (b) tune the deterministic
modules so future collect-time classification matches. Agents get
`title + department + description snippet (+ the salary window)`; they never see
or emit brand/stack strings. Batches sized so the fleet is ~150-200 agents total
("hundreds" authorized), plus a taxonomy-design pass and validation critics.

### `/stats` changes
- `stats.py::_catalog_dims` — add `role_category, comp_min, comp_max` to the
  `SELECT ... WHERE id = ANY(...)`; return `jid_role`, `jid_comp`.
- `stats.py::compute_stats` — build a `roles` list parallel to `companies`/`ats`:
  per category `{applied, invited (=interview+offer), action_needed, invite_rate,
  comp_median}` where `comp_median` = median of range-midpoints over that
  category's applied jobs that have comp; plus a global `comp` block
  (median/p25/p75 over all applied jobs with comp, and coverage count).
- `stats_ui.py` — a new full-width `st-card` **«По ролям»** inserted after the
  «По компаниям» card: a ranked `_hbars` "на какие роли приглашали больше всего"
  (value=invited, secondary=applied) on top, then a compact sortable table
  (category · подано · приглашали · % · медианная вилка $) reusing the
  `_company_table`/`stTbl` sort JS. One KPI tile "медиана вилки по подачам" added
  to the KPI row. All labels Russian, neutral, no stack strings.

## Non-goals / YAGNI
- No seniority cut (owner picked role-only for now; the module leaves room).
- No live per-request extraction on `/stats` (comp/role are precomputed columns).
- No change to the apply/fill engine — this is analytics + a collect-time tag.

## Phased plan
1. **Core (TDD, code):** schema columns + `role_category.py` + `comp_extract.py`
   with unit tests (initial ruleset, gold fixture stubbed).
2. **Fleet (Workflow):** classify all distinct titles + extract comp on all
   salary-bearing rows → gold JSONL; taxonomy-refine pass → owner approves any
   additions. Write gold fixture. Re-tune deterministic modules to the gold set.
3. **Backfill:** load gold into `job_catalog` (role by title-map, comp by row).
4. **Wire collector** (collect-time + `--backfill-roles/--backfill-comp`) + cron
   note in `CLAUDE.md`.
5. **Stats:** `stats.py` + `stats_ui.py` + `test_stats.py` extension.
6. **Verify:** restart `jobfinder-alan-dash`, load `/stats?refresh=1`, screenshot;
   run the full test suite; commit + push; update `CLAUDE.md`.

## Testing
- Pure unit: `test_role_category.py`, `test_comp_extract.py` (gold agreement +
  false-positive rejection + hourly annualization).
- `test_stats.py` extended: role aggregation dedups by jobid on the same 4908
  base; invited=interview+offer; comp_median ignores rows without comp.
- Live: `/stats?refresh=1` renders the new card with real numbers.
