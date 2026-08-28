"""backend/interviews/auth.py: bcrypt hashing + itsdangerous signed-cookie session."""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import backend.interviews.auth as auth
import backend.interviews.db as db


def test_bcrypt_roundtrip():
    h = auth.hash_password("s3cret!")
    assert h != "s3cret!"
    assert auth.verify_password("s3cret!", h) is True
    assert auth.verify_password("wrong", h) is False


def test_session_roundtrip():
    token = auth.make_session(42)
    assert auth.read_session(token) == 42


def test_tampered_session_rejected():
    token = auth.make_session(42)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert auth.read_session(tampered) is None


def test_expired_session_rejected():
    token = auth.make_session(1)
    assert auth.read_session(token, max_age=-1) is None


def test_verify_password_none_hash_returns_false():
    assert auth.verify_password("x", None) is False
    assert auth.verify_password("x", "") is False


def test_current_responsible_wired_dependency(monkeypatch):
    fake_responsible = {"id": 1, "login": "alan", "name": "Alan", "active": True}
    monkeypatch.setattr(db, "get_responsible", lambda rid: fake_responsible if rid == 1 else None)

    app = FastAPI()

    @app.get("/whoami")
    def whoami(responsible: dict = Depends(auth.current_responsible)):
        return responsible

    client = TestClient(app)

    # No cookie -> redirected to the login page, never reaching the handler.
    resp = client.get("/whoami", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/login"

    # Valid session cookie -> handler runs and returns the resolved responsible.
    token = auth.make_session(1)
    client.cookies.set(auth.COOKIE_NAME, token)
    resp = client.get("/whoami", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.json() == fake_responsible


def _wired_client(monkeypatch, responsible):
    """A TestClient whose only route depends on current_responsible, with
    db.get_responsible stubbed to return `responsible` for rid==1 and a valid cookie set."""
    monkeypatch.setattr(db, "get_responsible", lambda rid: responsible if rid == 1 else None)
    app = FastAPI()

    @app.get("/whoami")
    def whoami(r: dict = Depends(auth.current_responsible)):
        return r

    client = TestClient(app)
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(1))
    return client


def test_current_responsible_rejects_inactive(monkeypatch):
    # A deactivated employee's still-valid cookie must stop working immediately.
    client = _wired_client(monkeypatch, {"id": 1, "login": "alan", "name": "Alan", "active": False})
    resp = client.get("/whoami", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/login"

    # An active responsible still passes.
    ok_client = _wired_client(
        monkeypatch, {"id": 1, "login": "alan", "name": "Alan", "active": True})
    ok = ok_client.get("/whoami", follow_redirects=False)
    assert ok.status_code == 200
    assert ok.json()["login"] == "alan"


def test_serializer_fail_closed_in_prod(monkeypatch):
    # No secret configured (neither settings nor env) AND production flag set -> raise.
    monkeypatch.setattr(auth.settings, "interview_session_secret", "", raising=False)
    monkeypatch.delenv("INTERVIEW_SESSION_SECRET", raising=False)
    monkeypatch.setenv("IV_COOKIE_SECURE", "1")
    with pytest.raises(RuntimeError):
        auth._serializer()
    # ...and it also blocks the higher-level session calls.
    with pytest.raises(RuntimeError):
        auth.make_session(1)

    # With a secret configured, no raise (even with the production flag on).
    monkeypatch.setattr(auth.settings, "interview_session_secret", "a-real-secret", raising=False)
    token = auth.make_session(7)
    assert auth.read_session(token) == 7
