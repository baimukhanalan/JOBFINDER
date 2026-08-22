# JobFinder

> **Repo / GitHub — read first.** This project lives at **`baimukhanalan/JOBFINDER`**
> (`https://github.com/baimukhanalan/JOBFINDER`). The account is **`baimukhanalan`**, NOT `Abekemyn`
> like the other `/home/projects/*` repos. The PAT is embedded in the `origin` remote URL (see
> `git remote -v`), so `git push` works as-is — do NOT paste the token into any tracked file.
> **Convention: after any nontrivial change, `git add -A && git commit && git push` to this remote
> straight away — don't let work sit uncommitted — and edit THIS `CLAUDE.md` in the same commit
> whenever deploy / behavior / gotchas change.** **Live deploy dir is now lowercase
> `/home/projects/jobfinder`** (the working checkout of `baimukhanalan/JOBFINDER`, branch
> `feat/jobs-feed-mobile`), NOT uppercase `/home/projects/JOBFINDER`. Repointed 2026-08-21 — see Deploy.
>
> **This IS the live project** (`jobs.systeam.kz`, pm2 `jobfinder-alan-*`, Postgres `jobfinder_crm`).
> **Dir map — there are THREE `jobfinder*` dirs, don't get them confused:**
> - `/home/projects/jobfinder` (lowercase) — **THE LIVE CODE**, checkout of `baimukhanalan/JOBFINDER`.
>   pm2 `jobfinder-alan-*` run from here since 2026-08-21. Has `backend/ .env data/ uploads/ vnc/`.
> - `/home/projects/JOBFINDER` (uppercase) — **dead husk**: only a stale `uploads/`, no `backend/`.
>   pm2 pointed here until 2026-08-21 (served stale in-memory code + read an empty `uploads/`). Ignore.
> - `/home/projects/jobfinder.archive-2026-08-20-2158` — the OLDER retired `Abekemyn/jobfinder`
>   `michael` project (`jobfinder.systeam.kz`, `:8089`, display `:99`). Fully dead — ignore any such ref.

Semi-automatic job-application engine for remote US/CA roles, plus a **self-hosted candidate-mail CRM**.
It collects openings (company roster + live ATS APIs), tailors a résumé per JD, **pre-fills** the ATS
form (never submits), a human reviews + clicks Submit; recruiter replies land in a Gmail-style inbox
per candidate. Surfaces: a server-rendered dashboard whose **sidebar nav is 3 tabs** (Кандидаты
`/mail/candidates`, Каталог `/catalog`, Заявки `/apply` — reduced from 6 on 2026-08-21). The general
Инбокс `/mail` is still reachable via the in-page mail tab strip; `/queue` (per-candidate review queue,
the target of the Заявки cards → `/queue?profile=…`) and `/setup` (onboard a real candidate) still exist
but are drill-downs, not nav items. The duplicate **`/jobs` (Вакансии) and `/roles` (Компании) routes were
DELETED** 2026-08-21 (with `backend/tools/jobs_feed.py`) — `/catalog` is the single job-browsing surface.
`backend/tools/roles_dashboard.py` was **kept** (its `_is_remote` / `_workplace` helpers are imported by
`applier`/`apply_bot` + `online_roles`), it's just no longer routed. Plus a one-click browser extension
and a headful "co-pilot" Chromium watched over noVNC. Nav lives in
`backend/tools/mailcrm_ui.py`: `_sidebar` is the desktop left rail; on mobile (≤760px) it's
hidden and `_topbar`+`_drawer` render a **Gmail-style search pill** (☰ opens a slide-out menu with
the 3 tabs · a context-aware search box whose route/placeholder come from `_SEARCH_CTX` · a
decorative JF avatar). The per-screen inline search toolbars (`.toolbar`/`.cat-search`) are hidden on
mobile since the pill covers search there.

Stack: Python 3.12 · FastAPI · Playwright · **psycopg2 / Postgres `jobfinder_crm`** · Dovecot+Postfix
Maildir · aiogram (Telegram) · python-jobspy. Résumé tailoring + answer drafting use the **local Sumrak
LLM** (`llm_*` in `config.py`), not the Anthropic API (key is empty).

## Deploy (pm2, NOT systemd) — `jobs.systeam.kz`
All four pm2 services launch via `cd /home/projects/jobfinder && sg mail -c '…'` (`sg mail` is
mandatory — `programmer` isn't in the `mail` group interactively, and the Maildir is `vmail:mail 2770`).
**Their `pm_cwd` MUST be `/home/projects/jobfinder`** (lowercase — the live checkout). They used to
point at uppercase `/home/projects/JOBFINDER`, but that dir was emptied to just `uploads/`, so the
running processes served stale in-memory code and read an empty `uploads/`; on **2026-08-21** all four
were recreated with `cwd=/home/projects/jobfinder` (`pm2 delete <name>` → `pm2 start /usr/bin/bash
--name <name> --cwd /home/projects/jobfinder -- -c "cd /home/projects/jobfinder && exec sg mail -c
'<cmd>'"`, then `pm2 save`). Recreate with the correct `cwd`, never just `pm2 save`.

- `jobfinder-alan-dash` → `uvicorn backend.dashboard_app:app` on **127.0.0.1:8099** — the live CRM +
  review app. `/` redirects to `/mail/candidates`. No in-app auth (nginx basic-auth sits in front).
- `jobfinder-mail-indexer` → `python -m backend.tools.mail_indexer` — an **inotify watcher** over
  `/var/mail/vhosts/takhet.com/*` (~1000 dirs) that upserts into Postgres `jobfinder_crm.mail_index`.
  **This is what feeds `/mail`** (logs `watching N dirs …` on start).
- `jobfinder-alan-copilot` → `uvicorn backend.copilot:app` on **127.0.0.1:8102** with `DISPLAY=:98` —
  the headful Chromium the bot pre-fills; a **separate app** from the dashboard (own routes
  `/ /load /release /mark_submitted /state`).
- `jobfinder-alan-display` → `vnc/copilot_display.sh`: Xvfb **`:98`** + x11vnc **`:5901`** + websockify
  noVNC **`:6090`**.
- nginx vhost `jobs.systeam.kz` (certbot SSL): `/` → 8099, `/copilot/` → 8102, `/vnc/` → 6090.
  **basic-auth on EVERYTHING** (`/etc/nginx/.htpasswd-jobs`, user `job2026`, realm "JobFinder CRM").
  NOTE: unlike the retired lowercase deploy, the extension endpoints are NOT `auth_basic off` here, so
  the cross-origin one-click extension flow won't reach them through nginx without basic-auth.
- `backend.main:app` (legacy jobs API + APScheduler over the OLD `jobfinder` Postgres) exists but is
  **not deployed / not in nginx.** Treat it as the legacy/DB layer.

## Data stores
- **`jobfinder_crm` Postgres** — the isolated CRM DB, reached via `CRM_PG_DSN` in `.env` with
  **psycopg2 (sync, pooled)**. Explicitly NOT the shared `amasmail` MySQL and NOT the legacy `jobfinder`
  Postgres. Tables: `mail_index` (fed live by `mail_indexer`), `job_catalog` (fed **nightly by cron**,
  `tools/catalog_collector.py` over Ashby/Greenhouse/Lever/**Workable**, remote-only). `job_catalog`
  carries per-job `regions text[]` ∈ `{US,CA,UK,OTHER}` (multi-eligibility) + `region_source`
  (`rule`/`llm`/`unknown`), classified by `applier/regions.py` (deterministic-first, LLM residue);
  `questions JSONB` per job (GH via API, Ashby/Lever/Workable via `tools/catalog_forms.py` Playwright).
  A `dead BOOLEAN` + `dead_reason` blacklist marks postings confirmed gone at the source (e.g. a GH
  job id that now 404s); `catalog_db.mark_dead()` sets it (reversible) and `list_jobs`/`companies`/
  `jobs_for_drafting` all exclude `dead` so a human never opens an apply page that 404s.
- **Per-candidate Maildirs** `/var/mail/vhosts/takhet.com/<local>` (Dovecot/Postfix, `vmail:mail 2770`).
  Sending a reply goes out via Postfix SASL **as that candidate** (`mailcrm.send`, DKIM-signed).
- `uploads/prefill/<profile>/*/report.json` (+ a `status.json` overlay) — the apply review queue
  `/queue` reads these. All of `uploads/` is gitignored PII.

## Secrets & PII (all gitignored)
- `backend/.env` — `CRM_PG_DSN` (live CRM Postgres), `DATABASE_URL` (legacy), `TELEGRAM_BOT_TOKEN/CHAT_ID`,
  `LLM_URL/LLM_KEY/LLM_MODEL`, `ANTHROPIC_API_KEY` (empty), `PROXY_URL`, `DO_API_KEY`, and legacy Mailgun
  keys (`MAIL_PROVIDER`, `MAILGUN_*`, `MAIL_SEND_TRANSPORT`) that are superseded by the self-hosted
  Maildir/Postfix path. `config.py` uses `extra="ignore"` (tolerates leftover keys).
- `backend/.assist_token` — the `X-Assist-Token`; **must match the hardcoded `ASSIST_TOKEN` in
  `extension/background.js`** (both sides checked). Change one → change both.
- Real identity: `extension/profile.js` + `background.js`, `backend/data/{profiles.json,facts/*,etalons/*}`,
  per-candidate `backend/data/mailbox_passwords.json`, and `uploads/`. Only `.example`/`.template`/
  `sample.json` are committed.

## Cron (user `programmer`, JOBFINDER lines)
Mail CRM:
- `*/2` `mail_sink --poll` — **LEGACY / dead-end.** Writes `uploads/inbox/mail_sink.json`, which nothing
  reads; `/mail` is fed by `mail_indexer`→Postgres, not this. Safe to drop (left in place for now).
- `30 4` `mail_retention --days 30` — prune old indexed mail → `logs/retention.log`.
- `*/10` `mail_health check` — indexer/DB health probe → `logs/health.log`.

Job catalog (added 2026-08-20, `docs/superpowers/plans/phase1-cron.txt`):
- `30 5` `catalog_collector` — nightly collect Ashby/GH/Lever/Workable → `job_catalog` (remote-only,
  tags `regions` at collect time, GH questions inline) → `logs/catalog.log`.
- `15 6` `catalog_collector --backfill-regions` — LLM residue pass over `regions IS NULL` rows → `logs/regions.log`.
- `45 6` `catalog_forms --limit 200` — Playwright question scrape for Ashby/Lever/Workable → `logs/forms.log`.
- `0 7 * * 0` `applier.discovery` — weekly company/slug discovery refresh → `logs/discovery.log`.

- **No apply/prefill batch cron in this deploy** — `apply_cli` is a manual tool if used at all (real
  submits are human, from Alan's Mac; see the co-pilot/extension gotchas).

## Apply engine
`applier/runner.prefill_application`: tailor résumé → render PDF → open apply page (reuse saved Playwright
session) → pick ATS strategy → pre-fill every field → screenshot + `report.json`, then **stop**. Per-ATS
strategies in `applier/strategies/` (greenhouse, lever, ashby, workable, workday, icims) +
`base.GenericStrategy` fallback. `applier/` is imported **live** by the dashboard extension endpoints
(`analyzer`, `strategies.base.strip_review`, `profile_validator`) and by `copilot.py` — but **nothing
schedules a batch run in this deploy.** Tailoring (`services/tailor/`) is strictly no-fabrication.
`/queue` still defaults to `profile="michael"`.

## Gotchas
- **Run from the repo ROOT** — imports are absolute `backend.*`. Correct: `uvicorn
  backend.dashboard_app:app` / `python -m backend.tools.mail_indexer` / `python -m backend.apply_cli …`
  from `/home/projects/JOBFINDER`. `cd backend && uvicorn dashboard_app:app` is BROKEN.
- **Mail: one live store, one dead one.** LIVE = `mail_indexer` (inotify) → Postgres `mail_index` →
  `/mail` (`tools/mailcrm.py` reads DB-first with a live-Maildir fallback; `mailcrm_ui.py` renders;
  `mail_db.py` is the psycopg2 layer). DEAD leftovers — do NOT wire them expecting `/mail` to change:
  `mail_sink.py` / `mail_sink --poll` / `mail_sink.json` (Mailgun/Mailpit era),
  `dashboard_app._start_mail_poller()` (defined, never invoked), `tools/mail_dashboard.py` (zero importers).
- **`/catalog` is the ONLY job-browsing surface (DB-backed).** The old network-live `/roles` + `/jobs`
  routes were **removed 2026-08-21** (duplicates that fetched Ashby per request), along with
  `tools/jobs_feed.py`. `/catalog` reads Postgres (`catalog_db.py` → `jobfinder_crm.job_catalog`), no
  per-request egress. `tools/roles_dashboard.py` **stays but is unrouted** — it still holds the
  `_is_remote` / `_workplace` helpers that `apply_bot` + `online_roles` import. `online_roles.py` is NOT
  wired into any dashboard route (apply-side only).
- **Custom-ATS form scrape: WAIT for the React form to render, then RETRY on empty/partial**
  (`tools/catalog_forms.py`). Ashby/Lever/Workable apply pages are React SPAs — `networkidle` fires
  before the fields hydrate, so the old fixed 2.2s sleep-then-extract silently stored partial/empty
  results as complete (e.g. Cohere `q_count=0` while the form has 5 Yes/No screeners; Workable stored
  4 identity fields while the form has 3 language screeners). Fix: `_scrape` calls `_wait_for_fields`
  → `_poll_stable`, polling the field count (via the SAME `_JS` extractor) every 0.5s until it's
  nonzero AND stable across two reads (cap 12s) before reading; then `_scrape_with_retry` → `_retry_loads`
  reloads up to 3× with exponential-ish backoff, keeping the fullest result. **An empty scrape is
  "not scraped" (never persisted as 0 questions) and gets full retries; an identity-only result
  (name/email/phone/resume/linkedin, no `choice/select/textarea/multi_select`) is suspicious and gets
  ONE extra load.** Keep re-scrapes gentle (sequential single browser, or ≤3 parallel — the whole bug
  was hammering under batch concurrency). Do NOT re-add a fixed sleep or a tight retry loop, and do NOT
  touch the extractor's field-recognition (`_JS`) — it works; only the wait/retry/persistence is the fix.
  Pure helpers (`_looks_partial`/`_poll_stable`/`_retry_loads`) are unit-tested in
  `tests/test_catalog_forms_wait.py` (no network).
- **No auto-submit — by design** (commit `a8ab56e`). Real submits from a datacenter IP get
  spam-flagged/banned. Do NOT re-add. **Submit DETECTION is not submission**: the extension's
  `installSubmitWatch` and `copilot.py`'s confirmation poller only *record* a human's submit into
  `status.json`; they never click. `CONFIRM_RE` lives in `extension/content.js` with a copy in
  `copilot.py` that must stay in sync.
- **Profile reality gate** (`applier/profile_validator.py`) blocks prefill/apply affordances for profiles
  with reserved-fictional phones (555-01xx) or placeholder emails — such applications are undeliverable.
  `michael` is the synthetic default persona and is gated. Do NOT bypass the gate; onboard a real person
  in `/setup`.
- **The `[review]` prefix is a hard safety contract.** Behavioral / "describe a time" / specifics answers
  must NEVER reach a live field unflagged — the trust model is "the human only reviews flagged answers."
  The small local model drops the prefix, so `answers.py` re-adds it deterministically (`_NEEDS_REVIEW`);
  `strategies/base.strip_review` strips it before fill and reports the flag. Don't weaken either side.
- **Co-pilot ports are per-deploy** (Xvfb `:98`, x11vnc `5901`, noVNC `6090`, copilot `8102`) — the
  co-pilot is `backend/copilot.py`, a distinct app from the dashboard. **Pick host ports with
  `nginx -T | grep -oE '127.0.0.1:PORT'`, not `grep -r sites-enabled`** (grep doesn't follow the symlinks,
  so it misses most vhosts, and a port with nothing listening can still be claimed by a live vhost).
- **Local LLM default.** `ANTHROPIC_API_KEY` is empty; résumé polish (`--ai`) and answer drafting
  (`--draft`) hit Sumrak at `127.0.0.1:8080/v1` (`config.llm_url/llm_model=sumrak-smart`). Without the
  key, tailoring falls back to the deterministic keyword path.
- **Collector keys postings by the ATS job `id`, NOT the URL tail.** `catalog_collector.collect_board`
  uses `job["id"]` (ashby/lever UUID, greenhouse numeric, workable shortcode — passed through by
  `ats_boards`) as `external_id`, falling back to `_ext_id(url)` only when absent. The upsert key is
  `(ats, company_key, external_id)`, and ashby apply URLs ALL end in `/application`, lever's in `/apply`
  — so the old last-segment `_ext_id` gave every posting on a board the SAME external_id and they
  overwrote each other (ashby collapsed to ~52 total, lever to ~13, e.g. Salmon 1 of 30). Never revert
  to URL-tail ids for ashby/lever. Test: `backend/tests/test_collector_extid.py`.
- **`frontend/` (Vite/React) is not the deployed UI** — the live app is `dashboard_app.py`'s
  server-rendered HTML. The React app is an old job-browser talking to `backend.main` `/api` with no
  inbox/roles; not deployed.
- **Draft-generator eligibility polarity (`catalog_drafts._identity_choice`).** Work-auth questions come
  in two polarities and MUST be told apart: `_SPONSOR_RE` (do you *require/need* sponsorship → **No**)
  vs `_AUTH_RE`/`_WITHOUT_SPON_RE` (are you *authorized … without* sponsorship / can you present proof →
  **Yes**). A positive frame that merely mentions "visa sponsorship" is answered YES and beats
  `_SPONSOR_RE` — do NOT put bare `visa sponsorship` back into `_SPONSOR_RE` (it re-inverts "authorized
  to work without visa sponsorship" to a disqualifying No). **`_WITHOUT_SPON_RE` is checked FIRST in
  `_identity_choice`** and wins even when `_SPONSOR_RE` also fires: "authorized to work in the US **without
  requiring sponsorship now or in the future**?" contains the `sponsorship now or in the future` clause
  `_SPONSOR_RE` matches, so without the positive-frame-first ordering an authorized citizen was inverted to
  a self-disqualifying **No**. Keep the `_WITHOUT_SPON_RE` branch ahead of the `_SPONSOR_RE` branch. Free-text
  auth / "Education History" / no-option `type="select"` typeaheads are answered deterministically from the
  profile/résumé BEFORE the LLM/ideal path (else the model invents a contradicting degree or leaks the state
  code "OR" into an auth textarea via a `\b`-less `state` regex matching "United **State**s"). A "Photo"
  upload is `human`-only — never the résumé PDF (`_PHOTO_RE`); so are **specialized document uploads**
  (`_HUMAN_FILE_RE`: medical report / laudo·CID / passport / national ID / diploma / transcript / reference
  letter) — a résumé attached to a "please attach your medical report" field is a misrepresentation.
  Tests: `backend/tests/test_catalog_drafts.py`.
  `_identity_choice` answers "authorized in <country>?" YES country-blind, but that is now gated
  upstream by `pick_candidate` (below) — a US candidate never reaches a Japan/UK posting, so the
  question isn't asked. (A per-country auth check would still be belt-and-suspenders.)
- **Real apply uses the roster GATE; the ETALON demo invents a fictional persona.** Two distinct paths:
  - **`catalog_drafts.pick_candidate(regions, roster, pools)` — REAL apply only** (`run(ideal=False)`).
    Strict GATE over the real roster: a US person only when `US ∈ regions`, a Canadian only when
    `CA ∈ regions`, else **None** (OTHER/UK-only/untagged) — we never send a US candidate to a foreign
    posting (a "Remote - Japan" role must NOT get a US person claiming a Japanese visa). Do NOT re-add a
    US default.
  - **`backend.tools.synth_persona.synth_persona(job)` — the ETALON demo fill.** The one-click `/catalog`
    button (`ensure_and_wire`, always `ideal=True`) and `run(ideal=True)` invent a FRESH FICTIONAL
    applicant per job — **never a real roster person** ("не трогай моих людей"). The persona's NATIONALITY
    matches the JOB'S COUNTRY (`_country_of`: parse `location` first, else the region tag US→United States /
    CA→Canada / UK→United Kingdom, else Kazakhstan) so work-auth answers are TRUTHFUL and CONSISTENT — a
    Netherlands role gets a Dutch person ("authorized in NL: yes"), a Tbilisi role a Georgian, never a
    Kazakhstani in Almaty claiming US authorization. **Multi-country location** (e.g. Salmon's "Kazakhstan;
    Kyrgyzstan"): if **Kazakhstan** is one of the listed countries it wins (the agency's own market); else the
    **first country named in the location text** (by position, not `_LOC_COUNTRY` order). Keep this rule. LLM-authored (country-appropriate random name + street
    address + a résumé tailored to the JD) with a deterministic name-bank + template fallback so a click
    never fails; email `first.last<NUM>@takhet.com` (numeric suffix = a UNIQUE mailbox even when the LLM repeats a common name; real /setup onboarding keeps clean `first.last@`), persona flagged `is_synthetic`, phone a reserved-
    fiction 555-01xx number. Do NOT wire the demo path back onto the real roster, and do NOT revert persona
    selection to the region TAG only (an untagged "Remote U.S." job must resolve to a US persona via location).
    On the demo fill `ensure_and_wire` also **provisions the persona's mailbox** (best-effort,
    `provision_mailboxes.provision_email`) so its `@takhet.com` address is a LIVE deliverable box — a row in
    the SHARED **MySQL `amasmail.virtual_users`** (NOT Postgres — that is the Dovecot/Postfix account backend;
    `jobfinder_crm` Postgres holds only CRM data) + password in `mailbox_passwords.json` + a Maildir. Idempotent
    (`INSERT IGNORE`), and a provisioning failure never breaks the fill. Real onboarding via `/setup` still does
    NOT auto-provision — run `python -m backend.tools.provision_mailboxes --only <id>` for a real candidate.
    It also **registers the persona in `backend/data/demo_personas.json`** (gitignored) via
    `mailcrm.register_demo_persona`, and `mailcrm.candidates()` merges those in (flagged `is_demo`) so the
    persona's inbox actually SHOWS in the CRM `/mail` — the mail delivers to the Maildir regardless, but
    `mailcrm.build_index_row` returns None (skips) for any address that isn't a candidate, so without the
    registration a recruiter reply to the résumé email lands in the box but never surfaces in the UI. New
    demo personas need the **mail-indexer** to see them: `mail_indexer.build_index_row` re-checks
    `candidates()` per file and `run_once()` reconciles on startup, so a `pm2 restart jobfinder-mail-indexer`
    re-indexes freshly-registered personas (the inotify watcher already `add_tree`s new mailbox dirs).
    The synthetic persona is NOT in the profile store, so `ensure_and_wire` also writes
    `uploads/prefill/<demo_id>/<jobid>/persona.json` (`{profile, facts}`) and the **co-pilot `/load` loads it
    from there** when `get_profile(demo_*)` raises KeyError (else the headful fill crashed with "profile not
    found" and left the stale page up). `Profile` gained `street_address` + `is_synthetic` so `from_dict`
    accepts a synth persona. `_SCRAPE_V` was bumped to 3 so pre-persona.json cached drafts regenerate.
    A demo `demo_*` owner is **preemptible** in `copilot /load` (every demo click is a new persona, so the
    per-owner busy gate would otherwise leave the previous demo's page stuck = the "shows my old requests" bug).
  - **Greenhouse apply URL = the EMBED form, keyed by the gh_jid FROM THE URL.** `materialize_prefill` sends
    greenhouse jobs to `https://boards.greenhouse.io/embed/job_app?for=<company_key>&token=<gh_jid>` — the raw
    hosted form, which (unlike the board URL `job-boards.greenhouse.io/<slug>/jobs/<id>`) never 302-redirects
    to a company careers wrapper (samsara → samsara.com behind a cookie wall = 0 filled). **The `<gh_jid>` is
    extracted from the stored URL (`?gh_jid=<n>` or `/jobs/<n>`), NOT from `external_id`** — `external_id` can
    be a collector sha1 fallback (nebius stored `63f8…` while the real gh_jid `4930024101` is only in the URL),
    and a hash token makes the embed 404 → 0 filled. Verified: nebius 0→23, samsara 0→22, axon still 16.
    **Residual (not all greenhouse fills):** a few companies (e.g. oscar / hioscar.com) embed the form on their
    own domain with no working greenhouse endpoint at all — embed 404s and the board/stored URLs show only a
    listing → 0 filled. Those are genuinely un-auto-fillable; the human applies manually.
- **Region classifier is LOCATION-FIRST (`applier/regions.py`).** `classify_regions` now parses the
  `location` field FIRST (`_regions_from_location`) and, if it names a place, that RESTRICTS eligibility;
  only an uninformative location falls back to full-text signals, then LLM residue. Do NOT let bare
  "global"/"globally"/"worldwide" promote a job to all-four regions — that marketing fluff (in ~55% of
  JDs) was tagging "Remote - Japan"/"Brazil - Remote" as US-eligible; `_WORLDWIDE_RE` is now strict
  ("work from anywhere", "hire anywhere", …). Full US **state names** count as US in a location; 2-letter
  state codes are deliberately NOT used (", CA"/", DE" collide with Canada/Germany ISO) — those fall to
  the LLM. Tests: `backend/tests/test_regions.py`.
