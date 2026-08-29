"""Password hashing + signed-cookie session for the interview scheduler cabinet."""
import logging
import os

import bcrypt
from fastapi import HTTPException, Request
from itsdangerous import URLSafeTimedSerializer

from backend.config import settings

log = logging.getLogger(__name__)

COOKIE_NAME = "iv_session"
_SALT = "iv-session"
DEFAULT_MAX_AGE = 30 * 24 * 3600  # 30 days, in seconds
_DEV_SECRET = "dev-insecure-change-me"


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, h: str) -> bool:
    if not isinstance(h, str) or not h:
        return False
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except (ValueError, TypeError, AttributeError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    configured = (
        settings.interview_session_secret
        or os.environ.get("INTERVIEW_SESSION_SECRET")
    )
    if not configured:
        # Fail closed in production (nginx/HTTPS deploy sets IV_COOKIE_SECURE=1): an
        # unconfigured signing secret would make every session cookie forgeable.
        if os.environ.get("IV_COOKIE_SECURE") == "1":
            raise RuntimeError("INTERVIEW_SESSION_SECRET must be set in production")
        log.warning(
            "INTERVIEW_SESSION_SECRET is not set — using an INSECURE dev secret; "
            "set it (and IV_COOKIE_SECURE=1) before any real deploy")
    secret = configured or _DEV_SECRET
    return URLSafeTimedSerializer(secret, salt=_SALT)


def make_session(rid: int) -> str:
    return _serializer().dumps({"rid": rid})


def read_session(token: str, max_age: int = DEFAULT_MAX_AGE) -> int | None:
    try:
        data = _serializer().loads(token, max_age=max_age)
    except Exception:
        return None
    try:
        return int(data["rid"])
    except (KeyError, TypeError, ValueError):
        return None


def current_responsible(request: Request) -> dict:
    """FastAPI dependency: resolve the logged-in responsible from the session cookie.

    Imports backend.interviews.db lazily — that module may not exist yet during
    parallel development, and a module-level import would break importing this one.
    """
    from backend.interviews import db

    redirect = HTTPException(status_code=303, headers={"Location": "/login"})
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise redirect
    rid = read_session(token)
    if rid is None:
        raise redirect
    try:
        responsible = db.get_responsible(rid)
    except Exception:
        raise redirect
    # Re-check on every request: a deactivated employee's existing cookie must stop
    # working immediately, not only fail the next login.
    if not responsible or not responsible.get("active"):
        raise redirect
    return responsible
