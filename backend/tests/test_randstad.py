"""Unit tests for the Randstad résumé-drop lane + its FriendlyCaptcha (2captcha) solver wiring.

All network-free: `captcha_solver.httpx.AsyncClient` is monkeypatched with a scripted fake and the
2captcha poll interval is zeroed, so nothing touches the network and no test really sleeps. Covers:
  * the FriendlyCaptcha 2captcha in.php param build + solve routing (routes to 2captcha even when the
    default provider is CapSolver, which has no FriendlyCaptcha task type),
  * the key resolution (TWOCAPTCHA_KEY vs CAPTCHA_SOLVER_KEY) + the no-op-without-a-key behaviour,
  * that existing CapSolver kinds are unaffected,
  * the strategy helpers (RANDSTAD_ADVANCE gate, eligibility, drop-form shaping, ack matching).
"""
import asyncio

from backend.applier import captcha_solver as cs
from backend.applier.strategies import randstad as R


def _run(coro):
    return asyncio.run(coro)


# ---- a scripted fake httpx.AsyncClient (GET-based 2captcha in.php/res.php) ------------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeGetClient:
    """Scripts /in.php -> `in_resp` and /res.php -> the `res` queue (perpetual NOT_READY once drained).
    Records every (url, params)."""

    def __init__(self, *, in_resp=None, res=None):
        self.in_resp = in_resp if in_resp is not None else {"status": "1", "request": "CAPID"}
        self.res = list(res or [{"status": "1", "request": "FRC-TOKEN"}])
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/in.php"):
            return _Resp(self.in_resp)
        if self.res:
            return _Resp(self.res.pop(0))
        return _Resp({"status": "0", "request": "CAPCHA_NOT_READY"})


def _arm(monkeypatch, client, *, provider=None, key="tc-key", twocaptcha_key=None):
    monkeypatch.setattr(cs, "httpx", _FakeHttpx(client))
    monkeypatch.setattr(cs, "_POLL_INTERVAL", 0)
    if provider is not None:
        monkeypatch.setenv("CAPTCHA_SOLVER_PROVIDER", provider)
    else:
        monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    if key is not None:
        monkeypatch.setenv("CAPTCHA_SOLVER_KEY", key)
    else:
        monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    if twocaptcha_key is not None:
        monkeypatch.setenv("TWOCAPTCHA_KEY", twocaptcha_key)
    else:
        monkeypatch.delenv("TWOCAPTCHA_KEY", raising=False)
    return client


class _FakeHttpx:
    """A stand-in for the `httpx` module exposing only AsyncClient(...)=our fake client."""

    def __init__(self, client):
        self._client = client

    def AsyncClient(self, *a, **k):  # noqa: N802 (match httpx API)
        return self._client


# ---- FriendlyCaptcha 2captcha param build + routing ----------------------------------------

def test_friendlycaptcha_in_php_param_build(monkeypatch):
    c = _arm(monkeypatch, _FakeGetClient())
    tok = _run(cs._twocaptcha_solve("friendlycaptcha", "SITEKEY123", "https://x.example/drop"))
    assert tok == "FRC-TOKEN"
    inphp = [p for (u, p) in c.calls if u.endswith("/in.php")][0]
    assert inphp["method"] == "friendlycaptcha"
    assert inphp["sitekey"] == "SITEKEY123"
    assert inphp["pageurl"] == "https://x.example/drop"
    assert inphp["key"] == "tc-key"
    # NOT a reCAPTCHA build — no googlekey / version
    assert "googlekey" not in inphp and "version" not in inphp


def test_solve_routes_friendlycaptcha_to_2captcha_even_when_provider_is_capsolver(monkeypatch):
    # default provider (capsolver) — FriendlyCaptcha must STILL go to 2captcha (capsolver can't do it)
    c = _arm(monkeypatch, _FakeGetClient(), provider=None, key="tc-key")
    tok = _run(cs.solve("friendlycaptcha", "SK", "https://x/drop"))
    assert tok == "FRC-TOKEN"
    assert any(u.endswith("/in.php") for (u, _) in c.calls)


def test_solve_friendlycaptcha_uses_twocaptcha_key_over_solver_key(monkeypatch):
    c = _arm(monkeypatch, _FakeGetClient(), key="solver-key", twocaptcha_key="dedicated-2c")
    _run(cs.solve("friendlycaptcha", "SK", "https://x/drop"))
    inphp = [p for (u, p) in c.calls if u.endswith("/in.php")][0]
    assert inphp["key"] == "dedicated-2c"


def test_solve_friendlycaptcha_none_without_any_key(monkeypatch):
    c = _arm(monkeypatch, _FakeGetClient(), key=None, twocaptcha_key=None)
    assert _run(cs.solve("friendlycaptcha", "SK", "https://x/drop")) is None
    assert c.calls == []  # no network attempted


def test_solve_friendlycaptcha_none_without_sitekey(monkeypatch):
    _arm(monkeypatch, _FakeGetClient())
    assert _run(cs.solve("friendlycaptcha", "", "https://x/drop")) is None


def test_solve_friendlycaptcha_returns_none_on_2captcha_error(monkeypatch):
    c = _FakeGetClient(in_resp={"status": "0", "request": "ERROR_WRONG_USER_KEY"})
    _arm(monkeypatch, c)
    assert _run(cs.solve("friendlycaptcha", "SK", "https://x/drop")) is None


def test_friendlycaptcha_available_and_key_resolution(monkeypatch):
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_KEY", raising=False)
    assert cs.friendlycaptcha_available() is False
    monkeypatch.setenv("TWOCAPTCHA_KEY", "only-2c")
    assert cs.friendlycaptcha_available() is True
    assert cs._twocaptcha_key() == "only-2c"          # dedicated key wins
    monkeypatch.setenv("CAPTCHA_SOLVER_KEY", "solver")
    assert cs._twocaptcha_key() == "only-2c"
    monkeypatch.delenv("TWOCAPTCHA_KEY", raising=False)
    assert cs._twocaptcha_key() == "solver"           # falls back to the solver key


# ---- existing CapSolver kinds unaffected ---------------------------------------------------

def test_capsolver_kinds_unchanged_no_key_noop(monkeypatch):
    # turnstile with no key + default provider => None (is_enabled False), no FriendlyCaptcha regression
    monkeypatch.delenv("CAPTCHA_SOLVER_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_KEY", raising=False)
    monkeypatch.delenv("CAPTCHA_SOLVER_PROVIDER", raising=False)
    assert _run(cs.solve("turnstile", "SK", "https://x/y")) is None
    # friendlycaptcha is NOT a CapSolver task type
    assert "friendlycaptcha" not in cs._CAPSOLVER_TASK
    # but it IS a 2captcha method
    assert cs._TWOCAPTCHA_METHOD["friendlycaptcha"] == "friendlycaptcha"


def test_turnstile_still_routes_to_2captcha_when_provider_twocaptcha(monkeypatch):
    c = _arm(monkeypatch, _FakeGetClient(res=[{"status": "1", "request": "TS-TOKEN"}]),
             provider="twocaptcha")
    tok = _run(cs.solve("turnstile", "SK", "https://x/y"))
    assert tok == "TS-TOKEN"
    inphp = [p for (u, p) in c.calls if u.endswith("/in.php")][0]
    assert inphp["method"] == "turnstile" and inphp["sitekey"] == "SK"


# ---- strategy pure helpers -----------------------------------------------------------------

def test_advance_gate(monkeypatch):
    monkeypatch.delenv("RANDSTAD_ADVANCE", raising=False)
    assert R.advance_enabled() is False
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("RANDSTAD_ADVANCE", v)
        assert R.advance_enabled() is True
    monkeypatch.setenv("RANDSTAD_ADVANCE", "0")
    assert R.advance_enabled() is False


def test_row_is_staffable():
    assert R.row_is_staffable("Remote Customer Service Representative") is True
    assert R.row_is_staffable("Bilingual Call Center Agent") is True
    assert R.row_is_staffable("Licensed Insurance Agent") is False
    assert R.row_is_staffable("Life & Health Insurance Sales") is False
    assert R.row_is_staffable("Series 7 Registered Rep") is False


def test_build_drop_form_and_required():
    persona = {"full_name": "Mary Jane Watson", "email": "mjw12@takhet.com",
               "phone": "16145550142", "city": "Columbus", "state": "Ohio",
               "title": "Customer Service Representative"}
    form = R.build_drop_form(persona)
    assert form["first_name"] == "Mary" and form["last_name"] == "Jane Watson"
    assert form["email_address"] == "mjw12@takhet.com"
    assert form["phone_number"] == "(614) 555-0142"
    assert form["job_location"] == "Columbus, OH"
    assert form["job_title"] == "Customer Service Representative"
    assert form["op"] == R.RANDSTAD_OP and form["webform_id"] == R.RANDSTAD_WEBFORM_ID
    assert R.missing_required(form) == []
    # a persona missing name+email surfaces the required gaps
    bad = R.build_drop_form({"city": "Austin", "state": "TX"})
    miss = R.missing_required(bad)
    assert "first_name" in miss and "email_address" in miss


def test_split_name_and_phone():
    assert R.split_name("Alan") == ("Alan", "Alan")
    assert R.split_name("Jane Doe") == ("Jane", "Doe")
    assert R.split_name("") == ("", "")
    assert R.phone_digits("(614) 555-0142") == "(614) 555-0142"
    assert R.phone_digits("6145550142") == "(614) 555-0142"
    assert R.phone_digits("1-614-555-0142") == "(614) 555-0142"


def test_page_has_friendlycaptcha_and_guest():
    assert R.page_has_friendlycaptcha('<div class="frc-captcha" data-sitekey="x"></div>') is True
    assert R.page_has_friendlycaptcha('<div class="bluex-friendly-captcha"></div>') is True
    assert R.page_has_friendlycaptcha('<script src="/friendly-challenge.js"></script>') is True
    assert R.page_has_friendlycaptcha('<div class="g-recaptcha"></div>') is False
    assert R.has_guest_button("Continue as guest") is True
    assert R.has_guest_button("Sign in to apply") is False


def test_is_drop_ack():
    assert R.is_drop_ack(200, "Thank you! We've received your résumé.") is True
    assert R.is_drop_ack(200, '{"redirect":"/thanks","settings":{}}') is True
    assert R.is_drop_ack(200, "Browser check failed. Please try again.") is False
    assert R.is_drop_ack(200, "This field is required") is False
    assert R.is_drop_ack(500, "Thank you") is False
    # a body that carries BOTH an ack phrase and a benign word still counts as an ack
    assert R.is_drop_ack(200, "Your application was submitted successfully") is True
