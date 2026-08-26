import inspect
from contextlib import contextmanager

from backend.tools import employer_master, employer_master_db


def test_collect_keeps_mandatory_seed_and_deduplicates_domains(monkeypatch):
    mandatory = [{
        "source": "mandatory_employer", "source_external_id": f"m{i}",
        "legal_name": f"Mandatory {i}", "trade_name": f"Mandatory {i}",
        "domain": f"mandatory{i}.test", "metadata": {"mandatory_seed": True},
    } for i in range(15)]
    mandatory[0]["country"] = "FR"  # curated seed may be a global parent with US operations
    structured = [{
        "source": "everify_large_employer", "source_external_id": f"Q{i}",
        "legal_name": f"Employer {i}", "trade_name": f"Employer {i}",
        "domain": f"employer{i}.test", "metadata": {"employee_count": 1000-i},
    } for i in range(30)]
    structured.append({**structured[0], "source_external_id": "Qduplicate"})
    for row in structured[1:10]:
        row["domain"] = ""
    saved = []
    synced = []
    monkeypatch.setattr(employer_master, "fetch_employer_reservoir",
                        lambda **_: mandatory + structured)
    monkeypatch.setattr(employer_master.company_db, "ensure_schema", lambda: None)
    monkeypatch.setattr(employer_master.master_db, "ensure_schema", lambda: None)
    activated = []
    monkeypatch.setattr(employer_master.company_db, "upsert_records",
                        lambda rows: saved.extend(rows) or len(rows))
    monkeypatch.setattr(employer_master.master_db, "sync_source",
                        lambda source, rows: synced.append((source, list(rows))) or len(rows))
    monkeypatch.setattr(employer_master.master_db, "set_target_population",
                        lambda rows, expected: activated.append((list(rows), expected)) or expected)
    monkeypatch.setattr(employer_master.master_db, "counts", lambda: {"total": 20})
    monkeypatch.setattr(employer_master, "refresh_segments",
                        lambda: {"updated": 20, "general": 20})
    result = employer_master.collect(limit=20, source_limit=30, min_employees=500)
    assert result["selected"] == 20
    assert len(saved) == 20
    assert sum(row["source"] == "mandatory_employer" for row in saved) == 15
    nonempty_domains = [row["domain"] for row in saved if row["domain"]]
    assert len(nonempty_domains) == len(set(nonempty_domains))
    assert len({row["trade_name"] for row in saved}) == 20
    assert [source for source, _ in synced] == ["mandatory_employer", "everify_large_employer"]
    assert len(activated[0][0]) == activated[0][1] == 20
    assert result["segments"]["updated"] == 20


def test_strict_selection_requires_15k_reservoir_and_keeps_all_mandatory():
    mandatory = [{
        "source": "mandatory_employer", "source_external_id": f"m{i}",
        "legal_name": f"Mandatory {i}", "trade_name": f"Mandatory {i}",
        "domain": f"mandatory{i}.test", "country": "US",
        "metadata": {"mandatory_seed": True, "risk_flags": []},
    } for i in range(15)]
    candidates = [{
        "source": "gleif_lei", "source_external_id": f"lei{i}",
        "legal_name": f"Candidate {i}", "trade_name": f"Candidate {i}",
        "domain": "", "country": "US",
        "metadata": {"source_class": "authoritative_registry",
                     "employer_evidence": "legal_identity_only",
                     "employer_segment": "general", "risk_flags": []},
    } for i in range(15000)]
    mandatory[0]["legal_name"] = mandatory[0]["trade_name"] = \
        "Mandatory Family Revocable Trust"
    candidates[10]["legal_name"] = candidates[10]["trade_name"] = \
        "AAA Family Revocable Trust"
    candidates[10]["metadata"]["risk_flags"] = []
    selected, stats = employer_master.select_employers(
        mandatory + candidates, limit=10000, reservoir_min=15000)
    assert len(selected) == 10000
    assert sum(row["source"] == "mandatory_employer" for row in selected) == 15
    assert stats["reservoir_candidates"] == 15015
    assert stats["risk_excluded"] == 1
    assert stats["hard_quarantine_excluded"] == 1
    assert stats["hard_quarantine_rules"] == {"personal_or_family_trust": 1}
    assert stats["mandatory_quarantine_overrides"] == 1
    assert "lei10" not in {row["source_external_id"] for row in selected}
    assert stats["verification_required"] >= 9985
    assert stats["hiring_gate_accepted"] == 0
    assert stats["employer_evidence"]["candidate"] == 10000
    assert len({row["metadata"]["master_selection"]["dedup_key"]
                for row in selected}) == 10000


def test_review_lane_is_not_excluded_and_next_eligible_replaces_hard_quarantine():
    mandatory = [{
        "source": "mandatory_employer", "source_external_id": f"m{i}",
        "legal_name": f"Mandatory {i}", "trade_name": f"Mandatory {i}",
        "domain": f"mandatory{i}.test", "country": "US", "metadata": {},
    } for i in range(15)]
    candidates = [
        {"source": "gleif_lei", "source_external_id": "hard",
         "legal_name": "AAA Family Revocable Trust",
         "trade_name": "AAA Family Revocable Trust", "country": "US",
         "metadata": {}},
        {"source": "gleif_lei", "source_external_id": "review",
         "legal_name": "AAB Community Fund LLC", "trade_name": "AAB Community Fund LLC",
         "country": "US", "metadata": {}},
        {"source": "gleif_lei", "source_external_id": "replacement",
         "legal_name": "AAC Operating Company LLC",
         "trade_name": "AAC Operating Company LLC", "country": "US", "metadata": {}},
    ]
    selected, stats = employer_master.select_employers(
        mandatory + candidates, limit=17, reservoir_min=18)
    selected_ids = {row["source_external_id"] for row in selected}
    assert len(selected) == 17
    assert "hard" not in selected_ids
    assert {"review", "replacement"} <= selected_ids
    review = next(row for row in selected if row["source_external_id"] == "review")
    assert review["metadata"]["master_selection"]["population_quality_lane"] == "review"
    assert stats["hard_quarantine_excluded"] == 1


def test_stored_reconcile_dry_run_then_apply_preserves_exact_population(monkeypatch):
    mandatory = [{
        "source": "mandatory_employer", "source_external_id": f"m{i}",
        "legal_name": f"Mandatory {i}", "trade_name": f"Mandatory {i}",
        "domain": f"mandatory{i}.test", "country": "US", "metadata": {},
    } for i in range(15)]
    reservoir = mandatory + [
        {"source": "gleif_lei", "source_external_id": "hard",
         "legal_name": "AAA Family Revocable Trust",
         "trade_name": "AAA Family Revocable Trust", "country": "US",
         "metadata": {}},
        {"source": "gleif_lei", "source_external_id": "replacement",
         "legal_name": "AAB Employer LLC", "trade_name": "AAB Employer LLC",
         "country": "US", "metadata": {}},
        {"source": "gleif_lei", "source_external_id": "reserve",
         "legal_name": "AAC Reserve LLC", "trade_name": "AAC Reserve LLC",
         "country": "US", "metadata": {}},
    ]
    current = {("mandatory_employer", f"m{i}") for i in range(15)} | {
        ("gleif_lei", "hard")}
    monkeypatch.setattr(employer_master, "load_stored_reservoir", lambda: reservoir)
    monkeypatch.setattr(employer_master, "_active_source_identities", lambda: current)
    writes = []
    monkeypatch.setattr(employer_master.company_db, "upsert_records",
                        lambda rows: writes.append(("upsert", len(rows))) or len(rows))
    monkeypatch.setattr(employer_master.master_db, "sync_source",
                        lambda source, rows: writes.append(("sync", source, len(rows))) or len(rows))
    monkeypatch.setattr(employer_master.master_db, "set_target_population",
                        lambda rows, expected: writes.append(("activate", len(rows), expected)) or expected)
    monkeypatch.setattr(employer_master, "refresh_segments",
                        lambda: {"updated": 16})
    monkeypatch.setattr(employer_master.master_db, "counts", lambda: {"total": 18})

    dry_run = employer_master.reconcile_stored_population(
        limit=16, reservoir_min=18)
    assert dry_run["applied"] is False
    assert dry_run["selected"] == 16
    assert dry_run["added_source_ids"] == [["gleif_lei", "replacement"]]
    assert dry_run["removed_source_ids"] == [["gleif_lei", "hard"]]
    assert writes == []

    applied = employer_master.reconcile_stored_population(
        limit=16, reservoir_min=18, apply=True)
    assert applied["applied"] is True
    assert applied["activated"] == 16
    assert ("activate", 16, 16) in writes
    assert "DELETE" not in inspect.getsource(employer_master.reconcile_stored_population)


def test_stats_distinguish_domain_and_full_identity_verification():
    source = inspect.getsource(employer_master_db.counts)
    assert '"domain_verified"' in source
    assert '"identity_verified"' in source


def test_reconcile_can_shrink_active_cohort_without_deleting_history(monkeypatch):
    mandatory = [{
        "source": "mandatory_employer", "source_external_id": f"m{i}",
        "legal_name": f"Mandatory {i}", "trade_name": f"Mandatory {i}",
        "domain": f"mandatory{i}.test", "country": "US", "metadata": {},
    } for i in range(15)]
    reserve = mandatory + [{
        "source": "wikidata_employer", "source_external_id": f"q{i}",
        "legal_name": f"Employer {i}", "trade_name": f"Employer {i}",
        "country": "US", "metadata": {"employee_count": 10000 + i},
    } for i in range(25)]
    current = {(row["source"], row["source_external_id"]) for row in reserve}
    monkeypatch.setattr(employer_master, "load_stored_reservoir", lambda: reserve)
    monkeypatch.setattr(employer_master, "_active_source_identities", lambda: current)

    result = employer_master.reconcile_stored_population(limit=20, reservoir_min=40)

    assert result["resized"] is True
    assert result["selected"] == 20
    assert result["current_active"] == 40
    assert result["removed"] == 20
    assert result["mandatory"] == 15


def test_population_switch_preserves_history_and_requires_exact_active_count(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements = []
            self.rows = []
            self.rowcount = 0

        def execute(self, sql, params=None):
            self.statements.append(sql)
            if sql.startswith("SELECT id"):
                self.rows = [(1,), (2,)]
            elif "in_target_population=TRUE" in sql:
                self.rowcount = 2

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(employer_master_db.company_db, "conn", connection)
    records = [
        {"source": "source", "source_external_id": "one"},
        {"source": "source", "source_external_id": "two"},
    ]
    assert employer_master_db.set_target_population(records, expected=2) == 2
    assert not any("DELETE FROM" in sql for sql in cursor.statements)
    assert next(i for i, sql in enumerate(cursor.statements)
                if "in_target_population=FALSE" in sql) < next(
                    i for i, sql in enumerate(cursor.statements)
                    if "in_target_population=TRUE" in sql)


def test_all_master_candidate_lists_scope_active_population():
    for function in (
        employer_master_db.list_candidates,
        employer_master_db.list_structured_search_candidates,
        employer_master_db.list_registry_candidates,
        employer_master_db.list_domain_candidates,
        employer_master_db.list_search_candidates,
        employer_master_db.list_verified_employers,
    ):
        assert "in_target_population" in inspect.getsource(function)
