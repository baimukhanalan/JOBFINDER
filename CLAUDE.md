# JobFinder

> **Repo / GitHub — read first.** This project lives at **`baimukhanalan/JOBFINDER`**
> (`https://github.com/baimukhanalan/JOBFINDER`). The account is **`baimukhanalan`**, NOT `Abekemyn`
> like the other `/home/projects/*` repos. The PAT is embedded in the `origin` remote URL (see
> `git remote -v`), so `git push` works as-is — do NOT paste the token into any tracked file.
> **Convention: after any nontrivial change, `git add -A && git commit && git push` to this remote
> straight away — don't let work sit uncommitted — and edit THIS `CLAUDE.md` in the same commit
> whenever deploy / behavior / gotchas change.** **Live deploy dir is now lowercase
> `/home/projects/jobfinder`** (the working checkout of `baimukhanalan/JOBFINDER`, branch **`main`** —
> `feat/jobs-feed-mobile` was fast-forward-merged into `main` and retired 2026-08-24, so `main` is the
> single source of truth; commit straight to `main`), NOT uppercase `/home/projects/JOBFINDER`.
> Repointed 2026-08-21 — see Deploy.
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
per candidate. Surfaces: a server-rendered dashboard whose **sidebar nav is 4 tabs** (Инбокс `/mail`,
Каталог `/catalog`, Заявки `/apply`, Незавершённые `/unfinished` — was reduced from 6 to 3 on 2026-08-21,
then the «Незавершённые» tab was added 2026-08-25 for bulk-apply jobs that need a human to finish the
captcha; `_NAV` in `mailcrm_ui.py`). The general
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
  **This is what feeds `/mail`** (logs `watching N dirs …` on start). **It imports `mailcrm.build_index_row`,
  so ANY change to `mailcrm.py`'s parsing (body/attachment/kind classification) needs `pm2 restart
  jobfinder-mail-indexer` TOO — not just the dashboard** — else new mail is indexed with the OLD logic
  (2026-08-25: the cid-inline-logo `has_att` fix showed a fake paperclip on new "Security code" emails
  because only the dashboard was restarted; DB-stored fields like `has_att`/`snippet`/`kind` on already-
  indexed rows also need a recompute/reindex since the list view reads them, while the thread OPEN view
  re-parses from disk and reflects a mailcrm.py fix immediately).
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
**All cron lines `cd` into the LOWERCASE live `/home/projects/jobfinder` as of 2026-08-24** — before
that every line except `prefill_retention` still pointed at the dead uppercase `/home/projects/JOBFINDER`
husk (no `backend/` → each job silently did nothing: catalog never refreshed, retention never pruned,
health never probed). The uppercase path is fixed for `mail_retention`, `mail_health`, `catalog_collector`
(both), `catalog_forms`, `applier.discovery`. **The ONE deliberate exception is `mail_sink --poll`,
left on the uppercase husk on purpose** (it's a dead-end feature — see below). Verified after the fix:
`mail_health check` → `{"healthy": true}`; a `catalog_collector --ats greenhouse --limit 1` smoke
fetched 207 jobs and upserted (catalog ~6017 rows). If you ever re-add a cron line, `cd` into lowercase.
Mail CRM:
- `*/2` `mail_sink --poll` — **LEGACY / dead-end.** Writes `uploads/inbox/mail_sink.json`, which nothing
  reads; `/mail` is fed by `mail_indexer`→Postgres, not this. Deliberately still `cd`s into the dead
  uppercase husk (so it does nothing) — safe to drop, left in place dead for now.
- `30 4` `mail_retention --days 30` — prune old indexed mail → `logs/retention.log`.
- `*/10` `mail_health check` — indexer/DB health probe → `logs/health.log`.

Job catalog (added 2026-08-20, `docs/superpowers/plans/phase1-cron.txt`):
- `30 5` `catalog_collector` — nightly collect Ashby/GH/Lever/Workable → `job_catalog` (remote-only,
  tags `regions` at collect time, GH questions inline) → `logs/catalog.log`.
- `15 6` `catalog_collector --backfill-regions` — LLM residue pass over `regions IS NULL` rows → `logs/regions.log`.
- `45 6` `catalog_forms --limit 200` — Playwright question scrape for Ashby/Lever/Workable → `logs/forms.log`.
- `0 7 * * 0` `applier.discovery` — weekly company/slug discovery refresh → `logs/discovery.log`.
- `0 6` `mass_hiring --collect` — nightly refresh of the **Mass Hiring** board (`mass_hiring_jobs`,
  the human-apply remote-US mass-hiring surface, SEPARATE from auto-apply `job_catalog`) →
  `logs/masshiring.log`. Added 2026-08-27 (the board had NO cron and was going stale ~30h+; light
  ~40-60s job). `cd`s into the LOWERCASE repo root like every other line.
- `15 4` `prefill_retention --days 20` — delete `uploads/prefill/<cand>/<jobid>/` artifacts (tailored
  résumé PDF, screenshots, report.json) older than 20 days so résumés don't pile up forever →
  `logs/prefill_retention.log`. Added 2026-08-24; `cd`s into the LOWERCASE `/home/projects/jobfinder`
  (as do all other lines now — see the note at the top of this section).

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
- **A `text/plain` MIME part can contain raw HTML — flatten it before render (`mailcrm._parse_full`, 2026-08-26).**
  Some senders (GoFasti/HeyMilo async-interview "magic link" mails) put a literal `<a href="URL">URL</a>`
  INSIDE the `text/plain` alternative. The open-view (`_msg_card`) renders `plain` via `escape()` then
  `_linkify` (`https?://[^\s<]+`); `escape()` turns every `<` into `&lt;`, so the link regex has no `<`
  to stop at and swallows the closing `">…</a>.` into the href → a DEAD link ("не открывается") + visible
  `">…</a>` garbage. (The list snippet was fine because it uses `_message_text`, which strips tags.) Fix:
  `_parse_full` now runs `_html_to_text(plain)` when the plain body matches `_PLAIN_HTML_RE` (a FIXED HTML-tag
  whitelist, so it never mistakes a bare `<someone@example.com>` address for a tag). Tests:
  `test_mailcrm_body.py::test_plain_part_containing_html_is_flattened` / `_plain_email_address_is_not_treated_as_html`.
  NB: this is `_parse_full` (open-view/`get_message`), NOT `build_index_row` (the indexer's path already uses
  `_message_text`), so only the dashboard needs a restart for it.
- **`no_button` on a "fully-filled" GH/Ashby job = a DEAD posting, not a detection bug (2026-08-26).**
  ~11% of bulk GH/Ashby applies failed with `submit_reason=no_button` on forms that looked complete
  (`unfilled=[]`). Live-DOM investigation proved these are postings **GONE at the ATS by bulk-run time**
  (GH embed 404 "Sorry, but we can't find that page."; Ashby "Job not found"; a board URL that
  302s to `/<co>?error=true` company listing) while the nightly collector still has the row `dead=FALSE`.
  A 404 has **0 extractable fields**, so `unfilled=[]` is VACUOUSLY "complete" and `find_submit_button`
  correctly returns None — it's a canary, not the culprit. **Do NOT loosen `find_submit_button`** (searching
  frames / matching bare "Apply" would click the WRONG job on a listing-redirect page; verified no iframe is
  ever involved, and detection works on every LIVE GH/Ashby form). Fix is 4-part: (A) `copilot._click_submit_after_fill`
  returns a distinct `reason="no_form"` when `page_type∈{expired,login_required,captcha}` OR the page has no
  form (`filled` falsy AND `unfilled` empty) — before the find-button step; (B) `analyzer.detect_page_type`
  now catches the real wordings (`find that page`, `job not found`, `the job you requested`) + the
  `error=true` URL → classifies dead GH/Ashby pages as `expired`; (C) `dashboard._fill_one_on_worker`
  calls `catalog_db.mark_dead([(ats,company_key,external_id)], …)` on a `no_form` outcome (the real cure —
  the row leaves `list_jobs`/`fill_all` selection permanently) and `bulk_log.drop_many([jid])` (no human can
  finish a dead posting); (D) `_drain_partition` DROPs a `submit_reason==no_form` ledger entry instead of
  re-running it 3×. Verified: `find_submit_button` returns a real selector on every live form (railway/double/
  cresta/webflow/a live samsara embed); only the churned tokens 404. Tests: `test_analyzer_rules.py` (46) green.
- **CRM Postgres pool must be big enough for the parallel bulk lane (`mail_db.py`, 2026-08-26).**
  `mail_db._get_pool()` was `ThreadedConnectionPool(1, 8)` — only 8 connections. Once «Подать на все»
  fans out ~12 dashboard worker threads alongside the 3 background daemons (proxy revalidator, submit
  reconciler, drain loop) AND the operator browsing `/mail`, `getconn()` raised
  `PoolError("connection pool exhausted")` on nearly every read; `mailcrm.list_messages`/`counts`'
  blanket `except` then dropped to the SLOW live-Maildir disk scan (`_scan_messages`), firing the
  `mail_health` «Mail index unavailable — fell back to a live disk scan» alert repeatedly and making the
  whole dashboard drag (root-caused live: reproduced deterministically — 25 concurrent threads → PoolError
  at maxconn 8; 40 threads pass at 32). Fix: maxconn raised to **32** (`CRM_PG_POOL_MAX` env override;
  Postgres `max_connections=100`, only ~16 in use, ample headroom) and `conn()` now uses `_getconn()`
  which **wait-and-retries up to 5 s** on a momentary spike instead of instantly collapsing to the disk
  scan. minconn stays **1** so each importing PROCESS (indexer, retention cron, every copilot worker)
  holds just one idle connection at rest — only the dashboard bursts toward maxconn. The read-path
  fallbacks now log + Telegram the actual exception (`type: msg`) so a future incident is diagnosable
  (pool-exhausted vs Postgres-down). Do NOT lower maxconn back to 8.
- **Run from the repo ROOT** — imports are absolute `backend.*`. Correct: `uvicorn
  backend.dashboard_app:app` / `python -m backend.tools.mail_indexer` / `python -m backend.apply_cli …`
  from `/home/projects/JOBFINDER`. `cd backend && uvicorn dashboard_app:app` is BROKEN.
- **Mail: one live store, one dead one.** LIVE = `mail_indexer` (inotify) → Postgres `mail_index` →
  `/mail` (`tools/mailcrm.py` reads DB-first with a live-Maildir fallback; `mailcrm_ui.py` renders;
  `mail_db.py` is the psycopg2 layer). DEAD leftovers — do NOT wire them expecting `/mail` to change:
  `mail_sink.py` / `mail_sink --poll` / `mail_sink.json` (Mailgun/Mailpit era),
  `dashboard_app._start_mail_poller()` (defined, never invoked), `tools/mail_dashboard.py` (zero importers).
- **Inbox actions + classification.** Reply data is passed through `.reply-action` data attributes (not
  executable inline arguments — recruiter subjects/Message-IDs contain quotes). `/mail/delete` moves the
  whole thread into the candidate Maildir's recoverable `.Trash/cur` and prunes its index rows. `/mail`
  has the same stage funnel as Candidates; its filters update in place and block repeated taps while loading.
  Classification is driven by editable plain-text phrases at `/mail/keywords` (persisted in
  `uploads/mail_keywords.json`); saving atomically rewrites the rules and immediately reclassifies existing
  Postgres rows. `mailcrm.classifier_version()` includes a hash of the saved rules so `mail_indexer` also
  repairs stale rows after a restart. Defaults intentionally require explicit interview invitations and do
  not include broad acknowledgement-template words such as `next steps`, `screening`, or `move forward`.
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
  is. Everything secondary — region chips, «Подать на все», the proxy pool — lives in a **«Фильтры»
  MODAL dialog** (`#catSettings`, `.cat-modal`, toggled by `toggleFilters()` — backdrop/✕/Esc close it,
  body scroll locks while open; a centered dialog on desktop, a bottom-sheet ≤760px; **was an inline
  collapsed sheet before 2026-08-25**); the button shows the active region as a tag. Double-tap zoom is
  disabled app-wide via `touch-action:manipulation` on `html,body` in `mailcrm_ui._CSS` (pinch-zoom +
  scroll still work). Cards: the TITLE is the link to the posting (no separate «Открыть»), a
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
  misses; unbacked → review. **Required self-ID / personal DATA-processing consent SELECT**
  (`_data_consent_pick`, `_DATA_CONSENT_RE`, 2026-08-27) → the affirmative option, **BACKED** (unlike the
  other unbacked picks) so it does NOT create a `choice_review` → `review_item` that blocks auto-submit. It
  fires on `consent…(self-identification|demographic|diversity|personal|sensitive)…(data|information)…
  (process|collect|stor|use)` — a required LEGAL consent, NOT a protected-characteristic self-ID (consenting
  to PROCESS a separately-DECLINED survey claims nothing; same rationale as the checkbox-side
  `_DEMOGRAPHIC_CONSENT_RE`). This is a **live-only Greenhouse demographic-section select the nightly scrape
  never captures** (0/243 Remote rows have it), so it's uncached → fell to the LLM → `backed=False` →
  `choice_review` → blocked EVERY Remote (remote.com) submit (367 jobs, 0 auto-submits). **PARTIAL fix
  though:** it removes the SELF-ID-CONSENT trigger, but many Remote jobs ALSO have other unbacked-choice
  review triggers on the same live form (`How did you hear about Remote?` referral with no fact, a `What
  pronouns…` demographic, `Privacy notice` / `Notice at Collection` selects) and/or a behavioral textarea
  (`Tell us about a time…`, ~40 jobs, legitimately reviewed) — so a Remote job fully unblocks ONLY when the
  consent was its SOLE trigger (subagent-confirmed on job 1764). Does NOT touch `_NEEDS_REVIEW` (behavioral
  contract intact). Tests: `test_choices.py::test_data_consent_select_is_backed_not_review`.
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
- **Required Cover Letter is filled for SYNTHETIC personas only (2026-08-26).** Many GH/Ashby forms have a
  REQUIRED "Cover Letter" field (Attach/Dropbox/**Enter manually**); the engine already GENERATED the body
  (`services/tailor/answers.cover_letter` → stored in the draft's `cover_letter` key) but never wired it in,
  so ~390 submits/history blocked on "Cover Letter is required"/"please enter"/"please complete" (the single
  biggest FIXABLE block bucket — bigger than the 368 Ashby datacenter-spam blocks; 56% of the live catalog
  mentions a cover letter). Fix (3 parts): (A) `catalog_drafts.materialize_prefill` injects `d["cover_letter"]`
  as a known answer under "Cover Letter"/"Motivation Letter"/… **gated `profile_id.startswith("demo_")`** —
  a REAL applicant's letter must be human-written, never auto-fabricated prose, so real personas leave it
  blank (human finishes). (B) `dropdowns.fill_cover_letter_known(page, known)` fills a plain `<textarea>`
  directly, OR on a GH file-upload widget **clicks "Enter manually", waits for the revealed textarea, then
  types** — the textarea does NOT exist in the DOM until the toggle is clicked (React renders on click), so a
  bare known-answer replay hit the hidden file input and failed. Called additively in `base.prefill` after
  `fill_required_consent`. (C) `base.prefill` removes the handled cover-letter label from `unfilled` (the
  recheck reads the HIDDEN file input, whose `.value` stays empty → would phantom-block a complete form).
  `_fill_one_on_worker`→`ensure_and_wire`→`generate_draft`+`materialize_prefill` runs FRESH per bulk job, so
  a dash+copilot restart is all it takes to go live (no `_SCRAPE_V` bump). Tests: `test_dom_fixtures.py`
  (reveal+fill / plain textarea / no-touch-other / no-op) + `test_catalog_drafts.py` (synthetic-only gating).
  NB the 7 `test_workable_*` DOM failures are PRE-EXISTING (a stale datepicker assertion), unrelated.
- **Ashby "Autofill from resume" clobbers screener fills — wait for it to SETTLE (2026-08-26).** Some Ashby
  forms (Cohere, all ~23 in a run) have an "Upload resume to autofill" parser input (separate from the
  `_systemfield_resume` attachment). Ashby parses the résumé server-side and **RE-RENDERS its controlled
  form state in one or more ASYNC passes.** `ashby.autofill_from_resume` used to return as soon as ONE text
  field populated (+1500ms) — often BEFORE the screener re-render — so the analyzer's subsequent fill (e.g.
  the required "Are you authorized to work in the country you currently reside in?" Yes/No **button-toggle**,
  filled by `dropdowns.apply_button_choice` — a sound Playwright click, NOT the bug) ran between passes and a
  later pass **unbound it from React state**: submit then failed **"Your form needs corrections — Missing
  entry for required field: …"** while the button still SHOWED selected (different jobs flagged different
  fields = a pure timing race). Fix (`ashby.py` only → zero regression to GH/Lever/Workable or non-autofill
  Ashby like elevenlabs/Salmon/1Password, which have no autofill input): `autofill_from_resume` now waits
  until the form is STABLE — a field populated, no `parsing/pending` indicator visible
  (`.ashby-application-form-autofill-input-root` `data-state`), and the value+button-toggle signature
  unchanged across two reads (every re-render pass has landed) — before returning, so the analyzer fills onto
  a settled form. Verified live: a Cohere job that previously failed now returns "Your application has been
  submitted!". **PARTIAL though — ~1/3 of Cohere now submit; the rest still hit needs-correction because a
  SECOND async Ashby pass lands AFTER the stability window returns and clobbers one screener** (verified: a
  2-screener Cohere job where "based in Singapore?" committed but "Singaporean citizen?" stayed unbound). The
  settle-wait can't fully win a non-deterministic multi-pass race. **Next lever (the real completion): a
  gated re-assert in `copilot._click_submit_after_fill` — on `ev["blocked"]` matching "needs correction /
  missing entry" AND `strategy=="ashby"`, re-click the flagged screener's known answer then re-submit ONCE.**
  It's safe (fires only on an ACTUAL validation failure -> zero regression on working forms) but must use a
  NEW button harvest that IGNORES the "answered" skip (that skip reads the same stale visual state that's
  lying). Deferred 2026-08-26 (adding re-submit attempts under a load-16 bulk run was unwise then; the
  settle-wait is a no-regression partial win). Tests: `test_dom_fixtures.py::test_ashby_autofill_waits_for_parser_settle`
  / `_noop_without_autofill_input`. Verify Ashby fill changes with a `dry_run` co-pilot fill on one Cohere +
  one known-good (elevenlabs) jobid.
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
  optional, best-effort.) **Degree/Discipline react-select fill (2026-08-25):** a FIXED taxonomy
  ('Degree'='MSc Bioinformatics') filters to ZERO on the résumé value AND every prefix, so
  `apply_react_select_choice` surfaced no options and the degree-LEVEL match (`_degree_level`/`_OPT_LEVEL`)
  never ran → blank. Fix: when opts is STILL empty after the prefix retries, `_type_and_poll("")` clears the
  filter to reveal the FULL list (only fires when the fill was about to fail → can't regress a working
  react-select). **End-date waiver safety net (`materialize_prefill`):** a 'Present' role sets `Current
  role=Yes`; if that checkbox tick fails to WAIVE the End date it stays required+blank → submit blocked, so
  End date year/month now also `setdefault` to today (ignored when the waiver works). Tests still green:
  `test_dropdowns.py`/`test_catalog_drafts.py`.
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
  queue) rotates IPs. **The persistent headful browser is launched PLAIN (no launch-level proxy).**
  It used to launch with `proxy={"server":"per-context"}`, but in Playwright 1.49 that sentinel makes
  every context WITHOUT its own proxy fail with `net::ERR_PROXY_CONNECTION_FAILED` (Chromium tries to
  reach a proxy literally named "per-context") — so with the **empty pool** (the normal state) EVERY
  fill showed **"no internet" in noVNC** and nothing could be filled (root-caused + reproduced
  2026-08-25; `direct://` as a context proxy fails too). A plain launch still honors a per-CONTEXT
  proxy — verified sequentially in one browser: a context with `proxy=…` routes through it, one without
  goes DIRECT and has real internet, and switching direct↔proxy↔direct works — so `_use_proxy_context`
  rotation is unchanged while the empty-pool/direct case actually reaches the internet. **Do NOT re-add
  the `per-context` launch arg.** **Limits:** socks5 with auth won't route in the browser
  (Playwright can't authenticate socks5); the fast preview `/goto` stays on the direct IP (throwaway —
  only the real fill+submit is proxied); a fresh context per job briefly flickers the noVNC window.
  Parse/rotation are unit-tested (`tests/test_proxy_pool.py`, no network).
  **Self-healing + hidden list (2026-08-25):** the `/catalog` proxy panel no longer dumps every IP —
  it shows a one-line summary («🟢 N живых · проверка X назад», `#pxSummary` from `/proxies`'
  `count`+`last_check`); the full IP list is lazy behind a «показать список» toggle, and «Добавить
  прокси» (paste/upload) is collapsed. A **daemon thread in `dashboard_app._start_proxy_revalidator`**
  (started at import; needs `import logging` at module top — it crashes the app if missing) re-checks a
  rolling batch (~150) every ~10 min via `proxy_pool.revalidate_batch(batch, max_fails=3)`: a proxy that
  passes resets its `fails` streak (+refreshes egress IP), one that fails increments it and is DROPPED
  only after **3 CONSECUTIVE** failures — residential proxies flap, so a single timeout must never evict
  a good one (a separate `recheck_cursor` walks the pool; `last_check` is stamped). Manual pass:
  `POST /proxies/recheck` (Form `batch`). The FIRST pass over a fresh pool always evicts 0 (every
  `fails` goes 0→1). Tests: `tests/test_proxy_pool.py`.
- **Proxy SOURCE = Bright Data, refreshed DAILY (`tools/brightdata_proxies.py`, 2026-08-25).** The
  earlier pasted residential/mobile provider (`l6jkxlhnqy`) DIED — its account went to `407 Proxy
  Authentication Required` on every request (expired/unfunded), so a 100-job bulk run collapsed to
  12/100 filled, ~88 `net::ERR_TUNNEL_CONNECTION_FAILED`/`Timeout` at `Page.goto`. Replaced with a
  Bright Data account (token/customer/zone in `backend/.env` `BRIGHTDATA_*`, gitignored — customer
  `hl_63d6fad4`). BD rotating proxies use ONE gateway (`brd.superproxy.io:33335`) + a zone; the egress
  IP is chosen per **session**, where the session id is a `-session-<random>` token in the proxy
  **username** — so "N rotating proxies" = N usernames sharing the zone gateway/password, each with a
  distinct session (no per-IP allocation for rotating zones). **Active zone = `alibaba_dc` (datacenter
  shared, $0.60/GB — the absolute cheapest, chosen by owner for max volume on a low balance);**
  `alibaba_res` (residential shared, $4/GB, better anti-bot) also exists — switch by editing
  `BRIGHTDATA_ZONE` in `.env` (+ its `BRIGHTDATA_ZONE_PASSWORD`) and re-running the tool. **Balance is
  small (~$5.37 as of 2026-08-25 → ~1800 datacenter apps / ~270 residential) — top up in the BD
  dashboard.** `brightdata_proxies.refresh()` verifies the zone routes (one live probe; ABORTS without
  wiping the pool if the balance/zone is dead, so a drained account leaves yesterday's pool intact),
  mints `BRIGHTDATA_POOL_SIZE` (200) fresh sessions, validates a sample (proves rotation + records real
  egress IPs), and `proxy_pool.replace_pool`s the pool (delete old + write new, cursors/fail-streaks
  reset). CLI: `python -m backend.tools.brightdata_proxies --verify` / `--refresh [--count N]
  [--validate K]`. **Daily cron `45 4` refreshes the pool** → `logs/brightdata.log`. Tests:
  `tests/test_brightdata_proxies.py` (pure, no network).
- **Bulk worker falls back to DIRECT when proxies are dead (`dashboard._fill_one_on_worker`, 2026-08-25).**
  The per-job load has 3 attempts: the first two rotate a FRESH proxy (`next_proxy`, retried on a
  `_PROXY_ERR_RE` load failure — dead/slow egress), the **THIRD/final attempt goes DIRECT (no proxy)**
  so a fully-dead pool can't block the whole run (GH/Ashby submit fine from the datacenter server IP —
  that's how the earliest verified submits worked). With a healthy pool attempts 0/1 succeed and the
  direct fallback is never reached. Complements: `proxy_pool.next_proxy` now round-robins only the
  **lowest-`fails` tier** of the pool (not blindly over dead entries), so attempts 0/1 don't waste
  timeouts on known-bad egresses.
- **Ashby anti-spam flags the DATACENTER IP itself — direct is NOT immune (`_fill_one_on_worker`, 2026-08-25).**
  Ashby's submit returns **"We couldn't submit your application. Flagged as possible spam. Turn off your
  VPN or proxy."** on a risk-scored share of submits. FIRST believed proxy-specific (go direct), but
  VERIFIED WRONG by a screenshot: a **DIRECT submit (no proxy, our Contabo datacenter IP 173.249.18.153)
  to upguard/Ashby got the SAME banner** with a perfectly-filled form. So Ashby flags datacenter IPs
  **regardless of proxy** — both the BD datacenter proxy AND the direct server IP. (The earlier log
  "proxy=⅓ spam vs direct=low" was an artifact: the old `_SUBMIT_BLOCK_RE` didn't catch "couldn't
  submit", so proxied spam banners were logged `blocked=None`, undetected.) The parallel lane still goes
  **DIRECT** (`_PARA_ATS` skips proxy) — direct is no worse than the datacenter proxy and free, and
  Greenhouse's failures are fill gaps (missing-required-field / consent), NOT spam, so GH is fine direct.
  **The ONLY fix for the Ashby spam flag is RESIDENTIAL IPs** (zone `alibaba_res`, $4/GB) — a datacenter
  IP, proxied or not, gets flagged. **Owner's decision 2026-08-25: keep datacenter/direct (free) and
  accept that a risk-scored share of Ashby submits spam-fail → «Незавершённые».** NOTE those spam-flagged
  Ashby jobs can't be finished from noVNC either (same datacenter IP) — they'd need a residential
  connection; «Докрутить» on the co-pilot won't clear a spam flag. To switch Ashby to residential later,
  route `_PARA_ATS`==ashby through the `alibaba_res` zone. Do NOT re-add the earlier claim that "direct
  submits fine" — it doesn't, for Ashby.
- **`_SUBMIT_BLOCK_RE` must catch the real ATS rejection wordings (`copilot.py`, 2026-08-25).** It matched
  captcha / "is required" / "please enter" but MISSED "flagged as possible spam", "we couldn't submit",
  "missing entry", "needs corrections", "N items for a required section", "please accept the terms" — so a
  REJECTED submit was mislabeled `blocked=None` ("awaiting confirmation") and the inline watch burned the
  full `WAIT_SUBMIT_MAX`=300s waiting for a receipt that could never arrive (the ATS never emailed because
  the submit failed). Widened to catch them → the job is correctly recorded `blocked` and the watch is
  skipped (reclaims worker throughput). A rejected GH/Ashby submit means a genuine fill gap (hidden
  required field / required-consent checkbox / a fill-then-validate race), NOT a captcha.
- **Bulk auto-apply: «Подать на все» (`/catalog`) — PARALLEL (2026-08-25).** Was ONE sequential queue
  on the single noVNC co-pilot; a single 1Password/Ashby job could hang ~2h (the Ashby emailed-code
  wait), so 6128 jobs was days-to-months. **Now Greenhouse/Ashby fan out across `workers` HEADLESS
  browser workers** (`_do_fill_all_parallel` → `bulk_pool.start_workers(n)` spawns N `backend.copilot`
  processes with **`COPILOT_HEADLESS=1`** on ports **8110+** — each its own browser, no Xvfb; they
  inherit the dashboard's `mail` group so the emailed-code step still works; torn down when the run
  ends). One thread per worker pulls from a shared queue; a **hard `_PER_JOB_TIMEOUT`=360s** per `/load`
  means one hung job can't stall the run. **The emailed-security-code confirmation (GH/Ashby) is awaited
  INLINE by the worker (`/load?wait_submit=1` → `copilot._watch_submit` up to `WAIT_SUBMIT_MAX`=300s,
  which fills the code from the persona's Maildir and returns `confirmed`), NOT backgrounded** — the
  original background `_S["watch"]` is fine for the single co-pilot but in parallel the next job's `/load`
  would `_cancel_watch()` and kill it, so Ashby/GH would click Submit but never confirm (root-caused
  2026-08-25). The confirmation email takes MINUTES, so the worker is held that long (adaptive scaling
  adds workers to compensate); a job that doesn't confirm within 5 min falls to «Незавершённые», where
  «Докрутить» finishes it on the single co-pilot's full 10-min watch. `_watch_submit` now returns True on
  submit-detected so the inline path can set `submit_result["confirmed"]`. **Lever/Workable (and any
  non-`_PARA_ATS` = greenhouse/ashby) are SKIPPED in bulk, NOT parked (changed 2026-08-26).** They need a
  LIVE human captcha unsolvable from this datacenter IP, so parking them as `needs_human` re-inflated
  «Незавершённые» to 1775 with jobs no human would ever captcha-solve. The bulk lane now just `_bump`s
  them as skipped (logs the count); a specific Lever/Workable job can still be done by hand via the
  /catalog one-click. (Previously they were recorded `needs_human` with `reason=click_failed`.)
  **Ashby datacenter-IP SPAM blocks are likewise NOT parked (`bulk_log._update_ledger`, 2026-08-27):** a
  `couldn't submit / flagged as possible spam` block is un-completable from here (a human can't finish it
  from noVNC either — same IP), so `_update_ledger` DROPs (never parks) a job whose `blocked` matches
  `_SPAM_LEDGER_RE` (narrow: `couldn't submit|flagged as possible spam|possible spam` — a genuine fill-gap
  `is required` or a human-fixable `couldn't upload` is STILL parked). Without this, spam re-inflated
  «Незавершённые» ~+60/tick during Ashby-heavy segments (owner accepted the spam FAILURE itself as
  free/direct; this only keeps the ledger honest). A one-time purge of 258 existing spam entries was run at
  deploy (977→725). Test: `test_bulk_log_concurrency.py::test_spam_blocked_job_not_parked_in_ledger`.
  Verified live: 4 1Password/Ashby jobs, 2 workers → 4/4
  submitted in ~5.5 min (vs ~20 min/job sequential); workers auto-cleaned. `_FILL_ALL` counters are
  bumped under `_FILL_ALL_LOCK` (thread-safe). **`bulk_log`'s own shared-state files are now thread-safe
  too (2026-08-26):** the parallel lane's worker threads share one `run` dict and all call
  `record()`/`mark_submitted()`/`mark_done()`/`drop_many()`, which did an UNLOCKED read-modify-write and
  wrote a FIXED `<name>.json.tmp` — so concurrent writers crashed with `FileNotFoundError` when one
  thread's tmp was `os.replace`'d out from under another (caught → warning, not fatal) AND silently LOST
  each other's updates, dropping «Незавершённые» ledger entries so those jobs were never drained. Fixed
  with a module `_LOCK` (RLock) around every mutation + `_atomic_write_json` (UNIQUE per-pid/tid tmp).
  Bookkeeping-only leak (the ATS submits + the mail-derived reconciler/`submitted_jobids` are unaffected),
  so it's safe to let a mid-flight run finish on old code and apply on the next restart. Test:
  `tests/test_bulk_log_concurrency.py`. Proxy rotation: the DASHBOARD picks `next_proxy()` per job
  and passes it to the worker's `/load` (one cursor). **`count` is OPTIONAL: empty = EVERY available job;
  a number = first `count`, clamped 1..20000. `workers` is ADAPTIVE by default (2026-08-25): empty/
  "auto"/0 → `_do_fill_all_adaptive` seeds 4 workers and ramps +2 every ~12s WHILE there's headroom —
  CPU < 70% (`_ADAPT_TARGET_CPU`), 1-min load < 1.2×cores, and ≥6 GB RAM free — up to `_ADAPT_MAX`=**6**
  (grow-only; drains as the queue empties). **`_ADAPT_MAX` was lowered 18→6 on 2026-08-26: the ENTIRE
  parallel lane is `_PARA_ATS` (GH/Ashby) and every fill calls the ONE local LLM (127.0.0.1:8080); the
  ramp gates on CPU/load/RAM, none of which sense LLM saturation, so at 12-18 workers the single LLM
  serialized, per-fill time ballooned to ~240s and blew the 300s `_PER_JOB_TIMEOUT` → the dashboard
  ReadTimeout'd WHILE the worker kept filling+submitting, and the job was mis-recorded a permanent
  `error` (113 such in one run + it drove swap to 100%). Do NOT raise `_ADAPT_MAX` back up without a
  real LLM-latency probe. A `_TRANSIENT_ERR_RE` (ReadTimeout/Connection-refused/browser-closed) now
  gives such jobs a higher drain retry cap (`_DRAIN_TRANSIENT_MAX`=6) so a fillable job isn't dropped
  as dead. A NUMBER pins a fixed count (`_do_fill_all_parallel`, clamped
  1..18). NOTE the fills are I/O-bound (proxy/page/LLM waits) so CPU stays low — the real limiter is the
  **load-average gate**, not CPU%: measured ~216 MB RAM per idle worker, and adaptive ramped to ~12 and
  self-capped when load hit ~15-21 on the 12-core box (12/12 Ashby jobs submitted in ~4 min). Optionally
  narrowed by `company`/`region`;
  `gender` sets persona sex. Endpoints: `POST /catalog/fill_all` (Form `count/gender/company/region/
  workers`), `GET /catalog/fill_all_status` (poll; adds `workers`), `POST /catalog/fill_all_stop` (sets
  `_FILL_ALL_STOP`; workers stop pulling + are torn down). The old sequential `_do_fill_all` +
  single-job `_do_fill` (via co-pilot 8102) still exist for the one-click `/catalog/{id}/fill`. Live
  counters in `dashboard._FILL_ALL` (in-memory only). **«Незавершённые» section (`/unfinished` + nav tab, 2026-08-25):**
  a persistent ledger (`bulk_log.py` → gitignored `logs/unfinished.json`) of applications that didn't
  confirm — `record()` adds a not-confirmed job (captcha-blocked / errored / incomplete) and removes a
  confirmed one; `/unfinished` lists them with «Докрутить» (re-fill → finish by hand), «Открыть вакансию»,
  «Выполнено» (`POST /unfinished/{jobid}/done` → `mark_done`).
  **Ground-truth reconciler (`tools/submit_reconcile.py`, 2026-08-25) — critical:** our page-watch is
  latency-bound, so a REALLY-submitted GH/Ashby job is often parked here as "unconfirmed" (the ATS emails
  "received your application" 1–3 min AFTER we gave up — verified: a 3s miss). `submit_reconcile.reconcile_ledger()`
  walks the ledger, finds each job's persona Maildir (via the `profile` now stored in each record, else the
  newest `uploads/prefill/demo_*/<jid>/persona.json`), and if `mailcrm.classify` finds an ATS receipt/decision
  email (`ack`/`interview`/`offer`/`rejection`) it advances `status_store` (submitted/interview/rejected) and
  clears the job from «Незавершённые». Forward-only, read-only w.r.t. mail. Runs in a **dashboard background
  thread** (`_start_submit_reconciler`, every ~150s, needs the `mail` group — the dashboard has it) + on demand
  via `POST /unfinished/reconcile`. This is why «confirmed=False» must be read as "not DETECTED", not "not
  submitted" — an audit found ~73 real ATS receipts vs ~1 immediately-recorded confirm. Ledger entries now
  carry `profile` (`bulk_log.record(profile=)`).
  **Reconcile checks ALL persona variants + Postgres, not just the newest (2026-08-25).** A job re-applied
  across bulk runs gets a FRESH mail-less persona each time, and `_persona_for_job` used to return only the
  NEWEST `persona.json` — so a job genuinely submitted+acked under an EARLIER persona stayed parked forever
  (reconcile=0 on 436 while 7 held real "Thank you for applying" acks). Fixed: `_personas_for_job` returns
  EVERY (profile,email) for the jobid and `reconcile_ledger` takes the first with evidence; `_db_evidence`
  reads the classified `mail_index` (Postgres) first (faster, retention-race-proof, no `mail`-group disk
  need), disk scan as fallback (`_evidence`). **Anti-churn `submitted_jobids` set (`bulk_log`, gitignored
  `logs/submitted_jobids.json`):** "done" used to live only per-persona (status_store) / in the ledger, so a
  re-run under a new persona looked NEW — job 20219 was applied **10×**. Now a confirmed submit (or
  `mark_done`, or the reconciler) calls `bulk_log.mark_submitted(jobid)`; `_update_ledger` never re-parks a
  jobid already in that set, and **`/catalog/fill_all` excludes it from selection** so a done job is never
  re-applied. Seed it from `confirmed=True` log lines if rebuilding. **`POST /unfinished/rerun` +
  «Докрутить всё (N)» button = DRAIN (2026-08-25, `_drain_partition`):** it FINISHES what it can instead
  of hiding failures — re-runs every fixable job (`error` + `done` blocked-on-field/spam/clicked-no-confirm,
  retry-capped at `_DRAIN_MAX_RETRIES`=3) through the adaptive engine, and drops ONLY the truly-dead
  (`_UNFIXABLE_JOBIDS` embed-404, already-submitted, or retries-exhausted). `needs_human` (Lever/Workable
  captcha) is left for a human. Each ledger entry carries a `retries` count (`bulk_log._update_ledger`
  increments it when a job is re-parked); a job that confirms leaves the ledger, one that keeps failing is
  re-parked until it hits the cap and is dropped as dead — so we keep re-attempting fill-gap failures (which
  the fill fixes may now clear) without infinitely re-spamming Ashby. `bulk_log.drop_many` removes a stale
  entry WITHOUT marking it submitted (unlike `mark_done`), so a dropped job can still be re-hit by a future
  run. **The loop's per-cycle procedure now: random batch → reconcile → drain → reconcile → repeat** (don't
  just delete unfinished — drain them). **Audit trail
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
  no decline option stays blank (human sets it). **`fill_demographics_decline` handles radio / native
  `<select>` / react-select / Workable combobox demographics but NOT checkbox-group ones** (an EEO
  "select all that apply", e.g. 1Password's racial/ethnic survey, with its own 'Prefer not to say' box):
  `dropdowns.fill_demographic_checkboxes_decline` (2026-08-25, additive, called right after in
  `base.prefill`) ticks that decline checkbox. **`dropdowns.fill_required_consent` (same commit)** ticks a
  REQUIRED legal/privacy consent checkbox ('I agree' to the recruiting-privacy notice, 'I understand …') —
  you can't submit without it — via `_CONSENT_RE`, while `_CONSENT_SKIP_RE` leaves optional MARKETING
  opt-ins ('contact you about job opportunities', newsletters, talent-community) UNticked. Both were
  unfilled REQUIRED fields silently blocking 1Password/Ashby submits (verified live: the radio
  demographics already declined, but the checkbox racial survey + 'I agree' stayed blank). NO
  protected-characteristic is ever claimed — the demographic answer is always the decline option. Tests:
  `test_dropdowns.py::test_consent_regex_matches_required_not_marketing`. **The `_DEMOGRAPHIC` veto in
  `fill_required_consent` is carved out for a demographic-DATA-CONSENT box (`_DEMOGRAPHIC_CONSENT_RE`,
  2026-08-26):** Greenhouse's `gdpr_demographic_data_consent_given` ("I consent to <Co> collecting…my
  responses to the demographic data surveys above") is a REQUIRED legal consent, not a self-ID — vetoing
  it blocked datadog/smartsheet/varicent on "Please accept". Consenting to PROCESS a declined survey
  claims nothing, so it's ticked; a real self-ID ("I am a person with a disability", "protected veteran")
  has no consent verb → stays vetoed + blank. Also widened `_CONSENT_RE` for skillsoft's company-subject
  phrasing ("Skillsoft **has my consent** to collect/store/process my data" — `consent to
  (collect|stor|process)` + `has my consent`). `_CONSENT_SKIP_RE` still wins, so marketing never ticks.
  Test: `test_dropdowns.py::test_demographic_data_consent_ticks_but_selfid_does_not`. So the user's ask to "pick a
  gender for the required field" is moot: 1Password (and its kind) offer 'Prefer not to say', which we
  select — no need to state a gender/orientation. **`_DECLINE_RE` must match "do not **want** to answer"**,
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
    persona's inbox actually SHOWS in the CRM `/mail` — the mail delivers to the Maildir regardless. **register_demo_persona is now THREAD-SAFE (lock + atomic tmp-replace, 2026-08-25):** the parallel bulk lane runs N dashboard threads that each register a persona concurrently; the old bare read-modify-write RACED and clobbered `demo_personas.json`, silently dropping earlier personas — incl. real leads (gulmira's Salmon HR-interview thread) — from the registry so their mail stopped surfacing (files stay on disk, just unindexed). **NEVER `TRUNCATE mail_index` to rebuild:** `mail_indexer.run_once()` only re-indexes CURRENT `candidates()`, so any mailbox whose registration was lost loses its rows permanently — recover by re-registering every `/var/mail/vhosts/takhet.com/*` maildir that has mail, then re-index., but
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
- **Mass Hiring board connectors — the live catalog of source recipes (`tools/mass_hiring.py`).** The board
  (`mass_hiring_jobs`, human-apply remote-US mass-hiring surface, SEPARATE from auto-apply `job_catalog`)
  pulls each source with plain `httpx` (no browser). Every `fetch_X()` → `_mk_row(...)`, and the per-job
  decision lives in a PURE helper unit-tested with NO network (`tests/test_mass_hiring.py`, 27 tests). The
  two HARD RULES: `_is_remote`/location must be REMOTE, `categorize()` must return a mass-hiring ENTRY
  bucket (drops senior/dev via `_NOT_MASS`/`_DEV`). `categorize`'s healthcare/insurance bucket matches bare
  `rep` (not only `representative`) so "Licensed Health Insurance Rep" survives (guarded so "Sales Rep"/
  "Legal Rep" don't leak). Live sources + their gotchas:
  - **Amazon (RE-DIAGNOSED 2026-08-28 — the earlier "0 = seasonal, not a bug" claim was WRONG and masked a
    real bug).** TWO bugs, EITHER of which zeroed it: (1) `result_limit=200` — the API rejects `>100` with
    `{"error":...,"hits":0,"jobs":null}`, so `_amazon_row` never even ran → **0 on every query**; (2) `is_us`
    tested `normalized_location` EXACTLY `=='USA'`, dropping every state-tagged remote row (`'Texas, USA'`)
    — which is exactly where the CS roles live. Fix (`_amazon_row`/`fetch_amazon_remote`): `result_limit=100`
    + paginate `offset`; `is_remote = city.lower().startswith("virtual")` (catches `Virtual` /
    `Virtual Location - <State>` / `Virtual Contact Center-<xx>`); `is_us = country_code=="USA"`;
    `base_query="virtual"` (a remote posting's city literally contains "Virtual" so it's indexed on it —
    "work from home" matches only 4-6 rows and misses most). Send `Accept-Encoding: gzip, deflate` (else
    zstd). Do NOT re-add `result_limit=200` or `normalized_location`-exact US matching.
  - **Concentrix (Workday, tenant `cnx`, site `external_global`).** Now EMPTY `searchText` + the US
    `locationCountry` facet (id `bc33aa3152ec42d4995f4791a106ed09`) — the old `searchText="work at home"`
    missed US WAH rows whose text lacks that phrase, and the unfaceted `total` reads 0. `external_global`
    is the PROFESSIONAL tier (most WAH rows senior → dropped), so yield is ~1-2. No frontline CSR site is
    public on this tenant (`external_us` exists but is EMPTY; `careers.concentrix.com` doesn't resolve).
  - **Teleperformance (custom Umbraco, ~45).** `GET www.tp.com/Umbraco/Api/Careers/GetCareersBase?node=1780
    &workFromHome=True&country=United%20States&culture=en-us&pageSize=500`. `node=1780` = the US careers
    opportunitiesId; the two server-side filters mean every `resultado[]` row is US work-from-home by
    construction (`_tp_row` re-checks `workFromHome`+`country`). Rows are iCIMS reqs (`externalId`, `url`).
    Use `www.tp.com` (jobs.teleperformance.com/`www.*` subdomains are NXDOMAIN here).
  - **TTEC (Radancy TalentBrew, ~27).** `GET www.ttecjobs.com/en/search-jobs/results?SearchResultsModuleName=
    Search Results&CurrentPage=N&RecordsPerPage=100&keywords=remote`. Returns a JSON envelope whose
    `results` key is an **HTML fragment** — parse with bs4 `a[data-job-id]` (title in `h2`). **CRITICAL: a
    tile's `.job-location` span is the requisition HOME OFFICE, not the remote flag** — an offshore
    "Pasay, Philippines" row can carry a "…- Remote" title. So `_ttec_row` decides remote AND US strictly
    from the TITLE (`_TTEC_REMOTE_RE` + `_title_us`: "USA"/"United States"/a full US state name). Use
    `www.ttecjobs.com` (careers.ttecjobs.com is NXDOMAIN).
  - **CVS Health (Workday, tenant `cvshealth`, site `CVS_Health_Careers`, ~5).** 8000+ jobs, NO remote/
    workType facet, and `searchText` does NOT hard-filter (it only re-ranks — "work from home" still returns
    on-site store pharmacy techs first), so DON'T paginate the whole tenant. Narrow with the `jobFamilyGroup`
    facet **"Customer and Member Services"** (id `e65dbadf6a50100168ed7e8f60560002`, 57 rows), then filter
    WFH+US. CVS marks remote US with a bare 2-letter STATE CODE prefix (`"RI - Work from home"`) that generic
    `us_eligible()` misses → `_has_us_state()` catches it, and `_workday_row` then FORCES `us_eligible=True`
    on the row (else `collect(us_only=True)` would drop it).
  - **Sutherland (SmartRecruiters, ~4).** `GET api.smartrecruiters.com/v1/companies/Sutherland/postings?
    limit=100&offset=N`. `_smartrecruiters_row` keeps `location.country=="us"` (lowercase ISO-2) AND
    `location.remote==True` (server-side `remote=`/`country=` params are unreliable → filter client-side).
    Generic `_fetch_smartrecruiters(source, company)` — add another SmartRecruiters BPO by registering it.
  - **Working Solutions (Algolia, ~7).** `POST UM59DWRPA1-dsn.algolia.net/1/indexes/production_Working%20
    Solutions_jobs/query` with the PUBLIC referer-restricted search key (in code) + `Referer:
    https://apply.workingsolutions.com/` (mandatory, else 403). 100%-remote contractor CSR; keep the US
    `country` facet. Build `apply_url` from `hits[].id`.
  - **Shared helpers:** `_fetch_workday`/`_workday_row` (Concentrix + CVS; US+remote via `us_eligible(loc)
    OR _has_us_state(loc)`), `_fetch_smartrecruiters`/`_smartrecruiters_row`, `_has_us_state`/`_US_STATE_ABBR`/
    `_title_us`. **himalayas resilience (2026-08-28):** its API intermittently returns non-JSON at a random
    offset; the old `fetch_himalayas` `break`'d the WHOLE pagination on the first hiccup (observed collapse
    70→9), so it now RETRIES the offset (3× with backoff) before giving up. Yield still fluctuates (~7-70)
    with feed rotation + rate-limiting of rapid 200-page pagination — that variance is inherent, not a bug.
  - **Kelly (KellyConnect, WP REST, ~7).** `GET www.mykelly.com/wp-json/wp/v2/job-listings?per_page=100
    &page=N&_fields=id,link,date,title,acf`. The host is behind Akamai bot protection that **403s our
    datacenter IP**, so `fetch_kelly` routes through the rotating **proxy pool** (`_pool_proxy_url()` →
    `proxy_pool.next_proxy()`; the BD-gateway egress passes Akamai — verified live 200). remote + country
    live in ACF meta (`acf.remote=="1"` AND `acf.country_code=="US"`/`geolocation_country=="United States"`),
    no server-side filter, so we page all ~30 pages and filter client-side (`_kelly_row`, `html.unescape`
    the title). **If the pool is empty, Kelly is SKIPPED** (a bare datacenter request just 403s) — so its
    yield depends on a live proxy pool. It's the slow source (~55s, 30 proxied pages).
  - **Maximus (Avature, ~21).** NOT Workday (that tenant is dormant/422). Real ATS = the Avature portal
    (id 4). Two-step, no login/captcha: (1) `GET /careers/Job-Search_US` with a **cookie jar**; the HTML
    embeds a job-list widget whose `data-props` is **nested by device — use `['desktop']`** — carrying a
    STABLE `uuid` + a **per-session `qtvc` token** (64-hex, ROTATES every page load — scrape fresh, never
    hardcode) + `formId` + link configs; (2) `GET /4/_portalList` with the SAME cookies + those params.
    `_maximus_params` builds the querystring from a **WHITELIST** — sending the context-value keys
    (`recordIdContextValues`/`personIdContextValue`/`userIdContextValue`) makes it **HTTP 500**. `total`
    comes back a **STRING** (coerce to int, else the `offset >= total` page guard raises). Remote is not a
    structured field → `_maximus_row` derives it from the title/classification text (`_MAXIMUS_REMOTE_RE`);
    the portal is US-only. It's the FLAKIEST source (Cloudflare + the qtvc handshake + occasional
    `Temporary failure in name resolution`), so `fetch_maximus` RETRIES the whole two-step flow up to 3×
    (an empty result = a transient failure, since the board always has ~20+ reqs) — do NOT let a transient
    deactivate its rows. Uses a real-browser UA (`_BROWSER_UA`, shared with Kelly — the bot UA is WAF-rejected).
  - **Probed but NOT wired (recipes on file, 2026-08-28):** **Foundever** (~5) exposes only an XML sitemap
    (no JSON); **Liveops** — its Rippling corporate board has ZERO entry roles, and the actual
    contractor-agent product is a login-gated Salesforce community (un-scrapable). NB the full `--collect`
    is now ~85s (Kelly's proxied paging), up from the old ~40-60s.
