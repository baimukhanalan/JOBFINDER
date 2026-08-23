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
