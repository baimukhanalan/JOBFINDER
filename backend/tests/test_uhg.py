"""Network-free tests for the UnitedHealth Group / Optum recon + wall-watch probe.

UHG is BLOCKED (workforce Azure AD SSO, no candidate self-registration — see uhg_recon). These tests
lock in: the external Taleo apply-URL resolver, the SSO-vs-native landing classifier, the native
self-register-form detector (the single signal the probe watches for), the probe verdict, and the
promote/block decision gate. All PURE — no network, no browser.
"""
from backend.tools import uhg_recon
from backend.tools import uhg_probe_promote as pp


# ---- resolve_apply_url ------------------------------------------------------------------------

def test_resolve_apply_url_prefers_external_10020():
    html = ('<a href="https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=2384956">internal</a>'
            ' data-apply-url="https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=2384956"')
    assert (uhg_recon.resolve_apply_url(html)
            == "https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=2384956")


def test_resolve_apply_url_none_when_absent():
    assert uhg_recon.resolve_apply_url("<html>no taleo here</html>") is None
    assert uhg_recon.resolve_apply_url("") is None


def test_resolve_apply_url_falls_back_to_first_when_no_external():
    html = '<a href="https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=7">i</a>'
    assert uhg_recon.resolve_apply_url(html).endswith("careersection/10000/jobapply.ftl?job=7")


# ---- is_sso_url / classify_landing ------------------------------------------------------------

def test_is_sso_url_matches_azure_and_pingfederate_and_msa():
    assert uhg_recon.is_sso_url(
        "https://login.microsoftonline.com/db05faca-c82a-4b9d-b9c5-0f64b6755421/oauth2/v2.0/authorize")
    assert uhg_recon.is_sso_url("https://authgateway3.entiam.uhg.com/ext/microsoft-authn")
    assert uhg_recon.is_sso_url("https://login.live.com/oauth20_authorize?client_id=x")
    assert uhg_recon.is_sso_url("https://uhg.b2clogin.com/tfp/x/oauth2/authorize")


def test_is_sso_url_rejects_plain_taleo_and_radancy():
    assert not uhg_recon.is_sso_url("https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=1")
    assert not uhg_recon.is_sso_url("https://careers.unitedhealthgroup.com/job/x/y/34088/1")
    assert not uhg_recon.is_sso_url("")


def test_classify_landing():
    assert uhg_recon.classify_landing(
        "https://login.microsoftonline.com/db05faca/oauth2/v2.0/authorize") == "azure_sso"
    assert uhg_recon.classify_landing("https://authgateway3.entiam.uhg.com/ext/x") == "azure_sso"
    assert uhg_recon.classify_landing(
        "https://uhg.taleo.net/careersection/10020/createprofile.ftl") == "taleo_native"
    assert uhg_recon.classify_landing(
        "https://careers.unitedhealthgroup.com/search-jobs/") == "radancy"
    assert uhg_recon.classify_landing("https://example.com/") == "unknown"


# ---- is_native_register_form (the wall-drop signal) -------------------------------------------

_AZURE_SIGNIN = (  # an Azure sign-in page HAS email+password but is the SSO wall, NOT a self-register
    '<form><input name="loginfmt" type="email"><input name="passwd" type="password">'
    '<a>Create one!</a> urlMsaSignup=https://login.live.com/oauth20_authorize</form>')

_NATIVE_TALEO_REGISTER = (  # a genuine Taleo New-User self-registration form
    '<h1>Create an account</h1><form>'
    '<input id="new-user-email" type="email" name="EmailAddress">'
    '<input id="new-user-pw" type="password" name="password">'
    '<input id="new-user-pw2" type="password" name="passwordConfirm"></form>')


def test_azure_signin_is_not_a_native_register():
    # even standalone HTML, the MSA/microsoftonline markers veto it
    assert not uhg_recon.is_native_register_form(_AZURE_SIGNIN, "")
    # and on the actual Azure URL it is doubly rejected
    assert not uhg_recon.is_native_register_form(
        _AZURE_SIGNIN, "https://login.microsoftonline.com/db05faca/oauth2/v2.0/authorize")


def test_native_taleo_register_is_detected():
    assert uhg_recon.is_native_register_form(
        _NATIVE_TALEO_REGISTER, "https://uhg.taleo.net/careersection/iam/accessmanagement/newRegister.jsf")


def test_empty_or_privacy_page_is_not_a_register():
    assert not uhg_recon.is_native_register_form("", "")
    # the Privacy Agreement page has no password + no register wording
    assert not uhg_recon.is_native_register_form(
        "<title>Privacy Agreement</title><input type=button value='I Accept'>", "")


# ---- probe_verdict ----------------------------------------------------------------------------

def test_probe_verdict_blocked_on_azure():
    v, _ = uhg_recon.probe_verdict(
        "https://login.microsoftonline.com/db05faca/oauth2/v2.0/authorize", _AZURE_SIGNIN)
    assert v == "blocked_sso"


def test_probe_verdict_blocked_on_pingfederate_handoff():
    v, _ = uhg_recon.probe_verdict("https://authgateway3.entiam.uhg.com/ext/microsoft-authn", "")
    assert v == "blocked_sso"


def test_probe_verdict_self_register_open_flips_it():
    v, _ = uhg_recon.probe_verdict(
        "https://uhg.taleo.net/careersection/iam/accessmanagement/newRegister.jsf",
        _NATIVE_TALEO_REGISTER)
    assert v == "self_register_open"


def test_probe_verdict_unknown_on_other():
    v, _ = uhg_recon.probe_verdict("https://example.com/", "<html></html>")
    assert v == "unknown"


# ---- probe_promote.decide (the promote/block gate) --------------------------------------------

def test_decide_promotes_only_on_self_register_open():
    assert pp.decide("self_register_open", "x")["action"] == "promote"
    assert pp.decide("blocked_sso", "x")["action"] == "block"
    assert pp.decide("unknown", "x")["action"] == "pending"


def test_verified_marker_roundtrip(tmp_path, monkeypatch):
    marker = tmp_path / "uhg_verified.json"
    monkeypatch.setattr(pp, "VERIFIED_PATH", str(marker))
    assert pp.is_verified() is False
    pp.add_verified()
    assert pp.is_verified() is True
    # idempotent
    pp.add_verified()
    assert pp.is_verified() is True


def test_blocked_marker_records_reason(tmp_path, monkeypatch):
    marker = tmp_path / "uhg_probe_blocked.json"
    monkeypatch.setattr(pp, "BLOCKED_PATH", str(marker))
    pp.mark_blocked("workforce Azure AD SSO, no candidate self-registration")
    import json
    with open(marker) as f:
        data = json.load(f)
    assert data["uhg"].startswith("workforce Azure AD SSO")


def test_advance_gate_report_only_without_env(monkeypatch, tmp_path):
    # UHG_ADVANCE unset => run_once refuses the live browser walk (report-only), never promotes.
    monkeypatch.delenv("UHG_ADVANCE", raising=False)
    monkeypatch.setattr(pp, "VERIFIED_PATH", str(tmp_path / "v.json"))
    monkeypatch.setattr(pp, "next_job_page",
                        lambda: "https://careers.unitedhealthgroup.com/job/x/y/34088/1")
    res = pp.run_once()
    assert res.get("skipped") and "UHG_ADVANCE" in res.get("why", "")
