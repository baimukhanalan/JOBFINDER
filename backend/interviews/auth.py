"""Password hashing + signed-cookie session for the interview scheduler cabinet."""
import os

import bcrypt
from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.config import settings

COOKIE_NAME = "iv_session"
_SALT = "iv-session"
DEFAULT_MAX_AGE = 30 * 24 * 3600  # 30 days, in seconds


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except (ValueError, TypeError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    secret = (
        settings.interview_session_secret
        or os.environ.get("INTERVIEW_SESSION_SECRET")
        or "dev-insecure-change-me"
    )
    return URLSafeTimedSerializer(secret, salt=_SALT)


def make_session(rid: int) -> str:
    return _serializer().dumps({"rid": rid})


def read_session(token: str, max_age: int = DEFAULT_MAX_AGE) -> int | None:
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    try:
        return int(data["rid"])
    except (KeyError, TypeError, ValueError):
        return None


def current_responsible(request):
    """FastAPI dependency: resolve the logged-in responsible from the session cookie.

    Imports backend.interviews.db lazily — that module may not exist yet during
    parallel development, and a module-level import would break importing this one.
    """
    from backend.interviews import db

    redirect = HTTPException(status_code=303, headers={"Location": "/cabinet/login"})
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
    if not responsible:
        raise redirect
    return responsible
