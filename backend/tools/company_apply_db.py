"""Isolated PostgreSQL queue for company-discovery applications.

Automatic final actions are reachable only through a durable, audited batch
authorization.  This queue never writes to or claims work from the legacy job
catalog.
"""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    from psycopg2.extras import Json
except ModuleNotFoundError:  # helpers/tests remain importable without runtime DB extras
    psycopg2 = None
    Json = None


_ENV = Path(__file__).resolve().parents[1] / ".env"
_pool = None
_lock = threading.Lock()

STATES = (
    "queued", "claimed", "awaiting_approval", "approved", "preparing",
    "ready_for_review", "needs_input", "rejected", "blocked", "failed",
    "human_submitted", "submit_approved", "submitting", "auto_submitted",
    "submission_failed",
)
CLAIMABLE_STATES = ("queued", "approved", "submit_approved")
TERMINAL_STATES = (
    "rejected", "blocked", "human_submitted", "auto_submitted", "submission_failed",
)
TRANSITIONS = {
    "queued": {"claimed", "needs_input", "blocked"},
    "claimed": {"queued", "awaiting_approval", "needs_input", "failed", "blocked"},
    "awaiting_approval": {
        "approved", "submit_approved", "rejected", "blocked", "needs_input",
    },
    "approved": {"preparing", "rejected", "blocked", "needs_input"},
    "preparing": {"approved", "ready_for_review", "needs_input", "failed", "blocked"},
    "ready_for_review": {"human_submitted", "needs_input", "blocked"},
    "needs_input": {"queued", "awaiting_approval", "approved", "preparing", "rejected", "blocked"},
    "failed": {"queued", "approved", "blocked"},
    "submit_approved": {"submitting", "rejected", "blocked", "needs_input"},
    "submitting": {"auto_submitted", "submission_failed", "needs_input", "blocked"},
    "submission_failed": {"awaiting_approval", "blocked"},
    "rejected": set(),
    "blocked": {"queued"},
    "human_submitted": set(),
    "auto_submitted": set(),
}


class ApplicationStateError(ValueError):
    pass


class StaleApplicationError(ApplicationStateError):
    pass


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
        raise RuntimeError("psycopg2 is required for company application storage")
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


def _db_json(value: Any):
    return Json(value) if Json is not None else value


def apply_url_hash(url: str) -> str:
    """Stable identity for a URL, ignoring casing, fragments and trailing slash."""
    normalized = str(url).strip().split("#", 1)[0].rstrip("/").casefold()
    return hashlib.md5(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()


def ensure_schema() -> None:
    state_check = ",".join("'%s'" % value for value in STATES)
    with _cur(False) as cur:
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS company_remote_applications (
          id                  BIGSERIAL PRIMARY KEY,
          job_id              BIGINT NOT NULL REFERENCES company_remote_jobs(id) ON DELETE CASCADE,
          profile_id          TEXT NOT NULL,
          apply_url           TEXT NOT NULL,
          apply_url_hash      TEXT NOT NULL,
          revalidation_hash   TEXT NOT NULL,
          state               TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ({state_check})),
          priority            INTEGER NOT NULL DEFAULT 0,
          claimed_by          TEXT,
          lease_expires_at    TIMESTAMPTZ,
          queued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
          state_changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          human_submitted_at  TIMESTAMPTZ,
          human_submitted_by  TEXT,
          auto_submitted_at   TIMESTAMPTZ,
          submission_authorized_at TIMESTAMPTZ,
          submission_authorized_by TEXT,
          submission_batch_id TEXT,
          submission_receipt  JSONB,
          artifact_dir        TEXT,
          report              JSONB,
          policy_result       JSONB,
          last_error          TEXT,
          fit_score           NUMERIC,
          UNIQUE (job_id, profile_id),
          UNIQUE (profile_id, apply_url_hash)
        );""")
        # Migrate the original state check in-place when this feature is added to
        # an already initialized local database.
        cur.execute("ALTER TABLE company_remote_applications DROP CONSTRAINT IF EXISTS "
                    "company_remote_applications_state_check")
        cur.execute("ALTER TABLE company_remote_applications ADD CONSTRAINT "
                    "company_remote_applications_state_check CHECK (state IN (" +
                    state_check + "))")
        cur.execute("CREATE INDEX IF NOT EXISTS cra_claim ON company_remote_applications "
                    "(profile_id,state,priority DESC,queued_at,id)")
        for column in (
            "artifact_dir TEXT", "report JSONB", "policy_result JSONB",
            "last_error TEXT", "fit_score NUMERIC",
            "auto_submitted_at TIMESTAMPTZ", "submission_authorized_at TIMESTAMPTZ",
            "submission_authorized_by TEXT", "submission_batch_id TEXT",
        ):
            cur.execute("ALTER TABLE company_remote_applications ADD COLUMN IF NOT EXISTS " + column)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_application_attempts (
          id             BIGSERIAL PRIMARY KEY,
          application_id BIGINT NOT NULL REFERENCES company_remote_applications(id) ON DELETE CASCADE,
          phase          TEXT NOT NULL,
          outcome        TEXT NOT NULL,
          worker_id      TEXT,
          detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_application_reviews (
          id             BIGSERIAL PRIMARY KEY,
          application_id BIGINT NOT NULL REFERENCES company_remote_applications(id) ON DELETE CASCADE,
          action         TEXT NOT NULL CHECK (action IN
                         ('approve','reject','human_submitted','authorize_auto_submit','auto_submitted')),
          actor          TEXT NOT NULL,
          reason         TEXT,
          evidence       JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")
        cur.execute("ALTER TABLE company_remote_application_reviews DROP CONSTRAINT IF EXISTS "
                    "company_remote_application_reviews_action_check")
        cur.execute("ALTER TABLE company_remote_application_reviews ADD CONSTRAINT "
                    "company_remote_application_reviews_action_check CHECK (action IN "
                    "('approve','reject','human_submitted','authorize_auto_submit','auto_submitted'))")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_application_batches (
          id             TEXT PRIMARY KEY,
          profile_id     TEXT NOT NULL,
          actor          TEXT NOT NULL,
          confirmation   TEXT NOT NULL,
          application_ids JSONB NOT NULL,
          authorized_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_remote_application_profile_leases (
          profile_id     TEXT PRIMARY KEY,
          application_id BIGINT REFERENCES company_remote_applications(id) ON DELETE CASCADE,
          worker_id      TEXT NOT NULL,
          expires_at     TIMESTAMPTZ NOT NULL,
          updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")


_HASH_SQL = """md5(concat_ws('|',j.content_hash,j.apply_url,j.questions_status,
    coalesce(j.questions_checked_at::text,''),coalesce((SELECT string_agg(
    concat_ws(':',q.question_key,q.required::text,q.question_type,
    coalesce(q.options::text,'')),',' ORDER BY q.position,q.question_key)
    FROM company_remote_job_questions q WHERE q.job_id=j.id),'')))"""


def enqueue_eligible(profile_id: str, *, freshness_days: int = 7,
                     limit: int = 500) -> int:
    """Enqueue only fresh, active, confirmed-remote jobs with complete questions.

    Existing legacy catalog identities are excluded read-only by canonical URL
    or ATS board identity, keeping the two application systems disjoint.
    """
    if not str(profile_id).strip():
        raise ValueError("profile_id is required")
    if freshness_days < 1 or limit < 1:
        raise ValueError("freshness_days and limit must be positive")
    sql = f"""
      INSERT INTO company_remote_applications
        (job_id,profile_id,apply_url,apply_url_hash,revalidation_hash)
      SELECT j.id,%s,j.apply_url,
        md5(lower(regexp_replace(split_part(btrim(j.apply_url),'#',1),'/+$',''))),
        {_HASH_SQL}
      FROM company_remote_jobs j
      JOIN company_employer_master m ON m.company_id=j.company_id
      WHERE j.status='active' AND j.remote_type='remote'
        AND j.questions_status='success' AND j.apply_url ~* '^https://'
        AND m.in_target_population AND m.domain_verified
        AND m.identity_status='verified' AND m.monitoring_status='monitoring'
        AND m.hiring_cohort_status='verified_hiring'
        AND m.is_monitoring_representative
        AND j.last_seen_at >= now() - (%s * interval '1 day')
        AND NOT EXISTS (
          SELECT 1 FROM job_catalog old
          WHERE (old.url IS NOT NULL AND
                 lower(regexp_replace(split_part(btrim(old.url),'#',1),'/+$','')) =
                 lower(regexp_replace(split_part(btrim(j.apply_url),'#',1),'/+$','')))
             OR (lower(coalesce(old.ats,''))=lower(j.source)
                 AND coalesce(old.company_key,'')=j.source_board_id
                 AND old.external_id=j.source_job_id)
        )
      ORDER BY j.last_seen_at DESC,j.id DESC LIMIT %s
      ON CONFLICT DO NOTHING"""
    with _cur(False) as cur:
        cur.execute(sql, (str(profile_id).strip(), int(freshness_days), int(limit)))
        return cur.rowcount


def recover_stale_leases() -> int:
    """Return abandoned work to its safe pre-claim state and remove expired locks."""
    with _cur(False) as cur:
        cur.execute("""
          UPDATE company_remote_applications
          SET state=CASE
                WHEN state='preparing' THEN 'approved'
                WHEN state='submitting' THEN 'submission_failed'
                ELSE 'queued' END,
              claimed_by=NULL,lease_expires_at=NULL,state_changed_at=now(),updated_at=now()
          WHERE state IN ('claimed','preparing','submitting') AND lease_expires_at < now()""")
        recovered = cur.rowcount
        cur.execute("DELETE FROM company_remote_application_profile_leases "
                    "WHERE expires_at < now()")
        return recovered


def _normalize_from_states(values: Iterable[str]) -> tuple[str, ...]:
    states = tuple(dict.fromkeys(str(value).strip().casefold() for value in values))
    if not states or any(value not in CLAIMABLE_STATES for value in states):
        raise ValueError("from_states may contain only claimable states")
    return states


def claim_next(profile_id: str, worker_id: str, *, lease_seconds: int = 900,
               from_states: tuple[str, ...] = ("queued",),
               claimed_state: str | None = None,
               submission_batch_id: str | None = None) -> dict | None:
    """Claim one application with row locking and an exclusive profile lease."""
    profile_id, worker_id = str(profile_id).strip(), str(worker_id).strip()
    if not profile_id or not worker_id or lease_seconds < 30:
        raise ValueError("profile_id, worker_id and lease_seconds>=30 are required")
    states = _normalize_from_states(from_states)
    submission_batch_id = str(submission_batch_id or "").strip() or None
    if submission_batch_id is not None and states != ("submit_approved",):
        raise ValueError("submission_batch_id is valid only for submit_approved claims")
    default_targets = {
        ("queued",): "claimed",
        ("approved",): "preparing",
        ("submit_approved",): "submitting",
    }
    target = claimed_state or default_targets.get(states)
    if target is None:
        raise ApplicationStateError("claimable states with different targets cannot be mixed")
    if any(target not in TRANSITIONS[state] for state in states):
        raise ApplicationStateError(f"cannot claim {states!r} into {target}")
    with _cur() as cur:
        cur.execute("DELETE FROM company_remote_application_profile_leases "
                    "WHERE profile_id=%s AND expires_at < now()", (profile_id,))
        cur.execute("""
          INSERT INTO company_remote_application_profile_leases
            (profile_id,application_id,worker_id,expires_at)
          VALUES (%s,NULL,%s,now()+(%s * interval '1 second'))
          ON CONFLICT (profile_id) DO UPDATE SET worker_id=EXCLUDED.worker_id,
            application_id=NULL,expires_at=EXCLUDED.expires_at,updated_at=now()
          WHERE company_remote_application_profile_leases.expires_at < now()
          RETURNING profile_id""", (profile_id, worker_id, int(lease_seconds)))
        if not cur.fetchone():
            return None
        batch_clause = " AND a.submission_batch_id=%s" if submission_batch_id else ""
        claim_args: list[Any] = [profile_id, list(states)]
        if submission_batch_id:
            claim_args.append(submission_batch_id)
        cur.execute(f"""
          SELECT a.*,j.title,j.description,j.description_html,j.requirements,j.benefits,
            j.location_raw,j.locations,j.salary_min,j.salary_max,j.currency,j.source,
            j.source_board_id,j.source_job_id,j.status AS job_status,
            j.remote_type,j.questions_status,j.last_seen_at,
            d.canonical_name AS company_name,d.domain,d.careers_url,
            {_HASH_SQL} AS current_revalidation_hash,
            coalesce((SELECT jsonb_agg(jsonb_build_object(
              'question_key',q.question_key,'source_question_id',q.source_question_id,
              'position',q.position,'label',q.label,'required',q.required,
              'question_type',q.question_type,'options',q.options,'validation',q.validation)
              ORDER BY q.position,q.question_key)
              FROM company_remote_job_questions q WHERE q.job_id=j.id),'[]'::jsonb) AS questions
          FROM company_remote_applications a
          JOIN company_remote_jobs j ON j.id=a.job_id
          JOIN company_discovery d ON d.id=j.company_id
          JOIN company_employer_master m ON m.company_id=j.company_id
          WHERE a.profile_id=%s AND a.state=ANY(%s) AND j.status='active'
            AND j.remote_type='remote' AND j.questions_status='success'
            AND m.in_target_population AND m.domain_verified
            AND m.identity_status='verified' AND m.monitoring_status='monitoring'
            AND m.hiring_cohort_status='verified_hiring'
            AND m.is_monitoring_representative
            AND a.revalidation_hash={_HASH_SQL}
            {batch_clause}
          ORDER BY a.priority DESC,a.queued_at,a.id
          FOR UPDATE OF a SKIP LOCKED LIMIT 1""", tuple(claim_args))
        row = cur.fetchone()
        if not row:
            cur.execute("DELETE FROM company_remote_application_profile_leases "
                        "WHERE profile_id=%s AND worker_id=%s", (profile_id, worker_id))
            return None
        application_id = row["id"] if isinstance(row, dict) else row[0]
        cur.execute("""
          UPDATE company_remote_applications SET state=%s,claimed_by=%s,
            lease_expires_at=now()+(%s * interval '1 second'),state_changed_at=now(),updated_at=now()
          WHERE id=%s RETURNING *""", (target, worker_id, int(lease_seconds), application_id))
        claimed = cur.fetchone()
        cur.execute("""
          UPDATE company_remote_application_profile_leases SET application_id=%s,
            expires_at=now()+(%s * interval '1 second'),updated_at=now()
          WHERE profile_id=%s AND worker_id=%s""",
                    (application_id, int(lease_seconds), profile_id, worker_id))
        result = dict(row)
        if claimed:
            result.update(dict(claimed))
        result["state"] = target
        return result


def renew_lease(application_id: int, profile_id: str, worker_id: str, *,
                lease_seconds: int = 900) -> bool:
    if lease_seconds < 30:
        raise ValueError("lease_seconds must be at least 30")
    with _cur(False) as cur:
        cur.execute("""
          UPDATE company_remote_application_profile_leases p
          SET expires_at=now()+(%s * interval '1 second'),updated_at=now()
          FROM company_remote_applications a
          WHERE p.profile_id=%s AND p.application_id=%s AND p.worker_id=%s
            AND p.expires_at >= now() AND a.id=p.application_id
            AND a.claimed_by=p.worker_id AND a.state IN ('claimed','preparing','submitting')""",
                    (int(lease_seconds), str(profile_id), int(application_id), str(worker_id)))
        renewed = cur.rowcount == 1
        if renewed:
            cur.execute("UPDATE company_remote_applications SET lease_expires_at="
                        "now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s",
                        (int(lease_seconds), int(application_id)))
        return renewed


def record_attempt(application_id: int, phase: str, outcome: str, *,
                   worker_id: str | None = None, detail: dict | None = None) -> int:
    if not str(phase).strip() or not str(outcome).strip():
        raise ValueError("phase and outcome are required")
    with _cur() as cur:
        cur.execute("""
          INSERT INTO company_remote_application_attempts
            (application_id,phase,outcome,worker_id,detail)
          VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (int(application_id), str(phase), str(outcome), worker_id,
                     _db_json(detail or {})))
        row = cur.fetchone()
        return row["id"] if isinstance(row, dict) else row[0]


_JOINED_COLUMNS = f"""a.*,j.title,j.description,j.description_html,j.requirements,j.benefits,
  j.location_raw,j.locations,j.salary_min,j.salary_max,j.currency,j.source,
  j.source_board_id,j.source_job_id,j.status AS job_status,j.remote_type,
  j.questions_status,j.last_seen_at,d.canonical_name AS company_name,d.domain,d.careers_url,
  {_HASH_SQL} AS current_revalidation_hash,
  coalesce((SELECT jsonb_agg(jsonb_build_object(
    'question_key',q.question_key,'source_question_id',q.source_question_id,
    'position',q.position,'label',q.label,'required',q.required,
    'question_type',q.question_type,'options',q.options,'validation',q.validation)
    ORDER BY q.position,q.question_key)
    FROM company_remote_job_questions q WHERE q.job_id=j.id),'[]'::jsonb) AS questions"""


def get_application(application_id: int) -> dict | None:
    """Return one application with its live job, company, questions and hashes."""
    with _cur() as cur:
        cur.execute(f"""SELECT {_JOINED_COLUMNS}
          FROM company_remote_applications a
          JOIN company_remote_jobs j ON j.id=a.job_id
          JOIN company_discovery d ON d.id=j.company_id
          WHERE a.id=%s""", (int(application_id),))
        row = cur.fetchone()
        return dict(row) if row else None


def list_applications(profile_id: str | None = None, state: str | None = None,
                      limit: int = 100) -> list[dict]:
    """List reviewable queue records without acquiring a worker lease."""
    if state is not None and state not in STATES:
        raise ValueError(f"invalid application state: {state}")
    if limit < 1:
        raise ValueError("limit must be positive")
    clauses, args = [], []
    if profile_id is not None:
        clauses.append("a.profile_id=%s")
        args.append(str(profile_id))
    if state is not None:
        clauses.append("a.state=%s")
        args.append(state)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    args.append(int(limit))
    with _cur() as cur:
        cur.execute(f"""SELECT {_JOINED_COLUMNS}
          FROM company_remote_applications a
          JOIN company_remote_jobs j ON j.id=a.job_id
          JOIN company_discovery d ON d.id=j.company_id
          {where} ORDER BY a.priority DESC,a.queued_at,a.id LIMIT %s""", tuple(args))
        return [dict(row) for row in cur.fetchall()]


def transition(application_id: int, to_state: str, actor: str, *, reason: str | None = None,
               expected_revalidation_hash: str | None = None,
               payload: dict | None = None, _review_action: str | None = None,
               _review_evidence: dict | None = None) -> dict:
    """Apply a validated state transition, revalidating the live job/form hash."""
    to_state = str(to_state).strip().casefold()
    if to_state not in STATES or not str(actor).strip():
        raise ValueError("valid to_state and actor are required")
    with _cur() as cur:
        cur.execute(f"""
          SELECT a.*,j.status AS job_status,j.remote_type,j.questions_status,
                 {_HASH_SQL} AS current_revalidation_hash
          FROM company_remote_applications a JOIN company_remote_jobs j ON j.id=a.job_id
          WHERE a.id=%s FOR UPDATE OF a""", (int(application_id),))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"unknown application id: {application_id}")
        current = row["state"]
        if to_state not in TRANSITIONS[current]:
            raise ApplicationStateError(f"invalid transition: {current} -> {to_state}")
        if to_state == "submit_approved":
            raise ApplicationStateError("submit_approved requires authorize_batch")
        live_hash = row["current_revalidation_hash"]
        if (expected_revalidation_hash is not None and
                expected_revalidation_hash != row["revalidation_hash"]):
            raise StaleApplicationError("worker revalidation hash is stale")
        if live_hash != row["revalidation_hash"]:
            raise StaleApplicationError("job or application questions changed")
        if row["job_status"] != "active" or row["remote_type"] != "remote" or \
                row["questions_status"] != "success":
            raise StaleApplicationError("job is no longer eligible")
        release = to_state not in {"claimed", "preparing", "submitting"}
        payload = payload or {}
        if _review_action is not None and _review_action not in {
                "approve", "reject", "human_submitted", "authorize_auto_submit",
                "auto_submitted"}:
            raise ValueError("invalid review action")
        if to_state == "auto_submitted":
            if _review_action != "auto_submitted":
                raise ApplicationStateError("auto_submitted requires mark_auto_submitted")
            receipt = payload.get("receipt")
            if not isinstance(receipt, dict) or receipt.get("confirmed") is not True:
                raise ApplicationStateError(
                    "auto_submitted requires positive live confirmation evidence")
        fit_score = payload.get("fit_score")
        if fit_score is not None:
            fit_score = float(fit_score)
            if not 0 <= fit_score <= 100:
                raise ValueError("fit_score must be between 0 and 100")
        cur.execute("""
          UPDATE company_remote_applications SET state=%s,state_changed_at=now(),updated_at=now(),
            claimed_by=CASE WHEN %s THEN NULL ELSE claimed_by END,
            lease_expires_at=CASE WHEN %s THEN NULL ELSE lease_expires_at END,
            artifact_dir=COALESCE(%s,artifact_dir),report=COALESCE(%s,report),
            policy_result=COALESCE(%s,policy_result),last_error=COALESCE(%s,last_error),
            fit_score=COALESCE(%s,fit_score),
            human_submitted_at=CASE WHEN %s THEN now() ELSE human_submitted_at END,
            human_submitted_by=CASE WHEN %s THEN %s ELSE human_submitted_by END,
            auto_submitted_at=CASE WHEN %s THEN now() ELSE auto_submitted_at END,
            submission_receipt=CASE WHEN %s OR %s THEN %s ELSE submission_receipt END
          WHERE id=%s RETURNING *""",
                    (to_state, release, release, payload.get("artifact_dir"),
                     _db_json(payload["report"]) if "report" in payload else None,
                     _db_json(payload["policy_result"]) if "policy_result" in payload else None,
                     payload.get("last_error") or reason, fit_score,
                     to_state == "human_submitted", to_state == "human_submitted", actor,
                     to_state == "auto_submitted", to_state == "human_submitted",
                     to_state == "auto_submitted", _db_json(payload.get("receipt", {})),
                     int(application_id)))
        updated = cur.fetchone()
        cur.execute("""
          INSERT INTO company_remote_application_attempts
            (application_id,phase,outcome,worker_id,detail)
          VALUES (%s,'state_transition',%s,%s,%s)""",
                    (int(application_id), to_state, str(actor),
                     _db_json({"from": current, "reason": reason, **(payload or {})})))
        if release:
            cur.execute("DELETE FROM company_remote_application_profile_leases "
                        "WHERE application_id=%s", (int(application_id),))
        if _review_action:
            cur.execute("""
              INSERT INTO company_remote_application_reviews
                (application_id,action,actor,reason,evidence)
              VALUES (%s,%s,%s,%s,%s)""",
                        (int(application_id), _review_action, str(actor), reason,
                         _db_json(_review_evidence or {})))
        return dict(updated)


def _review(application_id: int, action: str, actor: str, *, reason: str | None = None,
            evidence: dict | None = None) -> None:
    with _cur(False) as cur:
        cur.execute("""
          INSERT INTO company_remote_application_reviews
            (application_id,action,actor,reason,evidence) VALUES (%s,%s,%s,%s,%s)""",
                    (int(application_id), action, str(actor), reason,
                     _db_json(evidence or {})))


def approve(application_id: int, actor: str, expected_revalidation_hash: str) -> dict:
    return transition(application_id, "approved", actor,
                      expected_revalidation_hash=expected_revalidation_hash,
                      _review_action="approve",
                      _review_evidence={"revalidation_hash": expected_revalidation_hash})


def reject(application_id: int, actor: str, reason: str) -> dict:
    if not str(reason).strip():
        raise ValueError("rejection reason is required")
    return transition(application_id, "rejected", actor, reason=reason,
                      _review_action="reject")


def mark_human_submitted(application_id: int, actor: str, *, receipt: dict | None = None,
                         expected_revalidation_hash: str | None = None) -> dict:
    """Audit a submission already performed/confirmed by the named human actor."""
    if not str(actor).strip():
        raise ValueError("human actor is required")
    return transition(application_id, "human_submitted", actor,
                      expected_revalidation_hash=expected_revalidation_hash,
                      payload={"receipt": receipt or {}},
                      _review_action="human_submitted",
                      _review_evidence=receipt or {})


def authorize_batch(profile_id: str, application_ids: Iterable[int], actor: str,
                    confirmation: str, expected_hashes: dict[int, str]) -> dict:
    """Atomically authorize one explicit set of applications for one final attempt.

    The confirmation phrase is deliberately count-bound so a stale UI cannot turn
    approval for a small preview into authorization for a larger batch.
    """
    profile_id, actor = str(profile_id).strip(), str(actor).strip()
    ids = tuple(dict.fromkeys(int(value) for value in application_ids))
    expected_hashes = {int(key): str(value) for key, value in expected_hashes.items()}
    if not profile_id or not actor or not ids:
        raise ValueError("profile_id, actor and application_ids are required")
    required_confirmation = f"SEND {len(ids)}"
    if str(confirmation).strip() != required_confirmation:
        raise ValueError(f"confirmation must equal {required_confirmation!r}")
    if set(expected_hashes) != set(ids) or any(not expected_hashes[value] for value in ids):
        raise ValueError("an expected revalidation hash is required for every application")

    batch_id = str(uuid.uuid4())
    with _cur() as cur:
        cur.execute(f"""
          SELECT a.id,a.profile_id,a.state,a.revalidation_hash,j.status AS job_status,
            j.remote_type,j.questions_status,{_HASH_SQL} AS current_revalidation_hash
          FROM company_remote_applications a
          JOIN company_remote_jobs j ON j.id=a.job_id
          WHERE a.id=ANY(%s) FOR UPDATE OF a""", (list(ids),))
        rows = [dict(row) for row in cur.fetchall()]
        if len(rows) != len(ids):
            raise ValueError("one or more applications do not exist")
        for row in rows:
            app_id = int(row["id"])
            if row["profile_id"] != profile_id or row["state"] != "awaiting_approval":
                raise ApplicationStateError(
                    f"application {app_id} is not awaiting approval for this profile")
            if row["revalidation_hash"] != expected_hashes[app_id] or \
                    row["current_revalidation_hash"] != row["revalidation_hash"]:
                raise StaleApplicationError(f"application {app_id} changed before approval")
            if row["job_status"] != "active" or row["remote_type"] != "remote" or \
                    row["questions_status"] != "success":
                raise StaleApplicationError(f"application {app_id} is no longer eligible")

        cur.execute("""
          INSERT INTO company_remote_application_batches
            (id,profile_id,actor,confirmation,application_ids)
          VALUES (%s,%s,%s,%s,%s)""",
                    (batch_id, profile_id, actor, required_confirmation,
                     _db_json(list(ids))))
        cur.execute("""
          UPDATE company_remote_applications
          SET state='submit_approved',state_changed_at=now(),updated_at=now(),
              submission_authorized_at=now(),submission_authorized_by=%s,
              submission_batch_id=%s,last_error=NULL
          WHERE id=ANY(%s)""", (actor, batch_id, list(ids)))
        for app_id in ids:
            evidence = {"batch_id": batch_id, "confirmation": required_confirmation,
                        "revalidation_hash": expected_hashes[app_id]}
            cur.execute("""
              INSERT INTO company_remote_application_reviews
                (application_id,action,actor,evidence)
              VALUES (%s,'authorize_auto_submit',%s,%s)""",
                        (app_id, actor, _db_json(evidence)))
            cur.execute("""
              INSERT INTO company_remote_application_attempts
                (application_id,phase,outcome,worker_id,detail)
              VALUES (%s,'batch_authorization','submit_approved',%s,%s)""",
                        (app_id, actor, _db_json(evidence)))
    return {"batch_id": batch_id, "profile_id": profile_id,
            "application_ids": list(ids), "count": len(ids)}


def mark_auto_submitted(application_id: int, worker_id: str, *, receipt: dict,
                        expected_revalidation_hash: str) -> dict:
    """Persist a confirmed automatic result; ambiguous outcomes are rejected."""
    if not isinstance(receipt, dict) or receipt.get("confirmed") is not True:
        raise ValueError("positive confirmation receipt is required")
    return transition(
        application_id, "auto_submitted", worker_id,
        expected_revalidation_hash=expected_revalidation_hash,
        payload={"receipt": receipt}, _review_action="auto_submitted",
        _review_evidence=receipt,
    )


def stats(profile_id: str | None = None) -> dict:
    args: tuple[Any, ...] = ()
    where = ""
    if profile_id is not None:
        where, args = " WHERE profile_id=%s", (str(profile_id),)
    with _cur() as cur:
        cur.execute("SELECT state,COUNT(*) AS count FROM company_remote_applications" +
                    where + " GROUP BY state ORDER BY state", args)
        rows = cur.fetchall()
    by_state = {row["state"]: row["count"] for row in rows}
    return {"total": sum(by_state.values()), "by_state": by_state}
