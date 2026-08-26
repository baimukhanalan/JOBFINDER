from contextlib import contextmanager
import io
import json
import zipfile

import pytest

from backend.tools import employer_official_crosswalk as crosswalk


def active(company_id=1, name="Acme Holdings, Inc.", state="NY", headquarters="New York, NY"):
    return {"id": company_id, "legal_name": name, "states": [state] if state else [],
            "headquarters": headquarters, "external_ids": {}, "provenance": {}}


def record(entity_id="sec_cik:0000000001", name="Acme Holdings Inc", state="NY",
           address="1 Main St, New York, NY"):
    return crosswalk.official_record(
        provider="sec_edgar", entity_id=entity_id, legal_name=name, state=state,
        address=address, source_url="https://data.sec.gov/submissions.zip",
        source_date="2026-08-25", observed_at="2026-08-26T00:00:00Z")


def test_legal_key_preserves_suffix_and_only_normalizes_representation():
    assert crosswalk.exact_legal_key("ACME Holdings, Inc.") == "acme holdings inc"
    assert crosswalk.exact_legal_key("Acme Holdings LLC") != \
        crosswalk.exact_legal_key("Acme Holdings Inc")
    assert crosswalk.exact_legal_key("A & B Corp") == \
        crosswalk.exact_legal_key("A and B Corp")


def test_exact_name_plus_unique_state_proposes_entity_id():
    result = crosswalk.propose_crosswalk([active()], [record()], provider="sec_edgar")
    assert result["proposed"] == 1
    proposal = result["proposals"][0]
    assert proposal["official_id"] == "0000000001"
    assert proposal["location_method"] == "state"
    assert proposal["matched_states"] == ["NY"]


def test_name_only_and_missing_location_never_match():
    row = active(state="", headquarters="")
    result = crosswalk.propose_crosswalk([row], [record()], provider="sec_edgar")
    assert result["proposed"] == 0
    assert result["reasons"] == {"no_location": 1}


def test_same_name_and_state_ambiguity_is_no_match():
    records = [record("sec_cik:0000000001"), record("sec_cik:0000000002")]
    result = crosswalk.propose_crosswalk([active()], records, provider="sec_edgar")
    assert result["proposed"] == 0
    assert result["reasons"] == {"ambiguous_location": 1}


def test_one_official_id_cannot_be_claimed_by_two_active_rows():
    result = crosswalk.propose_crosswalk(
        [active(1), active(2)], [record()], provider="sec_edgar")
    assert result["proposed"] == 0
    assert result["reasons"] == {"duplicate_active_claim": 2}


def test_different_state_is_location_mismatch():
    result = crosswalk.propose_crosswalk(
        [active(state="CA", headquarters="Los Angeles, CA")], [record()],
        provider="sec_edgar")
    assert result["proposed"] == 0
    assert result["reasons"] == {"location_mismatch": 1}


def test_exact_address_can_uniquely_disambiguate_without_us_state():
    row = active(state="", headquarters="10 King Street London")
    records = [record("sec_cik:0000000001", state="", address="10 King Street, London"),
               record("sec_cik:0000000002", state="", address="20 Queen Street, London")]
    result = crosswalk.propose_crosswalk([row], records, provider="sec_edgar")
    assert result["proposed"] == 1
    assert result["proposals"][0]["location_method"] == "exact_address"


def test_existing_conflicting_identifier_is_no_match():
    row = active()
    row["external_ids"] = {"sec_cik": "0000000999"}
    result = crosswalk.propose_crosswalk([row], [record()], provider="sec_edgar")
    assert result["proposed"] == 0
    assert result["reasons"] == {"existing_id_conflict": 1}


def test_existing_same_identifier_is_idempotently_not_reproposed():
    row = active()
    row["external_ids"] = {"sec_cik": "0000000001"}
    result = crosswalk.propose_crosswalk([row], [record()], provider="sec_edgar")
    assert result["proposed"] == 0
    assert result["reasons"] == {"already_linked": 1}


def test_irs_bmf_parser_and_match_require_exact_name_and_state():
    content = ("EIN,NAME,STREET,CITY,STATE,ZIP\n"
               "123456789,Acme Foundation Inc,1 Main St,Boston,MA,02110\n")
    records = crosswalk.irs_bmf_records(content, source_url="irs-eo1.csv",
                                        observed_at="2026-08-26")
    row = active(name="ACME FOUNDATION, INC.", state="MA", headquarters="Boston, MA")
    result = crosswalk.propose_crosswalk([row], records,
                                         provider="irs_exempt_org_bmf")
    assert result["proposed"] == 1
    assert result["proposals"][0]["entity_id"] == "irs_ein:123456789"


def test_sec_and_fdic_adapters_preserve_official_ids_and_source_date():
    sec = crosswalk.sec_submission_records([{
        "cik": "320193", "name": "Apple Inc.",
        "addresses": {"business": {"city": "Cupertino", "stateOrCountry": "CA"}},
    }], source_url="submissions.zip", observed_at="2026-08-26")
    assert sec[0]["entity_id"] == "sec_cik:0000320193"
    fdic = crosswalk.fdic_records([{
        "entity_id": "fdic_cert:628", "legal_name": "JPMorgan Chase Bank",
        "attributes": {"city": "Columbus", "state": "OH", "zip": "43240",
                       "dataset_timestamp": "2026-08-25"},
        "provenance": {"source_url": "https://api.fdic.gov", "observed_at": "2026-08-26"},
    }])
    assert fdic[0]["official_id"] == "628"
    assert fdic[0]["provenance"]["source_date"] == "2026-08-25"


def test_sec_full_list_zip_parser_is_bounded_and_keeps_business_location():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "ignored")
        archive.writestr("CIK0000320193.json", json.dumps({
            "cik": "320193", "name": "Apple Inc.",
            "addresses": {"business": {"city": "Cupertino", "stateOrCountry": "CA"}},
        }))
    records = crosswalk.sec_submission_zip_records(
        buffer.getvalue(), source_url="https://www.sec.gov/Archives/submissions.zip")
    assert len(records) == 1
    assert records[0]["entity_id"] == "sec_cik:0000320193"
    assert records[0]["states"] == ["CA"]
    with pytest.raises(ValueError, match="entry exceeds"):
        crosswalk.sec_submission_zip_records(
            buffer.getvalue(), source_url="https://www.sec.gov/archive.zip",
            max_entry_bytes=10)


class FakeCursor:
    rowcount = 1

    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchall(self):
        return [self.row]


def test_apply_revalidates_proposal_and_never_touches_domain_verified(monkeypatch):
    proposal = crosswalk.propose_crosswalk([active()], [record()],
                                           provider="sec_edgar")["proposals"][0]
    cursor = FakeCursor((1, "Acme Holdings, Inc.", ["NY"], {}, "New York, NY"))

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr(crosswalk.company_db, "_cur", fake_cur)
    assert crosswalk.apply_proposals([proposal]) == {"selected": 1, "updated": 1}
    update_sql, update_params = cursor.calls[1]
    assert "external_ids=jsonb_set" in update_sql
    assert "domain_verified" not in update_sql
    assert "exact_legal_name_and_unique_location" in update_params[2]


def test_apply_rejects_non_proposal_without_opening_db(monkeypatch):
    monkeypatch.setattr(crosswalk.company_db, "_cur",
                        lambda *_args: pytest.fail("database must not be opened"))
    with pytest.raises(ValueError, match="only valid proposed"):
        crosswalk.apply_proposals([{"status": "no_match", "provider": "sec_edgar"}])
