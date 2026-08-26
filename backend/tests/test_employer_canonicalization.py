from contextlib import contextmanager

import pytest

from backend.tools.employer_canonicalization import (
    apply_canonicalization,
    build_canonicalization_report,
    load_active_records,
)


def _row(company_id, name, *, source="gleif_lei", external_id=None,
         external_ids=None, address=None, domain="", canonical=None,
         brand=None, metadata=None):
    if external_id is None:
        external_id = f"5493000000000000{company_id:04d}"[-20:]
    snapshot = dict(metadata or {})
    if address:
        snapshot["legal_address"] = address
    return {
        "company_id": company_id, "legal_name": name,
        "brand_name": brand or name, "trade_name": brand or name,
        "source": source, "source_external_id": external_id,
        "external_ids": external_ids or {}, "domain": domain,
        "metadata": {"source_snapshot": snapshot},
        "qualification_evidence": {}, "canonical_company_id": canonical,
        "mandatory_seed": source == "mandatory_employer",
    }


ADDRESS = {"addressLines": ["100 Main Street"], "city": "Austin",
           "region": "US-TX", "postalCode": "78701", "country": "US"}


def test_same_durable_entity_id_is_apply_safe():
    rows = [
        _row(1, "Acme, Inc.", source="gleif_lei",
             external_id="5493001KJTIIGC8Y1R12"),
        _row(2, "Acme Corporation", source="everify_large_employer",
             external_id="hash", external_ids={"lei": "5493001KJTIIGC8Y1R12"}),
    ]
    report = build_canonicalization_report(rows)
    assert report["apply_safe_proposal_count"] == 1
    proposal = report["apply_safe_proposals"][0]
    assert proposal["member_company_ids"] == [1, 2]
    assert proposal["evidence"][0]["rule"] == "same_durable_entity_id"


def test_exact_legal_address_cross_namespace_official_ids_is_apply_safe():
    rows = [
        _row(1, "Acme Bank, Inc.", external_id="5493001KJTIIGC8Y1R12",
             address=ADDRESS),
        _row(2, "ACME BANK, INC.", source="everify_large_employer",
             external_id="hash", external_ids={"fdic_cert": "12345"},
             address={**ADDRESS, "addressLines": ["100 Main St."]}),
    ]
    report = build_canonicalization_report(rows)
    assert report["apply_safe_proposal_count"] == 1
    assert report["apply_safe_proposals"][0]["evidence"][0]["rule"] == \
        "exact_legal_address_with_official_ids"


def test_distinct_same_namespace_ids_block_legal_name_address_merge():
    rows = [
        _row(1, "Acme, Inc.", external_id="5493001KJTIIGC8Y1R12", address=ADDRESS),
        _row(2, "Acme, Inc.", external_id="213800D1EI4B9WTWWD28", address=ADDRESS),
    ]
    report = build_canonicalization_report(rows)
    assert report["apply_safe_proposal_count"] == 0
    conflict = report["review_conflicts"][0]
    assert conflict["reason"] == "distinct_durable_entities_same_name_address"
    assert set(conflict["conflicting_ids"]) == {"lei"}


def test_name_brand_and_domain_never_merge_distinct_subsidiaries():
    rows = [
        _row(1, "Acme East LLC", brand="Acme", domain="acme.com",
             external_id="5493001KJTIIGC8Y1R12"),
        _row(2, "Acme West LLC", brand="Acme", domain="acme.com",
             external_id="213800D1EI4B9WTWWD28"),
    ]
    report = build_canonicalization_report(rows)
    assert report["apply_safe_proposal_count"] == 0
    rules = {item["rule"] for item in report["parent_subsidiary_families"]}
    assert {"shared_brand_variant", "shared_official_domain"} <= rules
    assert all(item["decision"] == "review_distinct_entities"
               for item in report["parent_subsidiary_families"])


def test_exact_and_legal_suffix_variant_clusters_are_recalculated_review_only():
    rows = [_row(1, "Acme, Inc."), _row(2, "ACME, INC."), _row(3, "Acme LLC")]
    report = build_canonicalization_report(rows)
    assert report["exact_duplicate_cluster_count"] == 1
    assert report["variant_duplicate_cluster_count"] == 1
    assert report["apply_safe_proposal_count"] == 0


def test_official_parent_uei_creates_family_not_merge():
    rows = [
        _row(1, "Parent Agency", source="usaspending", external_id="parent",
             external_ids={"sam_uei": "ABCDEF123456"}),
        _row(2, "Child Agency", source="usaspending", external_id="child",
             external_ids={"sam_uei": "ZYXWVU987654"},
             metadata={"parent_uei": "ABCDEF123456", "parent_name": "Parent Agency"}),
    ]
    report = build_canonicalization_report(rows)
    family = next(item for item in report["parent_subsidiary_families"]
                  if item["rule"] == "official_parent_uei")
    assert family["parent_company_id"] == 1
    assert family["child_company_id"] == 2
    assert report["apply_safe_proposal_count"] == 0


def test_existing_canonical_outside_safe_component_demotes_to_review():
    rows = [
        _row(1, "Acme Inc.", external_id="5493001KJTIIGC8Y1R12", canonical=99),
        _row(2, "Acme Corp.", source="everify_large_employer", external_id="hash",
             external_ids={"lei": "5493001KJTIIGC8Y1R12"}),
    ]
    report = build_canonicalization_report(rows)
    assert report["apply_safe_proposal_count"] == 0
    assert report["review_conflicts"][0]["rule"] == "existing_canonical_conflict"


def test_report_is_deterministic():
    rows = [_row(2, "Beta Inc."), _row(1, "Acme Inc.")]
    assert build_canonicalization_report(rows) == \
        build_canonicalization_report(list(reversed(rows)))


def test_apply_requires_expected_fingerprint_and_updates_only_safe_members(monkeypatch):
    report = build_canonicalization_report([
        _row(1, "Acme Inc.", external_id="5493001KJTIIGC8Y1R12"),
        _row(2, "Acme Corp.", source="everify_large_employer", external_id="hash",
             external_ids={"lei": "5493001KJTIIGC8Y1R12"}),
    ])
    with pytest.raises(ValueError, match="fingerprint"):
        apply_canonicalization(report, expected_fingerprint="wrong")

    statements = []
    class Cursor:
        rowcount = 2
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, sql, params): statements.append((sql, params))
    class Connection:
        def cursor(self): return Cursor()
    @contextmanager
    def fake_conn(): yield Connection()
    from backend.tools import company_discovery_db
    monkeypatch.setattr(company_discovery_db, "conn", fake_conn)
    updated = apply_canonicalization(
        report, expected_fingerprint=report["snapshot_fingerprint"])
    assert updated == 2
    assert len(statements) == 1
    assert "DELETE" not in statements[0][0]
    assert "company_id=ANY" in statements[0][0]


def test_loader_sets_transaction_read_only(monkeypatch):
    statements = []
    class Cursor:
        description = [("company_id",), ("legal_name",)]
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, sql): statements.append(" ".join(sql.split()))
        def fetchall(self): return [(1, "Acme Inc.")]
    class Connection:
        def cursor(self): return Cursor()
    @contextmanager
    def fake_conn(): yield Connection()
    from backend.tools import company_discovery_db
    monkeypatch.setattr(company_discovery_db, "conn", fake_conn)
    assert load_active_records() == [{"company_id": 1, "legal_name": "Acme Inc."}]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert statements[1].startswith("SELECT ")
