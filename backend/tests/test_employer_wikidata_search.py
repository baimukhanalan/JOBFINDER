"""Strict identity and resume tests for Wikidata entity-search candidates."""
from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from backend.tools import employer_identity_enrichment as enrichment


def claim(value):
    return {"mainsnak": {"datavalue": {"value": value}}}


def row(company_id=1, name="Acme, Inc.", city="Austin"):
    return {
        "company_id": company_id, "brand_name": name, "legal_name": name,
        "trade_name": name, "source": "dol_oflc_lca", "source_external_id": "dol-1",
        "states": ["TX"], "country": "US", "headquarters_country": "US",
        "metadata": {"employer_address": {"city": city, "region": "TX",
                                             "country": "US"}},
    }


class Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {}
        self.payload = payload
        self.request = httpx.Request("GET", "https://www.wikidata.org/w/api.php")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=self.request,
                response=httpx.Response(self.status_code, request=self.request))

    def json(self):
        return self.payload


def exact_entity(*, country=True, location=None, domain="acme.example"):
    claims = {"P856": [claim(f"https://{domain}/jobs")]}
    if country:
        claims["P17"] = [claim({"id": "Q30"})]
    if location:
        claims["P159"] = [claim({"id": location})]
    return {
        "labels": {"en": {"value": "Acme Holdings"}},
        "aliases": {"en": [{"value": "Acme, Inc."}]},
        "claims": claims,
    }


def test_search_hit_requires_exact_normalized_label_or_alias():
    assert enrichment._exact_hit_ids(row(), [
        {"id": "Q1", "label": "Acme Holdings", "match": {"text": "Acme, Inc."}},
        {"id": "Q2", "label": "Acme Consulting"},
    ]) == ["Q1"]


def test_location_guard_accepts_exact_city_when_country_claim_is_missing():
    entity = exact_entity(country=False, location="Q100")
    location = {"labels": {"en": {"value": "Austin"}}, "claims": {}}
    accepted, evidence = enrichment._search_identity_guard(
        row(), entity, {"Q100": location})
    assert accepted is True
    assert evidence["us_country_claim"] is False
    assert evidence["headquarters_location_match"] is True


def test_deprecated_us_claim_does_not_satisfy_identity_guard():
    entity = exact_entity(country=False)
    entity["claims"]["P17"] = [{
        "rank": "deprecated", "mainsnak": {"datavalue": {"value": {"id": "Q30"}}},
    }]
    accepted, evidence = enrichment._search_identity_guard(row(), entity, {})
    assert accepted is False
    assert evidence["us_country_claim"] is False


def test_ip_address_p856_is_not_a_domain_candidate():
    entity = exact_entity(domain="127.0.0.1")
    assert enrichment._official_domain(entity) == ""


def test_search_dry_run_accepts_alias_plus_us_claim_and_never_persists(monkeypatch):
    monkeypatch.setattr(enrichment, "_list_wikidata_search_rows", lambda **_: [row()])

    class Client:
        def get(self, _url, params):
            if params["action"] == "wbsearchentities":
                return Response({"search": [{
                    "id": "Q7", "label": "Acme Holdings",
                    "match": {"text": "Acme, Inc."},
                }]})
            return Response({"entities": {"Q7": exact_entity()}})

    monkeypatch.setattr(
        enrichment, "_persist_wikidata_search_results",
        lambda _values: pytest.fail("dry-run must not persist"))
    result = enrichment.enrich_structured_search(
        limit=1, workers=99, min_interval=0, retries=0,
        dry_run=True, client=Client())
    assert result["workers"] == 4
    assert result["matched"] == 1
    assert result["updated"] == 0
    assert result["proposals"] == [{
        "company_id": 1, "candidate_domain": "acme.example",
        "wikidata_entity": "Q7",
    }]


def test_exact_name_and_p856_without_us_or_location_guard_is_rejected(monkeypatch):
    monkeypatch.setattr(enrichment, "_list_wikidata_search_rows", lambda **_: [row()])

    class Client:
        def get(self, _url, params):
            if params["action"] == "wbsearchentities":
                return Response({"search": [{"id": "Q7", "label": "Acme, Inc."}]})
            return Response({"entities": {"Q7": exact_entity(country=False)}})

    result = enrichment.enrich_structured_search(
        limit=1, min_interval=0, retries=0, dry_run=True, client=Client())
    assert result["matched"] == 0
    assert result["no_match"] == 1
    assert result["proposals"] == []


def test_search_checkpoint_batches_survive_later_interruption(monkeypatch):
    monkeypatch.setattr(
        enrichment, "_list_wikidata_search_rows",
        lambda **_: [row(1, "One LLC"), row(2, "Two LLC")])

    class Client:
        def get(self, _url, params):
            if params["action"] == "wbsearchentities":
                return Response({"search": []})
            return Response({"entities": {}})

    saves = []
    def persist(values):
        saves.append([value["company_id"] for value in values])
        if len(saves) == 2:
            raise KeyboardInterrupt()
        return {"matched": 0, "no_match": 1, "ambiguous": 0,
                "transient": 0, "updated": 1}

    monkeypatch.setattr(enrichment, "_persist_wikidata_search_results", persist)
    with pytest.raises(KeyboardInterrupt):
        enrichment.enrich_structured_search(
            limit=2, checkpoint_size=1, min_interval=0, retries=0, client=Client())
    assert saves[0] == [1]


def test_search_persistence_is_candidate_only(monkeypatch):
    calls = []
    class Cursor:
        rowcount = 1
        def execute(self, sql, params):
            calls.append((sql, params))

    @contextmanager
    def fake_cur(*_args, **_kwargs):
        yield Cursor()

    monkeypatch.setattr(enrichment.company_db, "_cur", fake_cur)
    enriched = {
        "candidate_domain": "acme.example", "identity_confidence": 0.68,
        "domain_evidence": [{"provider": "wikidata_entity_search"}],
        "qualification_evidence": {"wikidata_entity": "Q7"},
    }
    result = enrichment._persist_wikidata_search_results([
        {"company_id": 1, "status": "matched", "enriched": enriched},
        {"company_id": 2, "status": "transient", "reason": "provider"},
    ])
    assert result["matched"] == result["transient"] == 1
    sql = " ".join(statement for statement, _params in calls).casefold()
    assert "candidate_domain" in sql and "domain_evidence" in sql
    assert "wikidata_search_enrichment" in sql
    assert "domain_verified" not in sql
    assert "company_discovery" not in sql


def test_search_retry_loader_selects_only_transient(monkeypatch):
    calls = []
    class Cursor:
        def execute(self, sql, params): calls.append((sql, params))
        def fetchall(self): return []

    @contextmanager
    def fake_cur(*_args, **_kwargs): yield Cursor()
    monkeypatch.setattr(enrichment.company_db, "_cur", fake_cur)
    enrichment._list_wikidata_search_rows(limit=5, retry_transient=True)
    assert "wikidata_search_enrichment,status" in calls[0][0]
    assert "='transient'" in calls[0][0]
