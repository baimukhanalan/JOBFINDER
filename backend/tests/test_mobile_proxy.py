"""«Мобильный прокси» (the phone over Tailscale as a residential egress) — pure, no network:
settings roundtrip, TCP-alive against a throwaway local listener, the cached status, the
residential-slot merge in proxy_pool, and the fill payload carrying it. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_mobile_proxy.py -q
"""
import json
import socket

from backend.tools import mobile_proxy as mp


def _use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "_PATH", tmp_path / "mobile_proxy.json")
    mp._CACHE.update(ts=0.0, status=None)


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_settings_roundtrip_and_normalize(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    assert mp.load() == {} and mp.proxy_dict() is None
    d = mp.update(server="100.64.1.2:1080", enabled=True, note="iPhone")
    assert d["server"] == "socks5://100.64.1.2:1080" and d["enabled"] is True
    assert json.loads((tmp_path / "mobile_proxy.json").read_text())["note"] == "iPhone"
    assert mp.proxy_dict() == {"server": "socks5://100.64.1.2:1080", "username": None, "password": None}
    # an http proxy with credentials in the URL keeps them (Chromium can authenticate http)
    mp.update(server="http://u:p@10.0.0.5:8080")
    assert mp.proxy_dict() == {"server": "http://10.0.0.5:8080", "username": "u", "password": "p"}
    mp.update(enabled=False)
    assert mp.proxy_dict() is None


def test_tcp_alive_and_live_servers(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    s, port = _listener()
    try:
        assert mp.tcp_alive(f"socks5://127.0.0.1:{port}", timeout=1.0)
        mp.update(server=f"socks5://127.0.0.1:{port}", enabled=True)
        assert mp.live_servers() == [f"socks5://127.0.0.1:{port}"]
        mp.update(enabled=False)
        assert mp.live_servers() == []
    finally:
        s.close()
    assert not mp.tcp_alive(f"socks5://127.0.0.1:{port}", timeout=0.5)     # closed now


def test_status_states_and_cache(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    st = mp.status(force=True)
    assert st["configured"] is False and st["alive"] is False
    s, port = _listener()
    try:
        mp.update(server=f"socks5://127.0.0.1:{port}", enabled=True)
        calls = []

        def echo(px):
            calls.append(px["server"])
            return "203.0.113.7"

        st = mp.status(force=True, echo=echo)
        assert st["alive"] and st["egress"] == "203.0.113.7" and calls == [f"socks5://127.0.0.1:{port}"]
        # cached: a second call within the TTL does NOT probe again
        st2 = mp.status(echo=echo)
        assert st2["egress"] == "203.0.113.7" and len(calls) == 1
        # a SOCKS that answers TCP but cannot route (no egress) counts as DOWN
        st3 = mp.status(force=True, echo=lambda px: None)
        assert st3["alive"] is False and st3["egress"] is None
    finally:
        s.close()
    # configured but the phone is offline -> not alive, no probe through it
    st4 = mp.status(force=True, echo=lambda px: "x")
    assert st4["configured"] and st4["enabled"] and st4["alive"] is False


def test_residential_slots_include_the_phone(tmp_path, monkeypatch):
    from backend.tools import proxy_pool
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(proxy_pool, "_RES_COUNT", 0)          # no chisel loopback slots in the test
    proxy_pool._res_cache.update(ts=0.0, slots=[])
    s, port = _listener()
    try:
        mp.update(server=f"socks5://127.0.0.1:{port}", enabled=True)
        assert proxy_pool.residential_slots() == [f"socks5://127.0.0.1:{port}"]
        assert proxy_pool.residential_up()
        assert proxy_pool.next_proxy() == {"server": f"socks5://127.0.0.1:{port}", "username": None, "password": None}
    finally:
        s.close()
    proxy_pool._res_cache.update(ts=0.0, slots=[])
    assert proxy_pool.residential_slots() == [] and not proxy_pool.residential_up()


def test_do_fill_routes_through_the_phone_when_up(tmp_path, monkeypatch):
    """The campaign/single fill payload carries the phone proxy when it is up and falls back to
    the pool (here: none -> direct) when it is down. No real fill: httpx.post is captured."""
    import httpx

    from backend import dashboard_app as d
    from backend.tools import catalog_drafts, proxy_pool
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog_drafts, "ensure_and_wire", lambda *a, **k: ("demo_x", 20031, True))
    seen = []

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"filled": 1, "unfilled": 0, "submit_result": {"clicked": True}}

    def fake_post(url, data=None, timeout=None):
        seen.append((url, dict(data or {})))
        return _R()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(proxy_pool, "residential_proxy", lambda: {"server": "socks5://100.64.9.9:1080",
                                                                  "username": None, "password": None})
    d._do_fill(20031, None, "Dana Erlan", "dana.erlan1@takhet.com", "demo_x", wait_submit=True)
    load = [x for x in seen if x[0].endswith("/load")][-1][1]
    assert load["proxy_server"] == "socks5://100.64.9.9:1080" and "proxy_username" not in load
    assert load["wait_submit"] == "1" and d._FILL_JOBS[20031]["state"] == "done"
    # phone down + empty pool -> direct (no proxy fields)
    seen.clear()
    monkeypatch.setattr(proxy_pool, "residential_proxy", lambda: None)
    monkeypatch.setattr(proxy_pool, "_load", lambda: {"proxies": []})
    d._do_fill(20031, None, "Dana Erlan", "dana.erlan2@takhet.com", "demo_x")
    load = [x for x in seen if x[0].endswith("/load")][-1][1]
    assert "proxy_server" not in load and "wait_submit" not in load


def test_health_row_states(tmp_path, monkeypatch):
    from backend.tools import health
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "status", lambda force=False, echo=None: {"configured": False})
    rows = {r["name"]: r for r in health.proxy_rows()}
    assert rows["Мобильный прокси (телефон)"]["status"] == "info"
    monkeypatch.setattr(mp, "status", lambda force=False, echo=None:
                        {"configured": True, "enabled": True, "alive": False, "server": "socks5://100.1.1.1:1080"})
    assert {r["name"]: r for r in health.proxy_rows()}["Мобильный прокси (телефон)"]["status"] == "warn"
    monkeypatch.setattr(mp, "status", lambda force=False, echo=None:
                        {"configured": True, "enabled": True, "alive": True, "egress": "5.6.7.8",
                         "server": "socks5://100.1.1.1:1080"})
    r = {r["name"]: r for r in health.proxy_rows()}["Мобильный прокси (телефон)"]
    assert r["status"] == "ok" and "5.6.7.8" in r["detail"]
