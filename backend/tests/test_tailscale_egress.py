"""«Exit-node egress-мост» (tailscale_egress.py) — pure, no network/subprocess. Settings roundtrip +
slot key coercion, exit-node peer discovery from a fixture, the pure argv builders + key masking,
live_socks/running_slots filtering, the up() bring-up (mocked subprocess), sync() reconcile, the
_teardown pkill pattern (must match the FULL socket path, not a prefix), and the proxy_pool merge.
Run:  PYTHONPATH=. python3 -m pytest backend/tests/test_tailscale_egress.py -q
"""
import json

from backend.tools import tailscale_egress as te


def _use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "_PATH", tmp_path / "ts_egress.json")
    monkeypatch.setattr(te, "_STATE_ROOT", tmp_path / "ts-egress")
    te._CACHE.update(ts=0.0, status=None)
    te._RUN_CACHE.update(ts=0.0, slots=[])


# `tailscale status --json`: 2 online exit-node peers + 1 online non-exit + 1 offline exit-node
_TS = {
    "Self": {"HostName": "jobfinder-server", "TailscaleIPs": ["100.79.101.77"], "Online": True},
    "Peer": {
        "k1": {"HostName": "iphone-dana", "DNSName": "iphone-dana.ts.net.", "TailscaleIPs": ["100.100.0.1"],
               "Online": True, "OS": "iOS", "ExitNodeOption": True},
        "k2": {"HostName": "android-anchor", "DNSName": "android-anchor.ts.net.", "TailscaleIPs": ["100.100.0.2"],
               "Online": True, "OS": "android", "ExitNodeOption": True},
        "k3": {"HostName": "laptop", "DNSName": "laptop.ts.net.", "TailscaleIPs": ["100.100.0.3"],
               "Online": True, "OS": "linux", "ExitNodeOption": False},           # online but not an exit node
        "k4": {"HostName": "iphone-old", "DNSName": "iphone-old.ts.net.", "TailscaleIPs": ["100.100.0.4"],
               "Online": False, "OS": "iOS", "ExitNodeOption": True},             # exit node but offline
    },
}


def test_settings_roundtrip_and_slot_key_coercion(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    assert te.load()["enabled"] is False and te.load()["base_port"] == 10800 and te.load()["slots"] == {}
    d = te.load()
    d["slots"][0] = {"port": 10800, "exit_ip": "100.100.0.1", "hostname": "jf-egress-0", "note": "iPhone"}
    te.save(d)
    # reload: JSON stringifies the int slot key, _normalize coerces it back to int
    d2 = te.load()
    assert set(d2["slots"]) == {0} and isinstance(next(iter(d2["slots"])), int)
    assert d2["slots"][0]["exit_ip"] == "100.100.0.1"
    assert te.set_enabled(True)["enabled"] is True and te.load()["enabled"] is True
    assert te.slot_port(3) == 10803


def test_exit_node_peers_only_online_advertised(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(te, "_tailscale_status_json", lambda timeout=4.0: _TS)
    peers = te.exit_node_peers()
    ips = {p["ip"] for p in peers}
    assert ips == {"100.100.0.1", "100.100.0.2"}          # non-exit + offline excluded
    assert all(p["online"] for p in peers)
    # a failure yields [] not a raise
    monkeypatch.setattr(te, "_tailscale_status_json", lambda timeout=4.0: {})
    assert te.exit_node_peers() == []


def test_pure_argv_builders_and_key_masking(tmp_path):
    up = te.up_argv("/sd/0", 10800)
    assert "-tun=userspace-networking" in up
    assert "-socks5-server=127.0.0.1:10800" in up          # bound to loopback ONLY (security rule 1)
    assert "-socket=/sd/0/tailscaled.sock" in up
    join = te.join_argv("/sd/0", "tskey-auth-SECRET123", "jf-egress-0", "100.100.0.1")
    assert "--authkey=tskey-auth-SECRET123" in join
    assert "--exit-node=100.100.0.1" in join and "--exit-node-allow-lan-access" in join
    masked = te._mask_key(join)
    assert "--authkey=***" in masked
    assert not any("SECRET123" in a for a in masked)       # key never echoed
    assert "SECRET123" not in te._scrub("boot --authkey=tskey-auth-SECRET123 done")


def test_live_socks_gated_by_enabled_and_tcp(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    # a running slot: its state-dir socket file exists + the SOCKS port is tcp_alive
    sd = te._statedir(0)
    sd.mkdir(parents=True)
    (sd / "tailscaled.sock").write_text("")
    d = te.load()
    d["slots"][0] = {"port": 10800, "exit_ip": "100.100.0.1", "hostname": "jf-egress-0", "note": ""}
    te.save(d)
    monkeypatch.setattr(te, "tcp_alive", lambda s, timeout=1.0: True)
    # disabled -> nothing, even though the slot answers
    assert te.live_socks() == []
    te.set_enabled(True)
    te._RUN_CACHE.update(ts=0.0, slots=[])
    assert te.running_slots()[0]["server"] == "socks5://127.0.0.1:10800"
    assert te.live_socks() == ["socks5://127.0.0.1:10800"]
    # port stops answering -> dropped from the pool
    monkeypatch.setattr(te, "tcp_alive", lambda s, timeout=1.0: False)
    te._RUN_CACHE.update(ts=0.0, slots=[])
    assert te.live_socks() == []


def test_up_records_slot_and_returns_server(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)

    class _Proc:
        def poll(self):
            return None                                    # still running

    def fake_popen(argv, **kw):
        # userspace tailscaled "opens its socket": create the file the bring-up waits for
        sd = [a.split("=", 1)[1] for a in argv if a.startswith("-statedir=")][0]
        (te.Path(sd) / "tailscaled.sock").write_text("")
        return _Proc()

    class _R:
        returncode = 0
        stdout = "Success."
        stderr = ""

    monkeypatch.setattr(te.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(te.subprocess, "run", lambda *a, **k: _R())
    rec = te.up("100.100.0.1", "tskey-auth-SECRET123", note="iPhone Dana")
    assert rec and rec["slot"] == 0 and rec["server"] == "socks5://127.0.0.1:10800"
    assert rec["exit_ip"] == "100.100.0.1" and rec["note"] == "iPhone Dana"
    assert te.load()["slots"][0]["exit_ip"] == "100.100.0.1"          # persisted
    assert "SECRET123" not in json.dumps(rec)                          # key never surfaces
    # a second up() takes the next free slot
    rec2 = te.up("100.100.0.2", "tskey-auth-SECRET123")
    assert rec2["slot"] == 1 and rec2["server"] == "socks5://127.0.0.1:10801"
    # missing exit_ip / key -> None, no slot
    assert te.up("", "k") is None and te.up("1.2.3.4", "") is None


def test_up_failure_tears_down_the_half_started_slot(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)

    class _Proc:
        def poll(self):
            return None

    def fake_popen(argv, **kw):
        sd = [a.split("=", 1)[1] for a in argv if a.startswith("-statedir=")][0]
        (te.Path(sd) / "tailscaled.sock").write_text("")
        return _Proc()

    class _R:
        returncode = 1
        stdout = ""
        stderr = "backend error: --authkey=tskey-auth-SECRET123 rejected"

    killed = []
    monkeypatch.setattr(te.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(te.subprocess, "run", lambda argv, **k: killed.append(argv) or _R())
    assert te.up("100.100.0.1", "tskey-auth-SECRET123") is None
    assert te.load()["slots"] == {}                         # slot reservation rolled back
    # teardown matched the FULL socket path (not a prefix that would hit sibling slots)
    pk = [a for a in killed if a[:2] == ["pkill", "-f"]]
    assert pk and pk[0][2].endswith("/0/tailscaled.sock")


def test_sync_brings_up_desired_and_reaps_stale(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(te, "exit_node_peers",
                        lambda: [{"ip": "100.100.0.1", "name": "iphone-dana", "os": "iOS", "online": True},
                                 {"ip": "100.100.0.2", "name": "android", "os": "android", "online": True}])
    # pre-seed: slot 0 pins .1 (still desired -> kept), slot 5 pins .9 (gone -> reaped)
    d = te.load()
    d["slots"][0] = {"port": 10800, "exit_ip": "100.100.0.1", "hostname": "jf-egress-0", "note": ""}
    d["slots"][5] = {"port": 10805, "exit_ip": "100.100.0.9", "hostname": "jf-egress-5", "note": ""}
    te.save(d)
    ups, downs = [], []
    monkeypatch.setattr(te, "up", lambda ip, key, note="": ups.append(ip) or {"slot": 1, "server": "x"})
    monkeypatch.setattr(te, "down", lambda slot: downs.append(slot) or True)
    summary = te.sync("tskey-auth-SECRET123")
    assert ups == ["100.100.0.2"]                           # only the missing desired peer
    assert downs == [5]                                     # the stale slot reaped
    assert set(summary["desired"]) == {"100.100.0.1", "100.100.0.2"}
    assert summary["brought_up"] == ["100.100.0.2"] and summary["reaped"] == ["100.100.0.9"]
    assert summary["kept"] == ["100.100.0.1"]


def test_check_reports_egress_per_slot(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(te, "running_slots", lambda timeout=1.0: [
        {"slot": 0, "server": "socks5://127.0.0.1:10800", "port": 10800,
         "exit_ip": "100.100.0.1", "hostname": "jf-egress-0", "note": "iPhone"}])
    monkeypatch.setattr(te, "asn_of", lambda ip, timeout=5.0: "AS12345 CarrierMobile · US")
    rows = te.check(echo=lambda s: "77.88.99.100")
    assert rows[0]["egress"] == "77.88.99.100" and "CarrierMobile" in rows[0]["asn"]


def test_proxy_pool_merges_the_bridge(tmp_path, monkeypatch):
    from backend.tools import mobile_proxy, proxy_pool
    monkeypatch.setattr(proxy_pool, "_RES_COUNT", 0)                 # no chisel loopback slots
    proxy_pool._res_cache.update(ts=0.0, slots=[])
    monkeypatch.setattr(mobile_proxy, "live_servers", lambda: [])
    monkeypatch.setattr(te, "live_socks", lambda: ["socks5://127.0.0.1:10800", "socks5://127.0.0.1:10801"])
    slots = proxy_pool.residential_slots()
    assert set(slots) == {"socks5://127.0.0.1:10800", "socks5://127.0.0.1:10801"}
    # dedup: the same server from both sources appears once
    proxy_pool._res_cache.update(ts=0.0, slots=[])
    monkeypatch.setattr(mobile_proxy, "live_servers", lambda: ["socks5://127.0.0.1:10800"])
    slots2 = proxy_pool.residential_slots()
    assert slots2.count("socks5://127.0.0.1:10800") == 1
