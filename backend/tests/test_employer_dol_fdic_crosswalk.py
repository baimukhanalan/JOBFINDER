import inspect

import pytest

from backend.tools import employer_dol_fdic_crosswalk as crosswalk


def dol(name="Acme Bank, N.A.", state="TX", city="Austin", postal="78701"):
    return {"company_id": 1, "legal_name": name, "states": [state],
            "metadata": {"employer_address": {"region": state, "city": city,
                                                "postal_code": postal}},
            "external_ids": {}, "current": {}}


def fdic(cert="123", name="ACME BANK N.A.", state="TX", city="Austin",
         postal="78701", domain="acme.bank"):
    factor = ({"entity_id": f"fdic_cert:{cert}", "domain": domain,
               "assertion_type": "institution_reported_primary_website",
               "source_field": "WEBADDR", "provenance": {
                   "source_url": "https://api.fdic.gov/banks/institutions",
                   "observed_at": "2026-08-26T00:00:00Z"}} if domain else None)
    return {"provider": "fdic_bankfind", "entity_id": f"fdic_cert:{cert}",
            "entity_ids": {"fdic_cert": cert, "fed_rssd": "456"},
            "legal_name": name, "domain_assertions": [factor] if factor else [],
            "attributes": {"active": 1, "city": city, "state": state, "zip": postal,
                           "bank_class": "N", "dataset_timestamp": "2026-08-25"},
            "provenance": {"source_url": "https://api.fdic.gov/banks/institutions",
                           "observed_at": "2026-08-26T00:00:00Z"}}


def test_exact_name_and_state_plus_city_or_postal_required():
    plan = crosswalk.build_plan_from_rows([dol()], [fdic()])
    assert plan["matched"] == 1
    assert plan["candidate_domains"] == 1
    item = plan["proposals"][0]
    assert item["external_ids"] == {"fdic_cert": "123", "fed_rssd": "456"}
    assert item["match"]["methods"] == ["state_city", "state_postal"]
    assert item["domain_factor"]["verification_status"] == "candidate_not_verified"

    assert crosswalk.build_plan_from_rows(
        [dol(name="Acme Bank LLC")], [fdic()])["matched"] == 0
    mismatch = crosswalk.build_plan_from_rows(
        [dol(city="Dallas", postal="75201")], [fdic()])
    assert mismatch["matched"] == 0
    assert mismatch["reasons"] == {"location_mismatch": 1}


def test_state_alone_is_not_geographic_corroboration():
    plan = crosswalk.build_plan_from_rows(
        [dol(city="", postal="")], [fdic(city="", postal="")])
    assert plan["matched"] == 0


def test_unique_location_resolves_same_name_and_ambiguous_location_rejects():
    nodes = [fdic("1", city="Austin"), fdic("2", city="Dallas", postal="75201")]
    assert crosswalk.build_plan_from_rows([dol()], nodes)["proposals"][0][
        "entity_id"] == "fdic_cert:1"
    ambiguous = crosswalk.build_plan_from_rows([dol()], [fdic("1"), fdic("2")])
    assert ambiguous["matched"] == 0
    assert ambiguous["reasons"] == {"ambiguous_location": 1}


def test_fdic_main_office_is_typed_and_dol_address_is_never_hq():
    fields = crosswalk.build_plan_from_rows([dol()], [fdic()])["proposals"][0]["fields"]
    assert fields["headquarters"]["value"] == "Austin, TX, 78701"
    assert fields["headquarters_address_type"]["value"] == \
        "fdic_institution_headquarters_location"
    assert fields["headquarters"]["provenance"]["source_field"] == "CITY/STALP/ZIP"
    assert fields["headquarters"]["provenance"]["match"][
        "dol_disclosure_address_role"] == "identity_corroboration_only"


def test_existing_profile_is_not_overwritten_and_domain_conflict_is_skipped():
    target = dol()
    target["current"] = {"candidate_domain": "other.example", "industry": "banking",
                         "headquarters": "Existing HQ"}
    item = crosswalk.build_plan_from_rows([target], [fdic()])["proposals"][0]
    assert item["candidate_domain"] is None
    assert item["domain_factor"] is None
    assert item["fields"] == {}


def test_existing_conflicting_fdic_id_rejects_identity():
    target = dol()
    target["external_ids"] = {"fdic_cert": "999"}
    plan = crosswalk.build_plan_from_rows([target], [fdic()])
    assert plan["matched"] == 0
    assert plan["reasons"] == {"existing_fdic_id_conflict": 1}


def test_fingerprint_ignores_retrieval_time_but_not_source_data():
    first = fdic()
    second = fdic()
    second["provenance"]["observed_at"] = "2026-08-27T00:00:00Z"
    second["domain_assertions"][0]["provenance"]["observed_at"] = \
        "2026-08-27T00:00:00Z"
    assert crosswalk.build_plan_from_rows([dol()], [first])["fingerprint"] == \
        crosswalk.build_plan_from_rows([dol()], [second])["fingerprint"]
    second["attributes"]["zip"] = "78702"
    assert crosswalk.build_plan_from_rows([dol()], [first])["fingerprint"] != \
        crosswalk.build_plan_from_rows([dol()], [second])["fingerprint"]


def test_apply_requires_fingerprint_reloads_locked_active_rows():
    with pytest.raises(ValueError, match="expected-fingerprint"):
        crosswalk.apply_live(expected_fingerprint="")
    source = inspect.getsource(crosswalk.apply_live)
    assert "_load_targets(cur, for_update=True)" in source
    assert "plan changed; run dry-run again" in source
    assert "domain_verified" not in source
    assert "in_target_population" in source
    assert "jsonb_array_elements(domain_evidence)" in source
