"""«Сгенерировать пакет» — the cabinet prep card + its LLM cheat-sheet fallback + ownership guard.

Pure parts (render / button / cheat-sheet fallback / no stack-disclosure) run always; the ownership
part hits the live jobfinder_crm Postgres (skipped if unreachable), uses a `test_iv_%` prefix, and
forces announced=TRUE in teardown so the ivremind daemon never DMs the owner (shared-DB gotcha)."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.interviews import cabinet_ui, interview_prep
from backend.interviews.interview_prep import PrepPack

_STACK_RE = re.compile(r"(?i)claude|anthropic|openai|chatgpt|\bgpt\b|\bllm\b|\bai\b|\bии\b")
_RESP = {"id": 1035, "name": "Alan Bai", "tz": "Europe/Berlin",
         "roles": ["admin", "manager", "employee"]}


def _pack(**kw):
    base = dict(mailbox="emma.simmons5955@takhet.com", candidate_name="Emma Simmons",
                state="Washington", age=31, company="Affirm", role="CSR", jobid="1406",
                invite_subject="Call with Affirm!", invite_from="rec@affirm.com",
                booking_url="https://calendly.com/x", join_url=None,
                resume_filename="Emma Simmons - resume.pdf", has_resume=True)
    base.update(kw)
    return PrepPack(**base)


# ---------------------------------------------------------------- pure render / button / fallback
def test_prep_page_renders_all_sections():
    html = cabinet_ui.prep_page(_RESP, _pack(), "Вероятные вопросы\n- расскажите о себе", as_id=None)
    for s in ("Пакет к собеседованию", "Данные кандидата (авто)", "Emma Simmons", "Washington",
              "Affirm", "Шпаргалка к собесу", "расскажите о себе"):
        assert s in html, s


def test_prep_page_no_stack_disclosure():
    html = cabinet_ui.prep_page(_RESP, _pack(), "Вероятные вопросы", as_id=None)
    assert _STACK_RE.search(html) is None


def test_prep_page_pruned_resume_and_missing_sheet():
    html = cabinet_ui.prep_page(_RESP, _pack(has_resume=False, resume_filename=""), None, as_id=None)
    assert "не найдено" in html                       # pruned résumé surfaced
    assert "Автошпаргалка сейчас недоступна" in html  # LLM-down card, not an error


def test_home_row_has_generate_button():
    row = cabinet_ui._home_row({"mailbox": "a@b.com", "id": 5, "candidate": "A"}, "Europe/Berlin", None)
    assert "/cabinet/prep?mailbox=" in row and "Сгенерировать пакет" in row


def test_cheat_sheet_llm_down_returns_none(monkeypatch):
    from backend.services.tailor import tailor
    monkeypatch.setattr(tailor, "_llm_complete",
                        lambda prompt: (_ for _ in ()).throw(RuntimeError("down")))
    assert interview_prep.cheat_sheet(_pack()) is None


def test_cheat_sheet_returns_text_when_llm_up(monkeypatch):
    from backend.services.tailor import tailor
    monkeypatch.setattr(tailor, "_llm_complete", lambda prompt: "Вероятные вопросы\n- q1")
    out = interview_prep.cheat_sheet(_pack())
    assert out and "Вероятные вопросы" in out


def test_cheat_sheet_empty_llm_returns_none(monkeypatch):
    from backend.services.tailor import tailor
    monkeypatch.setattr(tailor, "_llm_complete", lambda prompt: "   ")
    assert interview_prep.cheat_sheet(_pack()) is None


# ------------------------------------------------------------------------- live-DB ownership guard
try:
    from backend.tools import mail_db
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
    _HAVE_DB = True
except Exception:
    _HAVE_DB = False

pytestmark = []


@pytest.mark.skipif(not _HAVE_DB, reason="no CRM DB")
def test_prep_route_ownership_guard():
    from fastapi.testclient import TestClient
    from backend.dashboard_app import app
    from backend.interviews import auth, db

    PW = "throwaway-test-pw-7742"
    MB = "test_iv_prep_cand@takhet.com"

    def _retry(fn, tries=6):
        for i in range(tries):
            try:
                return fn()
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(0.6)

    def _cleanup():
        with mail_db._cur(dict_rows=False) as cur:
            cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_prep%'")
            cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_prep%'")

    _retry(db.ensure_schema)
    _retry(_cleanup)
    client = TestClient(app)
    try:
        rid = _retry(lambda: db.add_responsible("test_iv_prep_E", auth.hash_password(PW),
                                                "Prep E", role="employee"))
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
        iid = _retry(lambda: db.insert_interview(
            mailbox=MB, responsible_id=rid, start_ts=start, end_ts=start + timedelta(hours=1),
            company="Acme", jobid="1", thread_key="t1", source_message_hash="h_prep"))
        _retry(lambda: _mark_announced())

        def _login():
            client.cookies.clear()
            r = client.post("/login", data={"login": "test_iv_prep_E", "password": PW},
                            follow_redirects=False)
            assert r.status_code == 303
            client.cookies.set(auth.COOKIE_NAME, r.cookies.get(auth.COOKIE_NAME))
        _retry(_login)

        def _get(url):
            for _ in range(20):
                r = client.get(url, follow_redirects=False)
                if not (r.status_code in (302, 303, 307) and r.headers.get("location", "").endswith("/login")):
                    return r
                time.sleep(1.0)
            return r

        # OWN mailbox → 200 prep card
        r_own = _get(f"/cabinet/prep?mailbox={MB}")
        assert r_own.status_code == 200 and "Пакет к собеседованию" in r_own.text
        # FOREIGN mailbox the user does not own → 404 (ownership guard)
        r_foreign = _get("/cabinet/prep?mailbox=someone.else9999@takhet.com")
        assert r_foreign.status_code == 404
    finally:
        _retry(_mark_announced)
        _retry(_cleanup)
        client.cookies.clear()


def _mark_announced():
    from backend.tools import mail_db as _m
    with _m._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_prep%'")
