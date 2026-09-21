"""Unit tests for the Workday Mass-Hiring auto-apply strategy (no network, no browser, no submit).

Covers URL routing (the 4 validated CxS tenants → WorkdayMassHiringStrategy; the stock
WorkdayStrategy still matches every Workday host and keeps its /catalog behaviour byte-identical
by NOT overriding prefill), the WORKDAY_ADVANCE gate (OFF by default so a plain fill is
side-effect-free AND /catalog is untouched), the deterministic truthful screener answers, option
matching, the demographic regex, password complexity, and strategy registration.
"""
import re

import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import ApplyStrategy
from backend.applier.strategies.workday import (
    WorkdayMassHiringStrategy,
    WorkdayStrategy,
    _CONSENT_CB_RE,
    _DEMOGRAPHIC_RE,
    _MARKETING_CB_RE,
    _MASSHIRING_HOST_RE,
    _env_advance,
    _gen_password,
)
from backend.tools import mass_hiring_apply

# Live sample apply URLs (from the DB, mass_hiring_jobs) — one per Mass-Hiring tenant.
_CNX = ("https://cnx.wd1.myworkdayjobs.com/en-US/external_global/job/USA-Work-at-Home/"
        "Seasonal-Licensed-Health-Insurance-Rep--Remote---Evergreen-_R1732661")
_CVS = ("https://cvshealth.wd1.myworkdayjobs.com/en-US/CVS_Health_Careers/job/"
        "TX---Work-from-home/Member-Engagement-Service-Coordinator_R0957391-1")
_CENTENE = ("https://centene.wd5.myworkdayjobs.com/en-US/Centene_External/job/Remote-AR/"
            "Care-Coordinator-II_1643171-1")
_CIGNA = ("https://cigna.wd5.myworkdayjobs.com/en-US/cignacareers/job/Tennessee-Work-at-Home/"
          "Customer-Service-Representative---Accredo---Remote_26009553")
_TENANTS = (_CNX, _CVS, _CENTENE, _CIGNA)
# Healthcare payers/BPOs collected onto the same Workday lane (register-captcha probe pending) —
# they must route to WorkdayMassHiringStrategy (the account-create wizard) so the probe can drive it.
_ELEVANCE = ("https://elevancehealth.wd1.myworkdayjobs.com/en-US/ANT/job/"
             "TN-NASHVILLE/Patient-Enrollment-Specialist-I--100--Virtual-_JR203233")
_HIGHMARK = ("https://highmarkhealth.wd1.myworkdayjobs.com/en-US/highmark/job/"
             "PA-Working-at-Home---Pennsylvania/Community-Health-Worker_J286859")
_SAGILITY = ("https://sagility.wd1.myworkdayjobs.com/en-US/SagilityUSA/job/"
             "WorkHome-USA/Work-from-Home-Customer-Service-Representative_REQ-026850")
_PAYER_TENANTS = (_ELEVANCE, _HIGHMARK, _SAGILITY)
# A /catalog Workday job on some OTHER tenant, and Humana (handled by PhenomWorkdayStrategy).
_GENERIC_WD = "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Remote/Engineer_R1"
_HUMANA = "https://humana.wd5.myworkdayjobs.com/en-US/Humana_External/job/Remote/CSR_R2"


# ---- strategy routing --------------------------------------------------------

def test_masshiring_matches_the_four_tenants():
    for u in _TENANTS:
        assert WorkdayMassHiringStrategy.matches(u), u
        assert WorkdayMassHiringStrategy.matches(u.upper()), u  # case-tolerant


def test_masshiring_does_not_match_other_workday_or_non_workday():
    # a generic /catalog Workday tenant, Humana, a greenhouse form, and empty -> NOT mass-hiring.
    assert not WorkdayMassHiringStrategy.matches(_GENERIC_WD)
    assert not WorkdayMassHiringStrategy.matches(_HUMANA)
    assert not WorkdayMassHiringStrategy.matches(
        "https://boards.greenhouse.io/embed/job_app?token=1")
    assert not WorkdayMassHiringStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not WorkdayMassHiringStrategy.matches("")


def test_stock_workday_still_matches_every_workday_host():
    # The stock strategy's broad matching is UNCHANGED — it must still catch the 4 tenants
    # (routing order is what sends them to the subclass), generic tenants, humana, and .workday.com.
    for u in (*_TENANTS, _GENERIC_WD, _HUMANA, "https://x.workday.com/job"):
        assert WorkdayStrategy.matches(u), u
    assert not WorkdayStrategy.matches("https://boards.greenhouse.io/x")
    assert not WorkdayStrategy.matches("")


def test_pick_strategy_routing():
    for u in _TENANTS:
        assert isinstance(_pick_strategy(u), WorkdayMassHiringStrategy), u
    # a generic Workday tenant falls through to the stock strategy (NOT the mass-hiring subclass).
    picked = _pick_strategy(_GENERIC_WD)
    assert isinstance(picked, WorkdayStrategy)
    assert not isinstance(picked, WorkdayMassHiringStrategy)
    # a greenhouse URL never routes to any Workday strategy.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"), WorkdayStrategy)


def test_registered_before_stock_workday():
    assert WorkdayStrategy in STRATEGIES
    assert WorkdayMassHiringStrategy in STRATEGIES
    # the subclass MUST be earlier than the broad stock class, else the stock class would win.
    assert STRATEGIES.index(WorkdayMassHiringStrategy) < STRATEGIES.index(WorkdayStrategy)


def test_masshiring_is_a_workday_subclass_with_name():
    assert issubclass(WorkdayMassHiringStrategy, WorkdayStrategy)
    assert WorkdayMassHiringStrategy.name == "workday_masshiring"
    assert WorkdayStrategy.name == "workday"


# ---- /catalog byte-identical guarantee ---------------------------------------

def test_stock_workday_has_no_prefill_override():
    # CRITICAL: the stock WorkdayStrategy must NOT override prefill — so a /catalog Workday fill
    # resolves to the shared base pipeline exactly as before, AND phenom.PhenomWorkdayStrategy's
    # super().prefill() (which it documents as resolving to base.prefill) keeps working.
    assert WorkdayStrategy.prefill is ApplyStrategy.prefill
    # the mass-hiring subclass DOES override prefill (its account-create + wizard-walk flow).
    assert WorkdayMassHiringStrategy.prefill is not ApplyStrategy.prefill


def test_masshiring_matches_the_healthcare_payer_tenants():
    # Elevance/Anthem, Highmark, Sagility route to the account-create wizard (probe-pending lane).
    for u in _PAYER_TENANTS:
        assert WorkdayMassHiringStrategy.matches(u), u
        assert isinstance(_pick_strategy(u), WorkdayMassHiringStrategy), u


def test_mass_hiring_apply_supports_the_healthcare_payer_tenants():
    for u in _PAYER_TENANTS:
        assert mass_hiring_apply.is_supported(u), u


def test_mass_hiring_apply_supports_the_four_tenants():
    for u in _TENANTS:
        assert mass_hiring_apply.is_supported(u), u
    # unchanged: the other supported hosts still resolve.
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert mass_hiring_apply.is_supported(_HUMANA)  # humana.wd5 is separately listed
    # a NON-listed Workday tenant is NOT silently attempted (tenant-specific, not blanket).
    assert not mass_hiring_apply.is_supported(_GENERIC_WD)
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- WORKDAY_ADVANCE gate (live-submit switch) -------------------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("WORKDAY_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("WORKDAY_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("WORKDAY_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # The account-create + wizard-walk (which transmits PII + creates the account + sends the
    # application on the final Submit) must be OFF unless WORKDAY_ADVANCE is explicitly set.
    monkeypatch.delenv("WORKDAY_ADVANCE", raising=False)

    class _Fresh(WorkdayMassHiringStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- account password --------------------------------------------------------

def test_generated_password_meets_complexity():
    for _ in range(20):
        pw = _gen_password()
        assert len(pw) >= 10
        assert re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


# ---- deterministic truthful screeners ----------------------------------------

def test_screener_answer_availability_and_eligibility():
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("are you able to work an 8 hour shift between 7am-7pm cst?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 50 miles of the remote hub?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / conflict are truthfully No for a fresh authorized persona
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = WorkdayMassHiringStrategy._screener_answer
    # member-services phrasing (CVS/Centene/Cigna) resolves via the customer/member lexicon
    exp = A("how many years of member services experience do you have?", {})
    assert exp and exp[0] == "5+ years"
    csr = A("how much experience do you have in a call center as a customer service rep?", {})
    assert csr and csr[0] == "5+ years"
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("what is your english proficiency?", {})[0] == "Native"
    assert A("what is your spanish proficiency?", {"bilingual": True})[0] == "Fluent"
    assert A("what is your spanish proficiency?", {})[0] in ("None", "No proficiency")
    assert A("what is your highest level of education?", {"education_level": "Associate"})[0] == "Associate"
    assert A("highest level of education achieved?", {})[0] == "Bachelor"


def test_screener_answer_contact_preference():
    # Concentrix create-account application screeners (live 2026-09-20). Email is the reachable
    # channel (we own the inbox; the phone is reserved-fiction), time-of-day is "Anytime".
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("what is your preferred method of communication?", {})[0] == "Email"
    assert A("what is the best time of day to be contacted?", {})[0] == "Anytime"


def test_screener_answer_concentrix_remaining():
    # The rest of the Concentrix step-2 screeners that were left values=None (live 2026-09-20).
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("do you have a high school diploma/ged?", {}) == ["Yes"]
    assert A("how did you hear about us?", {})[0] == "Job Board"
    assert A("to comfortably perform this job, you will be sitting, reaching, talking?", {}) == ["Yes"]
    # the education-LEVEL select must still return the tiered answer, not a bare Yes
    assert A("what is your highest level of education?", {})[0] == "Bachelor"


def test_screener_answer_concentrix_job328_screeners():
    # The job-328 "Licensed Health Insurance Rep" required screeners left values=None (live "Errors
    # Found", 2026-09-20). Each returns a candidate option list a select/radio can match against.
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("what schedule/hours are you looking for?", {})[0] == "Full-time"
    assert A("do you have any restrictions in your hours of availability?", {}) == ["No"]
    assert A("are you fluent in any other languages? if so, what languages?", {})[0] == "No"
    assert A("are you comfortable working in a sales environment and meeting sales goals?", {})[0] == "Yes"
    isp = A("who is your current internet service provider?", {})
    assert isp and isp[0] == "Comcast"
    itype = A("what type of internet service do you have?", {})
    assert itype and itype[0] == "Cable"
    # a bilingual persona (Spanish CSR role) answers the other-languages screener truthfully Yes
    assert A("are you fluent in any other languages?", {"bilingual": True})[0] == "Yes"


def test_screener_answer_sales_comfort_is_scale_tolerant():
    # Concentrix renders sales-comfort as a COMFORT SCALE select (not clean Yes/No) — the candidate
    # list must lead a strong positive AND keep "Yes" so either rendering commits.
    A = WorkdayMassHiringStrategy._screener_answer
    cands = A("are you comfortable working in a sales environment and meeting sales goals?", {})
    assert cands[0] == "Yes"
    assert any("comfortable" in c.lower() for c in cands)


def test_screener_answer_equipment_readiness_is_yes():
    # Home-office equipment Yes/No selects → Yes (a WFH persona has/obtains the kit); must NOT fall to
    # the blanket residual-No fallback (a knockout).
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("do you have a separate router and a modem with the ability to hardwire the router to your pc?", {}) == ["Yes"]
    assert A("does your computer have an available usb port?", {}) == ["Yes"]
    assert A("some of our positions require the use of a webcam. will you use one?", {}) == ["Yes"]
    assert A("some of our positions require you to purchase equipment. are you willing to purchase equipment?", {}) == ["Yes"]


def test_screener_text_answer_router_brand():
    T = WorkdayMassHiringStrategy._screener_text_answer
    assert T("what is the brand name of your router?") == "Netgear"
    assert T("router make and model?") == "Netgear"
    # not every free-text field is a router — an unrelated one stays None
    assert T("what is your favorite hobby?") is None


def test_internet_provider_and_type_beat_the_generic_internet_yesno():
    # The ISP-name and internet-TYPE selects contain the word "internet" — they MUST NOT collapse to
    # the generic "do you have internet? -> Yes" branch (they'd pick "Yes", not an ISP/type option).
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("who is your current internet service provider?", {}) != ["Yes"]
    assert A("what type of internet service do you have?", {}) != ["Yes"]
    # the generic high-speed-internet Yes/No still resolves to Yes
    assert A("do you have reliable high-speed internet at home?", {}) == ["Yes"]


def test_screener_text_answer_freetext_fields():
    # Free-text renderings (an <input>/<textarea>, not a select) are answered by _screener_text_answer.
    T = WorkdayMassHiringStrategy._screener_text_answer
    assert T("please provide your minimum base pay expectations for this role") == "$22 per hour"
    assert T("who is your current internet service provider?") == "Comcast"
    assert T("what type of internet service do you have?") == "Cable"
    assert T("are you fluent in any other languages? if so, what languages?") == "English only"
    assert T("are you fluent in any other languages?", {"bilingual": True}) == "Spanish"
    # the required free-text <textarea> (question in a <legend>) — fully-available synthetic persona
    assert "restriction" in T("do you have any restrictions in your hours of availability?").lower()
    # identity/address fields (and unknowns) are left alone — never overwritten with a screener value
    assert T("first name") is None
    assert T("home address line 1") is None
    assert T("") is None


def test_pick_checkbox_option_prefers_csr_relevant():
    P = WorkdayMassHiringStrategy._pick_checkbox_option
    # "experience with a variety of products and services" — prefer Customer Service over Retail
    prods = [{"gi": 0, "text": "Retail"}, {"gi": 1, "text": "Customer Service"},
             {"gi": 2, "text": "None of the above"}]
    assert P("experience with a variety of products and services", prods)["text"] == "Customer Service"
    # "experience within the healthcare industry" with NO CSR option -> safe "None of the above"
    health = [{"gi": 0, "text": "Medicare"}, {"gi": 1, "text": "Medicaid"},
              {"gi": 2, "text": "None of the above"}]
    assert P("experience within the healthcare industry", health)["text"] == "None of the above"
    # a call-center option wins over a generic secondary
    mixed = [{"gi": 0, "text": "Sales"}, {"gi": 1, "text": "Call Center"}]
    assert P("check all that apply", mixed)["text"] == "Call Center"
    # a decline/self-ID option is NEVER the fallback pick
    skip = [{"gi": 0, "text": "Prefer not to answer"}, {"gi": 1, "text": "Warehouse"}]
    assert P("check all that apply", skip)["text"] == "Warehouse"
    # secondary (retail/sales) chosen when no strong CSR option exists
    sec = [{"gi": 0, "text": "Manufacturing"}, {"gi": 1, "text": "Retail"}]
    assert P("check all that apply", sec)["text"] == "Retail"
    assert P("check all that apply", []) is None


def test_pick_checkbox_option_restrictions_group_picks_no():
    # Concentrix "Do you have any restrictions in your hours of availability?" is a check-all
    # group; a fully-available synthetic persona ticks the "no restrictions" option, NOT a
    # specific restriction (and NOT a CSR-relevant option a prefer-match would grab).
    P = WorkdayMassHiringStrategy._pick_checkbox_option
    q = "do you have any restrictions in your hours of availability?"
    opts = [{"gi": 0, "text": "Mornings only"}, {"gi": 1, "text": "Cannot work weekends"},
            {"gi": 2, "text": "No, I have no restrictions"}]
    assert P(q, opts)["text"] == "No, I have no restrictions"
    # a bare Yes/No rendering
    assert P(q, [{"gi": 0, "text": "Yes"}, {"gi": 1, "text": "No"}])["text"] == "No"
    # no "no restrictions" option at all -> leave blank rather than claim a restriction
    assert P(q, [{"gi": 0, "text": "Mornings only"}, {"gi": 1, "text": "Weekends only"}]) is None


def test_checkgroup_answer_regex_matches_concentrix_prompts():
    from backend.applier.strategies.workday import _CHECKGROUP_ANSWER_RE
    assert _CHECKGROUP_ANSWER_RE.search(
        "We are interested in your experience with a variety of products and services. "
        "Please check all that apply:")
    assert _CHECKGROUP_ANSWER_RE.search(
        "We are interested in your experience within the healthcare industry. "
        "Please check all that apply:")
    assert not _CHECKGROUP_ANSWER_RE.search("What is your gender?")


def test_screener_answer_unknown_returns_none():
    A = WorkdayMassHiringStrategy._screener_answer
    assert A("describe a time you resolved a conflict", {}) is None
    assert A("what is your favorite color?", {}) is None


def test_opt_match_boundary():
    m = WorkdayMassHiringStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False               # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False


# ---- pure regexes ------------------------------------------------------------

def test_demographic_regex():
    for lbl in ("What is your gender?", "Race/Ethnicity", "Are you Hispanic or Latino?",
                "Veteran status", "Disability status", "Please self-identify"):
        assert _DEMOGRAPHIC_RE.search(lbl), lbl
    # a geography screener that merely contains "Latin American" is NOT a demographic
    assert not _DEMOGRAPHIC_RE.search("Are you based in a Latin American country?")
    assert not _DEMOGRAPHIC_RE.search("How many years of customer service experience?")


def test_masshiring_host_regex_is_tenant_specific():
    for u in _TENANTS:
        assert _MASSHIRING_HOST_RE.search(u), u
    assert not _MASSHIRING_HOST_RE.search(_GENERIC_WD)
    assert not _MASSHIRING_HOST_RE.search(_HUMANA)
    assert not _MASSHIRING_HOST_RE.search("https://cnx.wd1.example.com/job")


# ---- proxy plumbing (Bright Data US egress for the register step) ------------
from backend.tools import workday_recon as _wr  # noqa: E402


def test_proxy_from_url_socks5_no_auth():
    # a local no-auth SOCKS5 slot → server only, no credential fields.
    assert _wr._proxy_from_url("socks5://127.0.0.1:10801") == {"server": "socks5://127.0.0.1:10801"}


def test_proxy_from_url_http_with_embedded_auth_is_split_out():
    # Playwright ignores creds baked into `server`, so they MUST be peeled into username/password.
    d = _wr._proxy_from_url("http://brd-customer-x-zone-alibaba_dc-country-us-session-abc:secret@brd.superproxy.io:33335")
    assert d["server"] == "http://brd.superproxy.io:33335"
    assert d["username"] == "brd-customer-x-zone-alibaba_dc-country-us-session-abc"
    assert d["password"] == "secret"


def test_proxy_from_url_bare_hostport_defaults_to_http():
    assert _wr._proxy_from_url("brd.superproxy.io:33335") == {"server": "http://brd.superproxy.io:33335"}


def test_pick_proxy_direct_override(monkeypatch):
    for v in ("0", "direct", "off", "none", "DIRECT"):
        monkeypatch.setenv("WORKDAY_PROXY", v)
        monkeypatch.delenv("WORKDAY_BRIGHTDATA", raising=False)
        assert _wr._pick_proxy() is None


def test_pick_proxy_explicit_http_proxy_is_authful(monkeypatch):
    monkeypatch.delenv("WORKDAY_BRIGHTDATA", raising=False)
    monkeypatch.setenv("WORKDAY_PROXY", "http://user:pass@gw.example.com:1000")
    d = _wr._pick_proxy()
    assert d == {"server": "http://gw.example.com:1000", "username": "user", "password": "pass"}


def test_pick_proxy_brightdata_path_builds_authful_dict(monkeypatch):
    # WORKDAY_BRIGHTDATA=1 wins over WORKDAY_PROXY and yields a server + username + password dict.
    monkeypatch.setenv("WORKDAY_BRIGHTDATA", "1")
    monkeypatch.setenv("WORKDAY_PROXY", "direct")  # must be overridden by the BD path
    fake = {"server": "http://brd.superproxy.io:33335",
            "username": "brd-customer-x-zone-alibaba_dc-country-us-session-deadbeef",
            "password": "zonepw"}
    monkeypatch.setattr(_wr, "_bd_proxy_dict", lambda: fake)
    assert _wr._pick_proxy() == fake


def test_pick_proxy_brightdata_falls_through_when_unconfigured(monkeypatch):
    monkeypatch.setenv("WORKDAY_BRIGHTDATA", "1")
    monkeypatch.setenv("WORKDAY_PROXY", "socks5://127.0.0.1:10801")
    monkeypatch.setattr(_wr, "_bd_proxy_dict", lambda: None)   # BD not configured
    assert _wr._pick_proxy() == {"server": "socks5://127.0.0.1:10801"}


# ---- create-account consent-checkbox classification (Concentrix "Please check the box") ------

def test_consent_checkbox_matches_concentrix_terms_wording():
    # The EXACT Concentrix create-account context that blocked account creation (screenshot
    # 08_after_create: unticked box + "Error: Please check the box to continue"). Its <input> is
    # NOT DOM-required, so the old `required`-only gate skipped it.
    ctx = ('By clicking the "Create Account" button, you are agreeing to our Recruiting Data '
           'Privacy Notice. Yes, I have read and consent to the terms and conditions.')
    assert _CONSENT_CB_RE.search(ctx)
    assert not _MARKETING_CB_RE.search(ctx)


def test_consent_checkbox_does_not_match_marketing_optin():
    ctx = "Yes, I would like to join the Talent Community and receive marketing opportunities."
    assert _MARKETING_CB_RE.search(ctx)


def test_consent_checkbox_matches_generic_agree_and_privacy_policy():
    assert _CONSENT_CB_RE.search("I agree to the Terms of Use")
    assert _CONSENT_CB_RE.search("I have read the Privacy Policy")
    assert not _CONSENT_CB_RE.search("Please enter your email address")
