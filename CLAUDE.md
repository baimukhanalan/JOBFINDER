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
- `15 4` `prefill_retention --days 20` — delete `uploads/prefill/<cand>/<jobid>/` artifacts (tailored
  résumé PDF, screenshots, report.json) older than 20 days so résumés don't pile up forever →
  `logs/prefill_retention.log`. **Added 2026-08-24; this ONE line correctly `cd`s into the LOWERCASE
  `/home/projects/jobfinder`** — NB every OTHER cron line still `cd`s into the uppercase
  `/home/projects/JOBFINDER` husk (likely broken, pre-existing — not fixed here).

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
  **`/catalog` UI (`tools/catalog_ui.py`) is decluttered (2026-08-23):** the top is ONE wide search
  input that **live-filters as you type** (250ms debounce → `GET /catalog/more?q=&region=&offset=`
  replaces `#catlist`; Enter is intercepted, no reload) — on mobile the shared Gmail top pill
  (`.gm-search input`) IS that search (the page's own `.cat-q` is hidden ≤760px), on desktop `#catq`
  is. Everything secondary — region chips, «Подать на все», the proxy pool upload — lives in ONE
  collapsed **«Фильтры»** sheet (`#catSettings`, toggled by `toggleFilters()`); the button shows the
  active region as a tag. Cards: the TITLE is the link to the posting (no separate «Открыть»), a
  compact text **М/Ж** sex toggle (`pickSex()` sets `.cat-sex-b.on`; `fillJob` reads it) + one primary
  «Заполнить»; Описание/Вопросы are scroll-capped `<details>`; **no decorative emoji** anywhere. Live
  search + pagination share `curQ`/`region` state in the one scroll IIFE — don't reintroduce a second
  search box.
- **Candidate applications page (`/candidates/{id}`, added 2026-08-24).** Shows where the bot applied +
  the tailored résumé PDF it used, per candidate. `tools/candidate_apps.py` reads
  `uploads/prefill/<id>/<jobid>/report.json` (company, job_title, apply_url) + the candidate-level
  `status.json` (submitted?). The Кандидаты roster row (`mailcrm_ui.render_candidate_rows`) gets a
  **📄 N** chip (an `onclick` span inside the row `<a>`, `stopPropagation` so it doesn't also open the
  inbox) → this page. Résumé download **reuses the existing `/resume/{jobid}?profile=<id>`** route
  (serves `uploads/prefill/<id>/<jobid>/resume.pdf`). Those artifacts are pruned at 20 days by the
  `prefill_retention` cron (above), so this page naturally only lists the last 20 days.
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
- **Auto-submit end-to-end status by ATS (verified live 2026-08-23, ground truth = the ATS
  "Thank you for applying" email in the persona's `@takhet.com` box).** GREENHOUSE ✅ (cresta, axon,
  gofasti) and ASHBY ✅ (elevenlabs, Salmon TPM, Salmon Flutter) submit and confirm end-to-end (incl.
  the emailed-security-code step — those two use an EMAIL CODE, not a live captcha, which is why they
  complete). **The dividing line is the final anti-bot step: GH/Ashby = email code (passable) vs
  Lever/Workable = a LIVE human captcha (not passable from a datacenter IP).** LEVER ⛔ hCaptcha-gated
  (`filler.click_submit` returns False → `click_failed`; `Current location` geocode also dead here).
  WORKABLE ⛔ **also live-captcha-gated** — verified on zyte 7383: after a complete fill the submit shows
  Cloudflare **Turnstile "Verify you are human"** and hangs on "Submitting…", no confirmation email.
  Turnstile fires on datacenter-IP risk score, so it will appear on most Workable submits from here.
  So Lever + Workable both need the human to solve the captcha in noVNC — same physical limit.
  **Workable FILL is fixed though** (the co-pilot fills the whole form so the human only solves the
  captcha): 1×1 aria-hidden dummy inputs skipped; apply page keyed by `/j/<shortcode>/` (company-slug
  redirect isn't a false page_drift); YES/NO `[role=radio]` screeners via `fill_role_radio_known`;
  `Availability to start` combobox picks the soonest/"Immediate" option (prose answer matches none);
  demographic combobox (pronouns/gender) declined via a Workable `input[role=combobox]` pass in
  `fill_demographics_decline` (closes the listbox + removes Workable's `data-ui=backdrop` between boxes,
  which else intercepts the next combobox click). Workable renders each label in a
  `<span id="<randomid>_label">` (aria-labelledby; the input has no visible label) and selects as
  readonly `input[role=combobox]`, so `extract_form_fields` sees only text/file inputs — ALL Workable
  selects/screeners are filled by `dropdowns.py`, not the analyzer. A REQUIRED demographic with NO
  decline option (zyte 'Preferred pronouns' = She/He/They only) stays blank by policy (never claim a
  protected characteristic) → that form can't be fully auto-filled, by design. Choice-engine edits are
  in the delicate SHARED `dropdowns.py`: verify via `dry_run` screenshot (no submit) + run
  `test_dropdowns.py`/`test_choices.py` so the working GH/Ashby fills don't regress. NOTE the analyzer's
  `unfilled` has blind spots (hidden required fields, demographics), so a required field the fill layer
  misses is a false "complete" → auto-submit clicks → the ATS rejects "This field is required" (caught
  as `submit_result.blocked`, diagnosable, never a false "submitted"). Also: `_identity_choice` answers
  eligibility ("EU nationality / authorized in <country>?") YES **country-blind**, so a persona whose
  nationality ≠ the job's country (e.g. a Kazakhstan default persona on a Lisbon job whose catalog
  `location` is empty → region OTHER → KZ) makes a FALSE "YES" — pre-existing, unrelated to fill.
- **Auto-submit: the co-pilot clicks Submit — but ONLY when safe (enabled 2026-08-23 by explicit
  owner request; GATED 2026-08-23 after a live 3-job smoke).** After the one-click fill, `copilot.py`'s
  `/load` calls `_click_submit_after_fill(page, result, expected_url, profile, shot_dir)` which presses
  the ATS Submit button (strategy `submit_selector`, else `analyzer.find_submit_button`, via
  `filler.click_submit`) — reversing the original human-submit-only design (commit `a8ab56e`) — so
  **`/catalog` «Заполнить» and the co-pilot `Fill →` both auto-submit**, incl. the SYNTHETIC demo
  persona to real ATS. Owner accepted the datacenter-IP / takhet.com spam-ban risk `a8ab56e` avoided.
  **It REFUSES to click in four cases (each returns a `submit_result` reason, never raises):**
  (1) `incomplete` — any unfilled required field (`result["unfilled"]`, e.g. Lever 'Current location'
  the dead datacenter geocode can't set) → leave for the human; (2) `needs_review` — any
  `result["review_items"]` (the `[review]` safety contract: a synth persona's behavioral/unbacked
  answers must be human-seen before going live); (3) `page_drift`/`preempted` — the SHARED single
  co-pilot browser drifted to a different company/job or another run took ownership (`_same_apply_page`
  / `_apply_identity` compare host+company; greenhouse embed and board collapse to one identity). The
  live smoke caught a cresta fill whose page had been raced to a salmon-group Ashby page — the guard
  now aborts instead of submitting the wrong form. On a real click it captures **post-submit evidence**
  (`_submit_evidence`: `after_submit.png` screenshot + `confirmed`/`blocked` via `looks_submitted` /
  `_SUBMIT_BLOCK_RE`) so a silent 'no confirmation' is diagnosable. **KNOWN LIMIT:** from a datacenter
  IP the final submit is often anti-bot/captcha-gated, so a click ≠ a completed application (the smoke
  got 0/3 confirmations); `status.json` only reaches `submitted` on a REAL confirmation the watch sees,
  never on the click. `_watch_submit` also bails the moment `_S["current"]`/`owner` shows another job
  took the shared browser (so a stale watch never marks the wrong job or clicks a stranger's page). To
  turn it OFF again, no-op `_click_submit_after_fill`. Offline gates test: scratchpad `test_gate.py`.
  It also finishes a
  **Greenhouse-style emailed-security-code step**: `_watch_submit` fills the code from the candidate's
  own mailbox AND now clicks that step's confirm/submit button (`_click_code_confirm`) to finalize —
  it still never touches a captcha (a captcha-gated step just waits for the human). NOTE: the
  **extension** path and the strategy layer are UNCHANGED — the extension's `installSubmitWatch` still
  only *records* the confirmation into `status.json` (never clicks), and
  `strategies/base`/`prefill_application` still expose no submit path (regression test
  `test_no_auto_submit_path` still passes). `CONFIRM_RE` lives in `extension/content.js` with a copy in
  `copilot.py` that must stay in sync.
- **Comboboxes are dropdowns.py's, NOT the analyzer's — never text-fill them.** A Greenhouse
  `.select__container` input AND a Workable readonly `input[role=combobox]` (aria-haspopup=listbox)
  are select widgets, not open-text fields. `analyzer.py`'s field-extraction SKIPS both (a text `fill`
  types prose the widget can't accept; a readonly combobox even type-ahead-jumps to the FIRST/WORST
  option). `dropdowns.fill_comboboxes_known` owns them: on a **readonly** combobox it does NOT type
  (typing filters the fixed list) — it opens and matches an option. Language-proficiency scales
  (Workable 'English Level', options are sentences like '…speak fluently'/'Native', not CEFR) are
  picked by CANDIDATE LEVEL (`_lang_option_rank`/`_cand_lang_rank`), NEVER the geo `opts[0]` fallback
  (opts[0] is 'cannot speak'). A Workable combobox has a SEPARATE `[name=…]` backing input the analyzer
  sees as an empty text field → `base.prefill` re-reads live values when building `unfilled` so a
  combobox-filled field isn't falsely reported unfilled. `dismiss_overlays()` runs in `base.prefill`
  (cookie/consent backdrop otherwise intercepts the combobox click). An Ashby **geo** typeahead that
  returns 0 options for an over-qualified query ('Denver, Colorado, United States.') retries progressively
  shorter queries (`_geo_shorten`: strip punctuation → drop trailing comma-segments → city) until the
  geocode resolves. Tests: `test_dropdowns.py`.
- **Deterministic Yes/No screeners in `services/tailor/choices.py::deterministic_choices`.** Three
  families answer WITHOUT the LLM (so they don't fall through to `choose_options` and get left blank):
  prior-employer ('worked with us before?' → **No**, unbacked/review), OFAC sanctioned-territory
  ('located in Cuba/Iran/Russia/…?' → **No**, unbacked/review), and English-Yes/No ('master English at
  C1?' → **Yes** only when `facts.english_level` BACKS the asked CEFR level, else defer — never
  over-claim). `_english_yesno_pick` must run BEFORE `_language_pick` (a Yes/No pair sent to
  `_language_pick` defaults to `len(opts)//2` = 'No'). `_prior_employer_pick`/`_sanctions_pick` only
  fire on a clean 2-option Yes/No pair. **SMS/text-contact consent** (`_consent_pick`) → the affirmative
  option, detected by LABEL (`communicationConsent`), option SENTENCES, OR the `given`/`notGiven` VALUE
  pair — the live analyzer extracts a radio's `value` attr as its option "text", so a sentence-only match
  misses; unbacked → review. Tests: `test_choices.py`.
- **Lever geocode 'Current location' is dead on this datacenter IP — `is_lever_loc` returns False.**
  Verified: after a pick OR a direct JS-set, Lever's React clears BOTH the visible input and the hidden
  `selectedLocation` on a LATER async reconcile (>1.2s — past any settle a single `fill_field` can wait),
  so there is NO reliable in-call signal the value stuck. `filler.py`'s `is_lever_loc` branch still polls
  `.dropdown-location` for a real suggestion and JS-sets both as best-effort, but **always returns False**
  (incl. on an exception — an hCaptcha overlay often blocks the click; do NOT fall through to the plain
  `.fill()` below, which types text React discards and would return True over a blank field). False means
  `fill_form` reports it: `fill_form` now returns `(success, fail, failed_required)` and `base.prefill`
  appends `failed_required` to `unfilled`, so a REQUIRED location surfaces for the human (who fills it on
  a real IP where the geocode resolves) instead of a phantom "complete". OPTIONAL locations aren't in
  `failed_required`, so they stay silently blank. Lever marks the location `required` in the DOM even with
  no visible asterisk, so most Lever forms carry this one human task — that is honest, not a regression.
- **Structured Greenhouse Employment/Education work-history block.** Newer Greenhouse hosted forms render
  an EMPLOYMENT block (Company name, Title, Start/End date month+year, a 'Current role' checkbox) and an
  EDUCATION block (School, Discipline, Degree). `materialize_prefill` supplies these as exact-label known
  answers from the persona's résumé `experience[0]`/`education[0]`: Company name, Title, Start date
  year (parsed from `dates`), Start date month='January' (résumé dates are year-only); a 'Present'/'Current'
  role sets `Current role='Yes'` (ticks the checkbox, which WAIVES the End date) else End date year/month
  is parsed. **The availability `_start_date` rule is negative-lookahead-guarded** (`start.?date(?!\s+(month|year))`)
  so it no longer types `available_start` ('Immediately' → garbage 'Imme') into a work-history 'Start date
  year' field. `_known_answer_exact` also collapses a doubled label ('Title Title' → 'Title') so a
  Greenhouse label duplicated across sibling nodes still matches its single-word drafted key. `_SCRAPE_V`
  bumped so cached drafts regenerate. (School/Discipline are slow remote-search react-select typeaheads —
  optional, best-effort.)
- **Known-answer replay is EXACT-match-first (`analyzer._best_known_answer`).** `analyze_page`'s known-
  answers loop used to bind the FIRST fuzzy `_known_answer_matches` hit (word-overlap ≥ max(2, sig//2)),
  so two Yes/No screeners sharing only generic words — "authorized to work…for our company?" (Yes) and
  "…require **sponsorship**…to work legally for our Company?" (No) — cross-bound: the authorized 'Yes' was
  applied to the SPONSORSHIP radio = a self-disqualifying answer. Now an EXACT normalized-label match
  (`_known_answer_exact`) is preferred over any fuzzy hit and over dict order; the sponsorship field's own
  full-text key binds it to 'No'. Fuzzy remains the fallback for short Ashby title labels.
- **Generic input placeholders are stripped from `display_text` (`analyzer._GENERIC_PLACEHOLDER`).** Lever
  puts "Type your response" on every custom field; `display_parts` used to fold it into the human-question
  text, and its word "your" pushed a field over the ≥2-significant-word fuzzy threshold onto "What is
  **your** nationality?" (earlier in dict order than the field's real key) — cross-binding the COUNTRY
  "Kazakhstan" into a Lever "earliest date you would be available to start? (DD/MM/YYYY)" text field
  (verified live on binance; the DRAFT was the correct date `24/08/2026`, the LIVE fill mis-bound it).
  `analyze_page` now skips a part matching `_GENERIC_PLACEHOLDER` ("Type your response", "Select…",
  "Choose…", "Please specify", em/hyphen) when building `display_text`, so the field's real nearbyText
  exact-matches its own date key. `match_text` still keeps the placeholder for rule signals. Do NOT widen
  the regex to swallow real questions ("Select the option that best applies" must NOT match). Tests:
  `test_analyzer_rules.py::test_date_field_*` / `test_generic_placeholder_matches_noise_not_questions`.
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
- **Proxy pool → rotating egress IP per application (`tools/proxy_pool.py`).** `/catalog` has a
  🛡️ **Прокси (N)** panel: paste a list (`host:port:user:pass` / `user:pass@host:port` / `scheme://…` /
  bare `host:port`) → `POST /proxies/upload` parses + VALIDATES each (http/https via a real IP-echo
  fetch THROUGH the proxy recording the egress IP; socks5 only TCP-probed) and keeps only the live
  ones. Pool + round-robin cursor persist in **`backend/data/proxies.json` (gitignored — creds)**.
  `next_proxy()` hands them out round-robin. `dashboard._do_fill` picks one per fill and passes
  `proxy_server/username/password` to co-pilot `/load`; `copilot._use_proxy_context` builds a FRESH
  browser context (⇒ new IP) per proxy — so every submit (single **and** the bulk «Подать на все»
  queue) rotates IPs. The persistent headful browser is now launched with
  `proxy={"server":"per-context"}` (verified: `browser:true` still — contexts w/o a proxy stay DIRECT,
  so an empty pool = today's behavior). **Limits:** socks5 with auth won't route in the browser
  (Playwright can't authenticate socks5); the fast preview `/goto` stays on the direct IP (throwaway —
  only the real fill+submit is proxied); a fresh context per job briefly flickers the noVNC window.
  Parse/rotation are unit-tested (`tests/test_proxy_pool.py`, no network).
- **Bulk auto-apply: «Подать на все» (`/catalog`).** One SEQUENTIAL server-side queue (co-pilot has
  ONE shared browser) over every **greenhouse+ashby** job — Lever/Workable are skipped (live captcha
  would stall it). Reuses `_do_fill` per job (so it auto-submits AND rotates proxy IPs). Endpoints:
  `POST /catalog/fill_all` (start), `GET /catalog/fill_all_status` (poll), `POST /catalog/fill_all_stop`
  (halts after the current job). Live counters in `dashboard._FILL_ALL` (in-memory only). **Audit trail
  (`tools/bulk_log.py`, gitignored `logs/`):** `bulk_apply.log` (append-only, a line per job + a FINISHED
  summary) + `bulk_apply_last.json` (full last run, rewritten each job so it survives a restart). Served
  by `GET /catalog/fill_all_report` (JSON) + `GET /catalog/fill_all_log` (download); the Фильтры sheet
  shows the last-run summary + a «Скачать лог» link. **Honest counters:** `filled_ok` = co-pilot filled
  (HTTP 200), NOT submitted; `submit_clicked` = Submit pressed; `submit_confirmed` = confirmation seen
  right after the click (best-effort — captcha-gated ATS confirm later/never; the real confirmation still
  only lands in per-job `status.json`).
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
- **Demographic self-ID is gated by LABEL *and* OPTIONS (`catalog_drafts._is_demographic`).** A
  synthetic persona must NEVER claim a protected characteristic. `_DEMOGRAPHIC` (dropdowns/analyzer) only
  tests the LABEL, but Ashby renders "Which of the following communities do you belong to?" whose label
  has NO demographic keyword — the signal ('Person with disability', 'Neurodivergent', 'Veteran',
  'Refugee') lives only in the OPTIONS, so the LLM ideal-fill was checking "Person with disability" (a
  false disability self-ID on a live EEO field). `generate_draft` now routes any question matching
  `_DEMOGRAPHIC_LABEL_RE` (label) OR ≥2 `_DEMOGRAPHIC_OPTION_RE` option hits to human/blank in BOTH modes
  — before the video/file/select branches. The ≥2-option threshold keeps a lone 'Prefer not to answer' on
  a real screener (hear-about) from tripping it; `age` matches only 'your/current age'/'age range'/'DOB'
  (NOT "18 years of age"). `_SCRAPE_V` bumped so cached drafts regenerate without the false claims.
  A REQUIRED demographic that offers an explicit non-disclosure option is answered with it by
  `dropdowns.fill_demographics_decline` ('Prefer not to answer' / 'Decline to self-identify' / 'I do not
  want to answer') — NOT left blank — so the form submits without ever claiming a characteristic; one with
  no decline option stays blank (human sets it). **`_DECLINE_RE` must match "do not **want** to answer"**,
  not only "wish to": Greenhouse's Disability Status decline is "I do not want to answer", so a want-only
  phrasing was missed and a REQUIRED Disability field blocked the whole submit ("This field is required")
  while Gender/Hispanic/Veteran (which say "Decline to self-identify") declined fine — verified live on axon
  (now submits end-to-end, "Thank you for applying to Axon!").
  All three `_DEMOGRAPHIC` regexes (dropdowns / analyzer `_skip` / catalog_drafts) use `rac(e|ial)` +
  `ethnic` (bare `race`/`ethnicit` missed 'racial'/'ethnic', leaking a race question as a false "1 left
  for the human"), and the analyzer location rule is `\bcity\b` (bare `city` substring-matched inside
  'Ethni**city**' → a demographic resolved to `_location`). **`latin[ox]?\b` is guarded with
  `(?!\s*americ)` in ALL FOUR demographic regexes** (dropdowns `_DEMOGRAPHIC`, analyzer `_skip`,
  catalog_drafts `_DEMOGRAPHIC_LABEL_RE` + `_DEMOGRAPHIC_OPTION_RE`): bare "Latin" matched the GEOGRAPHY
  "**Latin** American country", so a required screener "Are you based in a Latin American country?" was
  mis-gated as a Latino/Latinx EEO field and left blank → Greenhouse rejected it — verified live on gofasti
  (now answered "No" for a non-LatAm persona and submits end-to-end, "Thank you for applying to GoFasti").
  Latino/Latinx/Latine self-ID still gates. Tests: `test_catalog_drafts.py::test_latin_american_country_is_not_demographic`
  / `test_real_latino_demographic_still_gated`, `test_dropdowns.py::test_decline_re_matches_do_not_want_to_answer`. In the ETALON `ideal_fill` path,
  `_AFFIRM_NO_RE` answers "contractual obligations/agreements/commitments that would **impede/interfere
  with** your ability to join" → **No** (was a self-disqualifying Yes).
- **Long Ashby Yes/No labels: replay is prefix-tolerant (`strategies/base.prefill` custom-widget loop).**
  The scraper truncates a question label to `[:300]` (drafted-answer key) while `dropdowns.shape_button_group`
  truncates the live harvested label to `[:200]`; a >200-char label (e.g. 1Password's work-auth screener)
  makes the two keys unequal, so the exact `known_clean.get(qt)` replay missed and a REQUIRED Yes/No fell
  to the LLM and was left blank. The section-3 replay now falls back to a prefix match (`k.startswith(qt)
  or qt.startswith(k)`) so the already-drafted 'Yes' is recovered.
- **`analyze_page` dedups `display_parts` on a NORMALIZED key.** A leaky whitespace/case-variant part
  (Greenhouse aria-label ' Twitter' vs label 'Twitter') otherwise yields display_text 'Twitter Twitter',
  which the exact known-answer match can't hit (`_clean_text`'s A+A collapse needs ≥4 words), so the field
  stayed blank. Strip+lower each part before the dedup so variants collapse.
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
    **first country named in the location text** (by position, not `_LOC_COUNTRY` order). Keep this rule.
    **LatAm-exclusive employers** (GoFasti et al. auto-reject anyone not resident in Latin America — verified
    live: a KZ persona got "As GoFasti hires talent exclusively based in Latin America, we won't move
    forward"): when the location names NO concrete country but the JD text signals LatAm residence
    (`synth_persona._LATAM_RESIDENCE_RE` — "based in Latin America" / "TopTalent from LatAm" / "from LatAm" /
    "Latin American country"; a mere *market* mention like "customers across Latin America" does NOT trip it),
    `_country_of` returns a real LatAm country (`catalog_drafts.LATAM_COUNTRIES` = Mexico/Colombia/Argentina/
    Chile/Peru/Brazil; Spanish `_LATAM_ES_NAMES` bank for all but Brazil's Portuguese `_LATAM_PT_NAMES`, plus
    cities + gendered-agnostic surnames). The "Are you based in a Latin American country?" YES/NO screener is
    then answered deterministically from the persona's country by `catalog_drafts._identity_choice`
    (`_LATAM_RESIDENCE_Q_RE`, added to the eligibility gate): country ∈ `LATAM_COUNTRIES` → Yes, else No — so
    it never falls to the LLM and a non-LatAm persona still truthfully answers No. Real apply is unaffected
    (GoFasti is `regions=['OTHER']` → `pick_candidate` returns None → skipped). Tests:
    `test_synth_persona.py::test_requires_latam_signal` / `test_latam_persona_is_internally_consistent`,
    `test_catalog_drafts.py::test_latam_residence_screener_*`. **The NAME is OURS, not the LLM's** —
    `_pick_name` chooses first+last from the large per-country `_NAMES` banks, AVOIDING names in a rolling
    recent-history file (`backend/data/demo_used_names.json`, gitignored, atomic write, best-effort; `synth_persona`
    records each pick immediately). The local LLM is stateless (no generation history) so left to itself it
    collapsed to the same few "favourite" names per country on every fill — the name is now pinned into the LLM
    prompt and force-set on BOTH the LLM and fallback persona (`raw["full_name"] = name`), so the LLM only authors
    the résumé/city/experience tailored to the JD, never the identity. Do NOT revert to letting the LLM pick the
    name. Résumé + street address are LLM-authored with a template fallback so a click
    never fails; email `first.last<NUM>@takhet.com` (numeric suffix = a UNIQUE mailbox even when two personas share a common name; real /setup onboarding keeps clean `first.last@`), persona flagged `is_synthetic`, phone a reserved-
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
