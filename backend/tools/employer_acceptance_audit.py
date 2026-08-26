"""Executable acceptance audit for the isolated mass-hiring employer pipeline."""
from __future__ import annotations

import argparse
import json

from backend.tools import company_discovery_db as company_db
from backend.tools.company_job_sources import SUPPORTED_ATS


def run_audit(*, expected: int = 10_000) -> dict:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT COUNT(*) total,
            COUNT(*) FILTER (WHERE mandatory_seed) mandatory,
            COUNT(*) FILTER (WHERE identity_status='verified') identity_verified,
            COUNT(*) FILTER (WHERE monitoring_status='qualified') qualified,
            COUNT(*) FILTER (WHERE monitoring_status='monitoring') monitoring
          FROM company_employer_master WHERE in_target_population
        """)
        population = dict(cur.fetchone())
        total = population["total"]
        mandatory = population["mandatory"]
        identity_verified = population["identity_verified"]
        qualified = population["qualified"]
        monitoring = population["monitoring"]
        cur.execute("SELECT COUNT(*) physical_total FROM company_employer_master")
        physical_total = next(iter(cur.fetchone().values()))
        cur.execute("""
          SELECT COUNT(*) FROM company_employer_master m
          JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND (
            NULLIF(m.brand_name,'') IS NULL OR NULLIF(c.legal_name,'') IS NULL
            OR NULLIF(m.employer_segment,'') IS NULL OR m.brand_identity='{}'::jsonb
            OR (m.employee_count IS NULL AND m.employee_count_min IS NULL)
            OR NULLIF(m.industry,'') IS NULL OR NULLIF(m.headquarters,'') IS NULL
            OR NOT m.domain_verified OR NULLIF(c.domain,'') IS NULL
            OR NULLIF(c.careers_url,'') IS NULL OR NULLIF(c.ats,'') IS NULL)
        """)
        incomplete_employers = next(iter(cur.fetchone().values()))
        cur.execute("""
          SELECT COUNT(*) FROM company_employer_master m
          JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.domain_verified AND NOT (
            EXISTS (SELECT 1 FROM jsonb_array_elements(m.domain_evidence) evidence
              WHERE evidence->>'class'='structured_corporate_source'
                AND lower(COALESCE(evidence->>'candidate_domain',''))=lower(c.domain))
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.domain_evidence) evidence
              WHERE evidence->>'class'='official_site_identity')
          )
        """)
        invalid_domain_evidence = next(iter(cur.fetchone().values()))
        cur.execute("""
          SELECT COUNT(*) FROM company_employer_master m
          JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.identity_status='verified' AND (
            NOT m.domain_verified OR m.identity_confidence<0.95
            OR COALESCE((m.qualification_evidence->>'employee_count_conflict')::boolean,FALSE)
            OR m.entity_risk_flags ?| ARRAY[
              'shell_or_shared_services','fund_or_trust','aggregate_or_sentence_name']
            OR NULLIF(c.careers_url,'') IS NULL OR c.ats<>ALL(%s)
          )
        """, (list(SUPPORTED_ATS),))
        invalid_verified_identities = next(iter(cur.fetchone().values()))
        cur.execute("""
          SELECT COUNT(*) FROM company_employer_master m
          WHERE m.in_target_population AND m.monitoring_status='monitoring' AND (
            m.identity_status<>'verified' OR NOT m.is_monitoring_representative
            OR m.score_confidence<0.7 OR m.remote_score<40
            OR m.mass_hiring_score<50 OR m.hiring_activity_score<45
            OR NOT EXISTS (
              SELECT 1 FROM company_remote_jobs j WHERE j.company_id=m.company_id
                AND j.status='active' AND j.remote_type='remote')
            OR (SELECT COUNT(*) FROM company_remote_job_scans s
                WHERE s.company_id=m.company_id AND s.scan_complete AND s.scan_succeeded)<2
          )
        """)
        invalid_monitoring = next(iter(cur.fetchone().values()))
        cur.execute("""
          SELECT COUNT(*) FROM company_mass_application_queue q
          JOIN company_remote_jobs j ON j.id=q.job_id
          JOIN company_employer_master m ON m.company_id=q.company_id
          WHERE q.status IN ('pending_review','approved') AND NOT (
            m.in_target_population AND
            j.status='active' AND j.remote_type='remote' AND j.questions_status='success'
            AND m.identity_status='verified' AND m.monitoring_status='monitoring'
            AND m.is_monitoring_representative AND m.score_confidence>=0.7
            AND m.remote_score>=40 AND m.mass_hiring_score>=50
            AND m.hiring_activity_score>=45)
        """)
        invalid_queue = next(iter(cur.fetchone().values()))
        cur.execute("""
          SELECT COUNT(*) FROM (
            SELECT lower(c.domain) domain FROM company_employer_master m
            JOIN company_discovery c ON c.id=m.company_id
            WHERE m.in_target_population AND m.is_monitoring_representative
              AND NULLIF(c.domain,'') IS NOT NULL
            GROUP BY lower(c.domain) HAVING COUNT(*)>1
          ) duplicates
        """)
        duplicate_monitoring_domains = next(iter(cur.fetchone().values()))

    blockers = {
        "population_mismatch": abs(int(total) - int(expected)),
        "mandatory_seed_missing": max(0, 15 - int(mandatory)),
        "incomplete_employers": int(incomplete_employers),
        "invalid_domain_evidence": int(invalid_domain_evidence),
        "invalid_verified_identities": int(invalid_verified_identities),
        "invalid_monitoring": int(invalid_monitoring),
        "invalid_application_queue": int(invalid_queue),
        "duplicate_monitoring_domains": int(duplicate_monitoring_domains),
    }
    return {
        "expected": int(expected), "total": int(total),
        "active_total": int(total), "physical_total": int(physical_total),
        "mandatory": int(mandatory),
        "identity_verified": int(identity_verified), "qualified": int(qualified),
        "monitoring": int(monitoring), "blockers": blockers,
        "passed": not any(blockers.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit mass-hiring acceptance gates")
    parser.add_argument("--expected", type=int, default=10_000)
    args = parser.parse_args(argv)
    if args.expected < 15:
        parser.error("expected population must be at least 15")
    result = run_audit(expected=args.expected)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
