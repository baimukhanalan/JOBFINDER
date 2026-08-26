"""PostgreSQL persistence for independently discovered remote company jobs.

This module deliberately does not write to ``job_catalog``.  Jobs belong to a
``company_discovery`` row, retain their source payload/provenance, and get an
append-only snapshot whenever meaningful normalized content changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:  # Pure helpers must remain importable without the database dependency.
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover - depends on the runtime image
    psycopg2 = None
    Json = None


_ENV = Path(__file__).resolve().parents[1] / ".env"
_pool = None
_lock = threading.Lock()
_scan_sessions: dict[int, tuple[Any, int]] = {}
_scan_sessions_lock = threading.Lock()

SUPPORTED_COMPANY_JOB_ATS = (
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday",
    "icims", "oracle", "successfactors", "eightfold", "custom",
)


class BoardScanLocked(RuntimeError):
    """Another worker already owns the same company ATS board."""

JOB_STATUSES = ("active", "closed")
QUESTION_STATUSES = ("not_attempted", "success", "failed")
SNAPSHOT_EVENTS = ("first_seen", "content_changed", "closed", "reopened")
_MEANINGFUL_FIELDS = (
    "title", "department", "location_raw", "location_normalized", "locations", "country",
    "state", "city", "remote_type", "employment_type", "salary_min",
    "salary_max", "currency", "salary_interval", "compensation_text",
    "description", "description_html", "requirements", "benefits", "job_url",
    "apply_url", "posted_at", "source_updated_at", "status",
)
_SPACE_RE = re.compile(r"\s+")


def _dsn() -> str:
    dsn = os.environ.get("CRM_PG_DSN")
    if dsn:
        return dsn
    try:
        for line in _ENV.read_text().splitlines():
            if line.strip().startswith("CRM_PG_DSN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("CRM_PG_DSN not set (backend/.env or environment)")


def _get_pool():
    global _pool
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required for company job database access")
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=_dsn())
    return _pool


@contextmanager
def conn():
    pool = _get_pool()
    connection = pool.getconn()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        pool.putconn(connection)


@contextmanager
def _cur(dict_rows: bool = True):
    with conn() as connection:
        cursor = connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            yield cursor
        finally:
            cursor.close()


def normalize_text(value: Any) -> str | None:
    """Normalize inconsequential whitespace while preserving the actual wording."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).replace("\u00a0", " ")
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def normalize_remote_type(value: Any) -> str:
    """Return ``remote`` only for unambiguously remote postings.

    Step 2 is intentionally remote-only.  Hybrid, on-site, and unknown records are
    rejected instead of being silently mixed into this data set.
    """
    key = re.sub(r"[^a-z]+", "", str(value or "").casefold())
    if key in {"remote", "fullyremote", "remotefirst", "workfromhome", "wfh"}:
        return "remote"
    raise ValueError("company job store accepts only confirmed remote jobs")


def normalize_money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid salary amount: {value!r}") from exc


def normalize_timestamp(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Normalize ATS ISO/epoch/relative dates for PostgreSQL timestamptz columns."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= 100_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    base = now or datetime.now(timezone.utc)
    if re.search(r"\b(?:today|posted today)\b", text, re.I):
        return base
    days = re.search(r"\b(\d+)\+?\s+days?\s+ago\b", text, re.I)
    if days:
        return base - timedelta(days=int(days.group(1)))
    if re.search(r"\byesterday\b", text, re.I):
        return base - timedelta(days=1)
    return None


def prepare_job(row: dict) -> dict:
    """Validate and normalize a source job without dropping its raw evidence."""
    prepared = dict(row)
    for required in ("company_id", "source", "source_job_id", "title", "apply_url"):
        if row.get(required) is None or not str(row.get(required)).strip():
            raise ValueError(f"{required} is required")
    prepared["company_id"] = int(row["company_id"])
    prepared["source"] = str(row["source"]).strip().casefold()
    prepared["source_board_id"] = str(row.get("source_board_id") or "default").strip()
    prepared["source_job_id"] = str(row["source_job_id"]).strip()
    prepared["remote_type"] = normalize_remote_type(row.get("remote_type"))
    for field in (
        "title", "department", "location_raw", "location_normalized", "country",
        "state", "city", "employment_type", "currency", "description",
        "description_html", "requirements", "benefits", "job_url", "apply_url",
        "salary_interval", "compensation_text",
    ):
        prepared[field] = normalize_text(row.get(field))
    locations = row.get("locations") or []
    if not isinstance(locations, list):
        locations = [locations]
    prepared["locations"] = list(dict.fromkeys(
        value for value in (normalize_text(item) for item in locations) if value))
    prepared["posted_at"] = normalize_timestamp(row.get("posted_at"))
    prepared["source_updated_at"] = normalize_timestamp(row.get("source_updated_at"))
    prepared["salary_min"] = normalize_money(row.get("salary_min"))
    prepared["salary_max"] = normalize_money(row.get("salary_max"))
    if (prepared["salary_min"] is not None and prepared["salary_max"] is not None
            and prepared["salary_min"] > prepared["salary_max"]):
        raise ValueError("salary_min must not exceed salary_max")
    if prepared["currency"]:
        prepared["currency"] = prepared["currency"].upper()
    status = str(row.get("status") or "active").casefold()
    if status not in JOB_STATUSES:
        raise ValueError(f"invalid job status: {status}")
    prepared["status"] = status
    prepared["source_payload"] = row.get("source_payload") or {}
    prepared["provenance"] = row.get("provenance") or {}
    return prepared


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def meaningful_job_payload(row: dict) -> dict:
    """Canonical content used for history; excludes observation/provenance noise."""
    prepared = prepare_job(row)
    return {field: _json_value(prepared.get(field)) for field in _MEANINGFUL_FIELDS}


def job_content_hash(row: dict) -> str:
    payload = meaningful_job_payload(row)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def has_meaningful_change(previous_hash: str | None, row: dict) -> bool:
    return previous_hash != job_content_hash(row)


def snapshot_event(previous: Mapping[str, Any] | None, current_status: str) -> str:
    """Classify the immutable observation written for a meaningful transition."""
    if previous is None:
        return "first_seen"
    old_status = str(previous.get("status") or "active").casefold()
    new_status = str(current_status or "active").casefold()
    if old_status == "closed" and new_status == "active":
        return "reopened"
    if old_status == "active" and new_status == "closed":
        return "closed"
    return "content_changed"


def normalize_question(question: dict, position: int = 0) -> dict:
    """Normalize one application question while preserving its full source payload."""
    label = normalize_text(question.get("label") or question.get("question")
                           or question.get("text"))
    source_id = normalize_text(question.get("source_question_id") or question.get("id")
                               or question.get("name"))
    question_type = normalize_text(question.get("question_type") or question.get("type"))
    if not label and not source_id:
        raise ValueError("question requires a label or source identifier")
    identity = source_id or f"{position}:{label or ''}:{question_type or ''}"
    key = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()
    return {
        "question_key": key,
        "source_question_id": source_id,
        "position": int(position),
        "label": label,
        "normalized_label": (label or "").casefold() or None,
        "required": bool(question.get("required", False)),
        "question_type": question_type,
        "options": question.get("options") or [],
        "validation": question.get("validation") or {},
        "source_payload": question.get("source_payload") or question,
    }


def ensure_schema() -> None:
    with _cur(False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_job_scans (
          id              BIGSERIAL PRIMARY KEY,
          company_id      BIGINT NOT NULL REFERENCES company_discovery(id) ON DELETE CASCADE,
          source          TEXT NOT NULL,
          source_board_id TEXT NOT NULL,
          started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at     TIMESTAMPTZ,
          scan_complete   BOOLEAN,
          scan_succeeded  BOOLEAN,
          seen_job_count  INTEGER,
          error           TEXT
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_jobs (
          id                  BIGSERIAL PRIMARY KEY,
          company_id          BIGINT NOT NULL REFERENCES company_discovery(id) ON DELETE CASCADE,
          source              TEXT NOT NULL,
          source_board_id     TEXT NOT NULL DEFAULT 'default',
          source_job_id       TEXT NOT NULL,
          title               TEXT NOT NULL,
          department          TEXT,
          location_raw        TEXT,
          location_normalized TEXT,
          locations           JSONB NOT NULL DEFAULT '[]'::jsonb,
          country             TEXT,
          state               TEXT,
          city                TEXT,
          remote_type         TEXT NOT NULL CHECK (remote_type = 'remote'),
          employment_type     TEXT,
          salary_min          NUMERIC,
          salary_max          NUMERIC,
          currency            TEXT,
          salary_interval     TEXT,
          compensation_text   TEXT,
          description         TEXT,
          description_html    TEXT,
          requirements        TEXT,
          benefits            TEXT,
          job_url             TEXT,
          apply_url           TEXT NOT NULL,
          posted_at           TIMESTAMPTZ,
          source_updated_at   TIMESTAMPTZ,
          first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          closed_at           TIMESTAMPTZ,
          status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
          content_hash        TEXT NOT NULL,
          source_payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
          provenance          JSONB NOT NULL DEFAULT '{}'::jsonb,
          last_scan_id        BIGINT REFERENCES company_remote_job_scans(id),
          questions_status    TEXT NOT NULL DEFAULT 'not_attempted'
                              CHECK (questions_status IN ('not_attempted','success','failed')),
          questions_error     TEXT,
          questions_checked_at TIMESTAMPTZ,
          updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (company_id, source, source_board_id, source_job_id)
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS crj_company_status ON company_remote_jobs (company_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS crj_source_board ON company_remote_jobs (source, source_board_id, status)")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_job_snapshots (
          id             BIGSERIAL PRIMARY KEY,
          job_id         BIGINT NOT NULL REFERENCES company_remote_jobs(id) ON DELETE CASCADE,
          event_type     TEXT NOT NULL DEFAULT 'content_changed'
                         CHECK (event_type IN ('first_seen','content_changed','closed','reopened')),
          content_hash   TEXT NOT NULL,
          observed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          content        JSONB NOT NULL,
          source_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          provenance     JSONB NOT NULL DEFAULT '{}'::jsonb
        );""")
        cur.execute(
            "ALTER TABLE company_remote_job_snapshots ADD COLUMN IF NOT EXISTS "
            "event_type TEXT NOT NULL DEFAULT 'content_changed'")
        cur.execute("CREATE INDEX IF NOT EXISTS crjs_job_observed ON company_remote_job_snapshots (job_id, observed_at DESC)")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_job_questions (
          id                 BIGSERIAL PRIMARY KEY,
          job_id             BIGINT NOT NULL REFERENCES company_remote_jobs(id) ON DELETE CASCADE,
          question_key       TEXT NOT NULL,
          source_question_id TEXT,
          position           INTEGER NOT NULL,
          label              TEXT,
          normalized_label   TEXT,
          required           BOOLEAN NOT NULL DEFAULT FALSE,
          question_type      TEXT,
          options            JSONB NOT NULL DEFAULT '[]'::jsonb,
          validation         JSONB NOT NULL DEFAULT '{}'::jsonb,
          source_payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
          first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (job_id, question_key)
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS crjq_job_position ON company_remote_job_questions (job_id, position)")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_job_question_attempts (
          id          BIGSERIAL PRIMARY KEY,
          job_id      BIGINT NOT NULL REFERENCES company_remote_jobs(id) ON DELETE CASCADE,
          state       TEXT NOT NULL CHECK (state IN ('success','failed')),
          questions   JSONB NOT NULL DEFAULT '[]'::jsonb,
          error       TEXT,
          observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS crjqa_job_observed ON "
                    "company_remote_job_question_attempts (job_id, observed_at DESC)")


_JOB_COLS = (
    "company_id", "source", "source_board_id", "source_job_id", "title",
    "department", "location_raw", "location_normalized", "locations", "country", "state",
    "city", "remote_type", "employment_type", "salary_min", "salary_max",
    "currency", "salary_interval", "compensation_text", "description", "description_html",
    "requirements", "benefits", "job_url", "apply_url", "posted_at", "source_updated_at",
    "status", "content_hash", "source_payload", "provenance",
    "last_scan_id",
)
_JSON_COLS = {"locations", "source_payload", "provenance"}


def _db_json(value: Any):
    return Json(value) if Json is not None else value


def upsert_job(company_id: int | dict, record: dict | None = None,
               scan_id: int | None = None) -> dict:
    """Idempotently store one remote job and snapshot meaningful changes only."""
    if isinstance(company_id, dict):
        if record is not None:
            raise TypeError("record must be omitted when the first argument is a job dict")
        row = dict(company_id)
    else:
        row = dict(record or {})
        row["company_id"] = int(company_id)
    row["last_scan_id"] = scan_id
    prepared = prepare_job(row)
    content = meaningful_job_payload(prepared)
    content_hash = job_content_hash(prepared)
    prepared["content_hash"] = content_hash
    identity = (prepared["company_id"], prepared["source"],
                prepared["source_board_id"], prepared["source_job_id"])
    with _cur() as cur:
        # Serialize one source identity so concurrent first sightings cannot both
        # create an identical initial snapshot.
        lock_key = "\x1f".join(str(value) for value in identity)
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (lock_key,))
        cur.execute(
            "SELECT id, content_hash, status FROM company_remote_jobs WHERE company_id=%s "
            "AND source=%s AND source_board_id=%s AND source_job_id=%s FOR UPDATE", identity)
        existing = cur.fetchone()
        values = tuple(_db_json(prepared.get(c)) if c in _JSON_COLS
                       else prepared.get(c) for c in _JOB_COLS)
        placeholders = ",".join(["%s"] * len(_JOB_COLS))
        updates = [c for c in _JOB_COLS if c not in {
            "company_id", "source", "source_board_id", "source_job_id"}]
        cur.execute(
            "INSERT INTO company_remote_jobs (" + ",".join(_JOB_COLS) + ") VALUES ("
            + placeholders + ") ON CONFLICT (company_id,source,source_board_id,source_job_id) "
            "DO UPDATE SET " + ",".join(f"{c}=EXCLUDED.{c}" for c in updates)
            + ",last_seen_at=now(),closed_at=CASE WHEN EXCLUDED.status='closed' "
              "THEN COALESCE(company_remote_jobs.closed_at,now()) ELSE NULL END,"
              "updated_at=now() RETURNING id",
            values)
        returned = cur.fetchone()
        job_id = returned["id"] if isinstance(returned, dict) else returned[0]
        changed = existing is None or existing["content_hash"] != content_hash
        if changed:
            event_type = snapshot_event(existing, prepared["status"])
            cur.execute(
                "INSERT INTO company_remote_job_snapshots "
                "(job_id,event_type,content_hash,content,source_payload,provenance) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (job_id, event_type, content_hash, _db_json(content),
                 _db_json(prepared["source_payload"]), _db_json(prepared["provenance"])))
        return {"job_id": job_id, "content_hash": content_hash,
                "snapshot_created": changed}


def store_questions(job_id: int, questions: list[dict] | None, *,
                    scrape_succeeded: bool, error: str | None = None) -> int:
    """Replace the complete current question set only after a successful scrape.

    ``questions=[]`` is authoritative only with ``scrape_succeeded=True``.  A failed
    scrape retains every previously stored question and records the failure.
    """
    if not scrape_succeeded:
        with _cur(False) as cur:
            # Preserve every partially captured field as audit/history evidence,
            # but do not replace the last authoritative current question set.
            cur.execute(
                "INSERT INTO company_remote_job_question_attempts "
                "(job_id,state,questions,error) VALUES (%s,'failed',%s,%s)",
                (int(job_id), _db_json(questions or []),
                 normalize_text(error) or "question scrape failed"))
            cur.execute(
                "UPDATE company_remote_jobs SET questions_status='failed', "
                "questions_error=%s,questions_checked_at=now(),updated_at=now() WHERE id=%s",
                (normalize_text(error) or "question scrape failed", int(job_id)))
            return 0
    normalized = [normalize_question(question, position)
                  for position, question in enumerate(questions or [])]
    with _cur(False) as cur:
        cur.execute(
            "INSERT INTO company_remote_job_question_attempts "
            "(job_id,state,questions,error) VALUES (%s,'success',%s,NULL)",
            (int(job_id), _db_json(normalized)))
        cur.execute("DELETE FROM company_remote_job_questions WHERE job_id=%s", (int(job_id),))
        if normalized:
            values = [(
                int(job_id), q["question_key"], q["source_question_id"], q["position"],
                q["label"], q["normalized_label"], q["required"], q["question_type"],
                _db_json(q["options"]), _db_json(q["validation"]),
                _db_json(q["source_payload"]),
            ) for q in normalized]
            cur.executemany(
                "INSERT INTO company_remote_job_questions "
                "(job_id,question_key,source_question_id,position,label,normalized_label,"
                "required,question_type,options,validation,source_payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values)
        cur.execute(
            "UPDATE company_remote_jobs SET questions_status='success',questions_error=NULL,"
            "questions_checked_at=now(),updated_at=now() WHERE id=%s", (int(job_id),))
    return len(normalized)


def mark_missing_jobs_closed(*, company_id: int, source: str, source_board_id: str,
                             seen_source_job_ids: list[str],
                             scan_succeeded: bool, scan_complete: bool) -> int:
    """Close unseen jobs only when the caller attests to a complete successful scan."""
    if not (scan_succeeded and scan_complete):
        return 0
    seen = [str(value) for value in seen_source_job_ids]
    with _cur(False) as cur:
        return _mark_missing_jobs_closed_cur(
            cur, company_id=company_id, source=source,
            source_board_id=source_board_id, seen_source_job_ids=seen)


def _mark_missing_jobs_closed_cur(cur: Any, *, company_id: int, source: str,
                                  source_board_id: str,
                                  seen_source_job_ids: list[str]) -> int:
    """Close a board's missing jobs using the caller's current transaction."""
    cur.execute(
            "WITH missing AS (SELECT * FROM company_remote_jobs WHERE company_id=%s "
            "AND source=%s AND source_board_id=%s AND status='active' "
            "AND NOT (source_job_id = ANY(%s)) FOR UPDATE), snapshots AS ("
            "INSERT INTO company_remote_job_snapshots "
            "(job_id,event_type,content_hash,content,source_payload,provenance) SELECT id,"
            "'closed',md5(content_hash || ':closed'),jsonb_build_object("
            "'title',title,'department',department,'location_raw',location_raw,"
            "'location_normalized',location_normalized,'country',country,'state',state,"
            "'locations',locations,"
            "'city',city,'remote_type',remote_type,'employment_type',employment_type,"
            "'salary_min',salary_min,'salary_max',salary_max,'currency',currency,"
            "'salary_interval',salary_interval,'compensation_text',compensation_text,"
            "'description',description,'description_html',description_html,"
            "'requirements',requirements,'benefits',benefits,'job_url',job_url,"
            "'apply_url',apply_url,'posted_at',posted_at,'source_updated_at',source_updated_at,"
            "'status','closed'),source_payload,provenance FROM missing) "
            "UPDATE company_remote_jobs j SET status='closed',closed_at=now(),"
            "content_hash=md5(j.content_hash || ':closed'),updated_at=now() FROM missing m "
            "WHERE j.id=m.id",
            (int(company_id), source.strip().casefold(), str(source_board_id),
             [str(value) for value in seen_source_job_ids]))
    return cur.rowcount


def list_company_targets(status: str = "novel", limit: int = 100,
                         supported_ats: tuple[str, ...] = SUPPORTED_COMPANY_JOB_ATS) -> list[dict]:
    """Return independently discovered boards that are ready for job discovery.

    Job scans are evidence used to decide whether an employer can be promoted to
    monitoring.  Requiring ``qualified``/``monitoring`` here creates a circular
    dependency and leaves otherwise verified ATS boards unscanned.  The stricter
    identity and monitoring gates remain on promotion and application queues.
    """
    if status not in {"novel", "known", "possible_duplicate", "promoted"}:
        raise ValueError(f"invalid company discovery status: {status}")
    with _cur() as cur:
        cur.execute(
            "WITH boards AS (SELECT c.id,c.canonical_name,c.domain,c.careers_url,"
            "c.ats,c.ats_slug,c.ats_url,last_scan.last_scanned_at,"
            "ROW_NUMBER() OVER (PARTITION BY lower(c.ats),c.ats_slug ORDER BY "
            "(m.identity_status='verified') DESC,m.is_monitoring_representative DESC,c.id) AS rn "
            "FROM company_discovery c LEFT JOIN LATERAL ("
            "SELECT COALESCE(s.finished_at,s.started_at) AS last_scanned_at "
            "FROM company_remote_job_scans s WHERE s.company_id=c.id "
            "AND s.source=lower(c.ats) AND s.source_board_id=c.ats_slug "
            "ORDER BY s.started_at DESC LIMIT 1) last_scan ON TRUE "
            "JOIN company_employer_master m ON m.company_id=c.id "
            "WHERE c.status=%s AND m.in_target_population AND m.domain_verified "
            "AND lower(c.ats)=ANY(%s) AND c.ats_slug IS NOT NULL AND c.ats_slug <> '') "
            "SELECT id,canonical_name,domain,careers_url,ats,ats_slug,ats_url,last_scanned_at "
            "FROM boards WHERE rn=1 "
            "ORDER BY last_scanned_at ASC NULLS FIRST,id ASC LIMIT %s",
            (status, list(supported_ats), int(limit)))
        return [dict(row) for row in cur.fetchall()]


def get_company_target(company_id: int,
                       supported_ats: tuple[str, ...] = SUPPORTED_COMPANY_JOB_ATS) -> dict | None:
    with _cur() as cur:
        cur.execute("""
          SELECT c.id,c.canonical_name,c.domain,c.careers_url,c.ats,c.ats_slug,c.ats_url
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE c.id=%s AND m.in_target_population AND m.domain_verified
            AND lower(c.ats)=ANY(%s)
        """, (int(company_id), list(supported_ats)))
        row = cur.fetchone()
        return dict(row) if row else None


def begin_scan(company_id: int, ats: str, ats_slug: str) -> int:
    """Open an auditable board scan and return its database id."""
    source = str(ats).strip().casefold()
    board = str(ats_slug)
    if not source or not board.strip():
        raise ValueError("ats and ats_slug are required")
    with _cur() as cur:
        cur.execute(
            "INSERT INTO company_remote_job_scans (company_id,source,source_board_id) "
            "VALUES (%s,%s,%s) RETURNING id", (int(company_id), source, board))
        row = cur.fetchone()
        return row["id"] if isinstance(row, dict) else row[0]


def begin_locked_scan(company_id: int, ats: str, ats_slug: str) -> int:
    """Start a scan while retaining a non-blocking session advisory lock.

    The dedicated pooled connection is deliberately held until ``finish_scan``;
    PostgreSQL session locks would otherwise leak to an unrelated pool borrower.
    """
    source = str(ats).strip().casefold()
    board = str(ats_slug)
    if not source or not board.strip():
        raise ValueError("ats and ats_slug are required")
    pool = _get_pool()
    connection = pool.getconn()
    lock_key = "company_remote_board\x1f" + "\x1f".join(
        (str(int(company_id)), source, board))
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    locked = False
    try:
        cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked",
                       (lock_key,))
        row = cursor.fetchone()
        locked = bool(row["locked"] if isinstance(row, dict) else row[0])
        if not locked:
            connection.rollback()
            raise BoardScanLocked(f"board scan already running: {source}:{board}")
        cursor.execute(
            "INSERT INTO company_remote_job_scans (company_id,source,source_board_id) "
            "VALUES (%s,%s,%s) RETURNING id", (int(company_id), source, board))
        row = cursor.fetchone()
        scan_id = int(row["id"] if isinstance(row, dict) else row[0])
        connection.commit()
        with _scan_sessions_lock:
            _scan_sessions[scan_id] = (connection, lock_key)
        return scan_id
    except Exception:
        connection.rollback()
        if locked:
            try:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_key,))
                connection.commit()
            except Exception:
                connection.rollback()
        pool.putconn(connection)
        raise
    finally:
        cursor.close()


def save_questions(job_id: int, questions: list[dict] | None, scrape_state: str,
                   error: str | None = None) -> int:
    """Collector-facing question API with an explicit scrape outcome."""
    state = str(scrape_state).strip().casefold()
    if state == "success":
        return store_questions(job_id, questions, scrape_succeeded=True)
    if state == "failed":
        return store_questions(job_id, questions, scrape_succeeded=False, error=error)
    if state != "not_attempted":
        raise ValueError(f"invalid question scrape state: {scrape_state}")
    return 0


def list_pending_question_jobs(*, limit: int = 100,
                               retry_failed: bool = False) -> list[dict]:
    statuses = ["not_attempted", "failed"] if retry_failed else ["not_attempted"]
    with _cur() as cur:
        cur.execute("""
          SELECT j.id,j.company_id,j.source,j.apply_url,j.job_url,j.title,c.canonical_name
          FROM company_remote_jobs j JOIN company_discovery c ON c.id=j.company_id
          JOIN company_employer_master m ON m.company_id=j.company_id
          WHERE m.in_target_population AND j.status='active' AND j.questions_status=ANY(%s)
          ORDER BY j.first_seen_at,j.id LIMIT %s
        """, (statuses, max(1, int(limit))))
        return [dict(row) for row in cur.fetchall()]


def finish_scan(scan_id: int, seen_source_job_ids: list[str], complete: bool = True,
                error: str | None = None) -> int:
    """Finish a scan and close missing jobs only for a successful complete result."""
    with _scan_sessions_lock:
        locked_session = _scan_sessions.pop(int(scan_id), None)
    if locked_session:
        connection, lock_key = locked_session
        pool = _get_pool()
        cur = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            closed = _finish_scan_cur(
                cur, scan_id, seen_source_job_ids, complete=complete, error=error)
            connection.commit()
            return closed
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_key,))
                connection.commit()
            except Exception:
                connection.rollback()
            cur.close()
            pool.putconn(connection)
    with _cur() as cur:
        return _finish_scan_cur(
            cur, scan_id, seen_source_job_ids, complete=complete, error=error)


def _finish_scan_cur(cur: Any, scan_id: int, seen_source_job_ids: list[str], *,
                     complete: bool, error: str | None) -> int:
    """Finalize scan metadata and closures atomically on one transaction."""
    cur.execute(
        "SELECT company_id,source,source_board_id,finished_at FROM company_remote_job_scans "
        "WHERE id=%s FOR UPDATE", (int(scan_id),))
    scan = cur.fetchone()
    if not scan:
        raise ValueError(f"unknown scan id: {scan_id}")
    if scan.get("finished_at") is not None:
        raise ValueError(f"scan already finalized: {scan_id}")
    succeeded = bool(complete and not error)
    cur.execute(
        "UPDATE company_remote_job_scans SET finished_at=now(),scan_complete=%s,"
        "scan_succeeded=%s,seen_job_count=%s,error=%s WHERE id=%s",
        (bool(complete), succeeded, len(seen_source_job_ids),
         normalize_text(error), int(scan_id)))
    if not succeeded:
        return 0
    return _mark_missing_jobs_closed_cur(
        cur, company_id=scan["company_id"], source=scan["source"],
        source_board_id=scan["source_board_id"],
        seen_source_job_ids=seen_source_job_ids)


def counts() -> dict:
    with _cur(False) as cur:
        cur.execute(
            "SELECT COUNT(*),COUNT(*) FILTER (WHERE status='active'),"
            "COUNT(*) FILTER (WHERE status='closed'),"
            "COUNT(*) FILTER (WHERE questions_status='success') FROM company_remote_jobs")
        total, active, closed, questions_complete = cur.fetchone()
    return {"total": total, "active": active, "closed": closed,
            "questions_complete": questions_complete}
