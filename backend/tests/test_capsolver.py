"""Unit tests for backend.applier.capsolver (the paid CapSolver escalation client) + its
wiring into backend.applier.captcha_solver. All network-free: the HTTP client factory
(`capsolver._client`) and the poll sleeper (`capsolver._sleep`) are monkeypatched, so nothing
touches the network and no test ever really sleeps.
"""
import asyncio

from backend.applier import capsolver as cap
from backend.applier import captcha_solver as cs


def _run(coro):
    return asyncio.run(coro)


# ---- a scripted fake httpx.AsyncClient -----------------------------------------------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    """Scripts /createTask -> `create` and /getTaskResult -> the `results` queue (falls back to
    a perpetual 'processing' once the queue drains). Optionally raises on a URL substring."""

    def __init__(self, *, create=None, results=None, raise_on=None):
        self.create = create if create is not None else {"errorId": 0, "taskId": "T1"}
        self.results = list(results or [])
        self.raise_on = raise_on
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self.raise_on and self.raise_on in url:
            raise RuntimeError("boom")
        if url.endswith("/createTask"):
            return _Resp(self.create)
        if self.results:
            return _Resp(self.results.pop(0))
        return _Resp({"errorId": 0, "status": "processing"})


def _arm(monkeypatch, client):
    """Point capsolver at a fake client + a no-op sleeper; return the captured sleep schedule."""
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "k")
    monkeypatch.setattr(cap, "_client", lambda: client)
    sleeps = []

    async def _fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(cap, "_sleep", _fake_sleep)
    return sleeps


# ---- no-key no-op --------------------------------------------------------------------------

def test_no_key_is_pure_noop(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)

    def _boom():
        raise AssertionError("HTTP client must NOT be constructed without a key")

    monkeypatch.setattr(cap, "_client", _boom)
    assert cap.is_enabled() is False
    # solve() and run_task() both bail before any network work.
    assert _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk")) is None
    assert _run(cap.solve("aws_waf", page_url="https://x.com")) is None
    assert _run(cap.run_task({"type": "AntiTurnstileTaskProxyLess"})) is None


# ---- task-type selection -------------------------------------------------------------------

def test_resolve_task_type():
    assert cap.resolve_task_type("turnstile") == "AntiTurnstileTaskProxyLess"
    assert cap.resolve_task_type("hcaptcha") == "HCaptchaTaskProxyLess"
    # hCaptcha has no separate enterprise task type — enterprise is an enterprisePayload.
    assert cap.resolve_task_type("hcaptcha", enterprise=True) == "HCaptchaTaskProxyLess"
    assert cap.resolve_task_type("recaptcha_v2") == "ReCaptchaV2TaskProxyLess"
    assert cap.resolve_task_type("recaptcha_v2", enterprise=True) == "ReCaptchaV2EnterpriseTaskProxyLess"
    assert cap.resolve_task_type("recaptcha_v3") == "ReCaptchaV3TaskProxyLess"
    assert cap.resolve_task_type("recaptcha_v3", enterprise=True) == "ReCaptchaV3EnterpriseTaskProxyLess"
    assert cap.resolve_task_type("aws_waf") == "AntiAwsWafTaskProxyLess"
    assert cap.resolve_task_type("nonsense") is None


def test_build_task_shapes():
    t = cap.build_task("turnstile", page_url="https://x.com", site_key="sk")
    assert t == {"type": "AntiTurnstileTaskProxyLess", "websiteURL": "https://x.com",
                 "websiteKey": "sk"}

    v3 = cap.build_task("recaptcha_v3", page_url="https://x.com", site_key="sk", action="login")
    assert v3["type"] == "ReCaptchaV3TaskProxyLess"
    assert v3["pageAction"] == "login" and v3["minScore"] == 0.7

    hc = cap.build_task("hcaptcha", page_url="https://x.com", site_key="sk",
                        enterprise=True, enterprise_payload={"rqdata": "R"}, is_invisible=True)
    assert hc["type"] == "HCaptchaTaskProxyLess"
    assert hc["enterprisePayload"] == {"rqdata": "R"} and hc["isInvisible"] is True

    aws = cap.build_task("aws_waf", page_url="https://amazon.jobs/x",
                         aws_key="K", aws_context="C", aws_challenge_js="https://cdn/challenge.js")
    assert aws["type"] == "AntiAwsWafTaskProxyLess"
    assert aws["websiteURL"] == "https://amazon.jobs/x"
    assert aws["awsKey"] == "K" and aws["awsContext"] == "C"
    assert aws["awsChallengeJS"] == "https://cdn/challenge.js"
    assert "websiteKey" not in aws  # AWS WAF has no site key

    # Missing required inputs -> None (never a spend).
    assert cap.build_task("turnstile", page_url="https://x.com") is None      # no site key
    assert cap.build_task("turnstile", page_url="", site_key="sk") is None    # no url
    assert cap.build_task("nonsense", page_url="https://x.com", site_key="s") is None


# ---- token -> field mapping per solution ---------------------------------------------------

def test_extract_token_mapping():
    assert cap._extract_token("turnstile", {"token": "TT"}) == "TT"
    assert cap._extract_token("hcaptcha", {"gRecaptchaResponse": "HH"}) == "HH"
    assert cap._extract_token("recaptcha_v2", {"gRecaptchaResponse": "RR"}) == "RR"
    assert cap._extract_token("recaptcha_v3", {"gRecaptchaResponse": "RV3"}) == "RV3"
    assert cap._extract_token("aws_waf", {"cookie": "CKV"}) == "CKV"
    assert cap._extract_token("turnstile", {}) is None
    assert cap._extract_token("aws_waf", None) is None


# ---- end-to-end solve() per challenge (scripted) -------------------------------------------

def test_solve_turnstile(monkeypatch):
    client = _FakeClient(results=[{"status": "idle"},
                                  {"status": "ready", "solution": {"token": "TT"}}])
    _arm(monkeypatch, client)
    tok = _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk"))
    assert tok == "TT"
    # first call is createTask with the right task type
    assert client.calls[0][0].endswith("/createTask")
    assert client.calls[0][1]["task"]["type"] == "AntiTurnstileTaskProxyLess"
    assert client.calls[0][1]["clientKey"] == "k"


def test_solve_hcaptcha(monkeypatch):
    client = _FakeClient(results=[{"status": "ready", "solution": {"gRecaptchaResponse": "HH"}}])
    _arm(monkeypatch, client)
    assert _run(cap.solve("hcaptcha", page_url="https://x.com", site_key="sk")) == "HH"
    assert client.calls[0][1]["task"]["type"] == "HCaptchaTaskProxyLess"


def test_solve_recaptcha_v2_enterprise(monkeypatch):
    client = _FakeClient(results=[{"status": "ready", "solution": {"gRecaptchaResponse": "RR"}}])
    _arm(monkeypatch, client)
    tok = _run(cap.solve("recaptcha_v2", page_url="https://x.com", site_key="sk", enterprise=True))
    assert tok == "RR"
    assert client.calls[0][1]["task"]["type"] == "ReCaptchaV2EnterpriseTaskProxyLess"


def test_solve_recaptcha_v3(monkeypatch):
    client = _FakeClient(results=[{"status": "ready", "solution": {"gRecaptchaResponse": "V3"}}])
    _arm(monkeypatch, client)
    assert _run(cap.solve("recaptcha_v3", page_url="https://x.com", site_key="sk")) == "V3"
    task = client.calls[0][1]["task"]
    assert task["type"] == "ReCaptchaV3TaskProxyLess" and task["pageAction"] == "verify"


def test_solve_aws_waf(monkeypatch):
    client = _FakeClient(results=[{"status": "ready", "solution": {"cookie": "CKV"}}])
    _arm(monkeypatch, client)
    tok = _run(cap.solve("aws_waf", page_url="https://amazon.jobs/x", aws_key="K"))
    assert tok == "CKV"
    task = client.calls[0][1]["task"]
    assert task["type"] == "AntiAwsWafTaskProxyLess" and task["awsKey"] == "K"


# ---- error / timeout / backoff -------------------------------------------------------------

def test_create_task_error_returns_none(monkeypatch):
    client = _FakeClient(create={"errorId": 1, "errorCode": "ERROR_KEY_DENIED",
                                 "errorDescription": "bad key"})
    _arm(monkeypatch, client)
    assert _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk")) is None
    # never polled getTaskResult after a createTask error
    assert all(not u.endswith("/getTaskResult") for u, _ in client.calls)


def test_get_task_result_error_returns_none(monkeypatch):
    client = _FakeClient(results=[{"errorId": 1, "errorDescription": "solve failed"}])
    _arm(monkeypatch, client)
    assert _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk")) is None


def test_timeout_none_and_backoff_bounded(monkeypatch):
    # getTaskResult is always 'processing' -> the poll must terminate at the deadline, None.
    client = _FakeClient(results=[])   # perpetual 'processing'
    sleeps = _arm(monkeypatch, client)
    assert _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk")) is None
    assert sleeps, "must have polled at least once"
    # exponential backoff, non-decreasing, capped at the ceiling, first == start
    assert sleeps[0] == cap._POLL_START
    assert sleeps == sorted(sleeps)
    assert max(sleeps) == cap._POLL_CEIL
    assert all(s <= cap._POLL_CEIL for s in sleeps)
    # bounded: the loop terminated in a finite, small number of iterations (~ deadline/ceil)
    assert len(sleeps) < 40
    assert sum(sleeps) >= cap._POLL_MAX - cap._POLL_CEIL


def test_exception_returns_none(monkeypatch):
    client = _FakeClient(raise_on="/createTask")
    _arm(monkeypatch, client)
    assert _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk")) is None
    client2 = _FakeClient(raise_on="/getTaskResult")
    _arm(monkeypatch, client2)
    assert _run(cap.solve("turnstile", page_url="https://x.com", site_key="sk")) is None


def test_run_task_respects_custom_poll_max(monkeypatch):
    client = _FakeClient(results=[])   # never ready
    sleeps = _arm(monkeypatch, client)
    assert _run(cap.run_task({"type": "AntiTurnstileTaskProxyLess", "websiteURL": "u",
                              "websiteKey": "k"}, poll_max=5)) is None
    assert sum(sleeps) >= 5 and sum(sleeps) < 5 + cap._POLL_CEIL


# ---- captcha_solver wiring (free-first -> paid escalation) ---------------------------------

def test_captcha_solver_solve_delegates_to_capsolver(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "k")
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)  # -> capsolver
    seen = {}

    async def _fake(challenge, *, page_url, site_key=None, action=None,
                    enterprise=False, enterprise_payload=None, **kw):
        seen.update(challenge=challenge, page_url=page_url, site_key=site_key,
                    enterprise=enterprise)
        return "DELEGATED"

    monkeypatch.setattr(cap, "solve", _fake)
    tok = _run(cs.solve("turnstile", "sk", "https://x.com"))
    assert tok == "DELEGATED"
    assert seen == {"challenge": "turnstile", "page_url": "https://x.com",
                    "site_key": "sk", "enterprise": False}


class _FakePage:
    """Minimal Playwright page: evaluate() dispatches on the JS string; injection returns True."""
    def __init__(self, detect):
        self._detect = detect
        self.url = "https://boards.example.com/apply"
        self.injected = False

    async def evaluate(self, js, *args):
        if "cf-turnstile" in js and "closest" in js:      # _DETECT_JS
            return self._detect
        if "callGrecaptchaCb" in js or "setField" in js:  # _INJECT_JS
            self.injected = True
            return True
        if "captcha-sdk.awswaf.com" in js:                # _AWS_WAF_DETECT_JS
            return None
        return None


def test_solve_on_page_no_key_is_noop(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    called = {"n": 0}

    async def _fake_solve(*a, **k):
        called["n"] += 1
        return "X"

    monkeypatch.setattr(cs, "solve", _fake_solve)
    page = _FakePage({"kind": "turnstile", "key": "sk", "enterprise": False})
    # A token captcha is present but no key -> the paid tier must NOT fire.
    assert _run(cs.solve_on_page(page)) is False
    assert called["n"] == 0
    assert page.injected is False


def test_solve_on_page_escalates_and_injects(monkeypatch):
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "k")
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)

    async def _fake_solve(kind, site_key, page_url, action=None, *, enterprise=False, **kw):
        assert kind == "turnstile" and site_key == "sk"
        return "TOKEN"

    monkeypatch.setattr(cs, "solve", _fake_solve)
    page = _FakePage({"kind": "turnstile", "key": "sk", "enterprise": False})
    assert _run(cs.solve_on_page(page)) is True
    assert page.injected is True
