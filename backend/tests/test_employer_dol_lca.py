import zipfile

import httpx

from backend.tools.employer_dol_lca import download_dol_lca, parse_dol_lca_xlsx


def _xlsx(path, rows):
    def cell(column, row_number, value):
        escaped = (str(value).replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        return (f'<c r="{column}{row_number}" t="inlineStr"><is><t>'
                f'{escaped}</t></is></c>')

    columns = ["A", "B", "C", "D", "E", "F", "G"]
    header = ["CASE_STATUS", "EMPLOYER_NAME", "EMPLOYER_ADDRESS1",
              "EMPLOYER_CITY", "EMPLOYER_STATE", "EMPLOYER_POSTAL_CODE",
              "TOTAL_WORKER_POSITIONS"]
    xml_rows = ["<row r=\"1\">" + "".join(
        cell(column, 1, value) for column, value in zip(columns, header)) + "</row>"]
    for index, values in enumerate(rows, 2):
        xml_rows.append(f'<row r="{index}">' + "".join(
            cell(column, index, value) for column, value in zip(columns, values)) + "</row>")
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + "".join(xml_rows) + '</sheetData></worksheet>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


def test_dol_lca_parser_keeps_certified_activity_and_aggregates_employers(tmp_path):
    path = tmp_path / "dol.xlsx"
    _xlsx(path, [
        ["CERTIFIED", "Acme, Inc.", "1 Main St", "Austin", "TX", "78701", "3"],
        ["CERTIFIED-WITHDRAWN", "ACME INC", "1 Main St", "Austin", "TX", "78701", "2"],
        ["DENIED", "Acme, Inc.", "1 Main St", "Austin", "TX", "78701", "99"],
        ["CERTIFIED", "Beta LLC", "2 State St", "Boston", "MA", "02101", "8"],
    ])
    rows = parse_dol_lca_xlsx(
        path, limit=10, source_url="https://dol.example/disclosure.xlsx")
    assert [row["legal_name"] for row in rows] == ["Beta LLC", "Acme, Inc."]
    acme = rows[1]
    assert acme["metadata"]["certified_case_count"] == 2
    assert acme["metadata"]["certified_worker_positions"] == 5
    assert acme["metadata"]["case_status_counts"] == {
        "CERTIFIED": 1, "CERTIFIED-WITHDRAWN": 1}
    assert acme["states"] == ["TX"]
    assert acme["domain"] == acme["careers_url"] == acme["ats"] == ""
    assert acme["metadata"]["legal_identity_unverified"] is True
    assert len(acme["metadata"]["source_file_sha256"]) == 64
    assert acme["source_url"] == "https://dol.example/disclosure.xlsx"


def test_dol_lca_parser_is_bounded_and_deduplicated(tmp_path):
    path = tmp_path / "dol.xlsx"
    _xlsx(path, [
        ["CERTIFIED", "Same Co", "1 A", "A", "CA", "1", "1"],
        ["CERTIFIED", "Same-Co", "2 B", "B", "NY", "2", "4"],
        ["CERTIFIED", "Other Co", "3 C", "C", "WA", "3", "2"],
    ])
    rows = parse_dol_lca_xlsx(path, limit=1)
    assert len(rows) == 1
    assert rows[0]["metadata"]["certified_case_count"] == 2
    assert rows[0]["metadata"]["certified_worker_positions"] == 5
    assert rows[0]["metadata"]["employer_address"]["region"] == "NY"


def test_dol_download_is_bounded_cached_and_uses_official_url(tmp_path):
    fixture = tmp_path / "fixture.xlsx"
    _xlsx(fixture, [["CERTIFIED", "Acme", "1 A", "A", "CA", "1", "1"]])
    content = fixture.read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=content, request=request)

    target = tmp_path / "cache" / "dol.xlsx"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert download_dol_lca(cache_path=target, client=client) == target
        assert download_dol_lca(cache_path=target, client=client) == target
    assert len(requests) == 1
    assert requests[0].url.host == "www.dol.gov"
    assert target.read_bytes() == content
    assert not target.with_suffix(".xlsx.tmp").exists()
