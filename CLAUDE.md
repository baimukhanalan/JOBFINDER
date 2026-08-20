# JobFinder

> **Repo / GitHub — read first.** This project lives at **`baimukhanalan/JOBFINDER`**
> (`https://github.com/baimukhanalan/JOBFINDER`). The account is **`baimukhanalan`**, NOT `Abekemyn`
> like the other `/home/projects/*` repos. The PAT is embedded in the `origin` remote URL (see
> `git remote -v`), so `git push` works as-is — do NOT paste the token into any tracked file.
> **Convention: after any nontrivial change, `git add -A && git commit && git push` to this remote
> straight away — don't let work sit uncommitted — and edit THIS `CLAUDE.md` in the same commit
> whenever deploy / behavior / gotchas change.** Directory is uppercase `/home/projects/JOBFINDER`.
>
> **This IS the live project** (`jobs.systeam.kz`, pm2 `jobfinder-alan-*`, Postgres `jobfinder_crm`).
> The old lowercase `/home/projects/jobfinder` (repo `Abekemyn/jobfinder`, the `michael` persona,
> `jobfinder.systeam.kz`) was **RETIRED & archived 2026-08-20** — its pm2 / nginx vhost / cron were
> removed and the dir moved to `/home/projects/jobfinder.archive-2026-08-20-2158`. Treat any lingering
> lowercase-`jobfinder` path, `jobfinder.systeam.kz`, `:8089` or display `:99` reference in the
> sections below as STALE: the live stack is the Alan one — dash **:8099**, co-pilot **:8102**, noVNC
> **:6090**, display **:98** (see the "Alan's co-pilot/noVNC" gotcha for the authoritative ports).

Semi-automatic job-application engine for remote US/CA roles: scrapes openings, tailors a
résumé per JD, **pre-fills** the ATS form (never submits), and a human reviews + clicks Submit.
Three surfaces: a mobile review dashboard, a one-click browser extension, and a headful
"co-pilot" Chromium watched over noVNC.

Stack: Python 3.12 · FastAPI · SQLAlchemy 2.0 async + asyncpg/Postgres · Playwright · aiogram
(Telegram) · python-jobspy. Résumé tailoring + answer drafting use the **local Sumrak LLM**
(`llm_*` in `config.py`), not the Anthropic API (key is empty).

## Deploy (pm2, NOT systemd)
- `jobfinder-dash` → `uvicorn backend.dashboard_app:app` on **127.0.0.1:8089** — the real
  user-facing app (mobile review queue + all extension endpoints). No DB, no auth: reads
  `uploads/prefill/<profile>/*/report.json` + a `status.json` overlay + `uploads/inbox/*.json`.
- `jobfinder-copilot` → `uvicorn backend.copilot:app` on **127.0.0.1:8096** with `DISPLAY=:99`
  (headful Chromium the bot pre-fills; human watches via noVNC and submits).
- `jobfinder-display` (stopped by default) → `vnc/copilot_display.sh`: Xvfb `:99` + fluxbox +
  x11vnc `:5900` + websockify noVNC `:6080`. Bring up with `vnc/start_display.sh` if noVNC dies.
- nginx vhost `jobfinder.systeam.kz` (certbot SSL): `/` → 8089, `/copilot/` → 8096, `/vnc/` →
  6080. **basic-auth** (`/etc/nginx/.htpasswd-jobfinder`) on everything EXCEPT the extension
  endpoints (`/assist /draft /profile_form /job_pack /resume_file /mark_ext /health`), which are
  auth_basic off and guarded by `X-Assist-Token` instead (called cross-origin from job sites).
- `backend.main:app` (jobs API + APScheduler) exists but is **not deployed / not in nginx**;
  scraping runs from cron directly. Treat it as the legacy/DB layer.

## Secrets & PII (all gitignored)
- `backend/.env` — `DATABASE_URL`, `TELEGRAM_BOT_TOKEN/CHAT_ID`, `DO_API_KEY`, `PROXY_URL`,
  `APPLY_PROXY`. `config.py` uses `extra="ignore"` (tolerates leftover archived keys).
- `backend/.assist_token` — the `X-Assist-Token` value; **must match the hardcoded `ASSIST_TOKEN`
  in `extension/background.js`** (both sides checked). Change one → change both.
- Real identity lives in `extension/profile.js` + `extension/background.js`,
  `backend/data/profiles.json`, `backend/data/facts/*`, `backend/data/etalons/*`, and `uploads/`.
  Only `.example`/`.template`/`sample.json` are committed. Regenerating `background.js` from the
  example loses the live token.

## Cron
- `*/5` `sg mail -c 'python3 backend/inbox_index.py'` — classify each profile's Maildir → inbox feed.
- `0 8,16` `python -m backend.apply_cli --batch --profile all --source both --limit 60 --draft --ai` — refill the review queue.
- `0 7 * * 0` `--discover` (company/slug mining) · `30 6 * * *` `python -m backend.scrapers.manager` (scrape).

## Apply engine
`applier/runner.prefill_application`: tailor résumé → render PDF → open apply page (reuse saved
Playwright session) → pick ATS strategy → pre-fill every field → screenshot + `report.json`, then
**stop**. Per-ATS strategies in `applier/strategies/` (greenhouse, lever, ashby, workable, workday,
icims) + `base.GenericStrategy` fallback. `applier/batch.py` turns the roster into a review queue
with cross-run dedup; postings in terminal statuses (`submitted`/`rejected`/`interview`) are never
re-queued. Tailoring (`services/tailor/`) is strictly no-fabrication.

## Gotchas
- **Run from the repo ROOT** — imports are absolute `backend.*`. The old `cd backend && uvicorn
  main:app` is BROKEN (`ModuleNotFoundError: backend`). Correct: `uvicorn backend.main:app` /
  `python -m backend.apply_cli ...` from `/home/projects/JOBFINDER`.
- **No auto-submit — by design.** The engine only pre-fills; a human reviews the screenshot, solves
  CAPTCHAs/assessments, and submits. The auto-submit path was deliberately removed (commit
  `a8ab56e`) — real submits from a datacenter IP get spam-flagged/banned. Do not re-add it.
- **Profile reality gate.** `applier/profile_validator.py` blocks batch prefill AND dashboard
  apply affordances for profiles with reserved-fictional phones (555-01xx) or placeholder emails —
  applications from them are undeliverable by construction. A blocked profile logs
  `Batch blocked for '<id>'` 2x/day and Telegram-nags until fixed in `/setup`. This is intentional:
  do NOT bypass the gate; onboard a real person instead.
- **Submit DETECTION is not submission.** `copilot.py`'s confirmation poller and the extension's
  `installSubmitWatch` only *record* a human's submit into `status.json` (entries now carry `ts`;
  `inbox_index.py` also auto-marks `submitted` on a matched ATS ack email, never downgrading a
  status). None of these click anything — keep it that way. `CONFIRM_RE` lives in
  `extension/content.js`; `copilot.py` carries a copy that must stay in sync.
- **Alan's co-pilot/noVNC is its OWN stack** (this uppercase `/home/projects/JOBFINDER` deploy,
  `jobs.systeam.kz`), physically separate from the lowercase `jobfinder` one. pm2:
  `jobfinder-alan-copilot` → `uvicorn backend.copilot:app` on **127.0.0.1:8102** with `DISPLAY=:98`
  (under `sg mail`, cwd uppercase); `jobfinder-alan-display` → `vnc/copilot_display.sh` = Xvfb `:98`
  + x11vnc **5901** + websockify noVNC **6090**. nginx `jobs.systeam.kz`: `/copilot/ → 8102`,
  `/vnc/ → 6090` (both behind the dash basic-auth; prefix-stripped by a trailing-slash `proxy_pass`).
  The dashboard's "Open in co-pilot" button opens `/copilot/?profile=X` + POSTs `/copilot/load`.
  Display/ports MUST differ from the lowercase stack (`:99`/5900/6080) — `copilot.py` and the vnc
  scripts are per-deploy copies, so a shared display would clobber the other reviewer's browser.
- **Pick host ports with `nginx -T`, not `grep -r sites-enabled`.** `grep -r` does NOT follow the
  symlinks in `sites-enabled`, so it silently misses most vhosts. A port can have nothing listening
  yet still be claimed by a live vhost whose backend is down (e.g. `lalafo-vnc.systeam.kz` proxies to
  6081 → squatting it leaks that domain onto our noVNC). Always `nginx -T | grep -oE '127.0.0.1:PORT'`
  for the authoritative in-use list before choosing.
- **Queue staleness cutoff.** `batch.py` archives pending items whose `report.json` is older than
  `STALE_DAYS=14` into `uploads/prefill/<profile>/archived.json`; archived URLs never re-enter as
  "new". The Telegram digest deep-links to `/#job-<jid>` anchors rendered by the dashboard.
- **`inbox_index.py` must run under `sg mail`** — the Maildir is `vmail:mail 2770`. It reuses the
  amaskills CRM reader (`/home/projects/amaskills/crm/maildir_reader.py` on `sys.path`) and writes
  `uploads/inbox/<profile>.json` so the dashboard (runs as `programmer`, no mail group) can read it.
  Mailbox→profile mapping comes from the `mailbox` field in `profiles.json`; with none it falls back
  to a hardcoded `michael` mailbox.
- **The `[review]` prefix is a hard safety contract.** Behavioral / "describe a time" / specifics
  answers must NEVER reach a live field unflagged — the whole trust model is "the human only
  reviews flagged answers." The local model is small and drops the prefix, so `answers.py` re-adds
  it deterministically (`_NEEDS_REVIEW`); `strategies/base.strip_review` strips it before fill and
  reports the flag. Don't weaken either side.
- **Local LLM default.** `ANTHROPIC_API_KEY` is empty; résumé polish (`--ai`) and answer drafting
  (`--draft`) hit Sumrak at `127.0.0.1:8080/v1` (`config.llm_url/llm_model=sumrak-smart`). Without
  the key, tailoring falls back to the deterministic keyword path.
- **`frontend/` (Vite/React) is not the deployed UI** — the live app is `dashboard_app.py`'s
  server-rendered HTML. (Port 4001's next-server is janyl, not this project.)
