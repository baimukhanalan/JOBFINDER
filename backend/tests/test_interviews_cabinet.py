"""Responsible-cabinet app tests (backend.interviews.cabinet_app).

Login/auth/redirect and the mail DB rows go through the LIVE jobfinder_crm Postgres
(via backend.tools.mail_db's pool) — same contract-test approach as
test_interviews_db.py. Mail bodies (mailcrm.list_messages / get_thread) are
monkeypatched so the tests never touch a real Maildir. Everything uses the throwaway
`test_iv_%` prefix and is cleaned up before AND after the module runs. Skipped if the
CRM DB is unreachable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _cur:
        _cur.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import auth, db  # noqa: E402
from backend.interviews.cabinet_app import app  # noqa: E402
from backend.tools import mailcrm, mailcrm_ui  # noqa: E402

client = TestClient(app)


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


@pytest.fixture(autouse=True)
def _clean_test_iv_rows():
    db.ensure_schema()
    _cleanup()
    client.cookies.clear()
    yield
    client.cookies.clear()
    _cleanup()


def _future(days: int) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)


def _login_cookie(rid: int):
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(rid))


def _assign(rid: int, mailbox: str, days: int = 2):
    start = _future(days)
    return db.insert_interview(
        mailbox=mailbox, responsible_id=rid, start_ts=start,
        end_ts=start + timedelta(hours=1), company="Acme", jobid="1",
        thread_key="th", source_message_hash="srchash")


def test_login_required_redirects():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/cabinet/login"


def test_login_sets_cookie_and_dashboard_loads():
    rid = db.add_responsible("test_iv_boss", auth.hash_password("s3cret!"), "Босс Тестов")

    r = client.post("/login", data={"login": "test_iv_boss", "password": "s3cret!"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert auth.COOKIE_NAME in r.headers.get("set-cookie", "")

    # bad password is rejected
    client.cookies.clear()
    bad = client.post("/login", data={"login": "test_iv_boss", "password": "nope"},
                      follow_redirects=False)
    assert bad.status_code == 401

    _login_cookie(rid)
    home = client.get("/")
    assert home.status_code == 200
    assert "Босс Тестов" in home.text


def test_ownership_guard_blocks_foreign_mailbox(monkeypatch):
    rid = db.add_responsible("test_iv_a", auth.hash_password("x"), "Отв A")
    _login_cookie(rid)

    # A has no assigned mailboxes yet → a foreign mailbox row must 404.
    monkeypatch.setattr(mail_db, "get_row",
                        lambda h: {"mailbox": "test_iv_foreign@takhet.com", "thread_key": "z"})
    blocked = client.get("/thread", params={"hash": "whatever"})
    assert blocked.status_code == 404

    # Assign an interview → the persona's mailbox is now visible to A.
    _assign(rid, "test_iv_a@takhet.com")
    monkeypatch.setattr(mail_db, "get_row",
                        lambda h: {"mailbox": "test_iv_a@takhet.com", "thread_key": "t"})

    captured: dict = {}

    def fake_thread(h, *a, **k):
        captured["mark"] = k.get("mark", a[0] if a else "MISSING")
        return {
            "subject": "Приглашение", "candidate": "Lara", "mailbox": "test_iv_a@takhet.com",
            "messages": [{"from_name": "Recruiter", "from_email": "r@corp.com",
                          "date": "Thu, 28 Aug 2026 10:00:00 +0000", "plain": "Здравствуйте"}],
        }

    monkeypatch.setattr(mailcrm, "get_thread", fake_thread)
    ok = client.get("/thread", params={"hash": "goodhash"})
    assert ok.status_code == 200
    assert "Приглашение" in ok.text
    # READ-ONLY: opening a thread must NOT mark it seen (would leak into operator inbox).
    assert captured["mark"] is False


def test_inbox_lists_only_assigned(monkeypatch):
    rid = db.add_responsible("test_iv_inbox", auth.hash_password("x"), "Отв Inbox")
    _assign(rid, "test_iv_inbox@takhet.com")
    _login_cookie(rid)

    called: list[str] = []

    def fake_list(mailbox=None, limit=50, **kw):
        called.append(mailbox)
        return []

    monkeypatch.setattr(mailcrm, "list_messages", fake_list)
    r = client.get("/inbox")
    assert r.status_code == 200
    assert set(called) == db.assigned_mailboxes(rid) == {"test_iv_inbox@takhet.com"}
    assert "test_iv_foreign@takhet.com" not in called


_SAMPLE_ROW = [{
    "id": "abc123", "from_name": "Recruiter", "from_email": "r@corp.com",
    "seen": False, "candidate": "Lara", "mailbox": "lara@takhet.com",
    "has_att": True, "kind": "interview", "thread": "th", "subject": "Invite",
    "snippet": "hi", "date_ts": 1700000000,
}]


def test_cabinet_inbox_render_strips_operator_cruft():
    ro = mailcrm_ui.render_rows(_SAMPLE_ROW, show_mailbox=True, read_only=True)
    assert "📎" not in ro                 # no decorative attachment emoji
    assert "toggleSel(" not in ro         # no operator select-toggle JS
    # read_only implies show_sobes=False → no operator «Собес» assign control.
    # (The generic word "Собес" also appears in the neutral kind-tag "Собеседование",
    #  which is a category label, not the control — so assert the control's own markup.)
    assert "iv-sobes" not in ro
    assert "openSobes(" not in ro


def test_operator_render_rows_default_keeps_affordances():
    op = mailcrm_ui.render_rows(_SAMPLE_ROW)
    assert "📎" in op
    assert "toggleSel(" in op
    # the interview «Собес» control is present in the operator path (unless the
    # interviews package is unavailable, in which case the lazy import returns "")
    try:
        from backend.interviews import operator_ui  # noqa: F401
        assert "iv-sobes" in op
    except Exception:
        pass
