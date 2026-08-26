import json

import pytest

from backend.tools.employer_identity_orchestrator import (
    IdentityCrosswalk,
    exact_legal_name_key,
    gleif_identity_node,
    run_identity_batches,
)


def _employer(company_id=1, name="Acme Holdings, Inc.", state="CA", **extra):
    return {"company_id": company_id, "legal_name": name, "state": state, **extra}


def _node(provider="sec_edgar", entity_id="sec_cik:0000000001",
          name="Acme Holdings, Inc.", state="CA", domain="acme.com", **extra):
    assertion = [] if not domain else [{
        "provider": provider, "entity_id": entity_id, "domain": domain,
        "url": f"https://{domain}/", "source_field": "website",
        "assertion_type": "authority_reported_website",
        "provenance": {"source_url": "https://authority.test/entity"},
    }]
    return {
        "provider": provider, "entity_id": entity_id,
        "entity_ids": {"primary": entity_id.split(":", 1)[1]},
        "legal_name": name, "aliases": [], "domain_assertions": assertion,
        "attributes": {"state": state},
        "provenance": {
            "provider": provider, "source_url": "https://authority.test/entity",
            "observed_at": "2026-08-26T10:00:00Z",
            "retrieval_method": "fixture",
        },
        **extra,
    }


def test_exact_name_keeps_legal_suffix_identity_significant():
    assert exact_legal_name_key("ACME Holdings, Inc.") == "acme holdings inc"
    assert exact_legal_name_key("Acme Holdings LLC") == "acme holdings llc"
    assert exact_legal_name_key("Acme Holdings, Inc.") != exact_legal_name_key(
        "Acme Holdings LLC")


def test_exact_legal_name_state_and_entity_id_produce_proposed_evidence():
    result = IdentityCrosswalk([_employer()], [_node()]).decide(_employer())
    assert result["decision"] == "proposed"
    identity = result["proposed_identity_assertions"][0]
    assert identity["entity_id"] == "sec_cik:0000000001"
    assert identity["legal_name_match"]["rule"] == "exact_normalized_legal_name"
    assert identity["location_match"] == {"kind": "state", "values": ["CA"]}
    assert result["proposed_domain_assertions"][0]["proposal_state"] == "proposed"
    assert "domain_verified" not in json.dumps(result)


def test_legal_suffix_mismatch_is_no_match_even_with_same_state():
    result = IdentityCrosswalk(
        [_employer(name="Acme Holdings LLC")], [_node()]).decide(
            _employer(name="Acme Holdings LLC"))
    assert result["decision"] == "no_match"
    assert result["proposed_identity_assertions"] == []


@pytest.mark.parametrize(("employer_state", "node_state", "conflict"), [
    ("", "CA", "insufficient_location_corroboration"),
    ("NY", "CA", "location_conflict"),
])
def test_name_without_location_corroboration_is_quarantined(
        employer_state, node_state, conflict):
    employer = _employer(state=employer_state)
    result = IdentityCrosswalk([employer], [_node(state=node_state)]).decide(employer)
    assert result["decision"] == "quarantine"
    assert conflict in {item["type"] for item in result["conflicts"]}


def test_same_name_entities_in_other_states_do_not_poison_unique_state_match():
    employer = _employer(state="CA")
    nodes = [
        _node(entity_id="sec_cik:0000000001", state="CA"),
        _node(entity_id="sec_cik:0000000002", state="NY", domain="other.com"),
    ]
    result = IdentityCrosswalk([employer], nodes).decide(employer)
    assert result["decision"] == "proposed"
    assert [item["entity_id"] for item in result["proposed_identity_assertions"]] == [
        "sec_cik:0000000001"]


def test_multiple_matching_ids_from_one_provider_are_quarantined():
    employer = _employer()
    nodes = [_node(), _node(entity_id="sec_cik:0000000002")]
    result = IdentityCrosswalk([employer], nodes).decide(employer)
    assert result["decision"] == "quarantine"
    assert "ambiguous_provider_identity" in {item["type"] for item in result["conflicts"]}
    assert result["proposed_identity_assertions"] == []


def test_cross_provider_same_root_domain_is_compatible_proposed_evidence():
    employer = _employer()
    nodes = [
        _node(domain="www.acme.com"),
        _node(provider="fdic_bankfind", entity_id="fdic_cert:123",
              domain="investors.acme.com"),
        _node(provider="gleif_lei", entity_id="gleif_lei:12345678901234567890",
              domain=""),
    ]
    result = IdentityCrosswalk([employer], nodes).decide(employer)
    assert result["decision"] == "proposed"
    assert {item["provider"] for item in result["proposed_identity_assertions"]} == {
        "sec_edgar", "fdic_bankfind", "gleif_lei",
    }
    assert len(result["proposed_domain_assertions"]) == 2


def test_incompatible_authority_domains_are_quarantined_with_evidence_retained():
    employer = _employer()
    nodes = [
        _node(domain="acme.com"),
        _node(provider="sam_gov", entity_id="sam_uei:ABCDEF123456",
              domain="different.example"),
    ]
    result = IdentityCrosswalk([employer], nodes).decide(employer)
    assert result["decision"] == "quarantine"
    assert "domain_assertion_conflict" in {item["type"] for item in result["conflicts"]}
    assert len(result["provenance"]["quarantined_identity_evidence"]) == 2
    assert result["proposed_domain_assertions"] == []


def test_unbound_domain_assertion_quarantines_the_name_candidate():
    employer = _employer()
    node = _node()
    node["domain_assertions"][0]["entity_id"] = "sec_cik:9999999999"
    result = IdentityCrosswalk([employer], [node]).decide(employer)
    assert result["decision"] == "quarantine"
    assert "unbound_domain_assertion" in {item["type"] for item in result["conflicts"]}


def test_one_entity_id_cannot_be_proposed_for_two_employer_rows():
    employers = [_employer(company_id=1), _employer(company_id=2)]
    crosswalk = IdentityCrosswalk(employers, [_node()])
    for employer in employers:
        result = crosswalk.decide(employer)
        assert result["decision"] == "quarantine"
        assert "entity_id_shared_by_employers" in {
            item["type"] for item in result["conflicts"]}


def test_gleif_adapter_preserves_lei_and_address_but_asserts_no_domain():
    node = gleif_identity_node({
        "source_external_id": "5493001KJTIIGC8Y1R12",
        "legal_name": "Acme Holdings, Inc.", "trade_name": "Acme",
        "source_url": "https://api.gleif.org/api/v1/lei-records/5493001KJTIIGC8Y1R12",
        "source_observed_at": "2026-08-26T10:00:00Z",
        "metadata": {
            "entity_status": "ACTIVE",
            "legal_address": {"city": "San Francisco", "region": "US-CA",
                              "postalCode": "94105"},
        },
    })
    assert node is not None
    assert node["entity_id"] == "gleif_lei:5493001KJTIIGC8Y1R12"
    assert node["domain_assertions"] == []
    employer = _employer()
    result = IdentityCrosswalk([employer], [node]).decide(employer)
    assert result["decision"] == "proposed"


def test_checkpoint_resumes_exactly_after_completed_batch(tmp_path):
    employers = [_employer(company_id=index) for index in range(1, 6)]
    # Separate state ensures every entity ID remains unique to its employer.
    for index, employer in enumerate(employers, start=1):
        employer["state"] = ["CA", "NY", "TX", "WA", "FL"][index - 1]
    nodes = [
        _node(entity_id=f"sec_cik:{index:010d}", state=employer["state"])
        for index, employer in enumerate(employers, start=1)
    ]
    checkpoint = tmp_path / "identity-checkpoint.json"

    first = run_identity_batches(
        employers, nodes, checkpoint_path=checkpoint, batch_size=2, max_batches=1)
    assert first["next_offset"] == 2
    assert first["completed"] is False
    assert first["stats"]["processed"] == 2

    resumed = run_identity_batches(
        employers, nodes, checkpoint_path=checkpoint, batch_size=2, resume=True)
    assert resumed["completed"] is True
    assert resumed["next_offset"] == 5
    assert resumed["stats"] == {
        "processed": 5, "proposed": 5, "quarantined": 0, "no_match": 0}
    assert len({item["company_id"] for item in resumed["results"]}) == 5
    persisted = json.loads(checkpoint.read_text())
    assert persisted["completed"] is True


def test_checkpoint_rejects_changed_population(tmp_path):
    checkpoint = tmp_path / "identity-checkpoint.json"
    run_identity_batches(
        [_employer()], [_node()], checkpoint_path=checkpoint,
        batch_size=1, max_batches=1)
    with pytest.raises(ValueError, match="employer_fingerprint"):
        run_identity_batches(
            [_employer(name="Changed Legal Name, Inc.")], [_node()],
            checkpoint_path=checkpoint, batch_size=1, resume=True)


def test_explicit_inactive_rows_are_skipped_from_10k_contract():
    state = run_identity_batches([
        _employer(company_id=1),
        _employer(company_id=2, population_active=False),
        _employer(company_id=3, population_status="inactive"),
    ], [_node()], batch_size=2)
    assert state["active_selected"] == 1
    assert state["inactive_skipped"] == 2
    assert state["stats"]["processed"] == 1
