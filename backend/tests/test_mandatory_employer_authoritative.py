import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from backend.tools import mandatory_employer_authoritative as authoritative


def fixture():
    return authoritative.load_fixture()


def test_fixture_has_exactly_the_required_15_with_fact_level_sources():
    payload = fixture()
    rows = payload["employers"]
    assert len(rows) == 15
    assert {row["key"] for row in rows} == set(authoritative.LEGACY_SOURCE_IDS)
    assert len({row["exact_domain"] for row in rows}) == 15
    assert next(row for row in rows if row["key"] == "teleperformance")[
        "exact_domain"] == "tp.com"
    evidence = authoritative._evidence(rows[0], payload["observed_at"])
    assert evidence["class"] == "authoritative_first_factor"
    assert evidence["evidence_class"] == "authoritative_first_factor"
    assert evidence["assertion"] == "reported_official_domain"


def test_validation_rejects_search_rank_as_evidence():
    payload = fixture()
    payload["employers"][0]["sources"][0]["url"] = \
        "https://www.google.com/search?q=amazon"
    with pytest.raises(ValueError, match="search pages are not evidence"):
        authoritative.validate_fixture(payload)


def test_validation_rejects_unproven_required_fact():
    payload = fixture()
    payload["employers"][0]["sources"][0]["supports"].remove("exact_domain")
    with pytest.raises(ValueError, match="unsupported facts"):
        authoritative.validate_fixture(payload)


def test_audit_exposes_undated_lower_bounds_and_scope_caveats():
    audit = authoritative.audit_records(fixture()["employers"])
    assert audit["data_gaps"] == [{
        "key": "foundever", "field": "employee_count_as_of",
        "reason": "official page publishes no as-of date",
    }]
    assert {item["key"] for item in audit["caveats"]
            if item["reason"] == "source reports a lower bound"} == {
                "cvs_health", "unitedhealth_group", "state_farm",
            }
    assert {item["key"] for item in audit["caveats"]
            if item["field"] == "employee_count_scope"} == {"hilton", "marriott"}


class FakeCursor:
    def __init__(self, missing=None):
        self.missing = missing
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        ids = list(authoritative.LEGACY_SOURCE_IDS.values())
        return [(number, external) for number, external in enumerate(ids, 1)
                if external != self.missing]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, missing=None):
        self.cursor_instance = FakeCursor(missing)

    def cursor(self):
        return self.cursor_instance


def factory_for(connection):
    @contextmanager
    def factory():
        yield connection
    return factory


def test_apply_is_bounded_to_existing_mandatory_seed_and_replaces_own_evidence():
    connection = FakeConnection()
    result = authoritative.apply_fixture(
        fixture(), connection_factory=factory_for(connection))
    assert result["updated"] == 15
    calls = connection.cursor_instance.executed
    assert len(calls) == 31
    assert "source='mandatory_employer'" in calls[0][0]
    discovery_updates = calls[1::2]
    master_updates = calls[2::2]
    assert all("WHERE id=%s AND source='mandatory_employer'" in sql
               for sql, _ in discovery_updates)
    assert all("item->>'provider' <> 'mandatory_authoritative'" in sql
               for sql, _ in master_updates)
    assert all("item->>'provider'='official_site_identity'" in sql
               for sql, _ in master_updates)
    assert all("domain_verified=TRUE" not in sql for sql, _ in master_updates)
    assert any(params[3] == "tp.com" and '"exact_domain": "tp.com"' in params[-3]
               for _, params in discovery_updates)


def test_apply_fails_preflight_before_writes_if_a_seed_is_missing():
    connection = FakeConnection(missing="amazon.com")
    with pytest.raises(RuntimeError, match="amazon.com"):
        authoritative.apply_fixture(
            fixture(), connection_factory=factory_for(connection))
    assert len(connection.cursor_instance.executed) == 1


def test_cli_preview_does_not_open_database(monkeypatch, capsys):
    monkeypatch.setattr(authoritative, "apply_fixture",
                        lambda *_args, **_kwargs: pytest.fail("must not apply"))
    assert authoritative.main(["preview"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["validated"] == 15
    assert output["domains"] == 15
    assert output["dated_employee_counts"] == 14
