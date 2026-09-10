"""_fill_campaign_targets — the parallel apply-campaign filler (bulk_pool + co-pilot mocked).
Proves per-job pinned identity, per-jid result mapping, the worker cap, and error isolation.
    PYTHONPATH=. python3 -m pytest backend/tests/test_campaign_parallel.py -q
"""
import backend.dashboard_app as d
from backend.tools import bulk_pool, catalog_drafts


def _mock_common(monkeypatch, ports):
    seen_ids = []            # (email, pid) each ensure_and_wire pinned
    start_calls = []

    def fake_start(n, *a, **k):
        start_calls.append(n)
        return list(ports)
    monkeypatch.setattr(bulk_pool, "start_workers", fake_start)
    monkeypatch.setattr(bulk_pool, "stop_workers", lambda *a, **k: None)

    def fake_wire(jid, gender=None, name=None, email=None, pid=None):
        seen_ids.append((email, pid))
        return pid, jid, False
    monkeypatch.setattr(catalog_drafts, "ensure_and_wire", fake_wire)
    return seen_ids, start_calls


def test_parallel_pins_identity_maps_results_and_caps_workers(monkeypatch):
    seen_ids, start_calls = _mock_common(monkeypatch, ports=[8110, 8111])

    def fake_fill(base, jid, pid, *, wait_submit=False):
        assert wait_submit is True
        if jid == 202:                              # one job errors …
            return {"state": "error", "error": "boom"}
        return {"state": "done", "submit": {"confirmed": True}, "company": "C", "title": "T"}
    monkeypatch.setattr(d, "_fill_via", fake_fill)

    targets = [201, 202, 203]
    res = d._fill_campaign_targets(
        targets, gender="female", name="Dana Erlan",
        identity_for=lambda jid: (f"dana{jid}@takhet.com", f"pid{jid}"),
        workers=8)

    # every jid mapped back; the erroring job didn't sink the others
    assert set(res) == {201, 202, 203}
    assert res[201]["state"] == "done" and res[203]["state"] == "done"
    assert res[202]["state"] == "error"
    # N DISTINCT identities pinned (fixed name, fresh mailbox/pid per job) + threaded back
    assert sorted(seen_ids) == [("dana201@takhet.com", "pid201"),
                                ("dana202@takhet.com", "pid202"),
                                ("dana203@takhet.com", "pid203")]
    assert res[201]["mailbox"] == "dana201@takhet.com"
    # worker cap = min(workers, len(targets), 12) → 3 here (not 8)
    assert start_calls == [3]


def test_parallel_falls_back_to_single_copilot_when_no_workers(monkeypatch):
    seen_ids, _ = _mock_common(monkeypatch, ports=[])      # pool comes up empty
    bases = []

    def fake_fill(base, jid, pid, *, wait_submit=False):
        bases.append(base)
        return {"state": "done", "submit": {"confirmed": True}}
    monkeypatch.setattr(d, "_fill_via", fake_fill)

    res = d._fill_campaign_targets(
        [301, 302], name="X", identity_for=lambda jid: (f"x{jid}@takhet.com", f"p{jid}"),
        workers=8)
    assert set(res) == {301, 302}
    assert all(b == "http://127.0.0.1:8102" for b in bases)   # sequential on the single co-pilot


def test_workers_le_1_is_sequential_on_copilot(monkeypatch):
    _mock_common(monkeypatch, ports=[8110])
    bases = []
    monkeypatch.setattr(d, "_fill_via",
                        lambda base, jid, pid, *, wait_submit=False: bases.append(base) or {"state": "done"})
    res = d._fill_campaign_targets([401], name="X",
                                   identity_for=lambda jid: ("a@b", "p"), workers=1)
    assert set(res) == {401} and bases == ["http://127.0.0.1:8102"]
