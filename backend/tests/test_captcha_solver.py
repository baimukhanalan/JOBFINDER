"""Unit tests for backend.applier.captcha_solver — pure config/guard logic, no network."""
import asyncio

from backend.applier import captcha_solver as cs


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    assert cs.is_enabled() is False
    # solve() is a graceful no-op (returns None) when disabled — never touches the network.
    assert asyncio.run(cs.solve("recaptcha_v2", "sitekey", "https://x.com")) is None


def test_enabled_with_key(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    assert cs.is_enabled() is True


def test_solve_none_for_unknown_kind(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "test-key")
    assert asyncio.run(cs.solve("not_a_kind", "k", "https://x.com")) is None
    # missing site_key / url also no-op
    assert asyncio.run(cs.solve("recaptcha_v2", "", "https://x.com")) is None
    assert asyncio.run(cs.solve("recaptcha_v2", "k", "")) is None


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    assert cs._provider() == "capsolver"
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "TwoCaptcha")
    assert cs._provider() == "twocaptcha"


def test_task_type_maps_cover_all_kinds():
    kinds = {"recaptcha_v2", "recaptcha_v3", "hcaptcha", "turnstile"}
    assert set(cs._CAPSOLVER_TASK) == kinds
    assert set(cs._TWOCAPTCHA_METHOD) == kinds


# ---- FREE AWS WAF browser-token path (no key, uses the page's own AWS WAF SDK) --------------

class _FakeCtx:
    def __init__(self, cookies=None):
        self._cookies = list(cookies or [])

    async def add_cookies(self, cookies):
        self._cookies += cookies

    async def cookies(self):
        return self._cookies


class _FakeWafPage:
    """Fake Playwright page: evaluate() returns the configured getToken() result; the context
    optionally already carries an aws-waf-token cookie (as challenge.js would set)."""
    def __init__(self, token=None, cookie=None, url="https://passport.amazon.jobs/x"):
        self._token = token
        self.url = url
        self.context = _FakeCtx(
            [{"name": "aws-waf-token", "value": cookie}] if cookie else [])

    async def evaluate(self, js, *args):
        return self._token


def test_awswaf_browser_enabled_parsing(monkeypatch):
    monkeypatch.delenv("AWSWAF_BROWSER", raising=False)
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    assert cs._awswaf_browser_enabled() is False
    for val in ("1", "true", "YES", "On"):
        monkeypatch.setenv("AWSWAF_BROWSER", val)
        assert cs._awswaf_browser_enabled() is True
    monkeypatch.delenv("AWSWAF_BROWSER", raising=False)
    monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", "awswaf_browser")
    assert cs._awswaf_browser_enabled() is True


def test_aws_waf_available_reflects_both_paths(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    monkeypatch.delenv("AWSWAF_BROWSER", raising=False)
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    assert cs.aws_waf_available() is False
    monkeypatch.setenv("AWSWAF_BROWSER", "1")          # free path alone arms it
    assert cs.aws_waf_available() is True
    monkeypatch.delenv("AWSWAF_BROWSER", raising=False)
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "k")       # a paid key alone arms it
    assert cs.aws_waf_available() is True


def test_awswaf_browser_free_path_mints_token_without_key(monkeypatch):
    # No CapSolver key at all — the free in-browser path must still obtain + inject the token.
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    monkeypatch.setenv("AWSWAF_BROWSER", "1")
    page = _FakeWafPage(token="eee4187a-uuid:CQotoken")
    assert asyncio.run(cs.solve_aws_waf(page)) is True
    names = [c["name"] for c in page.context._cookies]
    assert "aws-waf-token" in names


def test_awswaf_browser_cookie_only_counts(monkeypatch):
    # getToken() returns None (unavailable) but challenge.js already set the cookie -> success.
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    monkeypatch.setenv("AWSWAF_BROWSER", "1")
    page = _FakeWafPage(token=None, cookie="uuid:cookievalue")
    assert asyncio.run(cs.solve_aws_waf(page)) is True


def test_awswaf_browser_disabled_by_default_noop(monkeypatch):
    # AWSWAF_BROWSER unset + no key => solve_aws_waf is a pure no-op even if getToken WOULD work.
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    monkeypatch.delenv("AWSWAF_BROWSER", raising=False)
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    page = _FakeWafPage(token="uuid:token")
    assert asyncio.run(cs.solve_aws_waf(page)) is False
    assert all(c["name"] != "aws-waf-token" for c in page.context._cookies)
