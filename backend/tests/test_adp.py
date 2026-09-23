"""Unit tests for the ADP "myjobs" (Afni) email-OTP account-create apply lane.

Pure logic only — NO network, NO browser, NO account creation, NO OTP generation, NO submission.
Covers URL routing, the AFNI_ADVANCE gate (must be OFF by default so a plain fill never creates an
account, generates an OTP or transmits PII), the create-profile password generator, the OTP
sender/subject/code matchers, the deterministic truthful screener answers, option matching, the
afni_recon row/eligibility/confirmation/egress helpers, and the afni_probe_promote classifier +
verified-marker round-trip.
"""
import asyncio
import re

import pytest

from backend.applier.strategies.adp import (
    AdpStrategy,
    _OTP_CODE_RE,
    _OTP_SENDER_RE,
    _OTP_SUBJECT_RE,
    _env_advance,
    _gen_password,
)


def _run(coro):
    return asyncio.run(coro)


# ---- strategy routing --------------------------------------------------------

def test_matches_adp_hosts():
    assert AdpStrategy.matches("https://myjobs.adp.com/afniexternalcareers/cx/job-details/500122")
    assert AdpStrategy.matches("https://myjobs.adp.com/afniexternalcareers/auth")
    assert AdpStrategy.matches(
        "https://myjobs.adp.com/afniexternalcareers/cx/job-details/500122".upper())
    assert AdpStrategy.matches("https://my.adp.com/myadp_prefix/mycareer/...")


def test_auth_url_derivation():
    f = AdpStrategy._auth_url
    assert f("https://myjobs.adp.com/afniexternalcareers/cx/job-details/5001190010500") == \
        "https://myjobs.adp.com/afniexternalcareers/auth"
    assert f("https://myjobs.adp.com/afniexternalcareers/auth") == \
        "https://myjobs.adp.com/afniexternalcareers/auth"
    assert f("https://myjobs.adp.com/afniexternalcareers") == \
        "https://myjobs.adp.com/afniexternalcareers/auth"
    assert f("") == ""


def test_does_not_match_non_adp():
    for url in (
        "https://boards.greenhouse.io/embed/job_app?token=1",
        "https://account.amazon.jobs/jobs/10481881/apply",
        "https://maximus.avature.net/careers/Register?folderId=1",
        "https://tenant.myworkdayjobs.com/en-US/careers",
        "https://careersus-teleperformance.icims.com/jobs/1/x/job",
        "",
    ):
        assert not AdpStrategy.matches(url), url


# ---- the live gate -----------------------------------------------------------

def test_advance_off_by_default():
    # Creating the account, GENERATING the OTP (which transmits the persona email to ADP) + walking
    # the wizard must be OFF unless AFNI_ADVANCE is explicitly set — a plain fill / dry-run stays on
    # the auth step and is side-effect-free at the employer.
    assert AdpStrategy().advance_wizard is False


def test_env_advance_parsing(monkeypatch):
    monkeypatch.delenv("ADP_ADVANCE", raising=False)
    for val in ("1", "true", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("AFNI_ADVANCE", val)
        assert _env_advance() is True
    for val in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("AFNI_ADVANCE", val)
        assert _env_advance() is False
    monkeypatch.delenv("AFNI_ADVANCE", raising=False)
    assert _env_advance() is False
    # ADP_ADVANCE is accepted as an alias
    monkeypatch.setenv("ADP_ADVANCE", "1")
    assert _env_advance() is True


def test_dry_run_open_form_fills_email_but_never_bootstraps(monkeypatch):
    """The core no-PII invariant: with AFNI_ADVANCE unset, open_form reaches the auth step and FILLS
    the email box (side-effect-free) but NEVER calls _bootstrap_account (which would press Continue,
    creating the account + generating the OTP)."""
    strat = AdpStrategy()
    assert strat.advance_wizard is False
    calls = {"cookie": 0, "auth": 0, "email": 0, "bootstrap": 0}

    async def _cookie(page):
        calls["cookie"] += 1

    async def _auth(page):
        calls["auth"] += 1

    async def _email(page):
        calls["email"] += 1
        return True

    async def _boot(page):
        calls["bootstrap"] += 1

    strat._pf = {"email": "jane.doe1@takhet.com", "full_name": "Jane Doe"}
    monkeypatch.setattr(strat, "_dismiss_cookie_banner", _cookie)
    monkeypatch.setattr(strat, "_go_to_auth", _auth)
    monkeypatch.setattr(strat, "_fill_auth_email", _email)
    monkeypatch.setattr(strat, "_bootstrap_account", _boot)

    _run(strat.open_form(object()))     # page is never touched (all methods stubbed)
    assert calls["auth"] == 1
    assert calls["email"] == 1          # dry-run DID fill the email (proves it reached the step)
    assert calls["bootstrap"] == 0      # …but NEVER bootstrapped the account (no OTP, no PII)


def test_advance_on_open_form_bootstraps(monkeypatch):
    """With the gate ON, open_form delegates to _bootstrap_account (the live account+OTP path)."""
    strat = AdpStrategy()
    strat.advance_wizard = True
    calls = {"email": 0, "bootstrap": 0}

    async def _noop(page):
        return None

    async def _email(page):
        calls["email"] += 1
        return True

    async def _boot(page):
        calls["bootstrap"] += 1

    strat._pf = {"email": "jane.doe1@takhet.com"}
    monkeypatch.setattr(strat, "_dismiss_cookie_banner", _noop)
    monkeypatch.setattr(strat, "_go_to_auth", _noop)
    monkeypatch.setattr(strat, "_fill_auth_email", _email)
    monkeypatch.setattr(strat, "_bootstrap_account", _boot)
    _run(strat.open_form(object()))
    assert calls["bootstrap"] == 1
    assert calls["email"] == 0          # the dry-run-only email fill is skipped on the live path


# ---- create-profile password complexity -------------------------------------

_ADP_PASSWORD_RULES = (
    r"(?=.*[A-Z])",
    r"(?=.*[a-z])",
    r"(?=.*\d)",
    r"(?=.*[!@#$%^&*?_\-])",
    r".{8,}",
)


def test_generated_password_meets_adp_rules():
    for _ in range(50):
        pw = _gen_password()
        for rule in _ADP_PASSWORD_RULES:
            assert re.search(rule, pw), f"{pw!r} failed {rule}"
        assert pw == pw.strip()
        assert len(pw) >= 8


# ---- OTP matchers (pure) -----------------------------------------------------

def test_otp_code_regex_six_digits():
    assert _OTP_CODE_RE.search("Your one-time password is 482913 valid for").group(1) == "482913"
    assert _OTP_CODE_RE.search("no code here") is None


def test_otp_sender_and_subject_matchers():
    assert _OTP_SENDER_RE.search("From: noreply@adp.com")
    assert _OTP_SENDER_RE.search("From: careers@afni.com")
    assert not _OTP_SENDER_RE.search("From: recruiter@example.com")
    assert _OTP_SUBJECT_RE.search("Your one-time password")
    assert _OTP_SUBJECT_RE.search("Verification code for sign-in")
    assert not _OTP_SUBJECT_RE.search("A job you might like")


# ---- deterministic truthful screener answers --------------------------------

def _s(label, facts=None):
    return AdpStrategy._screener_answer(label.lower(), facts or {})


def test_screener_customer_service_experience():
    ans = _s("How many years of customer service experience do you have?")
    assert ans and ans[0].startswith(("3", "5", "1"))


def test_screener_insurance_experience_matches():
    ans = _s("Do you have prior insurance or claims experience?")
    assert ans and (ans[0][0].isdigit() or ans[0] == "Yes")


def test_screener_english_native_lead():
    ans = _s("What is your English proficiency?")
    assert ans and ans[0] in ("Native", "Native or bilingual")


def test_screener_spanish_depends_on_bilingual():
    assert _s("Spanish proficiency", {"bilingual": True})[0] in ("Fluent", "Native")
    assert _s("Spanish proficiency", {"bilingual": False})[0] in (
        "None", "No proficiency", "Basic")


def test_screener_sponsorship_is_no():
    assert _s("Will you now or in the future require visa sponsorship?") == ["No"]


def test_screener_remote_workspace_is_yes():
    assert _s("Do you have a private, distraction-free workspace?") == ["Yes"]


def test_screener_schedule_conflict_is_no_but_behavioral_left():
    assert _s("Do you have any commitments that would interfere with your schedule?") == ["No"]
    assert _s("Describe a time you resolved a conflict with a customer.") is None


def test_screener_ack_privacy_left_to_tick_helper():
    # An AckPrivacyStatement / certify line is handled by _tick_acknowledge, not the MCQ answerer.
    assert _s("I acknowledge and agree to the privacy statement") is None


def test_screener_unknown_left_for_human():
    assert _s("What is your favourite colour?") is None


def test_opt_match_word_boundary():
    m = AdpStrategy._opt_match
    assert m("no", "no")
    assert m("yes", "yes, i am authorized")
    assert not m("no", "none")
    assert m("1-3 years", "1-3 years of experience")
    assert not m("", "anything")


# ---- afni_recon driver helpers (row decode / bilingual / confirmation) -------

from backend.tools.afni_recon import (  # noqa: E402
    _is_afni_confirmation,
    _is_bilingual,
    _state_from_afni_location,
)


def test_recon_state_from_location_state_code():
    assert _state_from_afni_location("SC, United States") == "South Carolina"
    assert _state_from_afni_location("TX, United States") == "Texas"
    assert _state_from_afni_location("Columbia, SC, United States") == "South Carolina"


def test_recon_state_from_location_stateless_is_blank():
    assert _state_from_afni_location("Remote, United States") == ""
    assert _state_from_afni_location("United States") == ""
    assert _state_from_afni_location("Work at Home, USA") == ""
    assert _state_from_afni_location("") == ""


def test_recon_is_bilingual():
    assert _is_bilingual("Bilingual Remote Customer Service Representative") is True
    assert _is_bilingual("Remote Customer Service Representative") is False
    assert _is_bilingual("") is False


def test_recon_confirmation_by_sender_and_subject():
    assert _is_afni_confirmation("From: no-reply@afni.com", "Subject: hello") is True
    assert _is_afni_confirmation("From: careers@adp.com",
                                 "Subject: Thank you for applying") is True
    assert _is_afni_confirmation("From: x@ex.com",
                                 "Subject: We have received your application") is True


def test_recon_confirmation_excludes_otp_and_junk():
    # an OTP / verification mail precedes the submit — it is NOT an application confirmation
    assert _is_afni_confirmation("From: no-reply@adp.com",
                                 "Subject: Your one-time password is 123456") is False
    assert _is_afni_confirmation("From: recruiter@randombpo.com",
                                 "Subject: A job you might like") is False


# ---- afni_recon egress wiring (DIRECT default, US opt-in, NEVER KZ phones) ----

import backend.tools.afni_recon as _ar  # noqa: E402


def _clear_afni_egress(monkeypatch):
    for k in ("AFNI_US", "AFNI_PROXY", "US_PROXY", "WEBSHARE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_recon_proxy_direct_by_default(monkeypatch):
    _clear_afni_egress(monkeypatch)
    assert _ar._afni_proxy("jane.doe1@takhet.com") is None


def test_recon_proxy_explicit_override_wins(monkeypatch):
    _clear_afni_egress(monkeypatch)
    monkeypatch.setenv("AFNI_PROXY", "http://u:p@1.2.3.4:8080")
    assert _ar._afni_proxy() == {
        "server": "http://1.2.3.4:8080", "username": "u", "password": "p"}


def test_recon_proxy_direct_keyword_forces_direct(monkeypatch):
    _clear_afni_egress(monkeypatch)
    monkeypatch.setenv("AFNI_US", "1")
    monkeypatch.setenv("AFNI_PROXY", "direct")
    import backend.tools.us_egress as ue
    monkeypatch.setattr(ue, "us_proxy",
                        lambda: (_ for _ in ()).throw(AssertionError("resolver consulted")))
    assert _ar._afni_proxy() is None


def test_recon_proxy_us_opt_in_uses_resolver(monkeypatch):
    _clear_afni_egress(monkeypatch)
    monkeypatch.setenv("AFNI_US", "1")
    import backend.tools.us_egress as ue
    monkeypatch.setattr(ue, "us_proxy", lambda: {"server": "http://brd-us:33335",
                                                 "username": "cust", "password": "pw"})
    assert _ar._afni_proxy() == {"server": "http://brd-us:33335",
                                 "username": "cust", "password": "pw"}


def test_recon_proxy_never_uses_kz_phones(monkeypatch):
    _clear_afni_egress(monkeypatch)
    monkeypatch.setenv("AFNI_US", "1")
    import backend.tools.proxy_pool as pp
    monkeypatch.setattr(pp, "residential_slots",
                        lambda: (_ for _ in ()).throw(AssertionError("KZ phone slots consulted")))
    import backend.tools.us_egress as ue
    monkeypatch.setattr(ue, "_free_us_proxy", lambda: None)
    monkeypatch.setattr(ue, "_bd_us_proxy", lambda: None)
    assert _ar._afni_proxy() is None


# ---- afni_probe_promote classifier + verified-marker round-trip --------------

import backend.tools.afni_probe_promote as _pp  # noqa: E402


def test_classify_confirmed():
    v, _d = _pp.classify("=== Afni apply\n[application CONFIRMED — Afni/ADP receipt in the Maildir]")
    assert v == "confirmed"


def test_classify_reached_account_incomplete():
    v, _d = _pp.classify("[SUBMIT clicked ...]\n[no confirmation within --keep (ADP acks can lag)]")
    assert v == "reached_account_incomplete"
    v2, _ = _pp.classify("[page_type=... wizard_at_submit=True unfilled=['Phone']]")
    assert v2 == "reached_account_incomplete"


def test_classify_no_account():
    v, _d = _pp.classify("[page_type=login_required reached_auth=True needs_account=True ...]\n"
                         "[DRY-RUN: reached the ADP auth/account-create step ...]")
    assert v == "no_account"


def test_classify_blocked_on_unexpected_wall():
    assert _pp.classify("... a reCAPTCHA challenge appeared ...")[0] == "blocked"
    assert _pp.classify("... please verify your phone number via SMS code ...")[0] == "blocked"


def test_classify_error():
    assert _pp.classify("TIMEOUT")[0] == "error"
    assert _pp.classify("[run error: TargetClosedError: ...]")[0] == "error"


def test_verified_marker_roundtrip(tmp_path, monkeypatch):
    vp = tmp_path / "afni_verified.json"
    monkeypatch.setattr(_pp, "VERIFIED_PATH", str(vp))
    assert _pp.is_verified() is False
    _pp.add_verified()
    assert _pp.is_verified() is True
    # idempotent: a second add doesn't duplicate / break
    _pp.add_verified()
    assert _pp.is_verified() is True
    import json as _json
    assert _json.loads(vp.read_text()) == ["afni"]


def test_probe_run_once_short_circuits_when_verified(tmp_path, monkeypatch):
    vp = tmp_path / "afni_verified.json"
    vp.write_text('["afni"]')
    monkeypatch.setattr(_pp, "VERIFIED_PATH", str(vp))
    # driving must NOT happen when already verified
    monkeypatch.setattr(_pp, "_drive",
                        lambda job: (_ for _ in ()).throw(AssertionError("drove while verified")))
    out = _pp.run_once(dry=False)
    assert out.get("verified") is True
