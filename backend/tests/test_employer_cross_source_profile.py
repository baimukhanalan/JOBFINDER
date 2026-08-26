import inspect

import pytest

from backend.tools import employer_cross_source_profile as profile


def _dol(name="Acme Inc.", state="TX", city="Austin", postal="78701"):
    return {"company_id": 1, "source": "dol_oflc_lca", "legal_name": name,
            "states": [state], "metadata": {"employer_address": {
                "city": city, "region": state, "postal_code": postal}}, "current": {}}


def _everify(name="ACME INC", states=None, brand="Acme Brand"):
    return {"company_id": 2, "source": "everify_large_employer",
            "source_external_id": "ev-1", "source_url": "https://source.test/ev",
            "legal_name": name, "trade_name": brand, "states": states or ["TX", "CA"],
            "metadata": {"brand_name": brand, "employee_count_min": 10000,
                         "workforce_range": "10,000 and over"}}


def test_exact_name_and_state_transfer_only_source_supported_fields():
    plan = profile.build_plan_from_rows([_dol()], [_everify()])
    assert plan["matched"] == 1
    fields = plan["matches"][0]["fields"]
    assert fields["employee_count_min"]["value"] == 10000
    assert fields["brand_name"]["value"] == "Acme Brand"
    assert "headquarters" not in fields
    evidence = fields["employee_count_min"]
    assert evidence["confidence"] == 0.90
    assert evidence["provenance"]["match"]["shared_states"] == ["TX"]


def test_suffix_variant_state_mismatch_and_city_conflict_do_not_match():
    assert profile.build_plan_from_rows(
        [_dol(name="Acme LLC")], [_everify(name="Acme Inc.")])["matched"] == 0
    state = profile.build_plan_from_rows([_dol()], [_everify(states=["CA"])])
    assert state["matched"] == 0
    assert state["diagnostics"]["state_not_independently_agreed"] == 1
    wikidata = {**_everify(), "source": "wikidata_employer", "states": ["TX"],
                "metadata": {"headquarters": "Dallas", "employee_count": 5000},
                "industry": "technology"}
    city = profile.build_plan_from_rows([_dol()], [wikidata])
    assert city["matched"] == 0
    assert city["diagnostics"]["city_conflict"] == 1


def test_wikidata_direct_fields_transfer_only_with_location_agreement():
    source = {**_everify(), "source": "wikidata_employer", "states": ["TX"],
              "source_external_id": "Q42", "source_url": "https://wikidata.test/Q42",
              "metadata": {"headquarters": "Austin", "employee_count": 5000},
              "industry": "technology"}
    plan = profile.build_plan_from_rows([_dol()], [source])
    fields = plan["matches"][0]["fields"]
    assert fields["employee_count"]["provenance"]["source_field"] == "P1128"
    assert fields["industry"]["provenance"]["source_field"] == "P452"
    assert fields["headquarters"]["value"] == "Austin"
    assert fields["headquarters_address_type"]["value"] == \
        "wikidata_P159_headquarters_location"


def test_stored_profile_requires_explicit_wikidata_field_provenance():
    source = _everify()
    source["stored_profile"] = {
        "employee_count": 25000, "employee_size_source": "wikidata:P1128",
        "industry": "manufacturing", "headquarters": "Austin",
        "headquarters_address_type": "operational",
        "qualification_evidence": {
            "wikidata_entity": "Q123", "structured_name_match": True},
        "identity_enrichment_provenance": {"field_sources": {
            "industry": "company_discovery.industry_or_stored_structured_evidence",
            "headquarters": "qualification_evidence.wikidata_entity/P159"}},
    }
    plan = profile.build_plan_from_rows([_dol()], [source])
    fields = plan["matches"][0]["fields"]
    assert fields["employee_count"]["provenance"]["provider"] == "wikidata"
    assert fields["employee_count"]["provenance"]["source_external_id"] == "Q123"
    assert fields["industry"]["value"] == "manufacturing"
    assert fields["headquarters"]["value"] == "Austin"
    assert fields["headquarters_address_type"]["value"] == \
        "wikidata_P159_headquarters_location"

    unproven = _everify()
    unproven["stored_profile"] = {**source["stored_profile"],
                                  "qualification_evidence": {}}
    unsafe_fields = profile.build_plan_from_rows([_dol()], [unproven])[
        "matches"][0]["fields"]
    assert "employee_count" not in unsafe_fields
    assert "industry" not in unsafe_fields
    assert "headquarters" not in unsafe_fields


def test_address_type_and_size_source_cannot_label_conflicting_existing_values():
    source = _everify()
    source["stored_profile"] = {
        "headquarters": "Dallas", "headquarters_address_type": "operational",
        "qualification_evidence": {
            "wikidata_entity": "Q123", "structured_name_match": True},
        "identity_enrichment_provenance": {"field_sources": {
            "headquarters": "qualification_evidence.wikidata_entity/P159"}},
    }
    target = _dol()
    target["current"] = {"headquarters": "Austin", "employee_count_min": 5000}
    fields = profile.build_plan_from_rows([target], [source])["matches"][0]["fields"]
    assert "headquarters_address_type" not in fields
    assert "employee_size_source" not in fields


def test_explicit_brand_can_replace_only_legal_name_fallback():
    target = _dol()
    target["current"] = {"brand_name": "Acme Inc."}
    brand = profile.build_plan_from_rows([target], [_everify()])[
        "matches"][0]["fields"]["brand_name"]
    assert brand["value"] == "Acme Brand"
    assert brand["replace_legal_name_fallback"] is True

    target["current"] = {"brand_name": "Existing Real Brand"}
    fields = profile.build_plan_from_rows([target], [_everify()])["matches"][0]["fields"]
    assert "brand_name" not in fields


def test_existing_values_are_not_overwritten_and_fingerprint_is_stable():
    target = _dol()
    target["current"] = {"employee_count_min": 25000}
    first = profile.build_plan_from_rows([target], [_everify()])
    second = profile.build_plan_from_rows([target], [_everify()])
    assert first["fingerprint"] == second["fingerprint"]
    assert "employee_count_min" not in first["matches"][0]["fields"]


def test_apply_requires_fingerprint_and_revalidates_under_lock():
    with pytest.raises(ValueError, match="expected-fingerprint"):
        profile.apply_plan(expected_fingerprint="")
    source = inspect.getsource(profile.apply_plan)
    assert "_load_rows(cur, for_update=True)" in source
    assert "plan changed; run dry-run again" in source
    assert "AND in_target_population" in source
