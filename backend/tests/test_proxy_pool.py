"""Pure parsing/rotation tests for proxy_pool (no network)."""
import backend.tools.proxy_pool as pp


def test_colon_form_host_port_user_pass():
    (p,) = pp.parse_proxies("1.2.3.4:8080:alice:secret")
    assert p["scheme"] == "http"
    assert p["server"] == "http://1.2.3.4:8080"
    assert p["username"] == "alice" and p["password"] == "secret"


def test_at_form_with_and_without_scheme():
    (a,) = pp.parse_proxies("alice:secret@1.2.3.4:8080")
    assert a["server"] == "http://1.2.3.4:8080" and a["username"] == "alice"
    (b,) = pp.parse_proxies("http://bob:pw@5.6.7.8:3128")
    assert b["server"] == "http://5.6.7.8:3128" and b["username"] == "bob"


def test_socks5_scheme_preserved():
    (p,) = pp.parse_proxies("socks5://9.9.9.9:1080")
    assert p["scheme"] == "socks5"
    assert p["server"] == "socks5://9.9.9.9:1080"
    assert "username" not in p


def test_bare_host_port_no_auth():
    (p,) = pp.parse_proxies("10.0.0.1:8000")
    assert p["server"] == "http://10.0.0.1:8000" and "username" not in p


def test_blank_comment_and_bad_lines_dropped():
    out = pp.parse_proxies("\n# comment\nnotaproxy\n1.2.3.4:99999\n1.2.3.4:8080\n")
    # 99999 is out of range, 'notaproxy' has no port -> only the last survives
    assert [p["server"] for p in out] == ["http://1.2.3.4:8080"]


def test_dedup_on_server_and_user():
    out = pp.parse_proxies("1.2.3.4:8080:u:p\n1.2.3.4:8080:u:p\n1.2.3.4:8080:v:p")
    assert len(out) == 2  # same server+user collapses; different user kept


def test_next_proxy_round_robin_and_empty(tmp_path, monkeypatch):
    store = tmp_path / "proxies.json"
    monkeypatch.setattr(pp, "_STORE", store)
    assert pp.next_proxy() is None                       # empty pool
    pp._save({"proxies": [{"server": "http://a:1"}, {"server": "http://b:2"}],
              "cursor": 0})
    servers = [pp.next_proxy()["server"] for _ in range(3)]
    assert servers == ["http://a:1", "http://b:2", "http://a:1"]  # wraps around


# ---- mass-hiring apply-lane egress (phones only; Mac excluded) --------------------------------

_PHONE0 = "socks5://127.0.0.1:10800"
_PHONE1 = "socks5://127.0.0.1:10801"
_MAC = "socks5://127.0.0.1:10802"


def _fake_running_slots():
    return [
        {"slot": "0", "server": _PHONE0, "hostname": "jf-egress-0", "note": "localhost"},
        {"slot": "1", "server": _PHONE1, "hostname": "jf-egress-1", "note": "localhost"},
        {"slot": "2", "server": _MAC, "hostname": "jf-egress-2", "note": "MacBook Air — Alan"},
    ]


def _patch_slots(monkeypatch, servers):
    """residential_slots() returns `servers`; running_slots() carries the Mac's hostname/note."""
    monkeypatch.setattr(pp, "residential_slots", lambda: list(servers))
    import backend.tools.tailscale_egress as te
    monkeypatch.setattr(te, "running_slots", lambda timeout=1.0: _fake_running_slots())


def test_apply_slots_excludes_mac_by_note(monkeypatch):
    _patch_slots(monkeypatch, [_PHONE0, _PHONE1, _MAC])
    monkeypatch.delenv("APPLY_EGRESS_EXCLUDE", raising=False)
    assert pp.apply_slots() == [_PHONE0, _PHONE1]        # Mac (note "MacBook Air") dropped


def test_apply_slots_drops_non_socks(monkeypatch):
    _patch_slots(monkeypatch, [_PHONE0, "http://5.6.7.8:8080", _MAC])
    monkeypatch.delenv("APPLY_EGRESS_EXCLUDE", raising=False)
    assert pp.apply_slots() == [_PHONE0]                 # http proxy + Mac both dropped


def test_apply_slots_exclude_disabled(monkeypatch):
    _patch_slots(monkeypatch, [_PHONE0, _PHONE1, _MAC])
    monkeypatch.setenv("APPLY_EGRESS_EXCLUDE", "")       # exclude nothing → Mac allowed back in
    assert pp.apply_slots() == [_PHONE0, _PHONE1, _MAC]


def test_apply_proxy_none_when_no_phone(monkeypatch):
    _patch_slots(monkeypatch, [_MAC])                    # only the Mac live
    monkeypatch.delenv("APPLY_EGRESS_EXCLUDE", raising=False)
    assert pp.apply_proxy() is None                      # Mac excluded → nothing → DIRECT


def test_apply_proxy_round_robin(monkeypatch):
    _patch_slots(monkeypatch, [_PHONE0, _PHONE1, _MAC])
    monkeypatch.delenv("APPLY_EGRESS_EXCLUDE", raising=False)
    pp._apply_cursor = 0
    got = [pp.apply_proxy()["server"] for _ in range(4)]
    assert got == [_PHONE0, _PHONE1, _PHONE0, _PHONE1]   # wraps over phones, never the Mac


def test_lane_egress_default_is_direct(monkeypatch):
    # NEITHER env set → DIRECT (never route a working lane through a KZ phone by default).
    _patch_slots(monkeypatch, [_PHONE0, _PHONE1])
    monkeypatch.delenv("TALEO_RESIDENTIAL", raising=False)
    monkeypatch.delenv("TALEO_PROXY", raising=False)
    assert pp.lane_egress("TALEO_RESIDENTIAL", "TALEO_PROXY") is None


def test_lane_egress_proxy_env_forced_direct(monkeypatch):
    _patch_slots(monkeypatch, [_PHONE0, _PHONE1])
    monkeypatch.setenv("TALEO_RESIDENTIAL", "1")             # even with opt-in on…
    for v in ("", "direct", "none", "off", "0", "false", "DIRECT"):
        monkeypatch.setenv("TALEO_PROXY", v)                # …an explicit direct wins
        assert pp.lane_egress("TALEO_RESIDENTIAL", "TALEO_PROXY") is None


def test_lane_egress_explicit_url_wins(monkeypatch):
    monkeypatch.setenv("TALEO_PROXY", "socks5://127.0.0.1:19999")   # e.g. a US slot
    assert pp.lane_egress("TALEO_RESIDENTIAL", "TALEO_PROXY") == {"server": "socks5://127.0.0.1:19999"}


def test_lane_egress_opt_in_prefers_phone(monkeypatch):
    _patch_slots(monkeypatch, [_PHONE0, _PHONE1, _MAC])
    monkeypatch.setenv("TALEO_RESIDENTIAL", "1")
    monkeypatch.delenv("TALEO_PROXY", raising=False)
    monkeypatch.delenv("APPLY_EGRESS_EXCLUDE", raising=False)
    pp._apply_cursor = 0
    assert pp.lane_egress("TALEO_RESIDENTIAL", "TALEO_PROXY")["server"] == _PHONE0   # phone, not the Mac


def test_lane_egress_opt_in_direct_when_no_phone(monkeypatch):
    _patch_slots(monkeypatch, [])                        # no residential at all
    monkeypatch.setenv("TALEO_RESIDENTIAL", "1")
    monkeypatch.delenv("TALEO_PROXY", raising=False)
    assert pp.lane_egress("TALEO_RESIDENTIAL", "TALEO_PROXY") is None
