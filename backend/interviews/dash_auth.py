"""In-app admin login + a fail-closed auth gate for the LIVE operator dashboard.

Isolated here so `backend.dashboard_app` only needs a one-line `dash_auth.install(app)`
at import. The gate requires, for EVERY request except an EXACT allowlist, a valid
session cookie whose responsible is `role=='admin'` AND `active` — otherwise it
redirects (303) to `/login`. Employees are NOT admins (they use the separate cabinet)
and are rejected.

nginx basic-auth stays as an OUTER gate for now; this becomes the sole gate at deploy.
"""
from __future__ import annotations

import logging
import os
from html import escape

from fastapi import Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from backend.interviews import auth, db
from backend.tools import mailcrm_ui

log = logging.getLogger(__name__)

# EXACT-match paths that bypass the gate. The last six are the browser-extension
# endpoints that self-authenticate via the `X-Assist-Token` header, so they MUST reach
# their handler without a session cookie. NOTE: exact match — do NOT add "/drafts"
# (that is an operator HTML view and must stay gated).
ALLOWLIST = {
    "/login", "/logout", "/favicon.ico",
    "/draft", "/assist", "/profile_form", "/job_pack", "/resume_file", "/mark_ext",
}

# Secure cookie on the HTTPS deploy (nginx). Off by default so an http TestClient works;
# the deploy sets IV_COOKIE_SECURE=1 (mirrors backend.interviews.cabinet_app).
_COOKIE_SECURE = os.environ.get("IV_COOKIE_SECURE") == "1"


# ---- style (standalone login page) -------------------------------------------------
_LOGIN_CSS = """
.login-wrap{max-width:360px;margin:10vh auto 0;}
.login-wrap .brand{width:44px;height:44px;border-radius:11px;background:var(--accent);
  color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;
  font-size:19px;margin:0 auto 18px;}
.login-wrap h1{font-size:20px;font-weight:600;text-align:center;letter-spacing:-.02em;
  margin:0 0 16px;color:var(--ink);}
.login-wrap .card{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--r);padding:24px;}
.login-wrap label{display:block;margin-top:14px;font-weight:600;font-size:13px;
  color:var(--ink-soft);}
.login-wrap input{width:100%;}
.login-wrap button{width:100%;margin-top:18px;}
.login-wrap .err{background:#fce8e6;color:var(--danger);border-radius:var(--r-sm);
  padding:9px 14px;margin-bottom:16px;font-weight:600;font-size:13px;}
"""


def _doc(body: str, title: str = "Вход") -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title>" + mailcrm_ui._FONTS +
        f"<style>{mailcrm_ui._CSS}{_LOGIN_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>")


def login_page(error: str = "") -> str:
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    body = (
        '<div class="login-wrap"><div class="brand">JF</div>'
        '<h1>Вход</h1>'
        f'{err}'
        '<div class="card"><form method="post" action="/login">'
        '<label>Логин</label>'
        '<input name="login" autocomplete="username" autofocus required>'
        '<label>Пароль</label>'
        '<input name="password" type="password" autocomplete="current-password" required>'
        '<button class="primary" type="submit">Войти</button>'
        '</form></div></div>')
    return _doc(body)


# ---- auth resolution ---------------------------------------------------------------
def _responsible_from_request(request: Request) -> dict | None:
    """Resolve the ACTIVE responsible (any role) from the session cookie, or None.

    Never raises — any failure (bad cookie, DB down, …) resolves to None so the caller
    fails closed to /login.
    """
    try:
        rid = auth.read_session(request.cookies.get(auth.COOKIE_NAME, ""))
        if rid is None:
            return None
        resp = db.get_responsible(rid)
        if resp and resp.get("active"):
            return resp
    except Exception:
        return None
    return None


def _admin_from_request(request: Request) -> dict | None:
    """The active responsible ONLY if role=='admin', else None (kept for callers that
    specifically need an admin)."""
    resp = _responsible_from_request(request)
    return resp if (resp and resp.get("role") == "admin") else None


def _home_for(resp: dict) -> str:
    """Where a freshly-authenticated / misrouted session belongs: admins own the whole
    operator dashboard (/), employees are confined to their cabinet (/cabinet)."""
    return "/" if resp.get("role") == "admin" else "/cabinet"


def _employee_allowed(path: str) -> bool:
    """The employee WHITELIST: an employee may reach ONLY their own cabinet surface.
    Everything else on this PII dashboard is blocked at the door (redirected to /cabinet),
    so isolation rests on this single positive check, not on every operator route
    remembering to exclude employees. /logout is already in ALLOWLIST (open to all)."""
    return path == "/cabinet" or path.startswith("/cabinet/")


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Fail-closed role gate over the merged dashboard:
      * ALLOWLIST paths (login/logout/favicon + self-authenticating extension endpoints)
        pass without a session.
      * no valid session            -> 303 /login
      * role=='admin'  (+active)    -> full access
      * role=='employee' (+active)  -> ONLY /cabinet/* (whitelist), else 303 /cabinet
    """
    async def dispatch(self, request, call_next):
        if request.url.path in ALLOWLIST:
            return await call_next(request)
        # Resolve OUTSIDE call_next so a downstream handler error is never masked as a
        # login redirect; any auth failure -> fail closed to /login.
        try:
            resp = _responsible_from_request(request)
        except Exception:
            resp = None
        if resp is None:
            return RedirectResponse("/login", status_code=303)
        if resp.get("role") == "admin":
            return await call_next(request)
        # employee: confined to the cabinet whitelist
        if _employee_allowed(request.url.path):
            return await call_next(request)
        return RedirectResponse("/cabinet", status_code=303)


# ---- install -----------------------------------------------------------------------
def install(app) -> None:
    """Add the fail-closed role gate + the SHARED /login, /logout routes. One login for
    both roles; the post-login redirect and the middleware route each role to its home."""
    app.add_middleware(AdminAuthMiddleware)

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        resp = _responsible_from_request(request)
        if resp is not None:
            return RedirectResponse(_home_for(resp), status_code=303)
        return HTMLResponse(login_page())

    @app.post("/login")
    def login_post(login: str = Form(...), password: str = Form(...)):
        row = None
        try:
            row = db.get_responsible_by_login(login)
        except Exception as e:
            log.warning("dash_auth login lookup failed: %s", e)
        if (row and row.get("active")
                and auth.verify_password(password, row.get("password_hash") or "")):
            resp = RedirectResponse(_home_for(row), status_code=303)
            resp.set_cookie(auth.COOKIE_NAME, auth.make_session(row["id"]),
                            httponly=True, samesite="lax", path="/",
                            secure=_COOKIE_SECURE)
            return resp
        # Generic message for every failure (wrong login / wrong password / inactive) —
        # no user enumeration.
        return HTMLResponse(login_page("Неверный логин или пароль"), status_code=200)

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE_NAME, path="/")
        return resp
