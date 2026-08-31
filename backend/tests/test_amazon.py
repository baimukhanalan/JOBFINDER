"""Unit tests for the Amazon corporate ATS apply strategy + its AWS WAF captcha wiring.

Pure logic only — NO network, NO browser, NO account creation, NO submission. Covers URL
routing, the AMAZON_ADVANCE gate (must be OFF by default so a plain fill never creates an
account or transmits PII), the Passport-complexity password generator, the deterministic
truthful screener answers, option matching, strategy registration, and the graceful no-op
behaviour of captcha_solver.solve_aws_waf when the solver is unconfigured.
"""
import re

from backend.applier import captcha_solver as cs
from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.amazon_apply import (
    AmazonStrategy,
    _env_advance,
    _gen_password,
)
from backend.applier.strategies.base import GenericStrategy
from backend.tools import mass_hiring_apply

# A live sample apply URL from the Mass Hiring board (recon 2026-08-31).
_AMZ_URL = "https://account.amazon.jobs/jobs/10481881/apply"


# ---- strategy routing --------------------------------------------------------

def test_matches_amazon_hosts():
    assert AmazonStrategy.matches(_AMZ_URL)
    assert AmazonStrategy.matches(_AMZ_URL.upper())
    # the SAML-redirect target (the register/login SPA)
    assert AmazonStrategy.matches("https://passport.amazon.jobs/")
    # the authenticated apply surface
    assert AmazonStrategy.matches(
        "https://account.amazon.jobs/applicant/jobs/10481881/apply")


def test_does_not_match_non_amazon():
    for url in (
        "https://boards.greenhouse.io/embed/job_app?token=1",
        "https://maximus.avature.net/careers/Register?folderId=1",
        "https://fa-abcd.fa.ocs.oraclecloud.com/sites/CX_1/job/551",
        "https://tenant.myworkdayjobs.com/en-US/careers",
        "https://careersus-teleperformance.icims.com/jobs/1/x/job",
        "",
    ):
        assert not AmazonStrategy.matches(url), url


def test_registered_in_runner_and_picked():
    assert AmazonStrategy in STRATEGIES
    assert isinstance(_pick_strategy(_AMZ_URL), AmazonStrategy)
    # a non-amazon URL never routes to Amazon (the generic fallback wins for an unknown host)
    assert not isinstance(_pick_strategy("https://example.com/careers"), AmazonStrategy)
    assert isinstance(_pick_strategy("https://example.com/careers"), GenericStrategy)


def test_mass_hiring_supported_host():
    assert mass_hiring_apply.is_supported(_AMZ_URL)
    assert mass_hiring_apply.is_supported("https://passport.amazon.jobs/")
    assert not mass_hiring_apply.is_supported("https://example.com/apply")


# ---- the live gate -----------------------------------------------------------

def test_advance_off_by_default():
    # Bootstrapping the account (creating it, transmitting PII) + walking the apply wizard must
    # be OFF unless AMAZON_ADVANCE is explicitly set — a plain fill / dry-run stays on the
    # Passport wall and is side-effect-free at the employer.
    assert AmazonStrategy().advance_wizard is False


def test_env_advance_parsing(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("AMAZON_ADVANCE", val)
        assert _env_advance() is True
    for val in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("AMAZON_ADVANCE", val)
        assert _env_advance() is False
    monkeypatch.delenv("AMAZON_ADVANCE", raising=False)
    assert _env_advance() is False


# ---- Passport password complexity -------------------------------------------
# The six PASSWORD_RULES scraped from the live passport.amazon.jobs shell.
_AMZ_PASSWORD_RULES = (
    r"(?=.*[A-Z])",
    r"(?=.*[a-z])",
    r"(?=.*\d)",
    r"(?=.*[!~%@*><_#^$?|;:&+=(){}\[\]\\\-`.,\/\"\'\s])",
    r".{8,255}",
    r"^$|^[\S]+(\s+[\S]+)*$",   # no leading/trailing whitespace
)


def test_generated_password_meets_amazon_rules():
    for _ in range(50):
        pw = _gen_password()
        for rule in _AMZ_PASSWORD_RULES:
            assert re.search(rule, pw), f"{pw!r} failed {rule}"
        # sanity: no surrounding whitespace, within Amazon's length window
        assert pw == pw.strip()
        assert 8 <= len(pw) <= 255


# ---- deterministic truthful screener answers --------------------------------

def _s(label, facts=None):
    return AmazonStrategy._screener_answer(label.lower(), facts or {})


def test_screener_customer_service_experience_strongest_first():
    ans = _s("How many years of customer service experience do you have?")
    assert ans and ans[0] in ("5+ years", "5 or more", "More than 5")


def test_screener_technical_support_experience_matches():
    # Amazon's Ring roles are "Technical Customer Support" — that phrasing must resolve too.
    ans = _s("Years of technical support experience")
    assert ans and ans[0].startswith("5")


def test_screener_english_native_lead():
    ans = _s("What is your English proficiency?")
    assert ans and ans[0] in ("Native", "Native or bilingual")


def test_screener_spanish_depends_on_bilingual():
    assert _s("Spanish proficiency", {"bilingual": True})[0] in ("Fluent", "Native")
    assert _s("Spanish proficiency", {"bilingual": False})[0] in (
        "None", "No proficiency", "Basic")


def test_screener_sponsorship_is_no():
    assert _s("Will you now or in the future require visa sponsorship?") == ["No"]


def test_screener_schedule_conflict_is_no_but_behavioral_is_left():
    # a real schedule/attendance conflict screener -> No
    assert _s("Do you have any commitments that would interfere with your schedule?") == ["No"]
    # a behavioral "describe a time" prompt must NOT be treated as a Yes/No screener
    assert _s("Describe a time you resolved a conflict with a customer.") is None


def test_screener_bilingual_yesno_before_proficiency():
    # a bilingual-ROLE yes/no screener answers Yes (persona designed to fit), not a scale
    assert _s("Are you able to speak, read, and write in Spanish and English?") == ["Yes"]


def test_screener_unknown_left_for_human():
    assert _s("What is your favourite colour?") is None
    assert _s("Please paste a link to your portfolio.") is None


def test_opt_match_word_boundary():
    # Contract: both args are already lowercased by the caller (_answer_radio_screeners).
    m = AmazonStrategy._opt_match
    assert m("no", "no")                       # exact
    assert m("yes", "yes, i am authorized")    # short answer, word-boundary prefix
    assert not m("no", "none")                 # short answers must not match inside a word
    assert m("1-3 years", "1-3 years of experience")
    assert not m("", "anything")


# ---- AWS WAF captcha wiring (graceful no-op, no network) ---------------------

class _FakeCtx:
    def __init__(self):
        self.cookies = []

    async def add_cookies(self, cookies):
        self.cookies += cookies


class _FakePage:
    def __init__(self, detect=None, raise_eval=False, url="https://passport.amazon.jobs/"):
        self._detect = detect
        self._raise = raise_eval
        self.evaluated = False
        self.url = url
        self.context = _FakeCtx()

    async def evaluate(self, js, *args):
        self.evaluated = True
        if self._raise:
            raise RuntimeError("no page")
        return self._detect


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_aws_waf_task_type_separate_from_recaptcha_tasks():
    # AWS WAF is Amazon-proprietary — it must NOT be folded into the reCAPTCHA/hCaptcha task
    # map (the existing test asserts that map's exact key set), it is its own constant.
    assert isinstance(cs._CAPSOLVER_AWS_WAF_TASK, str)
    assert "AwsWaf" in cs._CAPSOLVER_AWS_WAF_TASK
    assert "aws_waf" not in cs._CAPSOLVER_TASK


def test_aws_waf_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    page = _FakePage(detect={"present": True})
    assert _run(cs.solve_aws_waf(page)) is False
    # disabled => returns before ever touching the page (no network, no DOM probe)
    assert page.evaluated is False


def test_aws_waf_noop_for_twocaptcha(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "twocaptcha")
    page = _FakePage(detect={"present": True})
    # 2captcha's AWS-WAF method isn't wired here -> graceful no-op, page untouched
    assert _run(cs.solve_aws_waf(page)) is False
    assert page.evaluated is False


def test_aws_waf_noop_when_no_challenge(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "capsolver")
    page = _FakePage(detect=None)   # detection JS finds no AWS WAF challenge
    assert _run(cs.solve_aws_waf(page)) is False
    assert page.evaluated is True   # it DID probe, then bailed (no challenge -> no network)


def test_aws_waf_noop_on_page_error(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "capsolver")
    page = _FakePage(raise_eval=True)
    assert _run(cs.solve_aws_waf(page)) is False   # evaluate raised -> swallowed, no raise
