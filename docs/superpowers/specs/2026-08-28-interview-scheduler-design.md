# Interview Scheduler + Responsible Cabinet — Design

**Date:** 2026-08-28
**Status:** approved (owner) — build with subagents.

## Problem

An interview invitation lands in a persona's mailbox (e.g. "Lara Croft"). A real
human ("ответственный" / responsible person) must be assigned to attend it. Today
nothing represents an interview event, interviewer availability, or the
responsible↔persona link. The owner wants: an operator assigns an incoming
interview into a **free slot** of a responsible person — modelled exactly on how
**orta.study** assigns lessons into teacher time-slots — and the responsible then
sees **only** the mail of the personas assigned to them (assignment == visibility),
plus Telegram reminders before the interview.

## Reference: orta.study mechanic (what we port)

`/home/projects/kazakhstan/orta.study` (Node/Express/SQLite/React). Core:
- `teacher_availability` (recurring weekly working hours, one row per teacher×weekday,
  `HH:MM` start/end, `is_available`).
- `schedule` (concrete bookings: teacher_id + student_id, tz-aware start/end).
- **Visibility = assignment:** a teacher's list query is `WHERE teacher_id = me`; a
  student's is `WHERE student_id = me`. Admin (unfiltered) writes those ids to assign.
  Before the id is written, the row is invisible to the participant. This IS the
  owner's "до назначения не видит переписку, после — видит всё" requirement.
- **Assignment UI:** a hand-rolled CSS-grid week×hour matrix (no calendar lib);
  green = free, blue = selected, gray = busy; admin clicks a free cell → picks a free
  teacher → creates the booking. Reproduces 1:1 in server-rendered HTML+CSS.
- **Reminders:** node-cron every minute fires Telegram/email at N minutes before.

**Simplifications for interviews (drop orta complexity):** no groups, no recurring
series, no courses. One interview = one persona(mailbox) ↔ one responsible, one-off.
Times are entered/stored in **GMT/UTC** (owner asked "по GMT"); a per-responsible
timezone column exists (`tz`, default `UTC`) for a later refinement but MVP is GMT.

## Actors & surfaces

- **Operator** — existing dashboard user (behind nginx basic-auth `job2026`). Works
  **inside the existing `/mail` inbox**. NO new nav tab. On an interview-kind message
  a **«Собес»** action opens a **modal** (mirroring the `/catalog` `.cat-modal`) with
  a week grid of all responsibles' free slots; the operator clicks a slot, picks a
  free responsible, assigns — without leaving the inbox.
- **Responsible (ответственный)** — a separate **cabinet** app (`/cabinet/`, own port
  8103, `auth_basic off`, own cookie session). Logs in, edits weekly availability (in
  GMT), sees their upcoming interviews and the **read-only** mail of only their
  assigned personas. Layer 2: a Telegram bot is the alternative interface + reminders.
- **Admin of responsibles** — a CLI (`python -m backend.interviews.admin_cli`) creates
  responsibles / resets passwords (no extra UI in MVP).

## Data model (Postgres `jobfinder_crm`, via `mail_db` pool; `ensure_schema` IF NOT EXISTS)

- `iv_responsibles`: `id serial pk`, `login text unique not null`, `password_hash text
  not null`, `name text not null`, `tz text not null default 'UTC'`,
  `telegram_chat_id bigint`, `active boolean not null default true`,
  `created_at timestamptz default now()`.
- `iv_availability`: `responsible_id int references iv_responsibles(id) on delete
  cascade`, `dow smallint not null` (0=Mon..6=Sun), `start_min int not null`,
  `end_min int not null` (minutes-from-midnight in GMT), `enabled boolean not null
  default true`, `unique(responsible_id, dow)`.
- `iv_interviews`: `id serial pk`, `mailbox text not null` (persona address = the
  visibility key), `thread_key text`, `company text`, `jobid text`,
  `responsible_id int references iv_responsibles(id)`, `start_ts timestamptz`,
  `end_ts timestamptz`, `status text not null default 'assigned'`
  (`assigned|done|cancelled`), `source_message_hash text`, `notes text`,
  `reminded_60 boolean default false`, `reminded_5 boolean default false`,
  `created_at timestamptz default now()`. Index on `responsible_id`, on `mailbox`.
  Partial unique `(responsible_id, start_ts) where responsible_id is not null and
  status <> 'cancelled'` prevents double-booking.

Persona↔responsible link is **derived** (`SELECT DISTINCT mailbox FROM iv_interviews
WHERE responsible_id = :me AND status <> 'cancelled'`) — no separate table.

## Slot logic (`backend/interviews/slots.py`, pure, GMT)

- Availability window per (responsible, weekday) = `[start_min, end_min)` minutes GMT.
- Grid for a week: for each date in the week × each hour cell (configurable
  `HOURS`, default 8..20 GMT), a responsible is **free** iff (a) that weekday is
  enabled and the cell hour ∈ its window, and (b) no non-cancelled interview of that
  responsible overlaps the cell's `[start, start+DURATION)` (default 60 min).
- Grid returns, per cell (date+hour, UTC), the list of free responsible ids. The modal
  shows green when ≥1 free. Double-booking guarded again at insert (partial unique +
  overlap check → conflict raises `SlotConflict`).

## Auth (`backend/interviews/auth.py`)

- Passwords: `bcrypt` (installed). `hash_password`/`verify_password`.
- Session: signed cookie via `itsdangerous.URLSafeTimedSerializer` (installed), secret
  `INTERVIEW_SESSION_SECRET` (env). FastAPI dependency `current_responsible(request)`
  → responsible dict or 401/redirect to `/cabinet/login`.
- Cabinet ownership guard: a thread/message read verifies `mail_db.get_row(hash).mailbox
  ∈ assigned_mailboxes(me)` before calling `mailcrm.get_thread` — **no change to the
  shared `mailcrm` reads**.

## Deploy

- New pm2 `jobfinder-alan-cabinet` → `uvicorn backend.interviews.cabinet_app:app` on
  **127.0.0.1:8103** (verify free at deploy), launched via the same `cd … && sg mail -c`
  pattern (needs `mail` group to read Maildirs).
- Layer 2 pm2 `jobfinder-alan-ivbot` → `python -m backend.interviews.bot` (own bot
  token) which also runs the reminder loop.
- nginx `jobs.systeam.kz`: add `location /cabinet/ { auth_basic off; proxy_pass
  http://127.0.0.1:8103/; }` (keep `default_server` drop intact). Operator routes stay
  on 8099 behind basic-auth.
- `backend.interviews.db.ensure_schema()` called at dash + cabinet startup.
- Config additions (`backend/.env`, gitignored): `INTERVIEW_SESSION_SECRET`,
  `IV_BOT_TOKEN` (layer 2). `config.py` uses `extra="ignore"`.

## Branding (owner rule)

All responsible/operator-facing text is neutral (no stack names). Nothing here exposes
the tech stack.

## Build order

1. **MVP** — db + slots + auth + admin CLI + assignment service + operator «Собес»
   modal in inbox + cabinet (login, availability, my interviews, scoped read-only mail
   with ownership guard) + wiring/deploy.
2. **Layer 2** — Telegram bot (multi-user link, set availability, notifications) +
   reminder loop (−60 / −5 min).
3. **Layer 3** — auto-assign a free responsible; optional recruiter auto-reply is
   **default-OFF + human-gated** (never auto-send external mail without confirmation).

## Non-goals (MVP)

Parsing the proposed time out of the recruiter email (operator enters it); recruiter
auto-reply; per-responsible timezone display (GMT only); groups/recurring.
