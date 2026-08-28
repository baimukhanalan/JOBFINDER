# Interview Scheduler + Responsible Cabinet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator assign an incoming interview (a `kind=interview` mail) into a free time-slot of a responsible person from a modal inside the existing `/mail` inbox — modelled on orta.study's lesson-slot assignment — and give each responsible a cabinet showing only their assigned personas' mail plus Telegram reminders.

**Architecture:** A new isolated package `backend/interviews/` (db, slots, auth, service, operator UI, cabinet app, bot, reminders). The operator surface is new routes + a modal added to the live dashboard app (behind basic-auth), no new nav tab. The responsible cabinet is a separate FastAPI app on :8103 with its own cookie session behind an `auth_basic off` nginx location. Visibility == assignment (orta pattern): a responsible sees a persona's mail only because an interview row links them. Slots/times are GMT.

**Tech Stack:** Python 3.12, FastAPI 0.135, psycopg2 (via `backend.tools.mail_db` pool → Postgres `jobfinder_crm`), `bcrypt` 3.2, `itsdangerous` 2.2 (signed session cookie), `aiogram` 3.22 + `apscheduler`/daemon thread (layer 2), server-rendered HTML+CSS mirroring `backend/tools/catalog_ui.py` modal + orta grid. pytest in `backend/tests/`, run from repo root.

**Spec:** `docs/superpowers/specs/2026-08-28-interview-scheduler-design.md`

## Global Constraints

- Run everything from repo root; imports are absolute `backend.*` (e.g. `python -m pytest backend/tests/test_interviews_slots.py`).
- Do NOT modify shared `backend/tools/mailcrm.py` read functions; the cabinet ownership guard wraps them.
- Reuse the `mail_db` psycopg2 pool (`from backend.tools import mail_db; with mail_db._cur() as cur:`), do NOT open a second pool. minconn stays 1.
- All new tables `CREATE TABLE IF NOT EXISTS`, prefixed `iv_`. Never `TRUNCATE`/`DROP` anything.
- All user-facing text (operator + cabinet + bot) is neutral Russian — no stack names (Claude/AI/LLM/etc). Code/comments/commits in English.
- Weekday convention **0=Mon..6=Sun** everywhere (Python `date.weekday()`), stored times/`start_ts` in **UTC**; availability minutes are GMT minutes-from-midnight.
- Bind new services to `127.0.0.1` only. New secrets go in `backend/.env` (gitignored), read via `config.py` (`extra="ignore"`).
- Commit after each task: `type(interviews): …`, no assistant attribution trailer, push to `baimukhanalan/JOBFINDER` `main` at natural stopping points.

---

## Phase 1 — MVP

### Task 1: DB layer — schema + queries

**Files:**
- Create: `backend/interviews/__init__.py` (empty), `backend/interviews/db.py`
- Test: `backend/tests/test_interviews_db.py`

**Interfaces — Produces:**
- `ensure_schema() -> None` (idempotent; creates `iv_responsibles`, `iv_availability`, `iv_interviews` per spec).
- `add_responsible(login, password_hash, name, tz='UTC') -> int` (returns id; raises on dup login).
- `get_responsible_by_login(login) -> dict | None`; `get_responsible(rid) -> dict | None`; `list_responsibles(active_only=True) -> list[dict]`.
- `set_telegram_chat(rid, chat_id) -> None`.
- `get_availability(rid) -> list[dict]` (7 rows, missing days filled `enabled=False`); `set_availability(rid, rows) -> None` (rows: `[{dow,start_min,end_min,enabled}]`, UPSERT on `(responsible_id,dow)`).
- `insert_interview(mailbox, responsible_id, start_ts, end_ts, company, jobid, thread_key, source_message_hash, notes='') -> int` (raises `IntegrityError` on the partial-unique double-book).
- `interviews_for_responsible(rid, upcoming_only=False) -> list[dict]`; `assigned_mailboxes(rid) -> set[str]`; `interview_for_thread(mailbox, thread_key) -> dict | None`; `booked_intervals(rid, since, until) -> list[tuple[datetime,datetime]]`.
- `due_reminders(now, window_min) -> list[dict]` (layer 2 uses it; return assigned, not-cancelled, `start_ts` in `(now, now+window]`, matching flag unset).
- `mark_reminded(interview_id, which) -> None` (`which ∈ {'60','5'}`).

**Consumes:** `backend.tools.mail_db._cur`.

- [ ] Step 1: Write failing test `test_schema_idempotent_and_responsible_roundtrip` — call `ensure_schema()` twice, `add_responsible('lara','h','Lara')`, assert `get_responsible_by_login('lara')['name']=='Lara'`, dup login raises.
- [ ] Step 2: Run → fail (module missing).
- [ ] Step 3: Implement `db.py` (DDL from spec; parametrized SQL via `mail_db._cur()`).
- [ ] Step 4: Add tests `test_availability_upsert_fills_missing_days`, `test_insert_interview_double_book_raises`, `test_assigned_mailboxes_and_thread_lookup`, `test_due_reminders_window`. Run all → pass.
- [ ] Step 5: Commit `feat(interviews): postgres schema + query layer`.

> Test note: these tests hit the live `jobfinder_crm` DB. Use a throwaway login prefix `test_iv_%` and clean up in a fixture (`DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'` cascades). If `CRM_PG_DSN` is unset in CI, `pytest.skip`.

### Task 2: Slot logic (pure, GMT)

**Files:**
- Create: `backend/interviews/slots.py`
- Test: `backend/tests/test_interviews_slots.py`

**Interfaces — Produces:**
- Constants `HOUR_START=8`, `HOUR_END=20`, `DURATION_MIN=60`.
- `week_dates(monday: date) -> list[date]` (7 dates Mon..Sun).
- `cell_start_utc(d: date, hour: int) -> datetime` (tz-aware UTC).
- `is_free(avail_rows: list[dict], booked: list[tuple[datetime,datetime]], d: date, hour: int) -> bool` — weekday enabled AND `[hour*60, hour*60+DURATION_MIN)` inside `[start_min,end_min)` (in GMT) AND no `booked` interval overlaps `[cell_start, cell_start+DURATION)`.
- `free_grid(per_resp: dict[int, tuple[list[dict], list[tuple]]], monday: date) -> dict[str, list[int]]` — key `"YYYY-MM-DD:HH"`, value = sorted free responsible ids.
- `overlaps(a_start, a_end, b_start, b_end) -> bool`.

**Consumes:** stdlib only (`datetime`, `zoneinfo`).

- [ ] Step 1: Failing tests: `test_is_free_respects_window` (avail 09:00–17:00 → hour 8 & 17 not free, 9 & 16 free), `test_is_free_blocks_on_booking` (a booking at that cell → not free), `test_overlaps`, `test_free_grid_lists_only_free_responsibles`, `test_cell_start_utc_is_utc`.
- [ ] Step 2: Run → fail.
- [ ] Step 3: Implement pure functions.
- [ ] Step 4: Run → pass.
- [ ] Step 5: Commit `feat(interviews): pure GMT slot/grid/conflict logic`.

### Task 3: Auth (bcrypt + signed-cookie session)

**Files:**
- Create: `backend/interviews/auth.py`
- Test: `backend/tests/test_interviews_auth.py`

**Interfaces — Produces:**
- `hash_password(pw: str) -> str`; `verify_password(pw: str, h: str) -> bool` (bcrypt).
- `make_session(rid: int) -> str`; `read_session(token: str, max_age=..) -> int | None` (itsdangerous `URLSafeTimedSerializer`, secret from `config.settings.interview_session_secret` or env `INTERVIEW_SESSION_SECRET`; tampered/expired → None).
- `current_responsible(request) -> dict` FastAPI dependency: reads cookie `iv_session`, `read_session`, `db.get_responsible`; on failure raises `HTTPException(303, headers={'Location':'/cabinet/login'})` (redirect). `COOKIE_NAME='iv_session'`.

**Consumes:** `backend.interviews.db`, `backend.config.settings`.

- [ ] Step 1: Failing tests: `test_bcrypt_roundtrip`, `test_session_roundtrip`, `test_tampered_session_rejected`, `test_expired_session_rejected` (max_age=−1).
- [ ] Step 2–4: implement; add `interview_session_secret: str = ""` to `backend/config.py Settings`; run → pass.
- [ ] Step 5: Commit `feat(interviews): bcrypt + signed-cookie session`.

### Task 4: Responsible admin CLI

**Files:**
- Create: `backend/interviews/admin_cli.py`
- Test: `backend/tests/test_interviews_admin_cli.py`

**Interfaces — Produces:** `python -m backend.interviews.admin_cli add --login L --name N [--password P] [--tz UTC]` (prints a generated password if omitted, `openssl`-style `secrets.token_urlsafe`), `... list`, `... passwd --login L [--password P]`, `... setavail --login L --dow 0 --start 09:00 --end 17:00` (helper `hhmm_to_min`). Functions `cmd_add/cmd_list/cmd_passwd/cmd_setavail` importable + a `main(argv)`.

**Consumes:** `db`, `auth.hash_password`.

- [ ] Step 1: Failing test `test_cli_add_creates_responsible` (call `main(['add','--login','test_iv_bob','--name','Bob','--password','x'])`, assert `db.get_responsible_by_login('test_iv_bob')`). `test_hhmm_to_min`.
- [ ] Steps 2–4: implement; run → pass (cleanup fixture).
- [ ] Step 5: Commit `feat(interviews): responsible admin CLI`.

### Task 5: Assignment service

**Files:**
- Create: `backend/interviews/service.py`
- Test: `backend/tests/test_interviews_service.py`

**Interfaces — Produces:**
- `class SlotConflict(Exception)`.
- `grid_for_week(monday: date) -> dict` → `{ 'cells': {'YYYY-MM-DD:HH': [ {id,name} ... ]}, 'responsibles': [...], 'hours': [8..19], 'dates': [...] }` (builds `per_resp` from `db.get_availability` + `db.booked_intervals` for the week, calls `slots.free_grid`, resolves ids→names).
- `assign(mailbox: str, responsible_id: int, start_iso: str, company: str, jobid: str, thread_key: str, source_message_hash: str) -> dict` — parse `start_iso` (UTC), `end = start+DURATION`, verify the responsible is actually free (re-check `is_free`) then `db.insert_interview`; on `IntegrityError`/not-free raise `SlotConflict`. Returns the interview row.
- `mailbox_context(mailbox: str) -> dict` → best-effort `{company, jobid}` from the newest `uploads/prefill/*/<*>/report.json` whose persona email == mailbox (reuse glob; company optional).

**Consumes:** `db`, `slots`.

- [ ] Step 1: Failing tests: `test_grid_marks_free_cells`, `test_assign_books_and_links_mailbox` (after assign, `db.assigned_mailboxes(rid)` contains the mailbox), `test_assign_conflict_raises` (assign twice same slot → `SlotConflict`).
- [ ] Steps 2–4: implement; run → pass.
- [ ] Step 5: Commit `feat(interviews): assignment service (grid + conflict-checked assign)`.

### Task 6: Operator «Собес» modal in the inbox

**Files:**
- Create: `backend/interviews/operator_ui.py` (modal HTML + grid fragment + inline JS/CSS), `backend/interviews/routes_operator.py` (`APIRouter`)
- Modify: `backend/tools/mailcrm_ui.py` — in `render_rows` (row) and `render_thread` add a «Собес» action for the message (data attrs `data-mailbox`, `data-thread`, `data-hash`); add the modal container + `openSobes()` JS once (reuse `.cat-modal` styles or add `.iv-modal` mirroring them). Keep changes minimal/additive.
- Modify: `backend/dashboard_app.py` — `from backend.interviews.routes_operator import router as iv_router; app.include_router(iv_router)`; call `interviews_db.ensure_schema()` at startup near the other `_start_*()` calls.
- Test: `backend/tests/test_interviews_operator_routes.py`

**Interfaces — Produces (routes):**
- `GET /mail/interview/grid?mailbox=&monday=YYYY-MM-DD` → HTML fragment: the week grid (green free cells with free-count; a `<select>` of responsibles appears per selected cell) + prev/next week + a hidden form. Uses `service.grid_for_week`. Renders the orta look: `grid` of `HOUR` rows × 7 day columns, cell button green `#16a34a`-tint when free, gray when none free; click a green cell → reveal the free-responsible `<select>` + «Назначить».
- `POST /mail/interview/assign` (Form: `mailbox, responsible_id, start_iso, company, jobid, thread_key, source_message_hash`) → calls `service.assign`; returns a small HTML/JSON success or a 409 fragment on `SlotConflict`.
- `GET /mail/interview/status?mailbox=&thread=` → `{assigned: bool, responsible, start_ts}` so the row can show an «✓ назначено» badge.

**UI look (mirror orta ScheduleTab grid):** container `display:grid;grid-template-columns:40px repeat(7,1fr);gap:2px`; header row `Пн..Вс`; hour rows 8..19; free cell = pale green button, selected = blue, none-free = gray disabled. Modal chrome copied from `.cat-modal` (backdrop, ✕, Esc, bottom-sheet on ≤760px). Minimalistic.

- [ ] Step 1: Failing test `test_grid_route_renders_cells` (seed a responsible + availability via db, GET the grid, assert a known free cell button present) and `test_assign_route_books` (POST assign → 200 and `db.interview_for_thread` set) and `test_assign_conflict_returns_409`.
- [ ] Step 2: Run → fail.
- [ ] Step 3: Implement `operator_ui.py` + `routes_operator.py`; wire into `dashboard_app` + `mailcrm_ui` action button + modal.
- [ ] Step 4: Run route tests (via `fastapi.testclient.TestClient(app)`) → pass. Manually confirm `python -c "import backend.dashboard_app"` imports clean.
- [ ] Step 5: Commit `feat(interviews): operator «Собес» assign modal in inbox`.

### Task 7: Responsible cabinet app (:8103)

**Files:**
- Create: `backend/interviews/cabinet_app.py` (FastAPI `app`, SessionMiddleware not needed — cookie handled by `auth`), `backend/interviews/cabinet_ui.py` (login, availability editor, my-interviews, scoped inbox + thread), 
- Test: `backend/tests/test_interviews_cabinet.py`

**Interfaces — Produces (routes on cabinet `app`, all under root; nginx prefixes `/cabinet/`):**
- `GET /login` + `POST /login` (Form login/password → `auth.verify_password` → set `iv_session` cookie → redirect `/`). `GET /logout`.
- `GET /` (dep `current_responsible`) → dashboard: upcoming interviews (`db.interviews_for_responsible(rid, upcoming_only=True)`) + link to availability + inbox.
- `GET /availability` + `POST /availability` — 7-day editor (toggle + start/end time inputs in GMT), UPSERT via `db.set_availability`. Mirror orta TeacherSettings look (7 day cards).
- `GET /inbox` → merged scoped list: for each `m in db.assigned_mailboxes(rid)`, `mailcrm.list_messages(mailbox=m, limit=…)`, merge + sort by date; render read-only (reuse `mailcrm_ui.render_rows` but strip operator actions — pass a flag or post-process).
- `GET /thread?hash=` → **ownership guard**: `row = mail_db.get_row(hash); if not row or row['mailbox'] not in db.assigned_mailboxes(rid): 404`; else `mailcrm.get_thread(hash)` rendered read-only (no reply/delete).
- Startup: `interviews_db.ensure_schema()`.

**Consumes:** `db`, `auth`, `slots`, `backend.tools.mailcrm`, `backend.tools.mail_db`, `backend.tools.mailcrm_ui`.

- [ ] Step 1: Failing tests (TestClient): `test_login_required_redirects` (GET `/` no cookie → 303 → `/login`), `test_login_sets_cookie_and_dashboard_loads`, `test_ownership_guard_blocks_foreign_mailbox` (responsible A cannot open a hash whose mailbox is not assigned → 404), `test_inbox_lists_only_assigned` (assign persona to A; A's inbox shows it; unassigned persona hidden).
- [ ] Step 2: Run → fail.
- [ ] Step 3: Implement cabinet app + UI + guard.
- [ ] Step 4: Run → pass; `python -c "import backend.interviews.cabinet_app"` clean.
- [ ] Step 5: Commit `feat(interviews): responsible cabinet (login, availability, scoped read-only mail)`.

### Task 8: Wiring & deploy (MVP)

**Files:** Modify `backend/config.py` (already has `interview_session_secret`); create/update `backend/.env` (add `INTERVIEW_SESSION_SECRET=<openssl rand>`); nginx `jobs.systeam.kz`; pm2.

- [ ] Step 1: Pick a free port (verify 8103 unclaimed: `nginx -T | grep -oE '127.0.0.1:8103'` empty AND `ss -tln | grep :8103` empty; else next free).
- [ ] Step 2: `pm2 start /usr/bin/bash --name jobfinder-alan-cabinet --cwd /home/projects/jobfinder -- -c "cd /home/projects/jobfinder && exec sg mail -c '/usr/bin/python3 -m uvicorn backend.interviews.cabinet_app:app --host 127.0.0.1 --port 8103'"` then `pm2 save`.
- [ ] Step 3: nginx: add inside the `jobs.systeam.kz` server block (before the catch-all `/`): `location /cabinet/ { auth_basic off; proxy_pass http://127.0.0.1:8103/; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }`. Verify `nginx -t`, confirm the `00-default-drop default_server` is intact, `systemctl reload nginx`.
- [ ] Step 4: `pm2 restart jobfinder-alan-dash` (picks up the operator router + ensure_schema). Smoke: create a responsible via CLI, set availability, open `https://jobs.systeam.kz/cabinet/login`, log in; in the operator inbox open an interview mail → «Собес» → grid shows the slot → assign → confirm it appears in the cabinet and the persona's mail is now visible there.
- [ ] Step 5: Update project `CLAUDE.md` (new module, ports, pm2 service, nginx location, cron/none) + commit `feat(interviews): deploy cabinet (:8103) + nginx /cabinet + operator wiring` and push.

---

## Phase 2 — Telegram bot + reminders

### Task 9: Multi-user interviewer bot

**Files:** Create `backend/interviews/bot.py`, `backend/tests/test_interviews_bot.py` (pure helpers only).

**Interfaces — Produces:** an aiogram 3.x bot (own token `IV_BOT_TOKEN`) — `/start <code>` binds `telegram_chat_id` to a responsible (code minted by CLI `admin_cli link --login L` → one-time token via `itsdangerous`), `/menu` shows upcoming interviews + «Мои слоты» (FSM to edit availability, calls `db.set_availability`), `/whereami`. Pure helpers `parse_avail_line("Пн 09:00-17:00") -> (dow,start_min,end_min)` and `link_code(rid)/read_link_code(tok)` are unit-tested. Long-poll `dp.start_polling` in `_amain`. Model on `backend/tools/apply_bot.py` structure but per-user (no single-owner `_authorized`).

- [ ] Steps: TDD the pure helpers (`test_parse_avail_line`, `test_link_code_roundtrip`); implement bot; commit `feat(interviews): multi-user interviewer telegram bot`.

### Task 10: Reminder loop

**Files:** Create `backend/interviews/reminders.py`, `backend/tests/test_interviews_reminders.py`.

**Interfaces — Produces:** `due(now) -> list[(interview, which)]` pure selection (uses `db.due_reminders` twice: window 60 for `reminded_60`, window 5 for `reminded_5`), and `run_forever(interval=60)` daemon that sends Telegram to `responsible.telegram_chat_id` (neutral text «Через час собеседование…» / «Через 5 минут…») via a `send_dm(chat_id, text)` helper, then `db.mark_reminded`. Runs inside the bot process (`bot.py` starts it as a thread) — no extra pm2 service.

- [ ] Steps: TDD `test_due_selects_60_then_5_once` (idempotent after mark); implement; commit `feat(interviews): pre-interview reminder loop (-60/-5)`.

### Task 11: Bot + reminders deploy

- [ ] Add `IV_BOT_TOKEN` to `.env`; pm2 `jobfinder-alan-ivbot` → `python -m backend.interviews.bot` (cwd repo root, `sg mail` not required unless it reads Maildir — it doesn't; still launch from repo root). `pm2 save`. Smoke: `/start <code>` binds; a near-term interview fires a reminder. Update `CLAUDE.md`, commit + push.

---

## Phase 3 — Auto-assign

### Task 12: Auto-assign a free responsible

**Files:** Create `backend/interviews/autoassign.py`, `backend/tests/test_interviews_autoassign.py`.

**Interfaces — Produces:** `pick_responsible(start_ts, exclude=()) -> int | None` (least-loaded free responsible at that slot), `auto_assign(mailbox, start_ts, …) -> dict | None` (books via `service.assign`; returns None if none free). **Recruiter auto-reply is NOT built here** — a `propose_reply_draft(interview) -> str` may be generated but sending stays human-gated (default off, no external send in this task). An operator button «Авто-назначить» on the modal calls `auto_assign`.

- [ ] Steps: TDD `test_pick_least_loaded_free`, `test_auto_assign_books_or_none`; implement; wire the modal button; commit `feat(interviews): auto-assign free responsible`.

---

## Self-review notes

- Spec coverage: slots (T2), availability (T1/T7/T9), assignment (T5/T6), visibility==assignment (T5 link + T7 guard), GMT (T2), cabinet (T7), reminders (T9/T10), auto-assign (T12), deploy (T8/T11). ✓
- Double-booking guarded twice (DB partial-unique T1 + `is_free` re-check T5). ✓
- No shared-`mailcrm` edits (guard in T7). ✓
- Ports: cabinet 8103 (verify), bot long-poll (no port). ✓
- Signature consistency: `assigned_mailboxes` / `interview_for_thread` / `insert_interview` used consistently across T1/T5/T6/T7. ✓
