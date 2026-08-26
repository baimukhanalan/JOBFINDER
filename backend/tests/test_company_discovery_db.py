from contextlib import contextmanager

import pytest

from backend.tools import company_discovery_db as db


def test_normalizes_company_names_without_overstripping():
    assert db.normalize_company_name("The Acme, Inc.") == "acme"
    assert db.normalize_company_name("Müller & Söhne LLC") == "muller and sohne"
    assert db.normalize_company_name("Company Builders") == "company builders"
    assert db.normalize_company_name("UnitedHealth Group") == "unitedhealth group"
    assert db.normalize_company_name("CVS Health") == "cvs health"


def test_normalizes_domains_and_ats_identity():
    assert db.normalize_domain("https://careers.example.com/jobs/1") == "example.com"
    assert db.normalize_domain("jobs.example.co.uk") == "example.co.uk"
    assert db._catalog_url_identity("https://boards.greenhouse.io/Acme/jobs/123") == (
        "", "greenhouse", "acme")
    assert db._catalog_url_identity("https://jobs.acme.com/opening") == (
        "acme.com", "", "")
    assert db.normalize_ats_slug("icims", "Careers-Acme") == "careers-acme"
    assert db.normalize_ats_slug("eightfold", "app:Acme.com") == "app:acme.com"


@pytest.mark.parametrize(
    ("incoming", "known", "reason"),
    [
        ({"legal_name": "Acme Holdings", "external_ids": {"sec_cik": "123"}},
         {"company": "Different", "external_ids": {"sec_cik": "123"}}, "external_id"),
        ({"legal_name": "Acme Holdings", "domain": "www.acme.com"},
         {"company": "Different", "domain": "https://acme.com"}, "domain"),
        ({"legal_name": "Acme Holdings", "ats": "Greenhouse", "ats_slug": "acme-inc"},
         {"company": "Different", "ats": "greenhouse", "company_key": "acme_inc"},
         "ats_slug"),
    ],
)
def test_strong_identity_is_known(incoming, known, reason):
    result = db.classify_record(incoming, [known])
    assert result["status"] == "known"
    assert result["match_reason"] == reason


def test_name_only_match_requires_review():
    result = db.classify_record(
        {"legal_name": "The Acme Corporation"},
        [{"company": "ACME, Inc.", "company_key": "acme"}],
    )
    assert result == {"status": "possible_duplicate", "match_reason": "name_exact",
                      "matched_catalog_company_key": "acme"}


def test_ats_identity_can_be_derived_from_discovered_url():
    result = db.classify_record(
        {"legal_name": "Acme", "ats_url": "https://boards.greenhouse.io/acme/jobs/99"},
        [{"company": "Acme Jobs", "ats": "greenhouse", "company_key": "acme"}],
    )
    assert result["status"] == "known"
    assert result["match_reason"] == "ats_slug"


def test_generic_or_short_names_do_not_fuzzy_match():
    assert db.classify_record(
        {"legal_name": "Global Services"}, [{"company": "Global Solutions"}]
    )["status"] == "novel"
    assert db.classify_record(
        {"legal_name": "ABC"}, [{"company": "ABC Group"}]
    )["status"] == "novel"  # "Group" is part of the brand, not a removable legal suffix


def test_unrelated_company_is_novel():
    result = db.classify_record(
        {"legal_name": "Northwind Traders", "domain": "northwind.example"},
        [{"company": "Contoso", "domain": "contoso.example"}],
    )
    assert result["status"] == "novel"
    assert result["match_reason"] is None


def test_prepare_record_validates_and_normalizes():
    row = db.prepare_record({
        "source": " SEC ", "source_external_id": 123,
        "legal_name": "The Acme, Inc.", "domain": "https://www.acme.com/about",
        "ats": "Greenhouse.io", "ats_slug": "Acme-Inc", "external_ids": {"SEC_CIK": 42},
    })
    assert row["source"] == "sec"
    assert row["source_external_id"] == "123"
    assert row["canonical_name"] == "acme"
    assert row["domain"] == "acme.com"
    assert row["ats"] == "greenhouse"
    assert row["ats_slug"] == "acmeinc"
    assert row["external_ids"] == {"sec_cik": "42"}
    with pytest.raises(ValueError):
        db.prepare_record({"source": "sec", "legal_name": "Acme"})


def test_prepare_record_preserves_shared_ats_tenant_identity():
    row = db.prepare_record({
        "source": "gleif_lei", "source_external_id": "LEI-1",
        "legal_name": "Acme", "ats": "Eightfold",
        "ats_slug": "app:Acme.com",
        "ats_url": "https://app.eightfold.ai/careers?domain=acme.com",
    })
    assert row["ats"] == "eightfold"
    assert row["ats_slug"] == "app:acme.com"


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 0

    def executemany(self, sql, values):
        self.calls.append((sql, values))
        self.rowcount = len(values)

    def execute(self, sql, values=()):
        self.calls.append((sql, values))

    def fetchall(self):
        return []


def test_schema_migrates_source_provenance_columns(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    db.ensure_schema()
    sql = "\n".join(call[0] for call in cursor.calls)
    assert "ADD COLUMN IF NOT EXISTS source_url" in sql
    assert "ADD COLUMN IF NOT EXISTS source_observed_at" in sql


def test_upsert_uses_source_identity_boundary(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    assert db.upsert_records([{
        "source": "sec", "source_external_id": "0001", "legal_name": "Acme Inc.",
        "metadata": {"form": "10-K"},
    }]) == 1
    sql, values = cursor.calls[0]
    assert "ON CONFLICT (source, source_external_id)" in sql
    assert "last_seen=now()" in sql
    assert len(values) == 1


def test_second_acquisition_upsert_cannot_erase_verified_enrichment(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    identity = {"source": "gleif_lei", "source_external_id": "LEI-1",
                "legal_name": "Acme Inc."}
    db.upsert_records([{**identity,
                        "domain": "acme.example",
                        "careers_url": "https://acme.example/careers",
                        "ats": "workday", "ats_slug": "acme",
                        "ats_url": "https://acme.wd1.myworkdayjobs.com/acme",
                        "domain_confidence": 0.98, "careers_confidence": 0.95,
                        "provenance": {"web_enrichment": {"verified": True}}}])
    db.upsert_records([{**identity,
                        "domain": "", "careers_url": "", "ats": "",
                        "ats_slug": "", "ats_url": "",
                        "domain_confidence": 0.0, "careers_confidence": None,
                        "provenance": {}}])

    sql, values = cursor.calls[1]
    assert "domain=COALESCE(NULLIF(BTRIM(company_discovery.domain),'')" in sql
    assert "careers_url=COALESCE(NULLIF(BTRIM(company_discovery.careers_url),'')" in sql
    assert "ats_url=COALESCE(NULLIF(BTRIM(company_discovery.ats_url),'')" in sql
    assert ("domain_confidence=CASE WHEN company_discovery.domain_confidence>0 "
            "THEN company_discovery.domain_confidence") in sql
    assert ("provenance=COALESCE(company_discovery.provenance,'{}'::jsonb) || "
            "COALESCE(EXCLUDED.provenance,'{}'::jsonb)") in sql
    second = dict(zip(db._UPSERT_COLS, values[0]))
    assert second["domain"] is None
    assert second["ats"] is None
    assert second["ats_slug"] is None
    assert second["domain_confidence"] == 0.0


def test_empty_upsert_does_not_open_database(monkeypatch):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("database was opened"))
    assert db.upsert_records([]) == 0


def test_enrichment_candidates_require_domain_and_are_bounded(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    assert db.list_enrichment_candidates(limit=25) == []
    sql, values = cursor.calls[0]
    assert "domain IS NOT NULL" in sql
    assert "provenance ? 'web_enrichment'" in sql
    assert "ORDER BY id LIMIT %s" in sql
    assert values == (25,)


def test_enrichment_update_only_writes_enrichment_fields(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    updated = db.update_enrichment_results([{
        "id": 7, "careers_url": "https://acme.test/careers",
        "ats": "Lever", "ats_slug": "acme", "ats_url": "https://jobs.lever.co/acme",
        "domain_confidence": 0.9, "careers_confidence": 0.95,
        "provenance": {"web_enrichment": {"result": "ats_found"}},
    }])
    assert updated == 1
    sql, values = cursor.calls[0]
    assert "careers_url=COALESCE" in sql
    assert "provenance=provenance ||" in sql
    assert "domain=" not in sql
    assert values[0][1:4] == ("lever", "acme", "https://jobs.lever.co/acme")
