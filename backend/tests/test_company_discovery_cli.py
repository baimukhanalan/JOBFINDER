import json
import io
import zipfile

from backend.tools import company_discovery as cli


def _record(source="sec_edgar", external_id="1"):
    return {
        "source": source,
        "source_external_id": external_id,
        "legal_name": "Acme Inc",
        "trade_name": "",
        "domain": "acme.test",
        "careers_url": "",
        "country": "US",
        "states": ["CA"],
        "industry": "Services",
        "naics": "",
        "employee_size": "",
        "ats": "",
        "ats_slug": "",
        "ats_url": "",
        "metadata": {"cik": external_id},
    }


def test_prepare_source_record_adds_provenance_and_namespaced_id():
    row = cli.prepare_source_record(_record())
    assert row["external_ids"] == {"sec_cik": "1"}
    assert row["provenance"]["source"] == "sec_edgar"
    assert row["discovery_confidence"] == 1.0


def test_collect_records_filters_country_and_never_reads_catalog(monkeypatch):
    rows = [_record(), {**_record(external_id="2"), "country": "CA"}]
    monkeypatch.setattr(cli, "fetch_source", lambda *args, **kwargs: rows)
    monkeypatch.setattr(cli.company_db, "catalog_companies",
                        lambda: (_ for _ in ()).throw(AssertionError("catalog read during acquisition")))
    result, counts = cli.collect_records(["sec"], limit=10)
    assert [row["source_external_id"] for row in result] == ["1"]
    assert counts == {"sec": 1}


def test_sec_archive_can_be_imported_without_live_sec_access(tmp_path):
    archive_path = tmp_path / "submissions.zip"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("CIK0001.json", json.dumps({
            "cik": "1", "name": "Archive Acme", "entityType": "operating",
            "website": "https://archive-acme.test",
            "addresses": {"business": {"stateOrCountry": "CA"}},
        }))
    archive_path.write_bytes(stream.getvalue())
    rows = cli.fetch_source("sec", limit=10, sec_archive=str(archive_path))
    assert rows[0]["legal_name"] == "Archive Acme"
    assert rows[0]["domain"] == "archive-acme.test"


def test_collect_dry_run_writes_jsonl_without_database(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "fetch_source", lambda *args, **kwargs: [_record()])
    monkeypatch.setattr(cli.company_db, "ensure_schema",
                        lambda: (_ for _ in ()).throw(AssertionError("database opened")))
    output = tmp_path / "companies.jsonl"
    assert cli.main(["collect", "--source", "sec", "--limit", "1", "--dry-run",
                     "--output", str(output)]) == 0
    line = json.loads(output.read_text())
    assert line["legal_name"] == "Acme Inc"
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_persisted_collect_initializes_upserts_then_reconciles(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "fetch_source", lambda *args, **kwargs: [_record()])
    monkeypatch.setattr(cli.company_db, "ensure_schema", lambda: calls.append("init"))
    monkeypatch.setattr(cli.company_db, "upsert_records",
                        lambda rows: calls.append(("upsert", len(rows))) or len(rows))
    monkeypatch.setattr(cli.company_db, "reconcile_records",
                        lambda: calls.append("reconcile") or 1)
    monkeypatch.setattr(cli.company_db, "counts", lambda: {"total": 1})
    assert cli.main(["collect", "--source", "sec", "--limit", "1"]) == 0
    assert calls == ["init", ("upsert", 1), "reconcile"]
    assert json.loads(capsys.readouterr().out)["stored"] == 1
