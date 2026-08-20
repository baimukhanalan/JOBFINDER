"""Workable normalizer — no live network (httpx.get monkeypatched)."""
from backend.applier import ats_boards


class _Resp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


def test_workable_normalizes(monkeypatch):
    payload = {"jobs": [{
        "title": "Support Engineer", "shortcode": "ABC123",
        "url": "https://apply.workable.com/acme/j/ABC123/",
        "location": {"location_str": "Remote (US)"}, "telecommuting": True,
        "department": "Support", "description": "<p>Do support</p>",
    }]}
    monkeypatch.setattr(ats_boards.httpx, "get", lambda *a, **k: _Resp(payload))
    jobs = ats_boards.fetch_board("workable", "acme")
    assert jobs and jobs[0]["title"] == "Support Engineer"
    assert jobs[0]["isRemote"] is True
    assert jobs[0]["applyUrl"].endswith("/ABC123/")
    assert "US" in jobs[0]["location"]
    assert "workable" in ats_boards.SUPPORTED
