"""Evidence-based employer scoring and gated mass-application queue creation."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from backend.tools import company_discovery_db as company_db

try:
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover
    Json = None


def ensure_schema() -> None:
    with company_db._cur(False) as cur:
        cur.execute("""
          CREATE TABLE IF NOT EXISTS company_employer_score_history (
            id BIGSERIAL PRIMARY KEY,
            company_id BIGINT NOT NULL REFERENCES company_discovery(id) ON DELETE CASCADE,
            remote_score DOUBLE PRECISION NOT NULL,
            entry_level_score DOUBLE PRECISION NOT NULL,
            mass_hiring_score DOUBLE PRECISION NOT NULL,
            application_ease_score DOUBLE PRECISION NOT NULL,
            hiring_activity_score DOUBLE PRECISION NOT NULL,
            score_confidence DOUBLE PRECISION NOT NULL,
            evidence JSONB NOT NULL,
            evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now()
          );
          CREATE TABLE IF NOT EXISTS company_mass_application_queue (
            id BIGSERIAL PRIMARY KEY,
            company_id BIGINT NOT NULL REFERENCES company_discovery(id) ON DELETE CASCADE,
            job_id BIGINT NOT NULL UNIQUE REFERENCES company_remote_jobs(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending_review'
              CHECK (status IN ('pending_review','approved','submitted','skipped','failed')),
            gate_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
          );
        """)


def calculate_scores(row: dict) -> dict[str, float]:
    active = int(row.get("active_jobs") or 0)
    recent = int(row.get("recent_jobs") or 0)
    entry = int(row.get("entry_jobs") or 0)
    service = int(row.get("customer_service_jobs") or 0)
    scans = int(row.get("complete_scans") or 0)
    employees = int(row.get("employee_count") or row.get("employee_count_min") or 0)
    sites = int(row.get("hiring_sites") or 0)
    remote = min(100.0, 25.0 + active * 12.0 + recent * 3.0) if active else 0.0
    entry_ratio = min(active, entry + service) / active if active else 0.0
    entry_score = min(100.0, entry_ratio * 85.0 + (15.0 if entry_ratio else 0.0))
    employee_component = min(40.0, max(0.0, math.log10(max(employees, 1)) - 3) * 20.0)
    mass = min(100.0, employee_component + min(20.0, sites / 25.0)
               + min(40.0, active * 8.0))
    activity = min(100.0, min(60.0, active * 15.0) + min(20.0, recent * 5.0)
                   + min(20.0, scans * 10.0))
    ats_base = {"greenhouse": 85, "lever": 82, "ashby": 82, "workable": 75,
                "smartrecruiters": 68, "icims": 55, "oracle": 52,
                "successfactors": 48, "eightfold": 48, "workday": 45,
                "custom": 40}.get(str(row.get("ats") or ""), 35)
    successful = int(row.get("questions_success") or 0)
    failed = int(row.get("questions_failed") or 0)
    if successful:
        ease = min(100.0, ats_base + 10.0)
    elif failed:
        ease = min(float(ats_base), 30.0)
    else:
        ease = min(float(ats_base), 45.0)
    completeness = sum(bool(row.get(field)) for field in
                       ("industry", "headquarters", "ats", "careers_url")) / 4
    confidence = min(1.0, scans / 4.0) * (0.7 + 0.3 * completeness)
    return {"remote_score": round(remote, 2), "entry_level_score": round(entry_score, 2),
            "mass_hiring_score": round(mass, 2),
            "application_ease_score": round(ease, 2),
            "hiring_activity_score": round(activity, 2),
            "score_confidence": round(confidence, 4)}


def score_employers(*, limit: int = 2000) -> dict:
    ensure_schema()
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.employee_count,m.employee_count_min,m.industry,m.headquarters,
            c.careers_url,c.ats,COALESCE((c.metadata->>'hiring_sites')::integer,0) hiring_sites,
            COALESCE(j.active_jobs,0) active_jobs,COALESCE(j.recent_jobs,0) recent_jobs,
            COALESCE(j.entry_jobs,0) entry_jobs,
            COALESCE(j.customer_service_jobs,0) customer_service_jobs,
            COALESCE(j.questions_success,0) questions_success,
            COALESCE(j.questions_failed,0) questions_failed,
            COALESCE(s.complete_scans,0) complete_scans
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          LEFT JOIN LATERAL (
            SELECT COUNT(*) FILTER (WHERE status='active') active_jobs,
              COUNT(*) FILTER (WHERE status='active' AND posted_at>=now()-interval '30 days') recent_jobs,
              COUNT(*) FILTER (WHERE status='active' AND (title||' '||description) ~* '\\m(entry.?level|junior|no experience|required experience.{0,15}(0|1) year)\\M') entry_jobs,
              COUNT(*) FILTER (WHERE status='active' AND (title||' '||description) ~* '\\m(customer service|customer support|call center|data entry)\\M') customer_service_jobs,
              COUNT(*) FILTER (WHERE status='active' AND questions_status='success') questions_success,
              COUNT(*) FILTER (WHERE status='active' AND questions_status='failed') questions_failed
            FROM company_remote_jobs j WHERE j.company_id=m.company_id
          ) j ON TRUE
          LEFT JOIN LATERAL (
            SELECT COUNT(*) complete_scans FROM company_remote_job_scans s
            WHERE s.company_id=m.company_id AND s.scan_complete AND s.scan_succeeded
          ) s ON TRUE
          WHERE m.in_target_population AND m.identity_status='verified'
            AND m.hiring_cohort_status='verified_hiring'
            AND m.is_monitoring_representative
          ORDER BY m.company_id LIMIT %s
        """, (max(1, int(limit)),))
        rows = [dict(row) for row in cur.fetchall()]
    evaluated = promoted = 0
    with company_db.conn() as connection:
        cur = connection.cursor()
        try:
            for row in rows:
                if int(row.get("complete_scans") or 0) < 2:
                    continue
                scores = calculate_scores(row)
                evidence = {key: row.get(key) for key in (
                    "active_jobs", "recent_jobs", "entry_jobs", "customer_service_jobs",
                    "questions_success", "questions_failed", "complete_scans", "hiring_sites")}
                evidence["evaluated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                cur.execute("""
                  UPDATE company_employer_master SET remote_score=%s,entry_level_score=%s,
                    mass_hiring_score=%s,application_ease_score=%s,hiring_activity_score=%s,
                    score_confidence=%s,qualification_evidence=qualification_evidence || %s,
                    monitoring_status=CASE WHEN %s THEN 'monitoring'
                      WHEN monitoring_status='monitoring' THEN 'qualified'
                      ELSE monitoring_status END,
                    updated_at=now() WHERE company_id=%s AND in_target_population
                      AND hiring_cohort_status='verified_hiring'
                """, (scores["remote_score"], scores["entry_level_score"],
                      scores["mass_hiring_score"], scores["application_ease_score"],
                      scores["hiring_activity_score"], scores["score_confidence"],
                      Json({"score_inputs": evidence}) if Json is not None else {"score_inputs": evidence},
                      bool(row["active_jobs"] and scores["remote_score"] >= 40
                           and scores["mass_hiring_score"] >= 50
                           and scores["hiring_activity_score"] >= 45
                           and scores["score_confidence"] >= 0.7), row["company_id"]))
                if cur.rowcount != 1:
                    continue
                was_promoted = bool(row["active_jobs"] and scores["remote_score"] >= 40
                                    and scores["mass_hiring_score"] >= 50
                                    and scores["hiring_activity_score"] >= 45
                                    and scores["score_confidence"] >= 0.7)
                promoted += int(was_promoted)
                cur.execute("""
                  INSERT INTO company_employer_score_history
                    (company_id,remote_score,entry_level_score,mass_hiring_score,
                     application_ease_score,hiring_activity_score,score_confidence,evidence)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (row["company_id"], scores["remote_score"], scores["entry_level_score"],
                      scores["mass_hiring_score"], scores["application_ease_score"],
                      scores["hiring_activity_score"], scores["score_confidence"],
                      Json(evidence) if Json is not None else evidence))
                evaluated += 1
            cur.execute("""
              UPDATE company_mass_application_queue q SET status='skipped',
                gate_evidence=q.gate_evidence || jsonb_build_object(
                  'gate_valid',false,'invalidated_at',now(),
                  'reason','employer_or_job_no_longer_passes_mass_hiring_gate'),
                updated_at=now()
              FROM company_remote_jobs j JOIN company_employer_master m
                ON m.company_id=j.company_id
              WHERE q.job_id=j.id AND q.status IN ('pending_review','approved') AND NOT (
                m.in_target_population AND
                j.status='active' AND j.remote_type='remote' AND j.questions_status='success'
                AND m.monitoring_status='monitoring' AND m.identity_status='verified'
                AND m.hiring_cohort_status='verified_hiring'
                AND m.is_monitoring_representative AND m.score_confidence>=0.7
                AND m.remote_score>=40 AND m.mass_hiring_score>=50
                AND m.hiring_activity_score>=45
                AND COALESCE((m.qualification_evidence->>'employee_count_conflict')::boolean,FALSE)=FALSE
              )
            """)
            cur.execute("""
              INSERT INTO company_mass_application_queue (company_id,job_id,gate_evidence)
              SELECT j.company_id,j.id,jsonb_build_object(
                'active_remote_job',true,'questions_complete',true,
                'monitoring_status','monitoring','requires_user_review',true,
                'gate_valid',true,'score_confidence',m.score_confidence,
                'remote_score',m.remote_score,'mass_hiring_score',m.mass_hiring_score,
                'hiring_activity_score',m.hiring_activity_score)
              FROM company_remote_jobs j JOIN company_employer_master m ON m.company_id=j.company_id
              WHERE m.in_target_population AND j.status='active' AND j.remote_type='remote'
                AND j.questions_status='success' AND m.monitoring_status='monitoring'
                AND m.identity_status='verified' AND m.is_monitoring_representative
                AND m.hiring_cohort_status='verified_hiring'
                AND m.score_confidence>=0.7 AND m.remote_score>=40
                AND m.mass_hiring_score>=50 AND m.hiring_activity_score>=45
                AND COALESCE((m.qualification_evidence->>'employee_count_conflict')::boolean,FALSE)=FALSE
              ON CONFLICT (job_id) DO NOTHING
            """)
            queued = cur.rowcount
        finally:
            cur.close()
    return {"selected": len(rows), "evaluated": evaluated, "promoted": promoted,
            "queued": queued}
