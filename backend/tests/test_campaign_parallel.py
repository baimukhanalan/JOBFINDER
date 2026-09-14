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

    def fake_wire(jid, gender=None, name=None, email=None, pid=None, english_level=None):
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


# --- retry-on-spam (CAMPAIGN_ASHBY_RETRY), added 2026-09-14 -----------------------------------------

def _retry_setup(monkeypatch, retry=2, under_cap=True):
    _mock_common(monkeypatch, ports=[8110])
    monkeypatch.setattr(d, "CAMPAIGN_ASHBY_RETRY", retry)
    monkeypatch.setattr(d, "_company_under_day_cap", lambda jid: under_cap)


def test_retry_on_recaptcha_spam_then_lands(monkeypatch):
    _retry_setup(monkeypatch, retry=2)
    calls = {"n": 0}

    def fake_fill(base, jid, pid, *, wait_submit=False):
        calls["n"] += 1
        if calls["n"] == 1:                                  # first roll flags…
            return {"state": "done", "submit": {"blocked": "flagged as possible spam"}}
        return {"state": "done", "submit": {"confirmed": True}}   # …retry lands
    monkeypatch.setattr(d, "_fill_via", fake_fill)

    res = d._fill_campaign_targets([501], name="X", identity_for=lambda jid: ("a@b", "p"), workers=1)
    assert res[501]["submit"].get("confirmed") is True
    assert res[501]["attempt"] == 2 and calls["n"] == 2


def test_no_retry_on_validation_miss(monkeypatch):
    _retry_setup(monkeypatch, retry=2)
    calls = {"n": 0}

    def fake_fill(base, jid, pid, *, wait_submit=False):
        calls["n"] += 1
        return {"state": "done", "submit": {"blocked": "Missing entry for required field: Email"}}
    monkeypatch.setattr(d, "_fill_via", fake_fill)

    res = d._fill_campaign_targets([502], name="X", identity_for=lambda jid: ("a@b", "p"), workers=1)
    assert res[502]["attempt"] == 1 and calls["n"] == 1        # a fill gap is NOT retried


def test_no_retry_on_confirmed(monkeypatch):
    _retry_setup(monkeypatch, retry=2)
    calls = {"n": 0}
    monkeypatch.setattr(d, "_fill_via",
                        lambda base, jid, pid, *, wait_submit=False: calls.__setitem__("n", calls["n"] + 1)
                        or {"state": "done", "submit": {"confirmed": True}})
    res = d._fill_campaign_targets([503], name="X", identity_for=lambda jid: ("a@b", "p"), workers=1)
    assert res[503]["attempt"] == 1 and calls["n"] == 1


def test_retry_disabled_at_zero(monkeypatch):
    _retry_setup(monkeypatch, retry=0)
    calls = {"n": 0}
    monkeypatch.setattr(d, "_fill_via",
                        lambda base, jid, pid, *, wait_submit=False: calls.__setitem__("n", calls["n"] + 1)
                        or {"state": "done", "submit": {"blocked": "flagged as possible spam"}})
    res = d._fill_campaign_targets([504], name="X", identity_for=lambda jid: ("a@b", "p"), workers=1)
    assert res[504]["attempt"] == 1 and calls["n"] == 1        # RETRY=0 never retries


def test_no_retry_when_company_at_velocity_cap(monkeypatch):
    _retry_setup(monkeypatch, retry=2, under_cap=False)       # tenant already at cap
    calls = {"n": 0}
    monkeypatch.setattr(d, "_fill_via",
                        lambda base, jid, pid, *, wait_submit=False: calls.__setitem__("n", calls["n"] + 1)
                        or {"state": "done", "submit": {"blocked": "flagged as possible spam"}})
    res = d._fill_campaign_targets([505], name="X", identity_for=lambda jid: ("a@b", "p"), workers=1)
    assert res[505]["attempt"] == 1 and calls["n"] == 1        # cap blocks re-hammer


def test_recaptcha_score_flag_prefers_graphql_code_over_banner(tmp_path, monkeypatch):
    import json
    monkeypatch.chdir(tmp_path)
    dd = tmp_path / "uploads" / "prefill" / "pidZ" / "999"
    dd.mkdir(parents=True)
    banner = {"submit": {"blocked": "flagged as possible spam"}}
    # SAME banner, reliable code = low score -> retryable
    (dd / "submit_response.json").write_text(
        json.dumps({"body": '{"errors":[{"extensions":{"ashbyErrorType":"RECAPTCHA_SCORE_BELOW_THRESHOLD"}}]}'}))
    assert d._recaptcha_score_flag("pidZ", 999, banner) is True
    # SAME banner, reliable code = a validation error -> NOT retryable (fresh identity can't fix a fill gap)
    (dd / "submit_response.json").write_text(
        json.dumps({"body": '{"errors":[{"extensions":{"ashbyErrorType":"INVALID_INPUT"}}]}'}))
    assert d._recaptcha_score_flag("pidZ", 999, banner) is False
