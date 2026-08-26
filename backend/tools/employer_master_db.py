"""Qualification state for the curated mass-hiring employer population."""
from __future__ import annotations

from backend.tools import company_discovery_db as company_db

try:
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover
    Json = None


IDENTITY_STATES = ("candidate", "verified", "quarantined", "rejected")
MONITORING_STATES = ("candidate", "qualified", "monitoring", "rejected")
HIRING_COHORT_STATES = (
    "reservoir_candidate", "evidence_incomplete", "verified_hiring", "quarantined",
)


def ensure_schema() -> None:
    with company_db._cur(False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_employer_master (
          company_id                BIGINT PRIMARY KEY REFERENCES company_discovery(id)
                                    ON DELETE CASCADE,
          brand_name                TEXT NOT NULL,
          employee_count            BIGINT,
          employee_count_min        BIGINT,
          employee_count_max        BIGINT,
          employee_size_source      TEXT,
          industry                  TEXT,
          headquarters              TEXT,
          headquarters_country      TEXT NOT NULL DEFAULT 'US',
          headquarters_address_type TEXT,
          naics_code                 TEXT,
          linkedin_url              TEXT,
          employer_segment          TEXT NOT NULL DEFAULT 'general',
          entity_risk_flags         JSONB NOT NULL DEFAULT '[]'::jsonb,
          brand_identity            JSONB NOT NULL DEFAULT '{}'::jsonb,
          in_target_population      BOOLEAN NOT NULL DEFAULT FALSE,
          candidate_domain          TEXT,
          domain_verified           BOOLEAN NOT NULL DEFAULT FALSE,
          identity_status           TEXT NOT NULL DEFAULT 'candidate'
                                    CHECK (identity_status IN
                                      ('candidate','verified','quarantined','rejected')),
          identity_confidence       DOUBLE PRECISION NOT NULL DEFAULT 0,
          domain_evidence           JSONB NOT NULL DEFAULT '[]'::jsonb,
          mandatory_seed            BOOLEAN NOT NULL DEFAULT FALSE,
          monitoring_status         TEXT NOT NULL DEFAULT 'candidate'
                                    CHECK (monitoring_status IN
                                      ('candidate','qualified','monitoring','rejected')),
          hiring_cohort_status      TEXT NOT NULL DEFAULT 'reservoir_candidate'
                                    CHECK (hiring_cohort_status IN
                                      ('reservoir_candidate','evidence_incomplete',
                                       'verified_hiring','quarantined')),
          hiring_cohort_evidence    JSONB NOT NULL DEFAULT '{}'::jsonb,
          hiring_cohort_checked_at  TIMESTAMPTZ,
          qualification_evidence    JSONB NOT NULL DEFAULT '{}'::jsonb,
          canonical_company_id      BIGINT REFERENCES company_discovery(id),
          is_monitoring_representative BOOLEAN NOT NULL DEFAULT FALSE,
          remote_score              DOUBLE PRECISION,
          entry_level_score         DOUBLE PRECISION,
          mass_hiring_score         DOUBLE PRECISION,
          application_ease_score    DOUBLE PRECISION,
          hiring_activity_score     DOUBLE PRECISION,
          score_confidence          DOUBLE PRECISION NOT NULL DEFAULT 0,
          identity_enrichment_status TEXT NOT NULL DEFAULT 'pending',
          identity_enrichment_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
          identity_enrichment_gaps  JSONB NOT NULL DEFAULT '{}'::jsonb,
          identity_enriched_at      TIMESTAMPTZ,
          first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_verified_at          TIMESTAMPTZ,
          updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS cem_identity ON company_employer_master(identity_status)")
        cur.execute("CREATE INDEX IF NOT EXISTS cem_monitoring ON company_employer_master(monitoring_status)")
        cur.execute("CREATE INDEX IF NOT EXISTS cem_employee_count ON company_employer_master(employee_count DESC)")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS employee_count_min BIGINT")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS employee_count_max BIGINT")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS candidate_domain TEXT")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS domain_verified BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS canonical_company_id BIGINT REFERENCES company_discovery(id)")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS is_monitoring_representative BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS employer_segment TEXT NOT NULL DEFAULT 'general'")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS entity_risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS brand_identity JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS in_target_population BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS headquarters_address_type TEXT")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS naics_code TEXT")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS identity_enrichment_status TEXT NOT NULL DEFAULT 'pending'")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS identity_enrichment_provenance JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS identity_enrichment_gaps JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS identity_enriched_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS hiring_cohort_status TEXT NOT NULL DEFAULT 'reservoir_candidate' CHECK (hiring_cohort_status IN ('reservoir_candidate','evidence_incomplete','verified_hiring','quarantined'))")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS hiring_cohort_evidence JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE company_employer_master ADD COLUMN IF NOT EXISTS hiring_cohort_checked_at TIMESTAMPTZ")
        cur.execute("CREATE INDEX IF NOT EXISTS cem_target_population ON company_employer_master(in_target_population) WHERE in_target_population")
        cur.execute("CREATE INDEX IF NOT EXISTS cem_hiring_cohort ON company_employer_master(hiring_cohort_status) WHERE in_target_population")


def sync_source(source: str, records: list[dict]) -> int:
    if not records:
        return 0
    by_external = {str(row["source_external_id"]): row for row in records}
    with company_db._cur() as cur:
        cur.execute(
            "SELECT id,source_external_id FROM company_discovery "
            "WHERE source=%s AND source_external_id=ANY(%s)",
            (source, list(by_external)))
        identities = [(int(row["id"]), str(row["source_external_id"]))
                      for row in cur.fetchall()]
    values = []
    for company_id, external_id in identities:
        row = by_external[external_id]
        meta = row.get("metadata") or {}
        employee_count = meta.get("employee_count")
        employee_count_min = meta.get("employee_count_min")
        employee_count_max = meta.get("employee_count_max")
        values.append((
            company_id, meta.get("brand_name") or row.get("trade_name")
            or row.get("legal_name"), employee_count, employee_count_min,
            employee_count_max,
            "wikidata:P1128" if employee_count is not None else (
                "E-Verify workforce range" if employee_count_min is not None else None),
            row.get("industry") or None, meta.get("headquarters") or None,
            bool(meta.get("mandatory_seed")),
            Json({"source": source, "source_external_id": external_id})
            if Json is not None else {"source": source, "source_external_id": external_id},
        ))
    with company_db._cur(False) as cur:
        cur.executemany("""
          INSERT INTO company_employer_master
            (company_id,brand_name,employee_count,employee_count_min,employee_count_max,
             employee_size_source,industry,
             headquarters,mandatory_seed,qualification_evidence)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (company_id) DO UPDATE SET
            brand_name=EXCLUDED.brand_name,
            employee_count=COALESCE(EXCLUDED.employee_count,
                                    company_employer_master.employee_count),
            employee_count_min=COALESCE(EXCLUDED.employee_count_min,
                                        company_employer_master.employee_count_min),
            employee_count_max=COALESCE(EXCLUDED.employee_count_max,
                                        company_employer_master.employee_count_max),
            employee_size_source=COALESCE(EXCLUDED.employee_size_source,
                                          company_employer_master.employee_size_source),
            industry=COALESCE(EXCLUDED.industry,company_employer_master.industry),
            headquarters=COALESCE(EXCLUDED.headquarters,company_employer_master.headquarters),
            mandatory_seed=company_employer_master.mandatory_seed OR EXCLUDED.mandatory_seed,
            qualification_evidence=company_employer_master.qualification_evidence ||
                                   EXCLUDED.qualification_evidence,
            updated_at=now()
        """, values)
        return cur.rowcount


def reset_unverified_candidates() -> int:
    """Replace only generated, never-qualified master rows on a source refresh."""
    with company_db._cur(False) as cur:
        cur.execute("""
          DELETE FROM company_employer_master
          WHERE identity_status='candidate' AND monitoring_status='candidate'
        """)
        return cur.rowcount


def set_target_population(records: list[dict], *, expected: int) -> int:
    """Atomically replace the active population while preserving historical rows."""
    identities: dict[str, set[str]] = {}
    for row in records:
        identities.setdefault(str(row["source"]), set()).add(
            str(row["source_external_id"]))
    with company_db.conn() as connection:
        cur = connection.cursor()
        try:
            company_ids: set[int] = set()
            for source, external_ids in identities.items():
                cur.execute(
                    "SELECT id FROM company_discovery WHERE source=%s "
                    "AND source_external_id=ANY(%s)",
                    (source, list(external_ids)))
                company_ids.update(int(row[0]) for row in cur.fetchall())
            if len(company_ids) != int(expected):
                raise ValueError(
                    f"target population resolved {len(company_ids)} rows; expected {expected}")
            cur.execute(
                "UPDATE company_employer_master SET in_target_population=FALSE "
                "WHERE in_target_population")
            cur.execute(
                "UPDATE company_employer_master SET in_target_population=TRUE,updated_at=now() "
                "WHERE company_id=ANY(%s)", (list(company_ids),))
            if cur.rowcount != int(expected):
                raise ValueError(
                    f"target population activated {cur.rowcount} rows; expected {expected}")
        finally:
            cur.close()
    return int(expected)


def list_stored_identity_batch(*, limit: int = 500, after_company_id: int = 0,
                               retry_incomplete: bool = False) -> list[dict]:
    statuses = ["pending", "incomplete"] if retry_incomplete else ["pending"]
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.brand_name,m.employee_count,m.employee_count_min,
            m.employee_count_max,m.employee_size_source,m.industry,m.headquarters,
            m.headquarters_country,m.employer_segment,m.brand_identity,
            m.qualification_evidence,
            c.legal_name,c.trade_name,c.naics,c.states,c.source,c.source_external_id,
            c.metadata,c.provenance,c.source_url,c.source_observed_at
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.company_id>%s
            AND m.identity_enrichment_status=ANY(%s)
          ORDER BY m.company_id LIMIT %s
        """, (max(0, int(after_company_id)), statuses, max(1, int(limit))))
        return [dict(row) for row in cur.fetchall()]


def update_stored_identities(rows: list[dict]) -> int:
    if not rows:
        return 0
    values = [(
        row.get("brand_name"), row.get("employee_count"),
        row.get("employee_count_min"), row.get("employee_count_max"),
        row.get("employee_size_source"), row.get("industry"), row.get("naics_code"),
        row.get("headquarters"), row.get("headquarters_country"),
        row.get("headquarters_address_type"), row.get("employer_segment"),
        Json(row.get("brand_identity") or {}) if Json is not None else row.get("brand_identity") or {},
        row["status"],
        Json(row.get("provenance") or {}) if Json is not None else row.get("provenance") or {},
        Json(row.get("gaps") or {}) if Json is not None else row.get("gaps") or {},
        int(row["company_id"]),
    ) for row in rows]
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_employer_master SET
            brand_name=COALESCE(%s,brand_name),employee_count=COALESCE(%s,employee_count),
            employee_count_min=COALESCE(%s,employee_count_min),
            employee_count_max=COALESCE(%s,employee_count_max),
            employee_size_source=COALESCE(%s,employee_size_source),
            industry=COALESCE(%s,industry),naics_code=COALESCE(%s,naics_code),
            headquarters=COALESCE(%s,headquarters),
            headquarters_country=COALESCE(%s,headquarters_country),
            headquarters_address_type=COALESCE(%s,headquarters_address_type),
            employer_segment=COALESCE(%s,employer_segment),
            brand_identity=brand_identity || %s,
            identity_enrichment_status=%s,
            identity_enrichment_provenance=%s,
            identity_enrichment_gaps=%s,identity_enriched_at=now(),updated_at=now()
          WHERE company_id=%s AND in_target_population
        """, values)
        return cur.rowcount


def list_candidates(*, limit: int = 2000, offset: int = 0) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.legal_name,c.trade_name,c.canonical_name,c.domain,c.states,
                 c.source,c.source_external_id,c.metadata AS company_metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.identity_status IN ('candidate','quarantined')
          ORDER BY m.mandatory_seed DESC,
                   COALESCE((c.metadata->>'hiring_sites')::integer,0) DESC,m.company_id
          LIMIT %s OFFSET %s
        """, (max(1, int(limit)), max(0, int(offset))))
        return [dict(row) for row in cur.fetchall()]


def list_structured_search_candidates(*, limit: int = 2000) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.legal_name,c.trade_name,c.canonical_name,c.domain,c.states,
                 c.source,c.source_external_id,c.metadata AS company_metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.identity_status IN ('candidate','quarantined')
            AND NOT (m.qualification_evidence ? 'wikidata_entity')
          ORDER BY m.mandatory_seed DESC,
                   COALESCE((c.metadata->>'hiring_sites')::integer,0) DESC,m.company_id
          LIMIT %s
        """, (max(1, int(limit)),))
        return [dict(row) for row in cur.fetchall()]


def list_registry_candidates(*, limit: int = 2000) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.legal_name,c.trade_name,c.canonical_name,c.states,c.metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.identity_status IN ('candidate','quarantined')
            AND NULLIF(m.headquarters,'') IS NULL
            AND NOT (m.qualification_evidence ? 'gleif_entity')
          ORDER BY m.mandatory_seed DESC,
                   COALESCE((c.metadata->>'hiring_sites')::integer,0) DESC,m.company_id
          LIMIT %s
        """, (max(1, int(limit)),))
        return [dict(row) for row in cur.fetchall()]


def update_registry_evidence(rows: list[dict]) -> int:
    if not rows:
        return 0
    values = [(
        row.get("headquarters") or None, row.get("headquarters_country") or None,
        float(row.get("identity_confidence") or 0),
        Json(row.get("qualification_evidence") or {}) if Json is not None
        else (row.get("qualification_evidence") or {}), int(row["company_id"]),
    ) for row in rows]
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_employer_master SET
            headquarters=COALESCE(%s,headquarters),
            headquarters_country=COALESCE(%s,headquarters_country),
            identity_confidence=GREATEST(identity_confidence,%s),
            qualification_evidence=qualification_evidence || %s,updated_at=now()
          WHERE company_id=%s AND in_target_population
        """, values)
        return cur.rowcount


def update_structured_evidence(rows: list[dict]) -> int:
    if not rows:
        return 0
    values = []
    for row in rows:
        evidence = row.get("domain_evidence") or []
        values.append((
            row.get("candidate_domain") or None, row.get("employee_count"),
            row.get("employee_count_min"), row.get("employee_count_max"),
            row.get("employee_size_source") or None, row.get("industry") or None,
            row.get("headquarters") or None, row.get("linkedin_url") or None,
            float(row.get("identity_confidence") or 0),
            Json(evidence) if Json is not None else evidence,
            Json(row.get("qualification_evidence") or {}) if Json is not None
            else (row.get("qualification_evidence") or {}),
            int(row["company_id"]),
        ))
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_employer_master SET
            candidate_domain=COALESCE(%s,candidate_domain),
            employee_count=COALESCE(%s,employee_count),
            employee_count_min=COALESCE(%s,employee_count_min),
            employee_count_max=COALESCE(%s,employee_count_max),
            employee_size_source=COALESCE(%s,employee_size_source),
            industry=COALESCE(%s,industry),headquarters=COALESCE(%s,headquarters),
            linkedin_url=COALESCE(%s,linkedin_url),
            identity_confidence=GREATEST(identity_confidence,%s),
            domain_evidence=domain_evidence || %s,
            qualification_evidence=qualification_evidence || %s,updated_at=now()
          WHERE company_id=%s AND in_target_population
        """, values)
        return cur.rowcount


def clear_unqualified_exact_employee_counts() -> int:
    """Drop prior undated structured counts while preserving workforce ranges."""
    with company_db._cur(False) as cur:
        cur.execute("""
          UPDATE company_employer_master SET employee_count=NULL,
            employee_size_source=CASE WHEN employee_count_min IS NOT NULL
              THEN 'E-Verify workforce range' ELSE NULL END,updated_at=now()
          WHERE in_target_population AND identity_status='candidate'
        """)
        return cur.rowcount


def reset_structured_enrichment() -> int:
    """Rebuild generated structured evidence without touching verified identities."""
    with company_db._cur(False) as cur:
        cur.execute("""
          UPDATE company_employer_master AS m SET
            candidate_domain=CASE WHEN m.mandatory_seed THEN NULLIF(c.domain,'') ELSE NULL END,
            employee_count=NULL,
            employee_size_source=CASE WHEN employee_count_min IS NOT NULL
              THEN 'E-Verify workforce range' ELSE NULL END,
            industry=NULL,headquarters=NULL,linkedin_url=NULL,identity_confidence=0,
            domain_evidence='[]'::jsonb,
            qualification_evidence=qualification_evidence-'wikidata_entity'-'structured_name_match'
              -'domain_search_attempt',
            updated_at=now()
          FROM company_discovery AS c
          WHERE c.id=m.company_id AND m.in_target_population
            AND m.identity_status='candidate' AND NOT m.domain_verified
        """)
        return cur.rowcount


def list_domain_candidates(*, limit: int = 2000) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.legal_name,c.trade_name,c.canonical_name,c.source,c.metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND NOT m.domain_verified
            AND NULLIF(m.candidate_domain,'') IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(m.domain_evidence) evidence
              WHERE evidence->>'class'='structured_corporate_source'
                AND lower(COALESCE(evidence->>'candidate_domain',''))=
                    lower(m.candidate_domain)
            )
          ORDER BY m.mandatory_seed DESC,m.company_id LIMIT %s
        """, (max(1, int(limit)),))
        return [dict(row) for row in cur.fetchall()]


def list_all_verified_domains() -> list[dict]:
    """Return verified identities for evidence-contract auditing."""
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.domain,c.country,c.legal_name,c.trade_name
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.domain_verified
          ORDER BY m.company_id
        """)
        return [dict(row) for row in cur.fetchall()]


def list_search_candidates(*, limit: int = 100, offset: int = 0) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.legal_name,c.trade_name,c.canonical_name,c.source,c.metadata,c.states
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND NOT m.domain_verified
            AND NULLIF(m.candidate_domain,'') IS NULL
            AND NOT (m.qualification_evidence ? 'domain_search_attempt')
          ORDER BY m.mandatory_seed DESC,
                   COALESCE((c.metadata->>'hiring_sites')::integer,0) DESC,m.company_id
          LIMIT %s OFFSET %s
        """, (max(1, int(limit)), max(0, int(offset))))
        return [dict(row) for row in cur.fetchall()]


def list_verified_employers(*, limit: int = 2000) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.*,m.brand_name,m.employee_count,m.employee_count_min,m.mandatory_seed
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.domain_verified
            AND NULLIF(c.domain,'') IS NOT NULL
          ORDER BY m.mandatory_seed DESC,m.company_id LIMIT %s
        """, (max(1, int(limit)),))
        return [dict(row) for row in cur.fetchall()]


def reset_verified_careers() -> int:
    """Clear only the new employer-master career enrichment before a clean rebuild."""
    with company_db._cur(False) as cur:
        cur.execute("""
          UPDATE company_discovery AS c SET careers_url=NULL,ats=NULL,ats_slug=NULL,
            ats_url=NULL,careers_confidence=0,
            provenance=provenance-'web_enrichment',updated_at=now()
          FROM company_employer_master AS m
          WHERE m.company_id=c.id AND m.in_target_population AND m.domain_verified
        """)
        return cur.rowcount


def verified_career_counts() -> dict[str, int]:
    with company_db._cur(False) as cur:
        cur.execute("""
          SELECT COUNT(*),COUNT(*) FILTER (WHERE NULLIF(c.careers_url,'') IS NOT NULL),
            COUNT(*) FILTER (WHERE NULLIF(c.ats,'') IS NOT NULL)
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.domain_verified
        """)
        domains, careers, ats = cur.fetchone()
    return {"verified_domains": domains, "careers": careers, "ats": ats}


def classify_verified_custom_careers() -> int:
    with company_db._cur(False) as cur:
        cur.execute("""
          UPDATE company_discovery AS c SET ats='custom',
            ats_slug=regexp_replace(lower(split_part(split_part(c.careers_url,'://',2),'/',1)),
                                    '[^a-z0-9]+','','g'),
            ats_url=c.careers_url,updated_at=now()
          FROM company_employer_master AS m
          WHERE m.company_id=c.id AND m.in_target_population AND m.domain_verified
            AND NULLIF(c.careers_url,'') IS NOT NULL AND NULLIF(c.ats,'') IS NULL
        """)
        return cur.rowcount


def consolidate_verified_domains() -> int:
    """Choose one monitoring representative for every verified root domain."""
    with company_db._cur(False) as cur:
        cur.execute("""
          WITH ranked AS (
            SELECT m.company_id,
              FIRST_VALUE(m.company_id) OVER (
                PARTITION BY lower(c.domain)
                ORDER BY m.mandatory_seed DESC,
                  COALESCE((c.metadata->>'hiring_sites')::integer,0) DESC,m.company_id
              ) AS representative_id
            FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
            WHERE m.in_target_population AND m.domain_verified
              AND NULLIF(c.domain,'') IS NOT NULL
          )
          UPDATE company_employer_master AS m SET
            canonical_company_id=r.representative_id,
            is_monitoring_representative=(m.company_id=r.representative_id),updated_at=now()
          FROM ranked r WHERE r.company_id=m.company_id
        """)
        return cur.rowcount


def refresh_identity_qualification() -> dict[str, int]:
    """Verify identity only; hiring qualification requires observed job activity."""
    with company_db._cur(False) as cur:
        cur.execute("""
          UPDATE company_employer_master SET identity_status='candidate',
            monitoring_status=CASE WHEN monitoring_status='qualified' THEN 'candidate'
              ELSE monitoring_status END,
            qualification_evidence=qualification_evidence-'identity_gate',updated_at=now()
          WHERE in_target_population AND identity_status='verified'
            AND monitoring_status<>'monitoring'
        """)
        cur.execute("""
          UPDATE company_employer_master AS m SET identity_status='verified',
            qualification_evidence=m.qualification_evidence || jsonb_build_object(
              'identity_gate',jsonb_build_object('complete',true,'evaluated_at',now())),
            updated_at=now()
          FROM company_discovery AS c
          WHERE c.id=m.company_id AND m.in_target_population AND m.domain_verified
            AND m.identity_confidence>=0.95
            AND COALESCE((m.qualification_evidence->>'employee_count_conflict')::boolean,FALSE)=FALSE
            AND NOT (m.entity_risk_flags ?| ARRAY[
              'shell_or_shared_services','fund_or_trust','aggregate_or_sentence_name'])
            AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(m.domain_evidence) evidence
              WHERE evidence->>'class'='structured_corporate_source'
                AND lower(COALESCE(evidence->>'candidate_domain',''))=lower(c.domain)
            )
            AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(m.domain_evidence) evidence
              WHERE evidence->>'class'='official_site_identity'
            )
            AND (m.employee_count IS NOT NULL OR m.employee_count_min IS NOT NULL)
            AND NULLIF(m.industry,'') IS NOT NULL AND NULLIF(m.headquarters,'') IS NOT NULL
            AND NULLIF(m.brand_name,'') IS NOT NULL AND NULLIF(c.careers_url,'') IS NOT NULL
            AND NULLIF(c.ats,'') IS NOT NULL
        """)
        promoted = cur.rowcount
        cur.execute("""
          SELECT COUNT(*) FILTER (WHERE identity_status='verified'),
            COUNT(*) FILTER (WHERE monitoring_status='qualified'),
            COUNT(*) FILTER (WHERE hiring_cohort_status='verified_hiring'),
            COUNT(*) FILTER (WHERE is_monitoring_representative)
          FROM company_employer_master WHERE in_target_population
        """)
        identity_verified, qualified, hiring_verified, representatives = cur.fetchone()
    return {"promoted": promoted, "identity_verified": identity_verified,
            "qualified": qualified, "hiring_verified": hiring_verified,
            "representatives": representatives}


def record_search_attempts(rows: list[tuple[int, dict]]) -> int:
    if not rows:
        return 0
    values = [(Json({"domain_search_attempt": evidence}) if Json is not None
               else {"domain_search_attempt": evidence}, int(company_id))
              for company_id, evidence in rows]
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_employer_master SET
            qualification_evidence=qualification_evidence || %s,updated_at=now()
          WHERE company_id=%s AND in_target_population
        """, values)
        return cur.rowcount


def save_verified_domains(rows: list[dict]) -> int:
    if not rows:
        return 0
    updated = 0
    with company_db.conn() as connection:
        cur = connection.cursor()
        try:
            for row in rows:
                company_id = int(row["company_id"])
                evidence = row.get("domain_evidence") or []
                cur.execute("""
                  UPDATE company_employer_master SET domain_verified=TRUE,
                    identity_confidence=GREATEST(identity_confidence,%s),
                    domain_evidence=domain_evidence || %s,
                    last_verified_at=now(),updated_at=now()
                  WHERE company_id=%s AND in_target_population
                """, (float(row.get("identity_confidence") or 0.9),
                      Json(evidence) if Json is not None else evidence, company_id))
                if cur.rowcount != 1:
                    continue
                updated += 1
                cur.execute("""
                  UPDATE company_discovery SET domain=%s,careers_url=COALESCE(%s,careers_url),
                    ats=COALESCE(%s,ats),ats_slug=COALESCE(%s,ats_slug),
                    ats_url=COALESCE(%s,ats_url),domain_confidence=%s,
                    careers_confidence=GREATEST(COALESCE(careers_confidence,0),COALESCE(%s,0)),
                    provenance=provenance || %s,updated_at=now()
                  WHERE id=%s AND EXISTS (
                    SELECT 1 FROM company_employer_master m
                    WHERE m.company_id=company_discovery.id AND m.in_target_population
                  )
                """, (
                    row["domain"], row.get("careers_url") or None,
                    row.get("ats") or None, row.get("ats_slug") or None,
                    row.get("ats_url") or None, float(row.get("identity_confidence") or 0.9),
                    row.get("careers_confidence"),
                    Json({"employer_identity": row.get("provenance") or {}})
                    if Json is not None else {"employer_identity": row.get("provenance") or {}},
                    company_id,
                ))
        finally:
            cur.close()
    return updated


def quarantine_incompatible_search_domains() -> int:
    """Undo search-only US identities accidentally resolved to foreign ccTLDs."""
    allowed = ["ai", "co", "io", "me", "tv", "us"]
    with company_db.conn() as connection:
        cur = connection.cursor()
        try:
            cur.execute("""
              SELECT m.company_id FROM company_employer_master m
              JOIN company_discovery c ON c.id=m.company_id
              WHERE m.in_target_population AND m.domain_verified AND c.country='US'
                AND m.domain_evidence @> '[{"discovered_via":"public_search"}]'::jsonb
                AND length(split_part(lower(c.domain),'.',array_length(string_to_array(c.domain,'.'),1)))=2
                AND split_part(lower(c.domain),'.',array_length(string_to_array(c.domain,'.'),1))<>ALL(%s)
            """, (allowed,))
            ids = [int(row[0]) for row in cur.fetchall()]
            if not ids:
                return 0
            cur.execute("""
              UPDATE company_employer_master SET domain_verified=FALSE,
                identity_status='candidate',monitoring_status='candidate',
                canonical_company_id=NULL,is_monitoring_representative=FALSE,
                qualification_evidence=(qualification_evidence-'domain_search_attempt') ||
                  '{"domain_rejected":"foreign_country_tld"}'::jsonb,updated_at=now()
              WHERE company_id=ANY(%s)
            """, (ids,))
            cur.execute("""
              UPDATE company_discovery SET domain=NULL,careers_url=NULL,ats=NULL,ats_slug=NULL,
                ats_url=NULL,domain_confidence=0,careers_confidence=0,updated_at=now()
              WHERE id=ANY(%s)
            """, (ids,))
            return len(ids)
        finally:
            cur.close()


def list_search_verified_domains() -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.*,c.domain,c.country,c.legal_name,c.trade_name
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND m.domain_verified
            AND m.domain_evidence @> '[{"discovered_via":"public_search"}]'::jsonb
        """)
        return [dict(row) for row in cur.fetchall()]


def quarantine_domain_ids(ids: list[int], *, reason: str) -> int:
    if not ids:
        return 0
    with company_db.conn() as connection:
        cur = connection.cursor()
        try:
            cur.execute("""
              UPDATE company_employer_master SET domain_verified=FALSE,
                identity_status='candidate',monitoring_status='candidate',
                canonical_company_id=NULL,is_monitoring_representative=FALSE,
                identity_confidence=0,
                domain_evidence=COALESCE((SELECT jsonb_agg(value) FROM
                  jsonb_array_elements(domain_evidence) value
                  WHERE value->>'class'<>'official_site_identity'),'[]'::jsonb),
                qualification_evidence=(qualification_evidence-'domain_search_attempt') ||
                  jsonb_build_object('domain_rejected',%s),updated_at=now()
              WHERE company_id=ANY(%s)
            """, (reason, ids))
            cur.execute("""
              UPDATE company_discovery SET domain=NULL,careers_url=NULL,ats=NULL,ats_slug=NULL,
                ats_url=NULL,domain_confidence=0,careers_confidence=0,updated_at=now()
              WHERE id=ANY(%s)
            """, (ids,))
            return len(ids)
        finally:
            cur.close()


def counts() -> dict[str, int]:
    with company_db._cur(False) as cur:
        cur.execute("""
          SELECT COUNT(*), COUNT(*) FILTER (WHERE mandatory_seed),
                 COUNT(*) FILTER (WHERE employee_count IS NOT NULL OR employee_count_min IS NOT NULL),
                 COUNT(*) FILTER (WHERE NULLIF(industry,'') IS NOT NULL),
                 COUNT(*) FILTER (WHERE NULLIF(headquarters,'') IS NOT NULL),
                 COUNT(*) FILTER (WHERE domain_verified),
                 COUNT(*) FILTER (WHERE identity_status='verified'),
                 COUNT(*) FILTER (WHERE hiring_cohort_status='verified_hiring'),
                 COUNT(*) FILTER (WHERE monitoring_status='monitoring')
          FROM company_employer_master WHERE in_target_population
        """)
        total, mandatory, employees, industries, headquarters, domain_verified, \
            identity_verified, hiring_verified, monitoring = \
            cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM company_employer_master")
        physical_total = cur.fetchone()[0]
    return {"total": total, "active_total": total, "physical_total": physical_total,
            "mandatory": mandatory, "employee_count": employees,
            "industry": industries, "headquarters": headquarters,
            "domain_verified": domain_verified, "identity_verified": identity_verified,
            "hiring_verified": hiring_verified,
            "monitoring": monitoring}
