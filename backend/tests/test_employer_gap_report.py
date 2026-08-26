from contextlib import contextmanager

from backend.tools import employer_gap_report as report


def test_gap_report_has_exact_denominators_blockers_and_is_read_only(monkeypatch):
    rows = [
        {"company_id": 1, "source": "usaspending", "source_external_id": "UEI1",
         "legal_name": "Agency", "brand_identity": {"brand_name": None},
         "employee_count": None, "employee_count_min": None, "industry": None,
         "naics_code": None, "headquarters": None, "headquarters_address_type": None,
         "employer_segment": "government", "identity_enrichment_gaps": {
             "brand_name": "source_provides_legal_name_only",
             "employee_size": "source_provenance_has_no_workforce_evidence",
             "industry": "source_provenance_has_no_industry",
             "naics": "source_provenance_has_no_naics",
             "headquarters": "source_provenance_has_no_headquarters_address"},
         "metadata": {"uei": "UEI1", "source_snapshot_refresh": {"status": "pending",
                      "error": "exact binding unavailable"}}},
        {"company_id": 2, "source": "gleif_lei", "source_external_id": "LEI1",
         "legal_name": "Company", "brand_identity": {"brand_name": "Company"},
         "employee_count": None, "employee_count_min": None, "industry": None,
         "naics_code": None, "headquarters": "Dover, DE",
         "headquarters_address_type": "registered", "employer_segment": "general",
         "identity_enrichment_gaps": {"employee_size": "missing", "industry": "missing",
                                      "naics": "missing"},
         "metadata": {"source_snapshot_refresh": {"status": "success"},
                      "source_snapshot": {"legal_address": {"city": "Dover"}}}},
    ]

    class Cursor:
        def __init__(self): self.sql = ""
        def execute(self, sql): self.sql = sql
        def fetchall(self): return rows

    cursor = Cursor()

    @contextmanager
    def fake_cur():
        yield cursor

    monkeypatch.setattr(report.company_db, "_cur", fake_cur)
    monkeypatch.setattr(report, "_configured", lambda _name: False)
    result = report.build_report()
    assert result["active_population"] == 2
    assert "UPDATE " not in cursor.sql and "INSERT " not in cursor.sql
    usa_brand = next(row for row in result["manifest"]
                     if row["source"] == "usaspending" and row["field"] == "brand_name")
    assert usa_brand["resolution_class"] == "owner_credential"
    assert usa_brand["external_requirements"] == ["SAM_API_KEY"]
    gleif_hq = next(row for row in result["coverage"]
                    if row["source"] == "gleif_lei" and row["field"] == "headquarters")
    assert gleif_hq == {"source": "gleif_lei", "field": "headquarters",
                        "denominator": 1, "covered": 1, "gap": 0,
                        "coverage_rate": 1.0}
    assert set(result["owner_credentials_required"]) == {"SAM_API_KEY", "SEC_USER_AGENT"}


def test_csv_manifest_serializes_requirements(tmp_path):
    path = tmp_path / "gaps.csv"
    report.write_csv(path, [{
        "company_id": 1, "source": "usaspending", "source_external_id": "UEI1",
        "field": "naics", "reason": "missing", "blocker": "sam",
        "resolution_class": "owner_credential",
        "external_requirements": ["SAM_API_KEY", "authoritative_ID_crosswalk"],
        "retryable": True,
    }])
    text = path.read_text()
    assert "SAM_API_KEY|authoritative_ID_crosswalk" in text
    assert "true" in text
