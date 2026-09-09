"""«Мобильные прокси (телефоны)» — a POOL of phones on the Tailscale tailnet as residential egress.
Pure, no network: settings + migration, tailscale-peer discovery from a fixture, the manual∪
discovered dedup, live filtering + next_mobile round-robin, the residential merge in proxy_pool,
the join-argv builder (key never logged), the fill payload rotating across phones, and the health
row. Run:  PYTHONPATH=. python3 -m pytest backend/tests/test_mobile_proxy.py -q
"""
import json
import socket

from backend.tools import mobile_proxy as mp


def _use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "_PATH", tmp_path / "mobile_proxy.json")
    mp._CACHE.update(ts=0.0, status=None)
    mp._LIVE_CACHE.update(ts=0.0, servers=[])
    mp._DISC_CACHE.update(ts=0.0, endpoints=[], tailnet="")


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


# a `tailscale status --json` with 2 online phones + 1 offline laptop + self
_TS = {
    "Self": {"HostName": "jobfinder-server", "TailscaleIPs": ["100.79.101.77"], "Online": True},
    "MagicDNSSuffix": "jf-net.ts.net",
    "CurrentTailnet": {"Name": "jf-net.ts.net"},
    "Peer": {
        "k1": {"HostName": "iphone-dana", "DNSName": "iphone-dana.jf-net.ts.net.",
               "TailscaleIPs": ["100.100.0.1"], "Online": True, "OS": "iOS"},
        "k2": {"HostName": "android-fallback", "DNSName": "android-fallback.jf-net.ts.net.",
               "TailscaleIPs": ["100.100.0.2"], "Online": True, "OS": "android"},
        "k3": {"HostName": "macbook", "DNSName": "macbook.jf-net.ts.net.",
               "TailscaleIPs": ["100.100.0.9"], "Online": False, "OS": "macOS"},
    },
}


def test_settings_roundtrip_and_normalize(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    assert mp.load()["manual"] == [] and mp.load()["port"] == 1080
    mp.update(add="100.64.1.2:1080", note="iPhone", enabled=True)
    d = mp.load()
    assert d["enabled"] is True and d["manual"] == [{"server": "socks5://100.64.1.2:1080", "note": "iPhone"}]
    # dedup by host:port; a second add of the same endpoint is a no-op
    mp.update(add="socks5://100.64.1.2:1080")
    assert len(mp.load()["manual"]) == 1
    mp.update(add="http://u:p@10.0.0.5:8080", note="lan")
    assert len(mp.load()["manual"]) == 2
    mp.update(remove="100.64.1.2:1080")
    assert [m["server"] for m in mp.load()["manual"]] == ["http://u:p@10.0.0.5:8080"]


def test_migrates_old_single_server_file(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    (tmp_path / "mobile_proxy.json").write_text(json.dumps(
        {"enabled": True, "server": "socks5://100.1.2.3:1080", "note": "iPhone", "username": "", "password": ""}))
    d = mp.load()
    assert "server" not in d and d["manual"] == [{"server": "socks5://100.1.2.3:1080", "note": "iPhone"}]
    assert d["enabled"] is True and d["port"] == 1080 and d["cursor"] == 0
    assert d["discover"] == {"enabled": True, "match": ""}


def test_discovery_parses_online_peers_and_match(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "_tailscale_status_json", lambda timeout=4.0: _TS)
    assert mp.tailnet_name() == "jf-net.ts.net"
    eps = mp.discover_endpoints(force=True)
    assert eps == ["socks5://100.100.0.1:1080", "socks5://100.100.0.2:1080"]   # offline macbook excluded
    # a hostname filter narrows discovery
    mp.update(match="iphone")
    assert mp.discover_endpoints(force=True) == ["socks5://100.100.0.1:1080"]
    # a custom shared port
    mp.update(match="", port=1055)
    assert mp.discover_endpoints(force=True) == ["socks5://100.100.0.1:1055", "socks5://100.100.0.2:1055"]


def test_all_endpoints_merges_manual_and_discovered_deduped(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "_tailscale_status_json", lambda timeout=4.0: _TS)
    mp.update(add="socks5://100.100.0.1:1080", note="dana")     # same as discovered k1 -> manual wins
    mp.update(add="socks5://100.200.0.5:1080", note="extra")
    eps = mp.all_endpoints(force_discover=True)
    servers = [(e["server"], e["source"]) for e in eps]
    assert ("socks5://100.100.0.1:1080", "manual") in servers
    assert ("socks5://100.200.0.5:1080", "manual") in servers
    assert ("socks5://100.100.0.2:1080", "discovered") in servers
    assert sum(1 for s, _ in servers if s == "socks5://100.100.0.1:1080") == 1   # deduped


def test_live_servers_and_next_mobile_round_robin(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "_tailscale_status_json", lambda timeout=4.0: _TS)
    mp.update(enabled=True)
    alive = {"socks5://100.100.0.1:1080"}                       # only phone #1 answers TCP
    monkeypatch.setattr(mp, "tcp_alive", lambda s, timeout=3.0: s in alive)
    assert mp.live_servers() == ["socks5://100.100.0.1:1080"]
    alive.add("socks5://100.100.0.2:1080")
    mp._LIVE_CACHE.update(ts=0.0, servers=[])                   # bust the 15s cache
    live = mp.live_servers()
    assert set(live) == {"socks5://100.100.0.1:1080", "socks5://100.100.0.2:1080"}
    # next_mobile round-robins over the live set and persists the cursor
    a = mp.next_mobile()["server"]
    b = mp.next_mobile()["server"]
    assert {a, b} == set(live) and a != b
    assert mp.next_mobile()["server"] == a                     # wrapped
    # disabled -> no live servers, next_mobile None
    mp.update(enabled=False)
    mp._LIVE_CACHE.update(ts=0.0, servers=[])
    assert mp.live_servers() == [] and mp.next_mobile() is None


def test_status_aggregate(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(mp, "_tailscale_status_json", lambda timeout=4.0: _TS)
    mp.update(enabled=True)
    monkeypatch.setattr(mp, "tcp_alive", lambda s, timeout=3.0: True)
    egress = {"socks5://100.100.0.1:1080": "212.1.1.1", "socks5://100.100.0.2:1080": "212.2.2.2"}
    st = mp.status(force=True, echo=lambda s: egress.get(s))
    assert st["enabled"] and st["n_configured"] == 2 and st["n_online"] == 2
    assert set(st["egress_samples"]) == {"212.1.1.1", "212.2.2.2"}
    assert st["tailnet"] == "jf-net.ts.net"


def test_residential_slots_include_the_phones(tmp_path, monkeypatch):
    from backend.tools import proxy_pool
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(proxy_pool, "_RES_COUNT", 0)             # no chisel loopback slots
    proxy_pool._res_cache.update(ts=0.0, slots=[])
    monkeypatch.setattr(mp, "_tailscale_status_json", lambda timeout=4.0: _TS)
    mp.update(enabled=True)
    monkeypatch.setattr(mp, "tcp_alive", lambda s, timeout=3.0: True)
    slots = proxy_pool.residential_slots()
    assert set(slots) == {"socks5://100.100.0.1:1080", "socks5://100.100.0.2:1080"}
    assert proxy_pool.residential_up()
    proxy_pool._res_cache.update(ts=0.0, slots=[])
    monkeypatch.setattr(mp, "tcp_alive", lambda s, timeout=3.0: False)
    mp._LIVE_CACHE.update(ts=0.0, servers=[])
    assert proxy_pool.residential_slots() == [] and not proxy_pool.residential_up()


def test_egress_candidates_rotate_then_pool_then_direct(tmp_path, monkeypatch):
    from backend.tools import proxy_pool
    monkeypatch.setattr(proxy_pool, "residential_slots",
                        lambda: ["socks5://100.0.0.1:1080", "socks5://100.0.0.2:1080"])
    monkeypatch.setattr(proxy_pool, "_pool_pick", lambda: {"server": "http://dc:1", "username": None, "password": None})
    proxy_pool._res_cursor = 0
    c1 = proxy_pool.egress_candidates()
    assert [x["server"] if x else None for x in c1] == \
        ["socks5://100.0.0.1:1080", "socks5://100.0.0.2:1080", "http://dc:1", None]
    c2 = proxy_pool.egress_candidates()          # next fill starts on the OTHER phone
    assert c2[0]["server"] == "socks5://100.0.0.2:1080"
    assert c2[-1] is None                        # always ends with direct


def test_join_argv_builder_and_masking():
    argv = mp.join_argv("tskey-CRoNSecret123")
    assert "sudo" in argv and "up" in argv
    assert "--authkey=tskey-CRoNSecret123" in argv
    assert "--advertise-exit-node=false" in argv and "--accept-routes=false" in argv
    assert "--reset" in argv and "--ssh=false" in argv and "--hostname=jobfinder-server" in argv
    masked = mp._mask_key(argv)
    assert "--authkey=***" in masked
    assert not any("tskey-CRoNSecret123" in a for a in masked)     # key never echoed
    assert mp.logout_argv() == ["sudo", "tailscale", "logout"]


def test_do_fill_rotates_phones_then_falls_back(tmp_path, monkeypatch):
    """The fill payload carries a phone proxy; a co-pilot TRANSPORT failure on the first phone
    moves to the next before the pool/direct. No real fill: httpx.post is captured."""
    import httpx

    from backend import dashboard_app as d
    from backend.tools import catalog_drafts, proxy_pool
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(catalog_drafts, "ensure_and_wire", lambda *a, **k: ("demo_x", 20031, True))
    monkeypatch.setattr(proxy_pool, "egress_candidates", lambda: [
        {"server": "socks5://100.0.0.1:1080", "username": None, "password": None},
        {"server": "socks5://100.0.0.2:1080", "username": None, "password": None}, None])
    seen = []

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"filled": 1, "unfilled": 0, "submit_result": {"clicked": True}}

    def fake_post(url, data=None, timeout=None):
        data = dict(data or {})
        seen.append((url, data))
        if url.endswith("/load") and data.get("proxy_server") == "socks5://100.0.0.1:1080":
            raise httpx.ConnectError("phone 1 down")      # transport failure -> try the next
        return _R()

    monkeypatch.setattr(httpx, "post", fake_post)
    d._do_fill(20031, None, "Dana Erlan", "dana.erlan1@takhet.com", "demo_x", wait_submit=True)
    loads = [x[1] for x in seen if x[0].endswith("/load")]
    assert loads[0]["proxy_server"] == "socks5://100.0.0.1:1080"      # tried phone 1
    assert loads[-1]["proxy_server"] == "socks5://100.0.0.2:1080"     # succeeded on phone 2
    assert loads[-1]["wait_submit"] == "1" and d._FILL_JOBS[20031]["state"] == "done"


def test_health_row_states(tmp_path, monkeypatch):
    from backend.tools import health
    _use_tmp(tmp_path, monkeypatch)
    name = "Мобильные прокси (телефоны)"
    monkeypatch.setattr(mp, "status", lambda force=False, echo=None: {"configured": False, "tailnet": "jf-net.ts.net"})
    assert {r["name"]: r for r in health.proxy_rows()}[name]["status"] == "info"
    monkeypatch.setattr(mp, "status", lambda force=False, echo=None:
                        {"configured": True, "enabled": True, "n_online": 0, "n_configured": 2, "tailnet": "jf-net.ts.net"})
    assert {r["name"]: r for r in health.proxy_rows()}[name]["status"] == "warn"
    monkeypatch.setattr(mp, "status", lambda force=False, echo=None:
                        {"configured": True, "enabled": True, "n_online": 2, "n_configured": 3,
                         "egress_samples": ["5.6.7.8", "9.10.11.12"], "tailnet": "jf-net.ts.net"})
    r = {r["name"]: r for r in health.proxy_rows()}[name]
    assert r["status"] == "ok" and "5.6.7.8" in r["detail"] and "2 онлайн из 3" in r["detail"]
