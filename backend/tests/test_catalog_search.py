"""list_jobs country-eligibility search — DB-free (fake cursor). Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_catalog_search.py -q
"""
import contextlib

from backend.tools import catalog_db


class _FakeCur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def fetchall(self):
        return []


def _patch(monkeypatch):
    sink = []

    @contextlib.contextmanager
    def fake_cur():
        yield _FakeCur(sink)

    monkeypatch.setattr(catalog_db, "_cur", fake_cur)
    return sink


def test_country_query_uses_region_overlap(monkeypatch):
    sink = _patch(monkeypatch)
    catalog_db.list_jobs(q="Kazakhstan")
    sql, params = sink[-1]
    assert "regions && %s::text[]" in sql
    assert "plainto_tsquery" not in sql
    assert ["OTHER"] in params


def test_text_query_uses_full_text_search(monkeypatch):
    sink = _patch(monkeypatch)
    catalog_db.list_jobs(q="customer support")
    sql, params = sink[-1]
    assert "plainto_tsquery" in sql
    assert "regions && %s::text[]" not in sql
    assert "customer support" in params


def test_no_query_has_neither_clause(monkeypatch):
    sink = _patch(monkeypatch)
    catalog_db.list_jobs()
    sql, _ = sink[-1]
    assert "plainto_tsquery" not in sql and "regions && %s::text[]" not in sql


def test_list_job_ids_shares_the_filters_and_is_light(monkeypatch):
    sink = _patch(monkeypatch)
    catalog_db.list_job_ids(q="Kazakhstan", region=None, limit=3000)
    sql, params = sink[-1]
    assert sql.startswith("SELECT id, company, title FROM job_catalog")
    assert "regions && %s::text[]" in sql and "open_anywhere = TRUE" in sql
    assert "description" not in sql and params[-1] == 3000
