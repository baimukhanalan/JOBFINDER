import json
import re
from contextlib import contextmanager

import pytest

from backend.tools.employer_population_quality import (
    classify_population,
    classify_employer_record,
    load_active_population,
    organization_name_key,
    report_summary,
    typography_name_key,
)


def _row(company_id, name, *, brand=None, source="wikidata_employer",
         external_id=None, domain=None, **extra):
    return {
        "company_id": company_id,
        "legal_name": name,
        "brand_name": brand or name,
        "trade_name": brand or name,
        "source": source,
        "source_external_id": external_id or f"id-{company_id}",
        "domain": domain,
        **extra,
    }


def _decision(report, company_id):
    return next(item for item in report["decisions"]
                if item["company_id"] == str(company_id))


def _rules(decision):
    return {item["rule"] for item in decision["evidence"]}


def test_name_keys_distinguish_exact_legal_name_but_group_suffix_variants():
    assert typography_name_key("Acme, Inc.") != typography_name_key("Acme LLC")
    assert organization_name_key("The Acme, Inc.") == organization_name_key("Acme LLC")


def test_single_record_classifier_exposes_hard_lane_without_cluster_context():
    decision = classify_employer_record(_row(1, "Jane Doe Revocable Trust"))
    assert decision["proposed_lane"] == "quarantine"
    assert "personal_or_family_trust" in _rules(decision)


@pytest.mark.parametrize("name,rule", [
    ("Acme Corporation and its subsidiaries", "aggregate_multi_entity_name"),
    ("Acme Corporation & all its operating subsidiaries", "aggregate_multi_entity_name"),
    ("Jane Doe Revocable Trust", "personal_or_family_trust"),
    ("Acme 401(k) Profit Sharing Plan", "benefit_or_retirement_plan"),
    ("Acme 2026-1 Mortgage Loan Trust", "special_purpose_financial_vehicle"),
    ("Acme Opportunity Fund LP", "special_purpose_financial_vehicle"),
])
def test_high_confidence_non_employer_and_aggregate_names_propose_quarantine(name, rule):
    decision = _decision(classify_population([_row(1, name)]), 1)
    assert decision["proposed_lane"] == "quarantine"
    assert rule in _rules(decision)


def test_operating_trust_company_is_not_automatically_quarantined():
    decision = _decision(classify_population([_row(1, "Acme Trust Company Inc.")]), 1)
    assert "fund_or_trust_entity" not in _rules(decision)
    assert decision["proposed_lane"] == "keep"


def test_shell_division_holding_and_source_artifact_are_review_only():
    rows = [
        _row(1, "Acme Payroll Company LP"),
        _row(2, "Acme Division of Services"),
        _row(3, "Acme Holdings, Inc."),
        _row(4, "Acme Operations E-Verify+"),
    ]
    report = classify_population(rows)
    expected = {
        1: "shell_or_payroll_entity", 2: "organizational_unit_name",
        3: "holding_or_management_entity", 4: "source_artifact_in_name",
    }
    for company_id, rule in expected.items():
        decision = _decision(report, company_id)
        assert decision["proposed_lane"] == "review"
        assert rule in _rules(decision)


def test_exact_source_identity_collision_is_quarantine_and_related_ids_are_sorted():
    report = classify_population([
        _row(2, "Acme Two Inc.", external_id="same"),
        _row(1, "Acme One Inc.", external_id="same"),
    ])
    for company_id, related in ((1, ["2"]), (2, ["1"])):
        decision = _decision(report, company_id)
        assert decision["proposed_lane"] == "quarantine"
        evidence = next(item for item in decision["evidence"]
                        if item["rule"] == "duplicate_source_entity_id")
        assert evidence["related_company_ids"] == related


def test_legal_brand_and_domain_clusters_are_reviewable_not_merged():
    report = classify_population([
        _row(1, "Acme, Inc.", brand="Acme", domain="www.acme.com"),
        _row(2, "The Acme LLC", brand="Acme", domain="https://acme.com/jobs"),
    ])
    decision = _decision(report, 1)
    assert decision["proposed_lane"] == "review"
    assert {"legal_variant_cluster", "duplicate_brand_cluster", "shared_domain_group"} \
        <= _rules(decision)
    assert decision["company_id"] == "1"


def test_gleif_and_usaspending_are_reviewed_for_missing_employer_proof():
    report = classify_population([
        _row(1, "Acme Inc.", source="gleif_lei"),
        _row(2, "Beta Inc.", source="usaspending"),
    ])
    assert "legal_identity_only_source" in _rules(_decision(report, 1))
    assert "activity_without_workforce_proof" in _rules(_decision(report, 2))
    assert report["lane_counts"] == {"keep": 0, "review": 2, "quarantine": 0}


def test_canonical_link_and_abnormal_name_require_review():
    long_name = "One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Inc."
    report = classify_population([
        _row(1, long_name, canonical_company_id=9),
    ])
    decision = _decision(report, 1)
    assert decision["proposed_lane"] == "review"
    assert {"existing_canonical_link", "abnormal_name_shape"} <= _rules(decision)


def test_report_is_deterministic_independent_of_input_order_and_never_promotes_state():
    rows = [_row(2, "Beta Inc."), _row(1, "Acme Inc.")]
    first = classify_population(rows)
    second = classify_population(list(reversed(rows)))
    assert first == second
    serialized = json.dumps(first)
    assert "domain_verified" not in serialized
    assert "identity_status" not in serialized
    assert report_summary(first, examples_per_lane=1)["classified_total"] == 2


def test_bounded_contract_rejects_more_than_10k_limit():
    with pytest.raises(ValueError, match="between 1 and 10000"):
        classify_population([], max_records=10_001)


def test_database_loader_explicitly_marks_transaction_read_only(monkeypatch):
    statements = []

    class Cursor:
        description = [("company_id",), ("legal_name",)]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            statements.append(" ".join(statement.split()))

        def fetchall(self):
            return [(1, "Acme Inc.")]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_conn():
        yield Connection()

    from backend.tools import company_discovery_db
    monkeypatch.setattr(company_discovery_db, "conn", fake_conn)
    assert load_active_population() == [{"company_id": 1, "legal_name": "Acme Inc."}]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert statements[1].startswith("SELECT ")
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE)\b", statements[1], re.I)
