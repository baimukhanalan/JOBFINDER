"""Responsible cabinet — a SEPARATE small FastAPI app (own port 8103, own cookie
login). An employee ("ответственный") logs in, edits weekly GMT availability, sees
their upcoming interviews, and reads (READ-ONLY) the mail of ONLY the personas assigned
to them. It is NOT wired into the live operator dashboard.

Served at root; nginx prefixes `/cabinet/`. Run from the repo root:
    uvicorn backend.interviews.cabinet_app:app --host 127.0.0.1 --port 8103

Security core — the ownership guard (`GET /thread`): a responsible must NEVER read
another responsible's mail. Before rendering any thread we resolve the message row and
verify its mailbox is in THIS responsible's assigned set; otherwise 404. The shared
`mailcrm.get_thread` is left unchanged — the guard lives here.
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.interviews import auth, cabinet_ui, db
from backend.tools import mail_db, mailcrm

log = logging.getLogger("cabinet")

app = FastAPI(title="JobFinder cabinet")

# Secure cookie on the HTTPS deploy (nginx). Off by default so http TestClient works;
# the deploy sets IV_COOKIE_SECURE=1.
_COOKIE_SECURE = os.environ.get("IV_COOKIE_SECURE") == "1"


@app.on_event("startup")
def _startup() -> None:
    try:
        db.ensure_schema()
    except Exception as e:  # never block startup on a transient DB hiccup
        log.warning("cabinet ensure_schema failed: %s", e)


# ---- auth --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get() -> HTMLResponse:
    return HTMLResponse(cabinet_ui.login_page())


@app.post("/login")
def login_post(login: str = Form(...), password: str = Form(...)):
    responsible = None
    try:
        responsible = db.get_responsible_by_login(login)
    except Exception as e:
        log.warning("login lookup failed: %s", e)
    if not responsible or not responsible.get("active", True) \
            or not auth.verify_password(password, responsible.get("password_hash", "")):
        return HTMLResponse(cabinet_ui.login_page("Неверный логин или пароль."),
                            status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(responsible["id"]),
                    httponly=True, samesite="lax", path="/", secure=_COOKIE_SECURE)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# ---- pages -------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    interviews = db.interviews_for_responsible(responsible["id"], upcoming_only=True)
    return HTMLResponse(cabinet_ui.dashboard_page(responsible, interviews))


@app.get("/availability", response_class=HTMLResponse)
def availability_get(responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    rows = db.get_availability(responsible["id"])
    return HTMLResponse(cabinet_ui.availability_page(responsible, rows))


def _hhmm_to_min(s: str) -> int:
    try:
        h, m = (s or "").split(":")[:2]
        return max(0, min(24 * 60, int(h) * 60 + int(m)))
    except (ValueError, TypeError):
        return 0


@app.post("/availability", response_class=HTMLResponse)
async def availability_post(request: Request,
                            responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    form = await request.form()
    rows = []
    for d in range(7):
        rows.append({
            "dow": d,
            "start_min": _hhmm_to_min(form.get(f"start_{d}", "")),
            "end_min": _hhmm_to_min(form.get(f"end_{d}", "")),
            "enabled": form.get(f"enabled_{d}") is not None,
        })
    db.set_availability(responsible["id"], rows)
    rows = db.get_availability(responsible["id"])
    return HTMLResponse(cabinet_ui.availability_page(responsible, rows, saved=True))


@app.get("/inbox", response_class=HTMLResponse)
def inbox(responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    rows: list[dict] = []
    for m in sorted(db.assigned_mailboxes(responsible["id"])):
        try:
            rows.extend(mailcrm.list_messages(mailbox=m, limit=100))
        except Exception as e:
            log.warning("list_messages failed for %s: %s", m, e)
    rows.sort(key=lambda r: r.get("date_ts", 0), reverse=True)
    return HTMLResponse(cabinet_ui.inbox_page(responsible, rows))


@app.get("/thread", response_class=HTMLResponse)
def thread(hash: str, responsible: dict = Depends(auth.current_responsible)):
    # OWNERSHIP GUARD (security core): resolve the row first, verify its mailbox is
    # assigned to THIS responsible, and only then read the thread. Any miss → 404.
    row = None
    try:
        row = mail_db.get_row(hash)
    except Exception as e:
        log.warning("get_row failed: %s", e)
    if not row or row.get("mailbox") not in db.assigned_mailboxes(responsible["id"]):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    # READ-ONLY surface: mark=False so opening a thread never flips the persona's
    # messages to seen (which would also move them in the OPERATOR's inbox).
    thread = mailcrm.get_thread(hash, mark=False)
    if not thread:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    return HTMLResponse(cabinet_ui.thread_page(responsible, thread))
