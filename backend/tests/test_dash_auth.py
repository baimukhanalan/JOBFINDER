"""Tests for the in-app admin login + fail-closed gate on the operator dashboard
(backend.interviews.dash_auth, installed onto backend.dashboard_app:app).

Hits the LIVE jobfinder_crm Postgres (seeding `iv_responsibles`) — the whole module is
skipped if the DB is unreachable. Every seeded row uses a `test_iv_%` login prefix and a
fixture cleans it up before AND after so a crashed run never leaks rows. No real
credentials are logged or hardcoded (throwaway test-only password).
"""
from __future__ import annotations

import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _cur:
        _cur.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from backend.dashboard_app import app  # noqa: E402
from backend.interviews import auth, db  # noqa: E402

client = TestClient(app)

_ADMIN_LOGIN = "test_iv_dash_admin"
_EMP_LOGIN = "test_iv_dash_emp"
_PW = "throwaway-test-pw-9271"


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


@pytest.fixture(autouse=True)
def _clean_rows():
    db.ensure_schema()
    _cleanup()
    client.cookies.clear()  # no session leaks between tests
    yield
    _cleanup()
    client.cookies.clear()


def _has_set_cookie(resp) -> bool:
    return "set-cookie" in {k.lower() for k in resp.headers}


def test_unauth_root_redirects_to_login():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/login")


def test_unauth_inner_route_redirects_to_login():
    r = client.get("/mail/candidates", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/login")


def test_login_page_renders():
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="login"' in r.text
    assert 'name="password"' in r.text


def test_admin_login_then_root_passes_gate():
    db.add_responsible(_ADMIN_LOGIN, auth.hash_password(_PW), "Dash Admin", role="admin")

    r = client.post("/login", data={"login": _ADMIN_LOGIN, "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _has_set_cookie(r)
    cookie = r.cookies.get(auth.COOKIE_NAME)
    assert cookie

    # With the admin session cookie, the gate PASSES — so we reach the "/" handler,
    # whose OWN redirect points at /mail/candidates (NOT the gate's /login). The "/"
    # handler uses RedirectResponse's default 307, so assert a redirect + the target,
    # not a specific code — what matters is it wasn't bounced back to /login.
    client.cookies.set(auth.COOKIE_NAME, cookie)
    r2 = client.get("/", follow_redirects=False)
    assert 300 <= r2.status_code < 400
    assert r2.headers["location"].endswith("/mail/candidates")


def test_employee_login_rejected():
    db.add_responsible(_EMP_LOGIN, auth.hash_password(_PW), "Dash Emp", role="employee")

    r = client.post("/login", data={"login": _EMP_LOGIN, "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 200  # re-rendered login page, not a redirect
    assert not _has_set_cookie(r)  # no admin session granted
    assert 'name="password"' in r.text


def test_extension_endpoint_bypasses_gate():
    # An allowlisted extension endpoint must reach its own handler (it self-authenticates
    # via X-Assist-Token → 401/200), never be bounced to /login by the gate.
    r = client.get("/profile_form", follow_redirects=False)
    if r.status_code == 303:
        assert not r.headers.get("location", "").endswith("/login")


def test_install_fatal_in_sole_gate_mode(monkeypatch):
    """The gate-install guard degrades gracefully while basic-auth still fronts the
    dashboard, but MUST be fatal in sole-gate mode (IV_COOKIE_SECURE=1) so an install
    failure never boots the PII CRM ungated. DB-free."""
    from backend.dashboard_app import _install_dash_auth
    from backend.interviews import dash_auth

    def _boom(_app):
        raise RuntimeError("boom")

    monkeypatch.setattr(dash_auth, "install", _boom)
    dummy_app = object()

    # basic-auth-fronted deploy (signal unset): the failure is swallowed (logs only).
    monkeypatch.delenv("IV_COOKIE_SECURE", raising=False)
    _install_dash_auth(dummy_app)  # must NOT raise

    # sole-gate mode: refuse to boot ungated → re-raise.
    monkeypatch.setenv("IV_COOKIE_SECURE", "1")
    with pytest.raises(RuntimeError):
        _install_dash_auth(dummy_app)
