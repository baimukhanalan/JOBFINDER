# JobFinder

> **Repo / GitHub — read first.** Lives at **`baimukhanalan/JOBFINDER`**. Account is **`baimukhanalan`**,
> NOT `Abekemyn` like other `/home/projects/*` repos. PAT is embedded in the `origin` remote URL (`git
> remote -v`) — `git push` works as-is; do NOT paste the token into any tracked file. Branch **`main`** is
> the single source of truth (commit straight to it). **Convention: after any nontrivial change,
> `git add -A && git commit && git push` — don't let work sit uncommitted — and edit THIS `CLAUDE.md` in
> the same commit whenever deploy / behavior / gotchas change.**
>
> **This IS the live project** (`jobs.systeam.kz`, pm2 `jobfinder-alan-*`, Postgres `jobfinder_crm`).
> **Dir map — THREE `jobfinder*` dirs:**
> - `/home/projects/jobfinder` (lowercase) — **THE LIVE CODE**, checkout of `baimukhanalan/JOBFINDER`. pm2 runs from here.
> - `/home/projects/JOBFINDER` (uppercase) — **dead husk** (only stale `uploads/`). Ignore.
> - `/home/projects/jobfinder.archive-2026-08-20-2158` — retired `Abekemyn/jobfinder` `michael` project. Dead, ignore.

Semi-automatic job-application engine for remote US/CA roles + a self-hosted candidate-mail CRM. Collects openings
(company roster + live ATS APIs), tailors a résumé per JD, pre-fills the ATS form, a human reviews + submits (the co-pilot
can auto-submit — see Gotchas); recruiter replies land in a Gmail-style inbox per candidate.

**Nav** (`mailcrm_ui._NAV`, **5 rail entries**): **Кандидаты** (`/mail/candidates`, primary tab — Gmail-style inbox GROUPED BY
CANDIDATE), **Вакансии** (merges **Каталог** `/catalog` + **Mass Hiring** `/mass-hiring` + **Незавершённые** `/unfinished`
behind one rail entry + the in-page segmented control `vacancies_seg`; routes unchanged so deep-links work), **Статистика**
`/stats`, **Пользователи** `/users`, **Health**. Drill-downs: `/queue` (via the roster «📄 N» chip), `/setup` (onboard a real
candidate), `/candidates/{id}`. Deleted: `/jobs` `/roles` `/apply`; `/mail` (old flat `render_inbox`) kept unrouted as a
fallback. On mobile (≤760px) `_topbar`+`_drawer` render a Gmail search pill + slide-out menu; both carry «Админ» + `/logout`.

Stack: Python 3.12 · FastAPI · Playwright · **psycopg2 / Postgres `jobfinder_crm`** · Dovecot+Postfix Maildir · aiogram ·
python-jobspy. Résumé tailoring + answer drafting use the **local Sumrak LLM** (`llm_*` in `config.py`, `sumrak-smart`),
NOT the Anthropic API (`ANTHROPIC_API_KEY` empty).

## Deploy (pm2, NOT systemd) — `jobs.systeam.kz`
All pm2 services launch via `cd /home/projects/jobfinder && sg mail -c '…'` (`sg mail` mandatory — `programmer` isn't in `mail`
interactively; Maildir is `vmail:mail 2770`). **`pm_cwd` MUST be `/home/projects/jobfinder` (lowercase).** Recreate with the
correct cwd via `pm2 start /usr/bin/bash --name <n> --cwd /home/projects/jobfinder -- -c "cd … && exec sg mail -c '<cmd>'"` →
`pm2 save` — never just `pm2 save`. **Run everything from the repo ROOT** (imports are absolute `backend.*`; `cd backend &&
uvicorn dashboard_app:app` is BROKEN).

- `jobfinder-alan-dash` → `uvicorn backend.dashboard_app:app` on **127.0.0.1:8099** — live CRM + review app. `/` →
  `/mail/candidates`. In-app admin auth. Exports **`IV_COOKIE_SECURE=1`** (Secure cookie + makes gate-install failure FATAL so
  the CRM can't boot ungated); relies on `INTERVIEW_SESSION_SECRET` (.env).
- `jobfinder-mail-indexer` → `python -m backend.tools.mail_indexer` — inotify watcher over `/var/mail/vhosts/takhet.com/*`
  → upserts Postgres `mail_index`. Feeds `/mail`. **Imports `mailcrm.build_index_row` — ANY change to `mailcrm.py` parsing
  needs `pm2 restart jobfinder-mail-indexer` TOO + a reindex of DB fields (`has_att`/`snippet`/`kind`) the list view reads
  (the thread OPEN view re-parses from disk + reflects a fix immediately).**
- `jobfinder-alan-copilot` → `uvicorn backend.copilot:app` on **127.0.0.1:8102** `DISPLAY=:98` — headful Chromium the bot
  pre-fills. Separate app (routes `/ /load /release /mark_submitted /state`).
- `jobfinder-alan-display` → `vnc/copilot_display.sh`: Xvfb **`:98`** + x11vnc **`:5901`** + noVNC **`:6090`**.
- `jobfinder-alan-ivremind` → `python -m backend.interviews.reminders` — Telegram interview notifier (own process; no `sg mail`).
- nginx vhost `jobs.systeam.kz` (certbot SSL): `/`→8099, `/copilot/`→8102, `/vnc/`→6090. `location /` is `auth_basic off`,
  gated in-app (`dash_auth.py` fail-closed middleware; unauth → `/login`). **basic-auth (`/etc/nginx/.htpasswd-jobs`, user
  `job2026`) kept ONLY on `/copilot/` + `/vnc/`.** Extension endpoints (`/draft /assist /profile_form /job_pack /resume_file
  /mark_ext`) are on the allowlist, self-auth via `X-Assist-Token`. Rollback: re-add the two `auth_basic` lines at server
  level + reload (a `.bak-*` sits next to the vhost). Admin accounts: `admin_cli add --login … --role admin`.
- **Automation without an interactive login:** every non-allowlisted route 303-redirects to `/login` without a valid admin
  SESSION cookie (an empty-body 303, easy to mistake for "ran fine"). Drive the bulk drain OUT of the dash process: import
  `backend.dashboard_app` + call `_drain_partition(bulk_log.unfinished())` → `_do_fill_all_adaptive(rerun)` (no auth; ledger
  shared). Importing `dashboard_app` starts its own submit-reconciler thread — do NOT also call `reconcile_ledger()`.
- `backend.main:app` (legacy jobs API + APScheduler over the OLD `jobfinder` Postgres) exists but is NOT deployed.

## Data stores
- **`jobfinder_crm` Postgres** — isolated CRM DB via `CRM_PG_DSN` (.env), psycopg2 (sync, pooled). NOT the shared `amasmail`
  MySQL, NOT the legacy `jobfinder` Postgres. Tables:
  - `mail_index` — fed live by `mail_indexer`.
  - `job_catalog` — fed **nightly by cron** (`catalog_collector.py` over Ashby/Greenhouse/Lever/Workable, remote-only).
    Per-job cols: `regions text[]` ∈ `{US,CA,UK,OTHER}` + `region_source` (`applier/regions.py`); `open_anywhere BOOLEAN`
    (Kazakhstan-eligibility, `regions.open_anywhere`); `role_category` (13 buckets, `role_category.py`); posted `comp_*` +
    `comp_source` (`comp_extract.py`); RESEARCHED est comp `est_base_*`/`est_total_*`/`est_comp_source` (`est_comp.py`,
    DISTINCT from posted); `questions JSONB`; `dead BOOLEAN` + `dead_reason` (`catalog_db.mark_dead`; `list_jobs`/`companies`/
    `jobs_for_drafting` exclude dead).
  - `mass_hiring_jobs` — human-apply mass-hiring board, SEPARATE from auto-apply `job_catalog`.
  - **DDL rule:** both `ensure_schema`s check `information_schema` + ALTER only a genuinely-missing column, under `SET LOCAL
    lock_timeout='15s'` (a bare `ADD COLUMN IF NOT EXISTS` still takes ACCESS EXCLUSIVE + can hostage the table for hours —
    see the DB-lock gotcha). Add columns to `_EXTRA_COLS`/`_missing_columns`, not a bare nightly ALTER. `jobfinder_crm` has
    `idle_in_transaction_session_timeout='10min'`.
- **Per-candidate Maildirs** `/var/mail/vhosts/takhet.com/<local>` (Dovecot/Postfix, `vmail:mail 2770`). Reply via Postfix SASL
  as that candidate (`mailcrm.send`, DKIM-signed). `uploads/prefill/<profile>/<jobid>/{report,status}.json` — `/queue` reads
  these; all `uploads/` is gitignored PII.

## Secrets & PII (gitignored)
- `backend/.env` — `CRM_PG_DSN`, `DATABASE_URL` (legacy), `TELEGRAM_BOT_TOKEN/CHAT_ID`, `IV_BOT_TOKEN`,
  `INTERVIEW_SESSION_SECRET`, `LLM_URL/KEY/MODEL`, `ANTHROPIC_API_KEY` (empty), `PROXY_URL`, `DO_API_KEY`, `BRIGHTDATA_*`,
  `NOPECHA_KEY`, legacy Mailgun keys. `config.py` uses `extra="ignore"`.
- `backend/.assist_token` — the `X-Assist-Token`; **must match the hardcoded `ASSIST_TOKEN` in `extension/background.js`**.
- Real identity: `extension/{profile.js,background.js}`, `data/{profiles.json,facts/*,etalons/*}`, `mailbox_passwords.json`,
  `uploads/`. Only `.example`/`.template`/`sample.json` committed.

## Cron (user `programmer`)
All lines `cd` into the LOWERCASE `/home/projects/jobfinder`. (Exception left dead on purpose: `mail_sink --poll`.)
- `*/2` `mail_sink --poll` — **LEGACY/dead-end** (nothing reads it; `/mail` is fed by `mail_indexer`). Safe to drop.
- `30 4` `mail_retention --days 30` → `logs/retention.log`; `*/10` `mail_health check` → `logs/health.log`.
- `30 5` `catalog_collector` — nightly collect → `job_catalog` (remote-only, tags `regions`+`open_anywhere`, GH questions inline) → `logs/catalog.log`.
- `15 6` `catalog_collector --backfill-regions` — LLM residue over `regions IS NULL` → `logs/regions.log`.
- `45 6` `catalog_forms --limit 200` — Playwright question scrape → `logs/forms.log`. (Argparse: don't use `choices=` with
  `nargs="*"` + non-empty default — bpo-9625 crashes; validate ATS names by hand after `parse_args`.)
- `0 7 * * 0` `applier.discovery` — weekly slug refresh → `logs/discovery.log`.
- `30 */6` `mass_hiring --collect` (`flock -n logs/masshiring.lock`) → `logs/masshiring.log`.
- `15 4` `prefill_retention --days 20` — delete old `uploads/prefill` artifacts → `logs/prefill_retention.log`.
- `*/20` `harvest_runner --platform amcat --limit 1 --concurrency 1` (`flock logs/harvest_amcat.lock`, `DISPLAY=:98`, `sg
  mail`) — AMCAT/TP harvest, paced 1 token/20min (bursts trip AMCAT **NE500**). Don't raise concurrency (parallel browsers
  fight the one virtual mic); don't enable `HARVEST_PROXY`. **MUST include `cd /home/projects/jobfinder` inside `sg mail -c`**
  (cron cwd is `$HOME`; `PYTHONPATH=.` alone → `No module named 'backend'`).
- `*/15` `health --alert` — probe `health.gather()` + Telegram owner on DOWN (throttled 4h) → `logs/health_alert.log`.
- `*/10` + `@reboot sleep 45` `tailscale_egress --sync --authkey file:backend/.ts_authkey` (`flock -n logs/ts_egress.lock`) →
  `logs/ts_egress.log` — reconcile the exit-node egress bridge (one local-SOCKS slot per online exit-node phone; self-heals
  dead daemons, boot-safe). Reads the REUSABLE key from `backend/.ts_authkey` (chmod 600, gitignored; owner-approved on disk
  for the cron — revoke in the Tailscale console to kill it). No `sg mail`/`DISPLAY` (userspace tailscaled, no TUN/mail/X).
- `8 1,7,13,19` `apply_campaign_cron` — recurring apply-campaign driver. INERT until a campaign exists; `per_day` caps the
  daily total across the 4 runs. `cd` + `DISPLAY=:98` + `sg mail`.
- **Mass-hiring apply lanes (5×/day, `DISPLAY=:98`, `sg mail`, fcntl-locked, per-lane logs) — HOUR-STAGGERED
  2026-09-12** so they don't all pile headful Chromium onto the single `:98` at once (they used to ALL run
  `1,6,11,15,20`; each runs for HOURS, so they overlapped → 46 chrome procs, load ~7-10, fills dying mid-run with
  `TargetClosedError`). Now on distinct hour-phases (minutes unchanged): Maximus `0 0,5,10,15,20` · TP `12 1,6,11,16,21` ·
  Kelly `24 2,7,12,17,22` · Taleo `54 2,8,13,19,23` · SR `36 3,8,13,18,23` · Workday `48 4,9,14,19,0` (≤2 lanes start any
  hour, ≥6 min apart). **When editing a lane change ONLY the schedule; keep the staggering.** (Not git-tracked; the live
  crontab is authoritative — back it up before editing.)
- **`*/30` `chrome_reaper`** (`tools/chrome_reaper.py` → `logs/chrome_reaper.log`) — SIGKILLs ORPHANED (ppid==1) chromium
  procs older than `--min-age` (default 3600s). A live fill's browser is a child of its co-pilot/worker (ppid≠1) so it is
  NEVER touched at any age; only genuinely-leaked orphans (a crashed fill's browser reparented to init — 13 were 6.6 DAYS
  old on 2026-09-12) are reaped. `--dry-run` prints without killing.
- **No catalog-auto-apply batch cron** (the `/catalog` bulk drain is operator-triggered; `apply_cli` is manual).

## Vacancies (Каталог + Mass Hiring + Незавершённые)
- **`/catalog` is the ONLY job-browsing surface**, DB-backed (`catalog_db.py` → `job_catalog`, no per-request egress). UI
  `catalog_ui.py`: top search live-filters (250ms debounce → `GET /catalog/more?q=&region=&offset=`); region chips / bulk /
  proxy pool live in a «Фильтры» MODAL (`#catSettings`). Light (~284KB): `description_html` lazy-loads per-card (`GET
  /catalog/{id}/desc`); `catalog_db.counts()` TTL-cached 60s.
- **Search by COUNTRY = eligibility, not text** (`regions.query_eligibility_regions` + `catalog_db.list_jobs`). A non-US/CA/UK
  country (e.g. «Казахстан») → `regions && ARRAY['OTHER']`; US/CA/UK map to their code. `job_catalog.open_anywhere BOOLEAN`
  (`applier/regions.open_anywhere`, ~93% precision) filters the OTHER bucket to jobs a Kazakhstani can actually apply to (TRUE
  for Kazakhstan/CIS/Central-Asia + worldwide-without-a-pin; FALSE for any named country/US-state/Europe/EMEA/APAC + timezone/
  relocation/on-site pins). Set at collect time; re-run `catalog_collector --backfill-open --all` after a rule change. Tests:
  `test_regions.py`.
- **Card actions:** cards carry NO per-card М/Ж or «Заполнить» (owner: manual apply off). A round `.cat-pick` checkbox builds a
  selection in `sessionStorage['cat_sel']`; ticking one reveals the fixed bottom bar `#catSelBar` = «Выбрано N · Все · Снять ·
  Кампания». **«Все»** (`selectAll(btn)`, `.cat-selbar-all`) puts the WHOLE current search into the selection (`GET /catalog/ids`
  → `list_job_ids`); «Снять» = `clearPicks`; «Кампания» opens the sheet. (The old top `#catSelAll`/`toggleSelAll` checkbox row
  was removed 2026-09-11 — select-all now lives IN the bar.)
- **Recurring apply CAMPAIGNS** (`tools/apply_campaigns.py`, `data/apply_campaigns.json`) + `apply_campaign_cron.py`. Applies
  daily under a fixed persona name, N/day, fresh résumé each. Kinds: `search` (N NEW jobs/day, only `_AUTO_ATS` greenhouse/
  ashby, excludes applied+submitted), `job` (N/day one job), `jobs` (a `/catalog` selection: `job_ids` + a persisted `cursor`
  = a position in the FULL list on both sides; `create()` round-robins across companies via `interleave_by_company`; `fcntl`
  lock). Budget guards: **confirmed-exclusion** (`note_run` accumulates `confirmed_jobids`, never re-serves a landed job),
  **quarantine-exclusion** (`quarantine_jobids` — jobs un-landable from the current egress: a hard captcha wall (binance-
  Lever) or a velocity/spam-quarantining tenant; skipped UNCONDITIONALLY by `_eligible`, even under `CAMPAIGN_SOLVE_CAPTCHA=1`,
  so enabling the solver for one ATS can't re-hammer a parked wall; reversible via `quarantine_jobs(cid, ids, on=)`),
  **attempt-cap** (≤ `per_day × CAMPAIGN_MAX_ATTEMPTS_FACTOR`=4), **conditional ATS filter** (`jobs` SKIPS lever/workable by
  default; `CAMPAIGN_SOLVE_CAPTCHA=1` to attempt all). A fill counts done only when `fill_counts_as_done` (state done AND
  confirmed); a `no_form` fill marks the posting dead. **Parallel lane:** the cron fans per-day targets across `bulk_pool`
  headless workers (`_fill_campaign_targets`), each a FRESH `(email,pid)` minted in-worker (`next_identity(cid)`, holds the
  fcntl lock). `CAMPAIGN_WORKERS` default 8, max 12; parallel within a campaign, sequential across. `next_identity` issues
  `first.last<N>@takhet.com` per fill (`email_mode='per_apply'` → many CRM cards, same name). Cron finishes the emailed code
  INLINE (`_do_fill(wait_submit=True)`). JOURNAL: `data/apply_campaign_events.json` + `GET /catalog/campaigns/{cid}/events`.
  Tests: `test_apply_campaigns.py`, `test_campaign_parallel.py`.
- **Custom persona NAME/identity (#4A):** `synth_persona(job, gender, name=, email=, pid=)` uses `name` verbatim; email/pid pin
  a stable identity (used by campaigns), threaded through `ensure_and_wire`→`_do_fill`/`_fill_one_on_worker`.
- **Незавершённые** (`/unfinished`): persistent ledger of applications that didn't confirm (`bulk_log.py`,
  `logs/unfinished.json`). «Докрутить (N)» drains (re-fill → finish), «Открыть вакансию», «Выполнено». Render does ONE
  `catalog_db.jobs_by_ids()` batch. Regression test `test_inline_js_syntax.py` runs `node --check` on every inline script (a
  dropped `catch` tail once SyntaxError'd the whole page + killed every button — keep it green).
- **Mass Hiring:** collect cron `30 */6` (flock); large-employer reference panel under the job list; on-demand apply modal
  («▶ Запустить подачу») → `mh_ondemand.py` reuses each lane's cron driver as a subprocess.
- **Design:** all three surfaces share `mailcrm_ui._page_head` + the pill switcher `.seg-nav.vac-seg` (`seg_html=`, keep the double-class specificity). ONE blue `button.primary` per header; secondaries `.iconbtn`/`.hbtn`; green = status.
- **Phone rework (owner is phone-first).** GLASS top bar with sub-tabs on top (`_topbar` renders `vac_subtabs(active)`, a
  segmented pill inside `.gm-topbar`); main menu is the standard ☰ drawer (a bottom nav was built + REMOVED — don't
  reintroduce; `--jf-tabbar` stays 0px). **In-place switching `jfSwap(url)`** (shell `_JS`) fetches the target, swaps its
  `<main>`, slides (animates `left` on `position:relative` main — NOT `transform`, which becomes the containing block of the
  fixed FAB/toast), `pushState`s + re-executes inline scripts; other tabs prefetched on idle, cache TTL 90s; hard fallback to
  real nav on non-OK/redirected/no-`<main>`. **Page-script re-entrancy contract:** `<head>` defines `window.jfPage`
  (AbortController per swap) + `window.__jfGen` (bumped per swap); a page script uses only `var`/function/`window.x=` at top
  level, registers listeners with `{signal:(window.jfPage||{}).signal}`, and every async poll chain captures `gen=window.
  __jfGen` ONCE + bails when it changed (`teardown()` clears `window._mhTimer` + `body.style.overflow`). `lastUrl[tab]`
  remembers each tab's `?region=`/`?q=` + scroll.

## Apply engine
`applier/runner.prefill_application`: tailor résumé → render PDF → open apply page (reuse saved Playwright session) → pick ATS
strategy → pre-fill every field → screenshot + `report.json`, then stop. Per-ATS strategies in `applier/strategies/`
(greenhouse/lever/ashby/workable/workday/icims/avature/oracle_orc/smartrecruiters/kelly/taleo) + `base.GenericStrategy`.
`applier/` imported live by the dashboard extension endpoints + `copilot.py`. Tailoring (`services/tailor/`) is strictly
no-fabrication. `/queue` defaults to `profile="michael"`.

**Auto-submit end-to-end by ATS** (ground truth = the ATS "Thank you for applying" email): GREENHOUSE ✅ + ASHBY ✅ (EMAILED
CODE, passable); LEVER ⛔ (hCaptcha) + WORKABLE ⛔ (Cloudflare Turnstile — a live captcha unsolvable from a datacenter IP; the
FILL is fixed, the human only solves the captcha). The dividing line is the final anti-bot step: email code vs live captcha.

**Mass-Hiring auto-apply feasibility.** Ceiling = "auto-fill + submit → a human does the assessment"; per-lane status in the
Auto-apply lanes section below. BLOCKED: cigna/humana/cvs/concentrix (register-step reCAPTCHA needs a solver key + residential IP).

## Gotchas

**Process / infra**
- **Restart `jobfinder-alan-copilot` after changing apply code.** The single co-pilot (8102) is a PERSISTENT process holding
  old `strategies/base.py`, `choices.py`, `dropdowns.py`, `analyzer.py` in memory (the bulk drain's HEADLESS workers respawn
  fresh, but the single co-pilot serving `/catalog` one-click + «Докрутить» keeps old code). Diagnosis: run one REAL `/load`,
  read `choice_picks[...].backed`/`review_items` — a should-be-backed pick showing `backed:false` = a stale process.
- **CRM Postgres pool:** `mail_db._get_pool()` maxconn **32** (`CRM_PG_POOL_MAX`), minconn 1; `conn()` wait-retries up to 5s.
  The parallel bulk lane + daemons + operator can exhaust a small pool → the blanket `except` falls to a slow live-Maildir
  disk scan + a `mail_health` alert. Do NOT lower maxconn to 8.
- **Local LLM default:** `ANTHROPIC_API_KEY` empty; résumé polish + answer drafting hit Sumrak at `127.0.0.1:8080/v1`
  (`sumrak-smart`); without the key, tailoring falls back to the deterministic keyword path.
- **Co-pilot ports are per-deploy** (Xvfb `:98`, x11vnc `5901`, noVNC `6090`, copilot `8102`). Pick host ports with `nginx -T | grep -oE '127.0.0.1:PORT'` (a grep of `sites-enabled` misses symlinked vhosts).
- **Proxy pool** (`tools/proxy_pool.py`): the `/catalog` 🛡️ panel parses+validates a pasted list → `data/proxies.json`.
  `next_proxy()` round-robins the lowest-`fails` tier; `_do_fill` picks one per fill; `copilot._use_proxy_context` builds a
  FRESH context (⇒ new IP) per proxy. **The persistent headful browser is launched PLAIN — do NOT re-add a
  `proxy={"server":"per-context"}` launch arg** (Playwright 1.49 makes every no-proxy context fail
  `ERR_PROXY_CONNECTION_FAILED` → "no internet" in noVNC with an empty pool; a per-CONTEXT proxy still works). socks5-with-
  auth won't route in the browser. Daemon `_start_proxy_revalidator` re-checks ~150/10min, evicts after 3 CONSECUTIVE
  failures. Tests: `test_proxy_pool.py`.
- **Proxy SOURCE = Bright Data, daily** (`tools/brightdata_proxies.py`, `BRIGHTDATA_*`): rotating zone = one gateway
  (`brd.superproxy.io:33335`) + a session id in the username. Active `alibaba_dc` ($0.60/GB); `alibaba_res` ($4/GB,
  residential) — switch via `BRIGHTDATA_ZONE`. Daily cron `45 4` refreshes (aborts without wiping if the balance/zone is
  dead). Small balance — top up in the BD dashboard.
- **Mobile-proxy POOL** (`tools/mobile_proxy.py`, `data/mobile_proxy.json` gitignored): the owner's phones over a
  Tailscale tailnet as residential/mobile egress. `live_servers()` (TCP-alive) is appended by `proxy_pool.residential_
  slots()`, so `_do_fill` walks `egress_candidates()` (live phones · datacenter pool · direct) — a dead phone never
  blocks a fill. CLI `mobile_proxy --check|--discover|--join-tailnet <KEY> --yes`. **Pipeline VERIFIED end-to-end
  2026-09-12** (real local SOCKS5 stand-in: httpx routes through `socks5://`, ASN-detect works, `egress_candidates`
  ranks phones first→pool→direct) and the pool is now `enabled:true` (armed; 0 phones → `live_servers()`=[] so current
  fills are unaffected, Health row = benign `info`, and `check_and_alert` alerts only on `down` so no Telegram spam).
  **Each phone must run a no-auth SOCKS5 *server* on the shared port (1080), reachable at its tailnet IP — Tailscale
  alone is NOT enough.** ANDROID = the workhorse: **Every Proxy** (SOCKS5, port 1080) on a phone joined to the SAME
  tailnet (`alikhanzhomartov.github`) → discovered + used automatically, CONCURRENT (per-fill rotation). **iPHONE CANNOT
  serve SOCKS** (iOS sandbox has no reliable inbound-listener app) → an iPhone's carrier IP is reachable only as a
  Tailscale EXIT NODE, which is a GLOBAL server route (one at a time, would hijack ALL server egress) — so iPhones do NOT
  fit the concurrent SOCKS pool without a userspace-`tailscaled`-per-exit-node bridge (NOT built). Recommend Android-only.
  Still BLOCKED on the owner: phones online (Tailscale + a SOCKS server app). Tests: `test_mobile_proxy.py`.
- **Exit-node egress bridge** (`tools/tailscale_egress.py`, `data/ts_egress.json` + `data/ts-egress/<slot>/` gitignored) —
  the COMPLEMENT of the SOCKS pool, for phones that can't run a SOCKS server (**iPhone**; Android too). Each phone that is a
  Tailscale **exit node** gets a dedicated **userspace** `tailscaled` on the server (own socket+state, NO root — no TUN),
  `-socks5-server=127.0.0.1:<base_port+slot>` (base 10800, loopback ONLY), auth'd with a REUSABLE key and PINNED
  `--exit-node=<phone_ip>` → that local SOCKS egresses via the phone's carrier IP. Runs one per phone CONCURRENTLY without a
  global `tailscale up --exit-node` (never hijacks the host's routing). `proxy_pool.residential_slots()` merges `live_socks()`
  (running slots, TCP-alive) beside `mobile_proxy.live_servers()` — same guarded/additive hot path. **The phones live on the
  KEY'S tailnet, NOT the host's main node's** (proven 2026-09-12: server on `alikhanzhomartov.github`, phones+key on
  `baimukhanalan1@gmail.com`) — so `sync(authkey)` discovers via a CLEAN transient no-exit probe joined with the key (a
  pinned egress slot is a corrupt vantage: its ACTIVE exit node reports `ExitNode=true`/`ExitNodeOption=false` and hides
  itself — do NOT discover through a slot). The host's main node is NOT moved. `sync` is **REAP-SAFE**: a failed/empty/partial
  netmap (`Peer` empty) reaps NOTHING (a blip must not nuke live slots). `up`/`down`/`down_all`/`running_slots`/`check`
  (egress+ASN per slot; `check` retries once — the first hop through a cold exit node is slow). **LIVE-PROVEN end-to-end
  2026-09-12** (iPhone slot egressed `2.133.170.183` = AS9198 Kazakhtelecom), BUT phones-as-exit-node are FLAKY: the iPhone
  dropped offline in ~15min (iOS backgrounds the app) → the always-on ANDROID on a charger is the reliable anchor; keep iOS
  exit-nodes foregrounded. A dead exit node's LOCAL SOCKS stays TCP-alive (advertised live but egress-dead) — the `*/10`
  `--sync` cron reaps it + rebuilds dead daemons (self-healing, boot-safe via `@reboot`). Key on disk at `backend/.ts_authkey`
  (chmod 600, gitignored; owner-approved 2026-09-12). **IP-DIVERSITY CAVEAT: devices on the SAME WiFi share ONE public NAT IP**
  (proven: iphone-13 + macbook both → `91.198.101.66` NLS-KZ) — for distinct egress IPs put phones on CELLULAR/mobile-data
  (iphone-14 on cellular gave a different IP `2.133.170.183` Kazakhtelecom). Health row «Exit-node мост (телефоны)». Owner go-live: phone → toggle «Use as exit node» + APPROVE in the admin console; mint a REUSABLE
  (ideally ephemeral) key; server `tailscale_egress --sync --authkey file:/path/key` then `--on`. The key is NEVER tracked or
  logged (masked everywhere, `file:PATH` accepted). Teardown matches the FULL socket path (a bare state-dir path prefix-
  matches sibling slots ≥10). Tests: `test_tailscale_egress.py`.
- **Public donor onboarding `/join`** (`tools/donor_onboard.py` + dash routes, allowlisted in `interviews/dash_auth.py`
  `_public_asset`) — a SHAREABLE no-login link (`https://jobs.systeam.kz/join`) turning any volunteer's phone into an
  exit-node donor. `GET /join` = phone-first page (iOS/Android-aware store link + a «Стать донором» button + the one manual
  `Exit Node → Run as exit node` toggle, which CANNOT be automated — no mobile API/deep-link). `GET /join/go` mints a FRESH
  single-use Tailscale invite server-side (`POST …/user-invites` `[{"role":"member"}]`) via `backend/.ts_api_token` and 302s
  the visitor into the app; rate-limited ≤5/IP/hr; token NEVER reaches the client. **Donor SAFETY (proven via ACL tests):**
  the tailnet ACL grants network access ONLY to `group:trusted` (owner); an invited donor is a plain `member` with NO grant →
  EXIT-ONLY, cannot reach any tailnet node; `autoApprovers` auto-approves their exit node; `--sync` adds them in ≤10min.
  **The API token EXPIRES (~90 days)** → `/join/go` mint breaks until refreshed (durable fix: a scoped OAuth client). Secrets
  `backend/.ts_api_token` + `.ts_authkey_public` are chmod 600 + gitignored (never commit). `tailscale ping` is NOT ACL-gated
  (disco-layer) — don't use it to judge isolation; the ACL `tests` block is the authority.

**Mail / CRM**
- **One live store, one dead.** LIVE: `mail_indexer` (inotify) → Postgres `mail_index` → `/mail` (`mailcrm.py` reads
  DB-first with a live-Maildir fallback; `mail_db.py` is the psycopg2 layer). DEAD (don't wire): `mail_sink*`,
  `dashboard_app._start_mail_poller` (never invoked), `tools/mail_dashboard.py`.
- **Replying: submission password comes from the DB, not the JSON cache** (`mailcrm.send`): `virtual_users.password_plain` is
  authoritative; `mailcrm.send` falls back to `provision_mailboxes.get_submission_password(email)` when `mailbox_passwords.
  json` misses. Do NOT reset passwords.
- **Classification** driven by editable phrases at `/mail/keywords` (`uploads/mail_keywords.json`); saving rewrites +
  reclassifies. `_normalise_phrase` maps smart punctuation to ASCII BEFORE casefold (curly `’`→`'`). A `code` kind (LAST in
  `KEYWORD_KINDS`, «🔑 Код») captures ATS "Security code" mail so `other` = genuinely unclassified. Defaults require explicit
  interview invitations (no broad "next steps"/"screening"). Changing the classifier needs BOTH dash + indexer restart, then
  `reclassify_existing()`; bump `CLASSIFIER_VERSION`. **GOTCHA: `keyword_rules()` caps each kind at `raw[:100]` phrases** —
  edit the lists via `save_keyword_rules(dict)` (normalises + caps + updates the mtime-keyed cache), NOT a raw `json.dump`; a
  raw edit that pushes a list past 100 SILENTLY drops the overflow (a phrase appended at #101 never fires). `classify` reads
  subject+body only (not the DB snippet), so a phrase present only in the snippet won't match. `reclassify_existing()`
  re-reads all 12k Maildir files + commits ONE batch at the very END (all-or-nothing, ~10min, fragile) — for a targeted fix
  reclassify only the affected `kind` bucket in incremental batches (`build_index_row`→`mail_db.update_kinds`, under `sg mail`).
- **Mail-render URL/HTML gotchas** (`_parse_full`/`_msg_card`, dashboard-only restart; tests `test_mailcrm_linkify.py`): a
  `text/plain` body can contain raw HTML → flatten via `_html_to_text` when it matches `_PLAIN_HTML_RE` (a fixed tag whitelist,
  so a bare `<a@b.com>` isn't treated as a tag). `_msg_card` does `_linkify(escape(plain))` and `escape()` turns `<`→`&lt;`, so
  `_URL_RE = https?://(?:&amp;|[^\s<&])+` (keeps a real query `&amp;`, STOPS at any other bare `&` like `&gt;`) + peel trailing
  `.,!?)`; `_linkify` only ever receives `escape()`d text. HTML-only scheduling links: `_html_links` pulls the http(s) anchors
  from the HTML part, `_extra_links_block` renders them ONLY when the plain body has no URL; the iframe fallback uses
  `sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"` + `<base target="_blank">` (NO `allow-scripts`).
- **Кандидаты = the primary tab, a candidate-grouped inbox** (`tools/candidates_inbox.py`, `/mail/candidates`). One card per
  persona (`mailcrm.candidate_groups` → `mail_db.candidate_groups`, `GROUP BY mailbox`); a card expands INLINE to its thread,
  each message opens inline, an inline «Собес» books; offset pagination. Design =
  **«Строгая классика»** (bordered spaced cards, ONE ellipsized preview line, `_clean_snippet` strips leaked CSS/HTML — a
  "flush dense panel" rework was REVERTED). The `.cg-metaline` row is QUIET (`nowrap`, fixed `min-height` so a 0-badge card =
  a 5-badge card): stage dot · «Собес»/«Назначено» · assessment control · 📄 apps chip (`_apps_chip` maps `mailbox`→`cid` via
  `candidate_apps.id_for_email`) · «✉ N».
- **Operator assessment control** on grouped cards (`_assessment_control` → `assessment_inner`): a pending TEST (matching
  `mail_db._TEST_SUBJECT_SQL` — any test/proctor/aptitude/amcat/harver/`video interview`/`magic link` subject) shows «✓
  Отметить»; marking re-tags rows `action_needed→assessment_done` so the item LEAVES «Действие» (shared helper
  `mailcrm.mark_assessment_done`/`unmark_...` writes `shl_assess_done.json` + `_reclassify_assessment`). **`mailcrm._TEST_
  SUBJECT_RE` (Python) MUST stay in sync with `mail_db._TEST_SUBJECT_SQL`.** Changing `_kind_with_done_override` needs BOTH
  dash + indexer restart. Routes `POST /mail/assessment/mark`/`/unmark`.
- **NEVER `TRUNCATE mail_index` to rebuild:** `mail_indexer.run_once()` only re-indexes CURRENT `candidates()`, so any
  mailbox whose registration was lost loses its rows permanently. Recover by re-registering every takhet.com maildir with
  mail, then re-index.

**UI / buttons / labels**
- **Button scale** (`mailcrm_ui._CSS` `:root`): `--ctl-h:40px/--ctl-px:16px/--ctl-fs:13.5px` (every real button), compact
  `--chip-h:32px` (toolbar chips), thin `--chip-sm-h:20px` (list-row badges) — never mix chip heights in one row; exceptions
  mobile FAB 52px + Собес cell 30px. `_page_head(title,count,primary,icons,meta,info)` is the canonical header (LEFT title-
  once + mono count + `.ph-meta` + ⓘ; RIGHT exactly ONE `button.primary` + `.iconbtn` secondaries; on mobile the title hides +
  the primary moves into the `.fab-compose` slot). `_KIND` is the ONE stage taxonomy (labels ≤10 chars: interview=«Собес»,
  action_needed=«Действие», assessment_done=«Тест сдан», code=«Коды»). `_fmt(n)`=space thousands-sep. Neutral RU labels only
  (no stack disclosure). Micro-animations + `touch-action:manipulation` (double-tap zoom off) live in `_CSS`, included on
  every surface via `_page`/`_doc`/`dash_auth._doc` — one edit restyles the whole platform.
- **Brand / PWA:** mark = serif interlocked "JF" white on `#0c47c2` (`static/logo.svg` + maskable + PNGs; rebuild
  `rsvg-convert -w N -h N logo-maskable.svg -o icon-*.png`; keep `theme-color` `#0c47c2` in sync across `manifest.
  webmanifest` + `_HEAD_PWA`). Install = `_HEAD_PWA` + `_SW_REG`; `GET /sw.js` + `/static/*` on the dash_auth public allowlist.

**Stats**
- **`/stats` — outcomes attributed BY COMPANY, keyed on the JOBID** (`tools/stats.py` + `stats_ui.py`). Unit = the jobid (one
  posting), NOT the persona email (the bulk lane retries under a fresh persona → counting personas inflates `applied` ~2× +
  dilutes rates). A job is counted once; outcome = the FURTHEST any persona got (offer>interview>action_needed>rejection>ack>
  other); `submitted`=`bulk_log.submitted_jobids()`∩jobids. Mail join: `uploads/prefill/demo_*/<jobid>/persona.json` email
  joins `mail_index.mailbox` (NOT `.candidate`). Blob TTL-cached (`STATS_TTL`=600s, background refresh, lock-guarded; `?refresh=1`
  forces); charts inline SVG/CSS only. A company literally named **OpenAI** is business DATA, fine. «По ролям» cut + posted-comp
  median (`role_category` + comp midpoint clamped $10k–$2M). role+comp classified deterministically at collect + backfilled once
  by a fleet; one-shot `catalog_collector --backfill-roles`/`--backfill-comp`/`--backfill-est-comp`. Tests: `test_stats.py`,
  `test_role_category.py`, `test_comp_extract.py`.
- **Comp display/extract** (`comp_fmt.py`, neutral labels): show the POSTED range («по вакансии») + the estimated TOTAL only
  when it exceeds the posted ceiling; no-posted-pay jobs show «база · оценка» + «total · оценка». Refresh `est_comp.py::_MED`
  if the market shifts; the nightly collect self-heals NULLs. `comp_extract`: `_NONBASE` vetoes equity/stock/sign-on/relocation/
  stipend/401(k); `_MIN_ANNUAL`=15000 floor; `_RANGE` tolerates a currency code + "and"; currency-aware (£/€/C$/A$; estimate USD).

**Fill engine / choices**
- **Comboboxes are `dropdowns.py`'s, NOT the analyzer's — never text-fill.** A GH `.select__container` input AND a Workable
  readonly `input[role=combobox]` are select widgets; `analyzer.py` skips both; `dropdowns.fill_comboboxes_known` owns them (a
  readonly combobox is opened+matched, never typed). Language-proficiency scales picked by CANDIDATE LEVEL (`_lang_option_
  rank`/`_cand_lang_rank`), never `opts[0]`. `dismiss_overlays()` runs in `base.prefill`. An Ashby geo typeahead returning 0
  options retries shorter queries (`_geo_shorten`). Tests: `test_dropdowns.py`.
- **Truthful deterministic negations/neutrals are BACKED** so they don't create a `choice_review`→`review_item` that blocks
  auto-submit (`deterministic_choices`, owner policy for SYNTHETIC personas): `_noncompete_pick`/`_prior_employer_pick`/
  `_sanctions_pick`→No; `_referral_pick`→neutral; `_privacy_notice_pick`/`_data_consent_pick`/`_consent_pick`(SMS)→affirmative;
  `_capability_pick`/`_yesno_pick`→Yes (backed in BOTH `deterministic_choices` AND the `choose_options` capability override).
  `_language_pick` B2 stays UNbacked; `_english_yesno_pick` runs BEFORE `_language_pick`. Real apply must NOT reuse the
  affirmative backing. Tests: `test_choices.py`.
- **Eligibility polarity** (`catalog_drafts._identity_choice`): `_SPONSOR_RE` (require/need sponsorship→No) vs `_AUTH_RE`/
  `_WITHOUT_SPON_RE` (authorized without sponsorship→Yes). **`_WITHOUT_SPON_RE` is checked FIRST + wins** (else "authorized to
  work without sponsorship now or in the future" inverts to a disqualifying No); don't put bare `visa sponsorship` in
  `_SPONSOR_RE`. Free-text auth / no-option typeaheads answered deterministically before the LLM; photo/medical/passport/
  diploma uploads → `human` (never the résumé). Tests: `test_catalog_drafts.py`.
- **Demographic self-ID gated by LABEL *and* OPTIONS** (`catalog_drafts._is_demographic`) — a synthetic persona NEVER claims a
  protected characteristic. Route a question matching `_DEMOGRAPHIC_LABEL_RE` OR ≥2 `_DEMOGRAPHIC_OPTION_RE` hits to human/blank.
  A REQUIRED demographic with a non-disclosure option is answered with it (`fill_demographics_decline` for radio/select/react-
  select/Workable-combobox; `fill_demographic_checkboxes_decline` for an EEO checkbox-group; `_DECLINE_RE` matches "do not
  **want** to answer"; TP's is "Opt Out"). `fill_required_consent` ticks a REQUIRED legal/privacy consent (`_CONSENT_RE`) but
  `_CONSENT_SKIP_RE` leaves marketing unticked; the veto is carved out for a demographic-DATA-consent box
  (`_DEMOGRAPHIC_CONSENT_RE` — consenting to PROCESS a declined survey claims nothing). All four demographic regexes use
  `rac(e|ial)`+`ethnic`, `\bcity\b`, `latin[ox]?\b(?!\s*americ)` (bare "Latin" matched "Latin American country"). Tests:
  `test_dropdowns.py`, `test_catalog_drafts.py`.
- **Required Cover Letter filled for SYNTHETIC personas only** (`demo_` prefix): `materialize_prefill` injects the generated
  `cover_letter`; `dropdowns.fill_cover_letter_known` fills a `<textarea>` or clicks GH "Enter manually" first. A real persona
  leaves it blank. **Structured GH Employment/Education block:** `materialize_prefill` supplies exact-label known answers from
  `experience[0]`/`education[0]`; `Current role=Yes` waives the End date (+ a `setdefault` net); the availability `_start_date`
  rule is negative-lookahead-guarded so it doesn't type `available_start` into a work-history year field. Tests:
  `test_dom_fixtures.py`, `test_catalog_drafts.py`.
- **Known-answer replay is EXACT-match-first** (`analyzer._best_known_answer`; a prior fuzzy hit cross-bound "authorized" Yes
  onto a sponsorship radio). Long Ashby Yes/No labels: prefix-tolerant (300 vs 200 truncation mismatch). `analyze_page` dedups
  `display_parts` on a NORMALIZED key. Generic placeholders ("Type your response") stripped from `display_text`
  (`_GENERIC_PLACEHOLDER`). **Ashby "Autofill from resume" clobbers screener fills** — `ashby.autofill_from_resume` waits until
  the form is STABLE (no `parsing/pending`, unchanged signature across two reads) before returning; PARTIAL (a later async
  pass can still clobber one screener); Ashby-only, verify with a `dry_run`.
- **Lever geocode 'Current location' is DEAD on this datacenter IP** — `is_lever_loc` always returns False (React clears
  both inputs on a >1.2s async reconcile; hCaptcha often blocks the click). `fill_form` returns `(success, fail,
  failed_required)` and `base.prefill` appends `failed_required` to `unfilled` so a REQUIRED location surfaces for the
  human. Do NOT fall through to a plain `.fill()`.
- **`[review]` prefix = hard safety contract.** Behavioral/"describe a time" answers must NEVER reach a live field
  unflagged. The small local model drops the prefix; `answers.py` re-adds it (`_NEEDS_REVIEW`); `strategies/base.strip_
  review` strips before fill + reports the flag. Don't weaken either side.
- **Profile reality gate** (`applier/profile_validator.py`) blocks prefill/apply for profiles with reserved-fictional
  phones (555-01xx) or placeholder emails. `michael` is the synthetic default and is gated. Onboard a real person in `/setup`.

**Auto-submit**
- **The co-pilot clicks Submit but ONLY when safe** (`copilot._click_submit_after_fill`). REFUSES (returns a `submit_result`
  reason, never raises): `incomplete` (any unfilled required field), `needs_review` (any `review_items` — the `[review]`
  contract), `page_drift`/`preempted` (the shared browser drifted — `_same_apply_page`/`_apply_identity` compare host+company).
  On a real click captures `after_submit.png` + `confirmed`/`blocked`. Finishes a GH-style emailed-code step (`_watch_submit` +
  `_click_code_confirm`) but NEVER touches a captcha. The EXTENSION + strategy layer are UNCHANGED (`installSubmitWatch` only
  records into `status.json`; regression test `test_no_auto_submit_path`). `CONFIRM_RE` in `content.js` has a copy in
  `copilot.py` — keep in sync. Turn OFF: no-op `_click_submit_after_fill`.
- **`no_button` on a "fully-filled" GH/Ashby job = a DEAD posting, not a detection bug.** A 404/expired page has 0
  extractable fields → `unfilled=[]` is vacuously "complete" + `find_submit_button` correctly returns None (do NOT loosen it).
  `_click_submit_after_fill` returns `reason="no_form"` when `page_type∈{expired,login_required,captcha}` or no form;
  `analyzer.detect_page_type` catches "find that page"/"job not found"/`error=true`; `_fill_one_on_worker` calls
  `catalog_db.mark_dead(...)` + `bulk_log.drop_many`. Tests: `test_analyzer_rules.py`.
- **`_SUBMIT_BLOCK_RE` must catch the real ATS rejection wordings** (`copilot.py`): captcha / "is required" / "please enter" /
  "flagged as possible spam" / "we couldn't submit" / "missing entry" / "needs corrections" / "please accept the terms" — a
  missed one is mislabeled `blocked=None` + burns the full `WAIT_SUBMIT_MAX`=300s. **Ashby anti-spam flags the DATACENTER IP
  itself — direct is NOT immune** ("flagged as possible spam" fires on a risk-scored share regardless of proxy; the ONLY fix
  is RESIDENTIAL `alibaba_res`). Owner keeps datacenter/direct (free); spam-flagged Ashby jobs are DROPPED not parked
  (`bulk_log._update_ledger`, `_SPAM_LEDGER_RE` narrow: `couldn't submit|flagged as possible spam|possible spam`) since a
  human can't finish them from noVNC either.
- **Bulk «Подать на все» = PARALLEL headless workers.** GH/Ashby fan out across `workers` headless browsers
  (`_do_fill_all_parallel` → `bulk_pool.start_workers(n)` spawns `backend.copilot` `COPILOT_HEADLESS=1` on ports 8110+, torn
  down at end). Hard `_PER_JOB_TIMEOUT`=360s. The emailed code is awaited INLINE (`/load?wait_submit=1`), NOT backgrounded (the
  next `/load` would `_cancel_watch`). Lever/Workable + non-`_PARA_ATS` are SKIPPED (not parked); the lane goes DIRECT.
  `bulk_log` thread-safe (`_LOCK` + `_atomic_write_json`). `workers` adaptive (`_do_fill_all_adaptive` seeds 4, ramps up to
  **`_ADAPT_MAX`=6**). **Do NOT raise `_ADAPT_MAX` without an LLM-latency probe** — the whole lane hits the ONE local LLM; at
  12-18 workers it serialized, per-fill ballooned to ~240s + blew the timeout.
  `_TRANSIENT_ERR_RE` → `_DRAIN_TRANSIENT_MAX`=6 retries. Endpoints: `POST /catalog/fill_all`(+`_status`/`_stop`).
- **Незавершённые ledger:** `bulk_log.py` → `logs/unfinished.json`. `submitted_jobids` (`logs/submitted_jobids.json`) = the
  anti-churn set — a confirmed submit / `mark_done` / the reconciler calls `mark_submitted(jobid)`, `_update_ledger` never
  re-parks it + `/catalog/fill_all` excludes it. **`confirmed=False` = "not DETECTED", not "not submitted"** (the page-watch is
  latency-bound): `submit_reconcile.reconcile_ledger()` checks ALL persona variants + `mail_index` for an ATS receipt then
  advances `status_store` + clears the job (thread `_start_submit_reconciler` ~150s + `POST /unfinished/reconcile`). **DRAIN**
  (`_drain_partition`, `POST /unfinished/rerun`): re-runs every fixable job (retry-cap `_DRAIN_MAX_RETRIES`=3), drops only
  truly-dead; per-cycle random batch → reconcile → drain → reconcile. Tests: `test_bulk_log_concurrency.py`.
- **Datacenter-IP reCAPTCHA-code ceiling** (distinct from Ashby-spam): a class of GH companies mails the code (entered fine)
  but a FINAL reCAPTCHA on the code step blocks the submit from a datacenter IP — no ack ever (samsara/fivetran/calendly =
  un-completable; standard `job-boards.greenhouse.io` companies complete fine). A human at noVNC can't finish one either.
  Follow-up: a per-company completion-rate SKIP.
- **Two live-DOM GH fill bugs (root-caused, NOT fixed — need live iteration + `dry_run` so working fills don't regress):**
  natera «End date month» react-select stays unfilled; coalition «served in the military?» react-select is missed by
  `_DEMOGRAPHIC` (no `military`/`served` keyword). Both live-only. natera (standard board) is the higher-value fix.

**Catalog / collector / regions / personas**
- **Custom-ATS form scrape: WAIT for the React form, then RETRY** (`tools/catalog_forms.py`): `_scrape`→`_wait_for_fields`→
  `_poll_stable` (poll the field count until nonzero + stable across two reads) → `_retry_loads` (reload ≤3×, keep the
  fullest). An empty scrape is never persisted as 0 questions; keep re-scrapes gentle (≤3 parallel); don't re-add a fixed sleep
  or touch the extractor `_JS`. Tests: `test_catalog_forms_wait.py`.
- **Collector keys postings by the ATS job `id`, NOT the URL tail** (`catalog_collector.collect_board` uses `job["id"]`,
  falling back to `_ext_id(url)` only when absent). Upsert key `(ats, company_key, external_id)`. Ashby/Lever apply URLs
  all end in `/application`/`/apply`, so URL-tail ids collapse a whole board to one row. Test: `test_collector_extid.py`.
- **Region classifier is LOCATION-FIRST** (`applier/regions.py`): `classify_regions` parses `location` first (a named place
  RESTRICTS eligibility); only an uninformative location falls back to text signals, then LLM residue. Don't let bare
  "global"/"worldwide" promote to all-four (`_WORLDWIDE_RE` strict). US state NAMES count as US; 2-letter codes don't
  (`, CA`/`, DE` collide). Tests: `test_regions.py`.
- **Greenhouse apply URL = the EMBED form, keyed by the gh_jid FROM THE URL** (`materialize_prefill` →
  `boards.greenhouse.io/embed/job_app?for=<company_key>&token=<gh_jid>`). The board URL 302-redirects to a company
  wrapper (→ 0 filled); `external_id` can be a collector sha1 fallback (a hash token 404s the embed) so extract `<gh_jid>`
  from `?gh_jid=`/`/jobs/<n>` in the stored URL. Residual: some companies embed on their own domain with no working GH
  endpoint (oscar/hioscar) → genuinely un-auto-fillable.
- **Real apply uses the roster GATE; the ETALON demo invents a fictional persona.** `catalog_drafts.pick_candidate` (REAL
  apply, `run(ideal=False)`) is strict: a US person only when `US ∈ regions`, else None (never send a US candidate to a
  foreign posting). `synth_persona.synth_persona(job)` (the one-click `/catalog` + `run(ideal=True)`) invents a FRESH
  fictional applicant per job. NATIONALITY matches the job's COUNTRY (`_country_of`: parse `location`, else region tag, else
  Kazakhstan; multi-country → Kazakhstan if listed else the first named; LatAm-exclusive via `_LATAM_RESIDENCE_RE` →
  `LATAM_COUNTRIES`) so work-auth answers are truthful. The NAME is OURS (`_pick_name` from per-country `_NAMES` banks,
  avoiding `data/demo_used_names.json`; pinned into the LLM prompt + force-set `raw["full_name"]=name` — do NOT let the LLM
  pick it). Email `first.last<NUM>@takhet.com`, phone reserved-fiction 555-01xx. `ensure_and_wire` provisions the mailbox
  (`provision_email` → a row in **MySQL `amasmail.virtual_users`**, the Dovecot backend, NOT Postgres) + registers the persona
  in `data/demo_personas.json` (`register_demo_persona`, THREAD-SAFE: lock + atomic replace — a bare read-modify-write raced
  under the parallel lane + dropped personas) + writes `uploads/prefill/<demo_id>/<jobid>/persona.json` (co-pilot `/load` loads
  it when `get_profile(demo_*)` KeyErrors). New demo personas need a `pm2 restart jobfinder-mail-indexer` to surface. A
  `demo_*` owner is PREEMPTIBLE in `/load`. Tests: `test_synth_persona.py`, `test_catalog_drafts.py`.
- **DB-lock outage remediation** (recurs): a leaked `idle in transaction` session holds a lock; a queued `ALTER TABLE`
  needs AccessExclusive + every reader queues behind it → `/mass-hiring` hangs + apply lanes stall. Fix:
  `pg_terminate_backend` on `idle in transaction` >5min + anything on the table >1h (check `pg_stat_activity` for a waiting
  `ALTER TABLE` first). Prevented by the DDL rule + `idle_in_transaction_session_timeout='10min'`.
- **`frontend/` (Vite/React) is NOT the deployed UI** — the live app is `dashboard_app.py`'s server-rendered HTML. The
  React app talks to `backend.main` `/api`, no inbox/roles, not deployed.

## Mass Hiring board connectors (`tools/mass_hiring.py`)
Human-apply remote-US board (`mass_hiring_jobs`), SEPARATE from auto-apply `job_catalog`. Each `fetch_X()` → `_mk_row(...)`;
the per-job decision is a PURE helper unit-tested with NO network (`tests/test_mass_hiring.py`). Two HARD RULES:
`_is_remote`/location must be REMOTE; `categorize()` must return a mass-hiring ENTRY bucket (drops senior/dev/clinical via
`_NOT_MASS`/`_DEV`/`_CLINICAL`; `_CARE_EXTRA` adds the health-insurer member-services lexicon). **Hide-Spanish** is a
reversible setting (`mh_settings.hide_spanish`, default ON) applied to display + apply. **⭐ Stable-comp marker:** `comp_type`
→ `variable` iff `sales` or a commission signal (`_COMMISSION_RE`) else `fixed`; a gold ★ + hourly rate (`_pay_html`: posted
via `to_hourly`, else a labeled `_HOURLY_EST`). `_parse_hourly_wage` reads TTEC's detail-page rate.
Source recipes (endpoint + gotcha):
- **Amazon:** `result_limit=100` (200→0), paginate `offset`; `is_remote=city.startswith("virtual")`, `is_us=country_code=="USA"`, `base_query="virtual"`.
- **Concentrix** (Workday `cnx`/`external_global`): empty `searchText` + US `locationCountry` facet.
- **Teleperformance** (Umbraco `www.tp.com/Umbraco/Api/Careers/GetCareersBase`, `node=1780&workFromHome=True&country=United States`): rows are iCIMS reqs.
- **TTEC** (Radancy `www.ttecjobs.com/.../results?keywords=remote`): `results` is an HTML fragment (bs4 `a[data-job-id]`); decide remote+US from the TITLE (the `.job-location` span is the home office). Pay via `_ttec_detail_pay` (6-worker pool); `upsert_jobs` COALESCEs pay new-first.
- **CVS** (Workday `cvshealth`): narrow with the `jobFamilyGroup` "Customer and Member Services" facet; remote = a bare 2-letter state prefix (`_has_us_state`).
- **Sutherland** (SmartRecruiters `api.smartrecruiters.com/v1/companies/Sutherland/postings`): keep `location.country=="us"` + `.remote==True`.
- **Working Solutions** (Algolia, public referer-restricted key + `Referer: apply.workingsolutions.com`): **AUTO-APPLY NOT BUILDABLE** (native PC-scan app + video = human-only).
- **Kelly** (WP REST `www.mykelly.com/wp-json/wp/v2/job-listings`): behind Akamai (403s our IP) → route through `proxy_pool.next_proxy()`, SKIPPED if the pool is empty; `acf.remote=="1"` + `acf.country_code=="US"`.
- **Maximus** (Avature, id 4, NOT Workday): two-step `GET /careers/Job-Search_US` (cookie jar; `data-props['desktop']` has a STABLE `uuid` + a per-session `qtvc` token that ROTATES — scrape fresh) → `GET /4/_portalList`. `_maximus_params` is a WHITELIST (context-value keys → HTTP 500); `total` is a STRING; RETRIES 3× (flakiest).
- **UnitedHealth/Optum** (Radancy `POST careers.unitedhealthgroup.com/search-jobs/resultspost`): a `FacetFilters` array (Remote+US; fc/fl GET params ignored).
- **Centene** (`centene`) + **Cigna** (`cigna`): `_fetch_workday` with the US country facet (`us_confirmed=True`); remote in the location/path. Cigna's facet is `Location_Country`, Centene's `locationCountry`.
- **Humana** (Phenom `POST careers.humana.com/widgets`, `selected_fields.city=["Remote"]`): keep `country=="United States of America"` + (`isRemote=="Yes"` OR `city=="Remote"`). Seasonal (AEP Oct-Dec).
- **himalayas:** RETRY the offset on intermittent non-JSON, don't `break` the pagination.
- **E-Verify large-employer REFERENCE** (`tools/everify_employers.py`, keyless `h1btrack.com/e-verify/employers/`): yields EMPLOYERS (a mass-hiring SIGNAL), never feeds `mass_hiring_jobs`. Cached, refreshed by a guarded weekly hook at the TAIL of `mass_hiring.collect()`. Manual: `everify_employers --refresh`.

## Auto-apply lanes (mass hiring)
Ceiling for all: real HIRE is human-gated by a later assessment.

- **Maximus / Avature** (`strategies/avature.py`, cron `mass_hiring_apply_cron`) — COMPLETES a real submission. Gated
  `AVATURE_ADVANCE=1` (advancing transmits PII + creates the account on the final Submit; a plain fill is side-effect-free).
  Fills the account password ×2, the `*`-labelled Terms checkbox, Yes/No screeners (`_SCREENERS`/`_answer_radio_screeners`,
  only UNANSWERED selects), Country-dependent State, select2 Languages/Skills; `_advance_wizard` walks the 3 steps declining
  demographics + answering truthfully + fills the final page before recording the Submit. `synth_persona` makes the persona
  LIVE at the job's city (`_city_from_title`). Tests: `test_avature.py`.
- **Maximus SHL/OPQ assessment auto-completion** (`tools/shl_assessment.py`) — the one fully-passable assessment (personality,
  no right answer). REAL headful browser (SHL rejects headless — `DISPLAY=:98` + `sg mail`). `run_intro(link,
  persona, *, complete_scored=)` fills the intro + (with `SHL_COMPLETE_SCORED`, SYNTHETIC only) hands to `answer_scored`.
  **HARD BOUNDARY: a COGNITIVE/KNOWLEDGE item is NEVER auto-solved** (`_is_ability_item` → `needs_human`). Human-like pacing.
  Answer bank `data/shl_answer_bank.json` (keyed on question+option-set, stores option TEXT). `tools/shl_assess_runner.py` =
  autonomous driver (`--drain` loops until none pending; file-locked). EVENT-DRIVEN: `mail_indexer._maybe_trigger_shl` spawns
  `--drain` on a fresh Maximus invite (restart the indexer after touching the hook). **Never mark completed on weak
  heuristics** (progress≥99, `/opq/` not in url, or a "Reminder" email — which LAGS past real completion); the ONLY truth is
  the portal "0 assessments left". Do NOT wire `talentcentral@shl.com` into the trigger (that's the TP AMCAT invite —
  cognitive, human-only). **PHONE-EGRESS ESCAPE HATCH (2026-09-12):** the SHL portal `integration-talentcentral.us.shl.com:443`
  TCP-BLOCKS the datacenter IP after volume (proven: direct connect times out; a phone slot reaches it, 403 on the bare URL =
  normal). `shl_assess_runner._shl_proxy(name)` now routes the headful browser through a LIVE phone egress slot
  (`proxy_pool.residential_slots()`, round-robin per persona; `SHL_PROXY` env overrides; None=direct when no phone live) — so
  event-driven + `--drain` completions survive the block. Was 0 completions on 09-12 (block) until this. Tests: `test_shl_assessment.py`.
- **Teleperformance / iCIMS** (`tools/icims_recon.py`, cron `mass_hiring_apply_tp_cron`) — FULL-AUTO server-side from the
  datacenter IP with paid NopeCHA (`ICIMS_NOPECHA=1 ICIMS_PROXY=` forces DIRECT; no tunnel). Walks Profile (+résumé + emailed
  code) → Questions → EEO → per-job screener → submit. **State/Province gotcha:** State is an AJAX searchable dropdown; the
  widget auto-fired `parentValue=-999` → "No states available". `_load_state_via_fetch` reads Country's committed value,
  `fetch()`s profileoptions with it, injects into the native `<select>` + syncs the fake overlay; because `_tp_fill` re-fills
  every tick + wipes it, **ADVANCE in the SAME tick** if residence is ready. Screener/EEO in `strategies/icims.py` (right-to-
  work→Yes, employed-by-TP→No, "certify true"→affirmative, EEO decline = "Opt Out"); `_pick_state` reads the job location.
  `--workers N` = N isolated Chromium on `:98`. Heavy on NopeCHA credit. **Post-apply assessment is AMCAT (cognitive) —
  human-only.** FREE path = `extension_tp/` (MV3 extension the owner runs himself: auto-fills a baked Ohio persona, NEVER
  clicks Next/Submit, owner solves the captcha; fed by `/tp_code` + `/tp_resume`). Tests: `test_tp_apply_cron.py`.
- **TTEC / Oracle Taleo** (`tools/taleo_recon.py`, cron `mass_hiring_apply_taleo_cron`) — full-auto, no captcha. Drives
  `ttec.taleo.net` (register → emailed code → info + languages → prescreening → WOTC → CC-305 → submit) to a real ack.
  `TALEO_ADVANCE=1`, headful. Doable set = English-only + Spanish/Russian-bilingual (`job_is_staffable`); prescreening selects
  via `taleo.py::_BASICS_JS`. Gotcha: `set(sel,re)` skips placeholder options (`!ph(o.text)`) — else a Yes/No matched "No
  Selection" + Save-and-Continue bounced. Tests: `test_taleo.py`.
- **Kelly** (`strategies/kelly.py`, cron `mass_hiring_apply_kelly_cron`) — full-auto login-less Gravity Form; the apply page
  loads through the SAME rotating BD datacenter pool that clears Akamai (`_PROXY_APPLY_HOSTS=("mykelly.com",)`; no residential).
  Fixes: dismiss the Cookiebot modal (`#CybotCookiebotDialogBodyButtonDecline`); split First/Last GF sub-inputs (`_fill_name`);
  a REQUIRED résumé-source radio ("Upload resume") clicked with a REAL Playwright click (a synthetic `.checked` leaves the
  branch file input disabled) then attach the PDF to the enabled branch input LAST; `attach_resume` is a no-op (the early
  generic attach re-triggers conditional logic + detaches the branch). Tests: `test_kelly.py`.
- **Sutherland / SmartRecruiters** (`strategies/smartrecruiters.py`, cron `mass_hiring_apply_sr_cron`) — full-auto to a real
  ack from the DATACENTER IP (patchright + NopeCHA, NO DataDome). The form is a SHADOW-DOM web-component app (`<spl-*>`/`<oc-
  button>`) → shadow-piercing walks throughout. Wizard buttons are `<oc-button data-test="footer-next|footer-submit">` (the
  only real `<button>`s are Apply-With-Indeed/LinkedIn) → `_PRIMARY_JS` shadow-walks + excludes them. Screeners:
  `_answer_spl_radio_groups`, `_answer_eeo_autocompletes` (type "prefer not"), `_tick_spl_consent` BEFORE the EEO comboboxes +
  re-tick (judge by component `value`, not native `.checked`), `_fix_phone_country` (E.164). GOTCHA: keep `grep -c 'async def
  _tick_spl_consent' == 1` (a duplicate stale def won). Driver `smartrecruiters_recon.py` submits only when `unfilled==[]`.
- **Centene / Workday CxS** (`tools/workday_recon.py` `drive_apply`, cron `mass_hiring_apply_workday_cron --tenant centene`) —
  HEADFUL persistent-context Chromium on `:98` (headless bounces back to the job posting). `_LIVE_TENANTS={"centene"}` (KEYLESS
  — no register reCAPTCHA). `WORKDAY_ADVANCE=1`, `--workers 1`; partial per-attempt success. Gotcha: after the activation link
  the Sign-In button is covered by an invisible `<div data-automation-id="click_filter" role="button">` intercepting pointer
  events — `_sign_in` submits several ways (Enter, JS `.click()` on the div then the button) stopping when the URL leaves
  `/login`; the generic submit is SKIPPED on `/login`/captcha/expired. cigna/humana/cvs/concentrix stay `_BLOCKED` (register
  reCAPTCHA needs a solver key + US residential IP). Tests: `test_workday.py`.
- **Oracle ORC / Alorica** (`strategies/oracle_orc.py`, driver `tools/orc_recon.py`, gated `ORC_ADVANCE`) — REACHABLE but NOT
  yet reaching an ack (NOT cron-wired). Redwood JET single-page; submit returns "15 issues" (fill doesn't commit). Blockers:
  the 555-01xx phone fails Oracle's libphonenumber (OWNER POLICY) + an unbuilt WOTC "Tax Credit Assessment". Higher-value next lane.

## Assessment question-bank HARVESTER (`backend/tools/assessment_harvester/`)
A separate engine (manual/cron, `DISPLAY=:98 sg mail`, nothing live imports it → no pm2 restart): enters a post-apply
assessment as a synthetic persona and BANKS every question + options into a unified corpus. Walks the free-response ceiling
with a fake mic/camera (`core._launch_args`). Package: `core.py` (harvest loop), `bank.py` (`data/assessment_bank.json`,
`schema_version 2`, platform-scoped media-aware dedup key, atomic write; `migrate_from_shl()` imported 263 OPQ items),
`discover.py` (invites over `mail_index`, burned tokens via `harvest_state.json`), `mic.py` (pulseaudio virtual mic),
`camera.py` (v4l2loopback virtual camera — the video twin of `mic.py`; feeds a dark `/dev/video0`),
`asr.py` (faster-whisper venv `~/.venvs/asr`), `answer_key.py`, `writex.py`, `import_qa_snapshot.py`, `adapters/{base,shl,
amcat}.py`. CLI `harvest_runner.py --platform amcat --limit 1` or `--url --mailbox`. Bank/media gitignored.
- **Reachability:** the rich surface is **AMCAT/TP** (`amcatglobal.aspiringminds.com`, from `talentcentral@shl.com`, single-use
  ES256-JWT autologin — open a FRESH token). Device-check PASSES with the fake mic+camera; walks the WHOLE battery (Diagnostic
  → SVAR ×4 → Typing → Personality → Basic Analytical → Sales). **Maximus SHL-OPQ** is the one passable assessment
  (etalon-automated).
- **Sutherland = SHL front-door → AMCAT; the WCI200 camera wall is BEATEN by a REAL v4l2loopback camera (2026-09-12 —
  SUPERSEDES the earlier "un-completable" verdict; do NOT re-conclude that Sutherland can't get past the camera).** The "Your
  Sutherland assessment invitation" (`talentcentral@shl.com`) autologins to `talentcentral.us1.shl.com`, walks a short SHL
  intro (cookies → Welcome → **About-You[Submit]** → an SHL webcam check a DIM feed passes), then REDIRECTS to the AMCAT player
  (`amcatglobal.aspiringminds.com`) whose continuous **WCI200** proctor demands a REAL camera DEVICE — NOT a face (Chromium's
  `--use-fake-device` was rejected at any brightness/egress). **THE FIX (root-caused):** `videodev` (v4l2loopback's dep) was
  simply NOT INSTALLED, not "absent from the kernel" — it is `CONFIG_VIDEO_DEV=m`, shipped in `linux-modules-extra-$(uname -r)`
  (Contabo strips the media subsystem from the base image). `apt install linux-modules-extra-6.8.0-110-generic` AND
  `-6.8.0-139-generic` (the GRUB-default-boot kernel — reboot-safe both ways) provides it; `v4l2loopback.ko` already ships in
  base `linux-modules`. `camera.py` loads `/dev/video0` (`exclusive_caps=1`, card_label "Integrated Camera") + an ffmpeg
  producer feeding a DARK/BLANK unlit feed; Chromium enumerates a real `Integrated Camera` (live 640x480 yuv420p);
  `sutherland_assessment.camera_launch_args()` prefers it (`SUTHERLAND_FAKE_CAM=1` forces the old fake path). Boot-persisted:
  `/etc/modules-load.d/v4l2loopback.conf` + `/etc/modprobe.d/v4l2loopback.conf` (options) + `/etc/udev/rules.d/90-v4l2loopback-perm.rules`
  (0666) → `/dev/video0` exists at boot on either kernel; `camera.ensure()` self-heals via `sudo -n modprobe`. **PROVEN:** the
  run no longer terminates at `blocked_proctor_camera`. **PENDING (in progress):** `run_sutherland` navigation past the webcam
  check INTO the AMCAT battery — after About-You it currently loops generic-forward to max_steps (`stuck`); wire the harvester
  `AmcatAdapter` to answer. Per the etalon boundary the AMCAT COGNITIVE items are needs_human (auto-pass only AMPI personality +
  SVAR + banked-replay). `sutherland_runner --persona X` drives it; the mic twin is `mic.py` (virtmic) for SVAR speaking.
- **GOTCHAS:** (1) the diagnostic SUBMIT `#submit1` is REUSED by SVAR modals — click it EXACTLY ONCE (`_diag_submitted`); rapid
  double-clicks → "logged out"; NEVER click TRY LATER (→ `MIC200` logout). (2) `handle_speaking`: speak ONCE per record window
  then go SILENT (continuous audio keeps SUBMIT disabled); Section D free-speech feeds a continuous passage, never SUBMITs
  while recording. (3) Section C MCQ options are `.option-lable`; the cognitive module's `.optionDiv:has(input.
  EliminatorMaskerOptionInput)` (radio `display:none` — click the visible option). (4) `_PERSONALITY_ANSWER_JS` takes ONE array
  arg. (5) Resume-safety: a token resuming inside a scored module marks the diagnostic done EACH gate iteration so the gate
  never re-clicks `#submit1`. (6) **The real gate = `NE500`** (per-IP daily rate-limit): heavy same-IP use logs sessions out at
  Section C/D; a fresh direct walk after the IP cools (~hours) goes through (not a proxy problem). Paced sequential, never fan out.
- **ANSWER KEY + REPLAY** (`answer_key.py`): every banked MCQ stores `answer_key={text,index,source,needs_vision}`; `core`
  REPLAYS by ANSWER TEXT (option order varies). TEXT → the local model; IMAGE → offline vision (the text model 422s on images);
  listening → whisper transcript + local model. New TEXT question → LIVE-solved + cached.
  Typing at 75 WPM. **qa_bot import** (`import_qa_snapshot.py`, source `data/qa_snapshot/`): Sales best/worst SJT + Analytical +
  WriteX email into the bank (Sales replay via `adapter.answer_best_worst` — the live AMCAT Sales DOM is a TODO). Tests:
  `test_import_qa_snapshot.py`, `test_writex.py`, `test_harvest_bank.py`.

## Interview Scheduler & Responsible Cabinet (`backend/interviews/`)
Assign an incoming interview (`kind=interview` mail) into a free slot of a "responsible" (the human who attends as the persona).
**Visibility == assignment** (a responsible sees a persona's mail ONLY because an `iv_interviews` row links them). **Per-person timezones:** `iv_responsibles.tz` (auto-detected via `/cabinet/tz`); availability is wall-clock in
that zone, the «Собес» grid drawn in the OPERATOR's zone (`?tz=`). Bridge = absolute UTC intervals (`slots.*`); `start_ts` stays tz-aware UTC.
- **Data** (Postgres `jobfinder_crm` via the `mail_db` pool, `db.py::ensure_schema`): `iv_responsibles` (login, bcrypt hash,
  name, `tz`, `telegram_chat_id`, `role` admin|employee, `active`), `iv_availability` (**MULTIPLE windows/weekday**, the
  `UNIQUE(responsible_id,dow)` was DROPPED; a window is same-day / OVERNIGHT (`end<start`, e.g. US hours) / 24h —
  `HOUR_START/END` is full 0–24, do NOT re-add an `end<=start` rejection or `end>start` filter, it drops night windows;
  `set_availability` REPLACE-ALL; shared editor `avail_editor.py`), `iv_interviews` (mailbox=persona addr=visibility key,
  thread_key, company, jobid, responsible_id, start_ts, status, reminded_60/5, announced; partial-UNIQUE `(responsible_id,
  start_ts) WHERE status<>'cancelled'` = double-book guard).
- **Operator** (inside `/mail`): a «Собес» control opens a MODAL (`operator_ui.py`) with the week grid of all responsibles'
  free slots → click a cell → pick a responsible → «Назначить». Routes `routes_operator.py` (`.../grid`, `.../assign` 409 on
  `SlotConflict`, `/cancel`, `/status`), `include_router`'d inside try/except (a broken import → "no button", never crashes the
  CRM). Booked → «Назначено · ФИО» reassigns (insert new then cancel prior).
- **Employee cabinet** (`routes_cabinet.py`, `prefix="/cabinet"`, merged into the dash — the separate 8103 app +
  `cabinet.systeam.kz` are RETIRED, the vhost 301s to `/cabinet`): `/cabinet`, `/availability`, `/inbox`, `/thread`, `/reply`.
  Ownership guard in `/thread` + `/reply` (`get_row(hash).mailbox in assigned_mailboxes(rid)` else 404); reply sent FROM the
  persona TO the recruiter (headers derived server-side).
- **Auth** (`dash_auth.py` `AdminAuthMiddleware`, fail-closed): no session → `/login`; `admin` → full; `employee` → ONLY
  `/cabinet/*` (`_employee_allowed`) else 303. Allowlist = extension endpoints + `/login`/`/logout`/`/favicon.ico` + public
  assets (EXACT-match). `current_responsible` re-checks `active` per request; `_install_dash_auth` FATAL when
  `IV_COOKIE_SECURE=1`. Keep the nginx `00-default-drop` `default_server` intact.
- **Пользователи `/users`** (`users_ui.py` + `routes_users.py`, ADMIN-ONLY): create/reset-password/set-role/link-telegram/
  toggle-active + a weekly availability editor + a 7-day load calendar. Auto-refresh via `GET /users/signature` (registered
  BEFORE `/users/{rid}`). A responsible with history → DEACTIVATED (FK preserves history); `interview_count==0` →
  hard-DELETABLE (guards: not self, not with-history). Logins `1`/`2`/`3` are REAL interviewers (Alan/Аружан/Нурбол) — do NOT
  delete. CLI `admin_cli`.
- **Telegram NOTIFIER** (`reminders.py`, pm2 `jobfinder-alan-ivremind`; `notify.py`): polls every 60s → assignment notice +
  reminders at −60 (RICH: company · role · persona ФИО · Zoom link from the thread · résumé PDF via `sendDocument`,
  `service.interview_pack`) and −5. Target = the responsible's `telegram_chat_id` else the owner chat. Self-service linking:
  cabinet «Привязать TG» mints a `tg_link_code` → `t.me/<bot>?start=<code>`; `notify.poll_updates()` (offset `logs/iv_tg_
  offset`) binds the chat_id (a bot can't DM by @username — the user must press Start). Token = `IV_BOT_TOKEN` else
  `TELEGRAM_BOT_TOKEN`. **Token-leak gotcha:** `notify.py` pins `httpx`'s logger to WARNING at import (it logs the full
  `bot<TOKEN>` URL at INFO) — don't lower it. Deploy: `UPDATE iv_interviews SET announced=TRUE` once before first start.
- NOT YET BUILT (Phase 3, deferred): auto-assign. Tests: `test_interviews_*.py` (live DB, `test_iv_%`-prefixed, run SEQUENTIALLY).

## Live findings (reality checks — don't re-conclude the opposite)
- **Salmon (Ashby `salmon-group`) is ACCEPTING, degraded by VELOCITY — NOT a strict-tier wall.** `mail_index` has 49 real
  Salmon inbound (8 acks + 7 human-recruiter interview invites) whose Aug acks PREDATE the proxy pool → same datacenter IP the
  campaign uses now. Sept silence = accumulated submission VELOCITY (a soft spam-drop, no banner). Cure = LOW per-company
  velocity + residential/mobile egress. Don't abandon Salmon or call it "unbeatable".
- **Captcha/egress:** NopeCHA (`COPILOT_NOPECHA=1`, default off; `campaign_captcha_probe.py`) only helps where a captcha is
  PRESENTED (TP/iCIMS). **binance-Lever uses an INVISIBLE enterprise hCaptcha that risk-DENIES both our datacenter IP AND a
  BD-residential proxy** (no challenge to solve) → only a real mobile-CARRIER IP beats it. Fill-gaps (`dropdowns.py`): a
  Workable marketing opt-in radio → decline via `marketing_optin_pick` (tight `_MARKETING_OPTIN_RE`, NOT the broad
  `_CONSENT_SKIP_RE`). **`_HARVEST_MARKETING_RADIO_JS` fixed 2026-09-11** for nogigiddy's required 'Daily Drop' radio (was
  leaving 35 Workable jobs silently un-submitted): resolve the question via `aria-labelledby` (the prompt `<span>` sits OUTSIDE
  the `<fieldset>`) + `stripText` the inline-SVG `<desc>` pollution ('SVGs not supported…'), else the harvested question is
  garbage → regex misses → radio blank → Workable silently rejects submit. NopeCHA-into-campaign wiring NOT done (needs a phone online).
- **takhet.com MX/DNS:** if persona acks stop landing across ALL lanes at once, check DNS first — inbound mail dies if the
  `mail.orta.study` A record (takhet's MX target) is dropped (fix = owner adds A `mail.takhet.com`→173.249.18.153 + MX
  `takhet.com`→`mail.takhet.com`; not a bot bug).
