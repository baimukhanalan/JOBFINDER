# JobFinder

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
- **Server instance (this shared Linux box, NOT the Mac):** pm2 `jobfinder-alan-dash` →
  `uvicorn backend.dashboard_app:app` on **127.0.0.1:8099**, launched from the repo ROOT with the
  system `/usr/bin/python3` (all deps already present; there is NO `.venv` here — `ecosystem.config.js`
  is Mac-only). nginx vhost **`jobs.systeam.kz`** (certbot SSL, basic-auth `/etc/nginx/.htpasswd-jobfinder`)
  → 8099. Surfaces in use: **`/mail`** (candidate inbox) + **`/roles`** (live ATS vacancy tables). The
  inbox fills from the takhet.com Maildir via the poll cron `*/2 * * * * sg mail -c '… mail_sink --poll'`;
  `/roles` fetches job rows live from the ATS APIs at request time (needs egress). Counts stay 0 and
  attribution stays empty until `uploads/prefill/**` and a real `backend/data/profiles.json`
  (+ `mail_sink --assign`) are seeded. **Two sibling dirs on this box, case-only-different — do not
  confuse them:** `/home/projects/JOBFINDER` (THIS repo, baimukhanalan, jobs.systeam.kz:8099) vs
  `/home/projects/jobfinder` (Abekemyn, jobfinder.systeam.kz:8089).

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

## Mail (candidate mailboxes) — self-hosted, NOT Mailgun
`backend/tools/mail_sink.py` gives each candidate a real address `<slug>@<MAIL_DOMAIN>`,
keeps a durable classified inbox, and backs the human-reviewed reply UI (`/mail` in
`dashboard_app.py`, rendered by `tools/mail_dashboard.py`). Two providers (`MAIL_PROVIDER`):
- `mailpit` — local throwaway sink, dev only.
- `selfhost` — our OWN Postfix/Dovecot/OpenDKIM server (no third party). **This replaced
  the former Mailgun provider, which was removed entirely.**

selfhost wiring (on the shared mail server, same stack amaskills uses):
- **Inbound**: Postfix accepts the whole domain; a catch-all virtual alias
  `@<MAIL_DOMAIN> → bot@<MAIL_DOMAIN>` funnels every `<slug>[+jid]@` into ONE Maildir.
  `mail_sink._poll_maildir` reads it off disk via the vendored `tools/_maildir.py`
  (stdlib only) and attributes each message by its original `To` header
  (`resolve_application` handles the `+<jid>` per-application tag). No IMAP, no webhook.
- **Outbound**: `send_reply` → `sh_send` submits to `127.0.0.1:587` (STARTTLS + SASL as
  `MAIL_SMTP_LOGIN`); OpenDKIM signs. From = the candidate address (Postfix
  `smtpd_sender_login_maps` is empty, so the one submission account sends as any `@domain`).
- **Provisioning**: none per-candidate — the catch-all + one submission account (`bot@`)
  cover every address. `mail_sink --assign` only generates the `<slug>` labels; `--poll`
  merges the Maildir into the durable store; `--status` shows health.

## Gotchas
- **Run from the repo ROOT** — imports are absolute `backend.*`. The old `cd backend && uvicorn
  main:app` is BROKEN (`ModuleNotFoundError: backend`). Correct: `uvicorn backend.main:app` /
  `python -m backend.apply_cli ...` from `/home/projects/jobfinder`.
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
- **Self-hosted mail: catch-all + poll under `sg mail`.** Dovecot does NOT do a static-userdb
  catch-all here — an unprovisioned address bounces `550 User doesn't exist`. Inbound therefore
  relies on a Postfix catch-all alias `@<MAIL_DOMAIN> → bot@<MAIL_DOMAIN>` funnelling everything
  into one Maildir. The poll must run where `/var/mail/vhosts` is readable (it is `vmail:mail 2770`):
  `*/2 * * * * sg mail -c 'python -m backend.tools.mail_sink --poll'`, exactly like `inbox_index.py`.
  The dashboard runs as `programmer` (no `mail` group) so it SKIPS its in-process poll for `selfhost`
  and just reads the store the cron fills. Bringing up a NEW mail domain needs, on the server:
  add it to Postfix `virtual_mailbox_domains` + a catch-all alias in `/etc/postfix/virtual`, an
  OpenDKIM key (`opendkim-genkey` + SigningTable/KeyTable), one submission mailbox in a Dovecot
  passwd-file (mode 640 root:dovecot); and in DNS: `MX`, `SPF`, `DKIM` (mail._domainkey), `DMARC`.
