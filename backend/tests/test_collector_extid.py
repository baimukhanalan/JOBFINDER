"""collect_board must key each posting by the ATS's stable job id, not the last URL
segment — ashby apply URLs all end in '/application' and lever's in '/apply', which
used to collapse an entire board to a single row (external_id='application'/'apply')."""
from backend.tools import catalog_collector as cc


def _board(monkeypatch, ats, jobs):
    monkeypatch.setattr(cc.ats_boards, "fetch_board", lambda a, s: jobs)
    return cc.collect_board(ats, "acme", "Acme", remote_only=False)


def test_ashby_ids_do_not_collapse(monkeypatch):
    jobs = [
        {"id": "aaa-1", "title": "Engineer", "isRemote": True,
         "applyUrl": "https://jobs.ashbyhq.com/acme/aaa-1/application", "location": "Remote"},
        {"id": "bbb-2", "title": "Designer", "isRemote": True,
         "applyUrl": "https://jobs.ashbyhq.com/acme/bbb-2/application", "location": "Remote"},
    ]
    rows = _board(monkeypatch, "ashby", jobs)
    ext = sorted(r["external_id"] for r in rows)
    assert ext == ["aaa-1", "bbb-2"]          # both kept, not collapsed to "application"
    assert "application" not in ext


def test_lever_ids_do_not_collapse(monkeypatch):
    jobs = [
        {"id": "uuid-a", "title": "AE", "isRemote": True,
         "applyUrl": "https://jobs.lever.co/acme/uuid-a/apply", "location": "Remote"},
        {"id": "uuid-b", "title": "SDR", "isRemote": True,
         "applyUrl": "https://jobs.lever.co/acme/uuid-b/apply", "location": "Remote"},
    ]
    rows = _board(monkeypatch, "lever", jobs)
    ext = sorted(r["external_id"] for r in rows)
    assert ext == ["uuid-a", "uuid-b"]
    assert "apply" not in ext


def test_falls_back_to_url_when_no_id(monkeypatch):
    jobs = [{"title": "X", "isRemote": True,
             "applyUrl": "https://boards.greenhouse.io/acme/jobs/12345", "location": "Remote"}]
    rows = _board(monkeypatch, "greenhouse", jobs)
    assert rows[0]["external_id"] == "12345"
