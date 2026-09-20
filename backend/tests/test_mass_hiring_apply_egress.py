"""Egress-plan tests for the co-pilot mass-hiring lane (Maximus/Kelly). Pure, no network."""
import backend.tools.mass_hiring_apply as mha
import backend.tools.proxy_pool as pp

_KELLY = {"apply_url": "https://www.mykelly.com/job/123-remote-csr"}
_MAX = {"apply_url": "https://maximus.avature.net/careers/JobDetail/456"}
_PHONE0 = "socks5://127.0.0.1:10800"
_PHONE1 = "socks5://127.0.0.1:10801"


def test_kelly_uses_datacenter_pool_retry_all(monkeypatch):
    monkeypatch.setattr(pp, "_pool_pick", lambda: {"server": "http://dc:1", "username": "u",
                                                   "password": "p"})
    plan, retry_all = mha._egress_plan(_KELLY)
    assert retry_all is True
    assert len(plan) == 3 and all(p and p["server"] == "http://dc:1" for p in plan)


def test_maximus_default_is_direct(monkeypatch):
    # No opt-in → DIRECT (Maximus is a working lane; never route it through a KZ phone by default).
    monkeypatch.delenv("MH_COPILOT_PROXY", raising=False)
    monkeypatch.delenv("MH_COPILOT_RESIDENTIAL", raising=False)
    plan, retry_all = mha._egress_plan(_MAX, "demo_x@takhet.com")
    assert plan == [None] and retry_all is False


def test_maximus_opt_in_prefers_phone_then_direct(monkeypatch):
    monkeypatch.setenv("MH_COPILOT_RESIDENTIAL", "1")
    monkeypatch.delenv("MH_COPILOT_PROXY", raising=False)
    seq = iter([{"server": _PHONE0}, {"server": _PHONE1}])
    monkeypatch.setattr(pp, "apply_proxy", lambda name="": next(seq, None))
    plan, retry_all = mha._egress_plan(_MAX, "demo_x@takhet.com")
    assert retry_all is False
    assert [(p or {}).get("server") for p in plan] == [_PHONE0, _PHONE1, None]  # ends DIRECT


def test_maximus_opt_in_no_phone_falls_direct(monkeypatch):
    monkeypatch.setenv("MH_COPILOT_RESIDENTIAL", "1")
    monkeypatch.delenv("MH_COPILOT_PROXY", raising=False)
    monkeypatch.setattr(pp, "apply_proxy", lambda name="": None)
    plan, retry_all = mha._egress_plan(_MAX)
    assert plan == [None] and retry_all is False


def test_maximus_forced_direct(monkeypatch):
    monkeypatch.setenv("MH_COPILOT_RESIDENTIAL", "1")       # opt-in on…
    monkeypatch.setenv("MH_COPILOT_PROXY", "direct")        # …but explicit direct wins
    plan, retry_all = mha._egress_plan(_MAX)
    assert plan == [None] and retry_all is False


def test_maximus_forced_url(monkeypatch):
    monkeypatch.delenv("MH_COPILOT_RESIDENTIAL", raising=False)
    monkeypatch.setenv("MH_COPILOT_PROXY", "socks5://127.0.0.1:19999")   # e.g. a US slot
    plan, retry_all = mha._egress_plan(_MAX)
    assert plan == [{"server": "socks5://127.0.0.1:19999"}, None] and retry_all is False


def test_maximus_opt_in_dedupes_same_phone(monkeypatch):
    monkeypatch.setenv("MH_COPILOT_RESIDENTIAL", "1")
    monkeypatch.delenv("MH_COPILOT_PROXY", raising=False)
    monkeypatch.setattr(pp, "apply_proxy", lambda name="": {"server": _PHONE0})  # only one phone live
    plan, _ = mha._egress_plan(_MAX)
    assert [(p or {}).get("server") for p in plan] == [_PHONE0, None]  # no duplicate phone
