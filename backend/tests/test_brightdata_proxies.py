"""Unit tests for the Bright Data proxy provisioner — pure logic, NO network.

Covers the username format, session minting (shape/uniqueness), config-missing
detection, and the atomic pool replace. The live zone probe / --verify path is
network and is not exercised here.
"""
import json

from backend.tools import brightdata_proxies as bd
from backend.tools import proxy_pool

_CFG = {
    "token": "tok", "customer": "hl_abc123", "zone": "alibaba_dc",
    "password": "pw123", "host": "brd.superproxy.io", "port": 33335,
    "country": "us",
}


def test_username_format_with_country():
    u = bd._username(_CFG, "deadbeef")
    assert u == "brd-customer-hl_abc123-zone-alibaba_dc-country-us-session-deadbeef"


def test_username_omits_blank_country():
    cfg = {**_CFG, "country": ""}
    u = bd._username(cfg, "cafe")
    assert u == "brd-customer-hl_abc123-zone-alibaba_dc-session-cafe"
    assert "country" not in u


def test_make_sessions_shape_and_uniqueness():
    sessions = bd.make_sessions(50, _CFG)
    assert len(sessions) == 50
    users = {s["username"] for s in sessions}
    assert len(users) == 50, "each session must have a distinct session id"
    for s in sessions:
        assert s["scheme"] == "http"
        assert s["host"] == "brd.superproxy.io"
        assert s["port"] == 33335
        assert s["server"] == "http://brd.superproxy.io:33335"
        assert s["password"] == "pw123"
        assert s["username"].startswith("brd-customer-hl_abc123-zone-alibaba_dc-country-us-session-")
        assert s["ip"] is None


def test_make_sessions_zero_and_negative():
    assert bd.make_sessions(0, _CFG) == []
    assert bd.make_sessions(-5, _CFG) == []


def test_missing_config_detection():
    assert bd._missing(_CFG) == []
    assert set(bd._missing({**_CFG, "customer": "", "password": ""})) == {"customer", "password"}
    assert bd._missing({**_CFG, "zone": ""}) == ["zone"]


def test_replace_pool_is_atomic_and_resets(tmp_path, monkeypatch):
    store = tmp_path / "proxies.json"
    # seed an old pool with fail streaks + a nonzero cursor
    store.write_text(json.dumps({
        "proxies": [{"server": "http://old:1", "username": "old", "password": "x",
                     "scheme": "http", "host": "old", "port": 1, "ip": "9.9.9.9", "fails": 2}],
        "cursor": 7, "recheck_cursor": 3,
    }), encoding="utf-8")
    monkeypatch.setattr(proxy_pool, "_STORE", store)

    fresh = bd.make_sessions(5, _CFG)
    res = proxy_pool.replace_pool(fresh)
    assert res["count"] == 5

    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data["proxies"]) == 5
    assert data["cursor"] == 0 and data["recheck_cursor"] == 0
    # old entry gone; fresh entries carry no fail streak (fails defaults to 0)
    assert all(p.get("username", "").endswith(tuple("0123456789abcdef")) for p in data["proxies"])
    assert all("fails" not in p for p in data["proxies"]), "_clean drops fails → fresh start"
    # egress starts unknown
    assert all(p.get("ip") is None for p in data["proxies"])
