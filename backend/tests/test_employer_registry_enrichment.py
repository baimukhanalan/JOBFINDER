from backend.tools.employer_registry_enrichment import _entity_names, _headquarters


def test_registry_entity_parses_exact_name_and_headquarters():
    entity = {
        "legalName": {"name": "Example, Inc."},
        "otherNames": [{"name": "Example"}],
        "headquartersAddress": {"city": "Dallas", "region": "US-TX", "country": "US"},
    }
    assert "example" in _entity_names(entity)
    assert _headquarters(entity) == ("Dallas, TX", "US")
