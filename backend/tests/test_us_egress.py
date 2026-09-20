"""Network-free tests for the US-IP egress resolver (backend/tools/us_egress.py).

Every network call (free-source fetch, geo-validate probe, Bright Data builder) is
monkeypatched, so the priority chain is exercised without touching the wire.
"""
import backend.tools.us_egress as ue


def _clear_env(monkeypatch):
    for k in ("US_PROXY", "WEBSHARE_API_KEY", "US_EGRESS_CACHE_TTL",
              "LANE_US", "LANE_PROXY"):
        monkeypatch.delenv(k, raising=False)


def _isolate_cache(monkeypatch, tmp_path):
    """Point the validation cache at a fresh tmp file (starts empty)."""
    monkeypatch.setattr(ue, "_CACHE", tmp_path / "us_egress_cache.json")


# ---- 1. explicit override --------------------------------------------------
def test_override_http_url_wins(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    # free/bd must NOT be consulted when an explicit URL is set.
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: (_ for _ in ()).throw(AssertionError("free called")))
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: (_ for _ in ()).throw(AssertionError("bd called")))
    monkeypatch.setenv("US_PROXY", "http://user:secret@9.9.9.9:8080")
    assert ue.us_proxy() == {"server": "http://9.9.9.9:8080",
                             "username": "user", "password": "secret"}


def test_override_socks5_url(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("US_PROXY", "socks5://1.2.3.4:1080")
    assert ue.us_proxy() == {"server": "socks5://1.2.3.4:1080"}


def test_override_direct_and_empty_force_none(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    # If direct is forced, neither the free nor BD tier may run.
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: (_ for _ in ()).throw(AssertionError("free called")))
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: (_ for _ in ()).throw(AssertionError("bd called")))
    for v in ("", "direct", "none", "off", "0", "false", "DIRECT"):
        monkeypatch.setenv("US_PROXY", v)
        assert ue.us_proxy() is None


def test_override_unparseable_falls_through_to_free(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("US_PROXY", "not a proxy at all")
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: {"server": "http://free:1"})
    assert ue.us_proxy() == {"server": "http://free:1"}


# ---- 2/3/4. priority: free > BD > direct -----------------------------------
def test_free_wins_over_bd(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: {"server": "http://free:1"})
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: {"server": "http://bd:2"})
    assert ue.us_proxy() == {"server": "http://free:1"}


def test_bd_used_when_free_fails(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: None)
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: {"server": "http://bd:2",
                                                     "username": "u", "password": "p"})
    assert ue.us_proxy() == {"server": "http://bd:2", "username": "u", "password": "p"}


def test_direct_when_all_fail(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: None)
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: None)
    assert ue.us_proxy() is None


# ---- free source geo-validation --------------------------------------------
def test_free_rejects_non_us(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    cand = {"server": "http://5.5.5.5:8080"}
    monkeypatch.setattr(ue, "_fetch_free_candidates", lambda limit=8: [cand])
    monkeypatch.setattr(ue, "_egress_country", lambda c: "DE")   # geolocates NON-US → rejected
    assert ue._free_us_proxy() is None
    # nothing cached
    assert ue._cache_read() is None


def test_free_accepts_us_and_caches(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    cand = {"server": "http://7.7.7.7:8080"}
    monkeypatch.setattr(ue, "_fetch_free_candidates", lambda limit=8: [cand])
    monkeypatch.setattr(ue, "_egress_country", lambda c: "US")
    assert ue._free_us_proxy() == cand
    # cached: a second call returns it WITHOUT re-fetching (fetch now raises).
    monkeypatch.setattr(ue, "_fetch_free_candidates",
                        lambda limit=8: (_ for _ in ()).throw(AssertionError("refetched")))
    assert ue._free_us_proxy() == cand


def test_free_skips_dead_tries_next(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    dead = {"server": "http://dead:1"}
    good = {"server": "http://good:2"}
    monkeypatch.setattr(ue, "_fetch_free_candidates", lambda limit=8: [dead, good])
    monkeypatch.setattr(ue, "_egress_country",
                        lambda c: "US" if c["server"] == "http://good:2" else None)
    assert ue._free_us_proxy() == good


def test_free_geo_validate_helper(monkeypatch):
    monkeypatch.setattr(ue, "_egress_country", lambda c: "US")
    assert ue._geo_validate({"server": "x"}) is True
    monkeypatch.setattr(ue, "_egress_country", lambda c: "CA")
    assert ue._geo_validate({"server": "x"}) is False
    monkeypatch.setattr(ue, "_egress_country", lambda c: None)   # dead
    assert ue._geo_validate({"server": "x"}) is False


# ---- Bright Data US pinning ------------------------------------------------
def test_bd_forces_us_country_pin(monkeypatch):
    import backend.tools.brightdata_proxies as bd
    # Config present but pinned to a NON-US country → us_egress must still force US.
    monkeypatch.setattr(bd, "_cfg", lambda: {
        "token": "", "customer": "cust", "zone": "alibaba_dc", "password": "pw",
        "host": "brd.superproxy.io", "port": 33335, "country": "de"})
    monkeypatch.setattr(bd, "_missing", lambda cfg: [])
    got = ue._bd_us_proxy()
    assert got is not None
    assert got["server"] == "http://brd.superproxy.io:33335"
    assert "-country-us-" in got["username"]          # forced US, not "de"
    assert "country-de" not in got["username"]


def test_bd_none_when_unconfigured(monkeypatch):
    import backend.tools.brightdata_proxies as bd
    monkeypatch.setattr(bd, "_missing", lambda cfg: ["customer", "zone", "password"])
    assert ue._bd_us_proxy() is None


# ---- lane opt-in wrapper ----------------------------------------------------
def test_lane_us_egress_default_direct(monkeypatch):
    _clear_env(monkeypatch)
    assert ue.lane_us_egress("LANE_US", "LANE_PROXY") is None


def test_lane_us_egress_opt_in_uses_resolver(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LANE_US", "1")
    monkeypatch.setattr(ue, "us_proxy", lambda: {"server": "http://free:1"})
    assert ue.lane_us_egress("LANE_US", "LANE_PROXY") == {"server": "http://free:1"}


def test_lane_us_egress_proxy_env_is_hard_override(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LANE_US", "1")               # opt-in on…
    # …but an explicit proxy URL wins and is terminal.
    monkeypatch.setenv("LANE_PROXY", "http://u:p@2.2.2.2:3128")
    assert ue.lane_us_egress("LANE_US", "LANE_PROXY") == {
        "server": "http://2.2.2.2:3128", "username": "u", "password": "p"}
    # explicit DIRECT is terminal too (does NOT fall through to the resolver).
    monkeypatch.setattr(ue, "us_proxy", lambda: (_ for _ in ()).throw(AssertionError("resolver called")))
    for v in ("", "direct", "none", "off", "0", "false"):
        monkeypatch.setenv("LANE_PROXY", v)
        assert ue.lane_us_egress("LANE_US", "LANE_PROXY") is None


def test_kz_phone_never_returned(monkeypatch, tmp_path):
    """us_proxy() must not consult residential_slots (the KZ phones)."""
    _clear_env(monkeypatch)
    _isolate_cache(monkeypatch, tmp_path)
    import backend.tools.proxy_pool as pp
    monkeypatch.setattr(pp, "residential_slots",
                        lambda: (_ for _ in ()).throw(AssertionError("residential_slots consulted")))
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: None)
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: None)
    assert ue.us_proxy() is None
