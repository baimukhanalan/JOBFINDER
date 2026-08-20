# Design — Remote-jobs data pipeline, single-source CRM, and one-click Mac apply

Date: 2026-08-20
Repo: `baimukhanalan/JOBFINDER` (`/home/projects/JOBFINDER`), branch `feat/jobs-feed-mobile`
Status: proposed (awaiting review)

## North star
Alan opens the CRM in Chrome on his MacBook, sees remote jobs (segmented by country he can
apply to), clicks **one button** on a job, the application page opens in his own browser and
auto-fills from pre-prepared answers, he reviews the flagged answers and clicks **Submit** — a
real application sent from his real machine/IP. No server-side auto-submit.

## Current state (ground truth, not assumptions)
- **Collector already works.** `tools/catalog_collector.py` pulls Ashby/Greenhouse/Lever across
  ~157 companies (`data/targets.json` + `data/discovered_slugs.json`, Salmon = `salmon-group`
  included) into Postgres `jobfinder_crm.job_catalog`.
- **Live data now: 2845 jobs, all remote, 2837 with questions.** Remote filtering + question
  collection already run at scale.
- **JD text** via API for all ATS. **Questions**: Greenhouse via API (`?questions=true`);
  Ashby/Lever via sync Playwright (`tools/catalog_forms.py`) into `job_catalog.questions` (JSONB
  `{label, required, type}` + `q_count`). Workable: not collected.
- **Remote/geo logic** is rich and deterministic (`applier/boards.py`: `is_remote`,
  `is_us_eligible`, `is_geo_eligible`, `is_language_eligible`; `applier/geo.py` pay-region). No LLM.
- **Store schema** `job_catalog`: `id, ats, company_key, company, external_id, title, location,
  department, workplace, is_remote, url, description, description_html, questions, q_count,
  first_seen, last_seen`; unique `(ats, company_key, external_id)`; `psycopg2` pool via `CRM_PG_DSN`
  (mirrors `tools/mail_db.py`).
- **Three overlapping job UIs today**: `/roles` (live Ashby over targets.json), `/jobs`
  (`jobs_feed` wrapping roles), `/catalog` (the DB store). This is the duplication to collapse.
- **Extension exists** (`extension/`, Apply Assist, MV3) with the one-click `apply_url#aa=<profile>:<jid>`
  flow. But `background.js` `DEFAULT_SERVER = http://127.0.0.1:8089` (the RETIRED lowercase port) and
  `jobs.systeam.kz` has basic-auth on **everything** — the extension endpoints are not `auth_basic off`.
- **Dead code confirmed**: `tools/mail_sink.py` + `mail_sink --poll` cron + `uploads/inbox/mail_sink.json`
  (nothing reads it), `dashboard_app._start_mail_poller()` (never invoked), `tools/mail_dashboard.py`
  (zero importers). `tools/salmon_autofill.py` is a fictional-candidate test harness.

## Scope
IN: (1) canonical remote-jobs store with country segmentation + complete questions; (2) collapse the
CRM to 3 tabs; (3) delete verified-dead modules; (4) one-click apply on Alan's Mac via the extension.
OUT (by construction): Workday / iCIMS / LinkedIn / Indeed (account-gated / ban-risk); server-side
auto-submit (deliberately removed, stays removed); chasing new company coverage beyond current
sources — priority is remote-only + single source + all questions, not raw volume.

---

## Workstream 1 — Data: single source, remote-only, all questions, country tags

### 1a. Data model (extend `job_catalog`, non-breaking)
- `regions text[]` — subset of `{US, CA, UK, OTHER}` the job is open to (multi-eligibility). GIN index
  for `'US' = ANY(regions)`.
- `region_source text` — `rule` | `llm` | `unknown` (auditability).
- Backfill both over the existing 2845 rows; new rows classified on upsert.

### 1b. Region classifier `applier/regions.py` (deterministic-first)
- `classify_regions(job) -> set[str]` over `location` + `workplace` + description head, reusing the
  existing keyword/regex infra:
  - US markers (United States, US, state names, "Remote – US") → `{US}`
  - Canada → `{CA}`; "North America" / "US & Canada" → `{US, CA}`; UK / United Kingdom → `{UK}`
  - EMEA / EU / LatAm / APAC / India / etc. (not US/CA/UK) → `{OTHER}`
  - Worldwide / Anywhere / Global / unrestricted → `{US, CA, UK, OTHER}`
  - Ambiguous/empty → **LLM fallback only on the residue** (local Sumrak), cached by normalized
    location string; unresolved → `region_source='unknown'`, empty `regions` (never guessed).
- Rationale for not pure-LLM: deterministic rules resolve almost all of 2845 for free and repeatably;
  LLM is slow/nondeterministic → fallback only.

### 1c. Collector: remote-only, all questions, Workable, automation
- Compute `regions` at upsert time inside `catalog_collector`.
- **Remote is the hard gate everywhere** (already true; make it explicit/enforced in the collector).
- **Complete the questions**: every catalog job gets a question pass (GH API + Ashby/Lever/Workable
  Playwright). Add **Workable** to `ats_boards.SUPPORTED` + a Workable question path in `catalog_forms`
  so its already-discovered remote jobs + questions land in the one store.
- **Cron** (JOBFINDER style, correct cwd, `sg mail` where Maildir isn't needed it's plain):
  - daily — full `catalog_collector` (collect + classify + refresh `last_seen`)
  - weekly — `discovery` refresh (kept modest; not aggressive expansion)
  - one-time backfill (regions + missing questions), then folded into daily.
- **Staleness**: a `last_seen`-cutoff view (unseen N days ⇒ closed) so UIs hide stale jobs; no deletes.

### 1d. Single source
`job_catalog` becomes the only job store any surface reads. `/roles` and `/jobs`' live-fetch paths are
retired in favor of reading the store (see Workstream 2).

---

## Workstream 2 — CRM: 3 tabs
Keep exactly: **Jobs** · **Инбокс** · **Очередь**.
- **Jobs** = merged `/jobs` + `/catalog`, reading `job_catalog`, remote-only, filterable by region
  (US/CA/UK/Other) and search. One route, one store.
- **Инбокс** = `/mail` (unchanged; candidate list folds in here — no separate Candidates tab).
- **Очередь** = `/queue` (review/apply queue where the one-click fill lives).
- Delete tabs/routes: **Компании** (`/roles`), **Каталог** (merged into Jobs), **Кандидаты**
  (`/mail/candidates` as a top-level tab). Nav bar reduced to the 3.

---

## Workstream 3 — Delete unused modules (verify each before deleting)
Candidates for removal, each confirmed unimported/uninvoked before `git rm`:
- `tools/mail_sink.py` + the `mail_sink --poll` cron line + `uploads/inbox/mail_sink.json`
- `dashboard_app._start_mail_poller()` (dead function)
- `tools/mail_dashboard.py` (zero importers)
- `tools/salmon_autofill.py` (fictional-candidate test harness — not prod)
- `tools/online_roles.py` if it's only referenced by the retired live paths after Workstream 2
Method: `grep -rn` the symbol across `backend/` (+ cron/pm2/nginx) → remove only if no live reference.
Removing `/roles`/`/jobs` live code is part of Workstream 2, not a blind delete.

---

## Workstream 4 — One-click apply on Alan's Mac (Apply Assist extension)
Flow: Alan views the CRM in his Mac Chrome (extension installed) → clicks **Fill** on a job → the
`apply_url#aa=<profile>:<jid>` link opens the ATS page in a new tab of HIS Chrome → the content
script fetches the pre-collected pack (`/job_pack`, `/assist`, `/resume_file`) → fills instantly →
floating review panel highlights `[review]` answers → Alan checks them and clicks the ATS **Submit**.
Concrete tasks:
- **Re-point** `background.js` `DEFAULT_SERVER` → `https://jobs.systeam.kz`; keep `aa_base` override.
- **Token sync**: `ASSIST_TOKEN` in `background.js` must equal `backend/.assist_token` (verify/rotate
  both together; never commit the token into a tracked file that ships publicly — background.js is
  gitignored-live, `background.example.js` is the template).
- **nginx**: add `auth_basic off` + `X-Assist-Token`-guarded `location`s for `/assist /draft
  /profile_form /job_pack /resume_file /mark_ext /health` on `jobs.systeam.kz` (mirror the retired
  lowercase vhost). Everything else stays basic-auth.
- **Pre-prepared answers**: `/job_pack` / `/assist` serve answers built from `job_catalog.questions`
  (from Workstream 1) so the fill is instant (no live LLM at click-time). `[review]` contract preserved.
- **Dashboard**: the Jobs/Queue "Fill" affordance emits the `#aa=` one-click link (target=_blank).
- **Verify** end-to-end in a real Mac Chrome on a live posting: opens, fills, resume attaches, nothing
  is auto-submitted; Alan submits manually.

---

## Data flow
ATS APIs / boards → `catalog_collector` (remote gate + `classify_regions` + question pass) →
`job_catalog` (single store) → Jobs tab (browse/filter) + `/job_pack`+`/assist` (answers) →
extension on Alan's Mac (fill) → Alan clicks Submit → `/mark_ext` records submit into `status.json` →
recruiter reply → Maildir → `mail_indexer` → `mail_index` → Инбокс.

## Error handling / edge cases
- Classifier unresolved ⇒ `unknown`, surfaced (not silently dropped); job still browsable, just
  region-untagged.
- Question collection failure for a job ⇒ existing `questions` never wiped (COALESCE upsert); retried
  next run.
- Extension can't reach a token-gated endpoint (401/basic-auth) ⇒ explicit console/panel error, not a
  silent blank form.
- Stale/closed posting (link 404/expired) ⇒ flagged by `last_seen` cutoff; extension shows "no form".

## Testing
- Unit: `classify_regions` on fixtures (US-only, US+CA, UK, EMEA, Worldwide, empty) — deterministic.
- Backfill dry-run stat: counts per region after classify (eyeball correctness).
- Question-completeness stat: `with_questions / total` per ATS before vs after Workable + backfill.
- Extension: one-click E2E on a real Mac Chrome (manual, live posting) + the existing fixture tests.

## Rollout order (each phase → its own implementation plan)
1. **Workstream 1** — foundation (classifier, regions column+backfill, remote gate, all-questions +
   Workable, cron, staleness). Ship the single source.
2. **Workstream 2** — 3-tab CRM reading the single source.
3. **Workstream 3** — delete verified-dead modules.
4. **Workstream 4** — one-click Mac apply (extension re-point + nginx + answer wiring + E2E).

## Open questions / risks
- Multi-eligibility precision depends on messy ATS location strings; the LLM residue path bounds this.
- Workable question extraction via Playwright may be brittle (widget iframes) — de-scope to "best
  effort" if flaky; Workable is additive, not blocking.
- Real submits from Alan's Mac assume a real, reality-gate-passing profile is selected in the
  extension popup (not the synthetic `michael`).
