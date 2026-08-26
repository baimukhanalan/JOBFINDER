from backend.tools import employer_careers


def test_verified_career_enrichment_is_master_scoped(monkeypatch):
    rows = [{"id": 3, "domain": "acme.test"}]
    saved = []
    monkeypatch.setattr(employer_careers.master_db, "list_verified_employers",
                        lambda **_: rows)
    monkeypatch.setattr(employer_careers, "enrich_company",
                        lambda row, **_: {**row, "careers_url": "https://acme.test/careers"})
    monkeypatch.setattr(employer_careers.company_db, "update_enrichment_results",
                        lambda values: saved.extend(values) or len(values))
    monkeypatch.setattr(employer_careers.master_db, "verified_career_counts",
                        lambda: {"verified_domains": 1, "careers": 1, "ats": 0})
    result = employer_careers.enrich_verified_careers(limit=5, workers=1)
    assert result["selected"] == 1
    assert saved[0]["id"] == 3
