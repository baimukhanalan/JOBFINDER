"""Tests for the responsible admin CLI (backend.interviews.admin_cli).

Like test_interviews_db.py, the CLI tests hit the LIVE jobfinder_crm Postgres DB
(via backend.tools.mail_db's pool) — no mock for this layer. Every fixture uses a
throwaway login prefix `test_iv_%` and a fixture cleans it up before AND after the
module runs so a crashed prior run never leaks rows into the next one. If
CRM_PG_DSN is unset / the DB is unreachable, the whole module is skipped.
"""
from __future__ import annotations

import re

import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _cur:
        _cur.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import admin_cli, auth, db  # noqa: E402


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "DELETE FROM iv_availability WHERE responsible_id IN "
            "(SELECT id FROM iv_responsibles WHERE login LIKE 'test_iv_%')"
        )
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


@pytest.fixture(autouse=True)
def _clean_test_iv_rows():
    db.ensure_schema()
    _cleanup()
    yield
    _cleanup()


def test_hhmm_to_min():
    assert admin_cli.hhmm_to_min("00:00") == 0
    assert admin_cli.hhmm_to_min("09:30") == 570
    assert admin_cli.hhmm_to_min("20:00") == 1200
    assert admin_cli.hhmm_to_min("09:00") == 540
    assert admin_cli.hhmm_to_min("23:59") == 1439


def test_cli_add_creates_responsible():
    admin_cli.main(["add", "--login", "test_iv_bob", "--name", "Bob", "--password", "x"])

    fetched = db.get_responsible_by_login("test_iv_bob")
    assert fetched is not None
    assert fetched["name"] == "Bob"
    assert auth.verify_password("x", fetched["password_hash"])


def test_cli_add_without_password_generates_and_prints(capsys):
    admin_cli.main(["add", "--login", "test_iv_carl", "--name", "Carl"])

    fetched = db.get_responsible_by_login("test_iv_carl")
    assert fetched is not None
    assert fetched["name"] == "Carl"

    out = capsys.readouterr().out
    match = re.search(r"Generated password:\s*(\S+)", out)
    assert match is not None, f"no 'Generated password:' line in output: {out!r}"
    printed_password = match.group(1)
    # the PRINTED password must actually verify against what got stored
    assert auth.verify_password(printed_password, fetched["password_hash"])


def test_cli_add_defaults_tz_to_utc():
    admin_cli.main(["add", "--login", "test_iv_dana", "--name", "Dana", "--password", "x"])
    fetched = db.get_responsible_by_login("test_iv_dana")
    assert fetched["tz"] == "UTC"


def test_cli_add_custom_tz():
    admin_cli.main(["add", "--login", "test_iv_eve", "--name", "Eve",
                     "--password", "x", "--tz", "Asia/Almaty"])
    fetched = db.get_responsible_by_login("test_iv_eve")
    assert fetched["tz"] == "Asia/Almaty"


def test_cli_list_includes_added_responsible(capsys):
    admin_cli.main(["add", "--login", "test_iv_frank", "--name", "Frank", "--password", "x"])
    capsys.readouterr()  # discard the add output

    admin_cli.main(["list"])
    out = capsys.readouterr().out
    assert "test_iv_frank" in out
    assert "Frank" in out


def test_cli_passwd_changes_password():
    admin_cli.main(["add", "--login", "test_iv_gina", "--name", "Gina", "--password", "old"])
    before = db.get_responsible_by_login("test_iv_gina")["password_hash"]

    admin_cli.main(["passwd", "--login", "test_iv_gina", "--password", "newpass"])

    after = db.get_responsible_by_login("test_iv_gina")["password_hash"]
    assert after != before
    assert auth.verify_password("newpass", after)
    assert not auth.verify_password("old", after)


def test_cli_passwd_without_password_generates_and_prints(capsys):
    admin_cli.main(["add", "--login", "test_iv_hank", "--name", "Hank", "--password", "old"])
    capsys.readouterr()

    admin_cli.main(["passwd", "--login", "test_iv_hank"])
    out = capsys.readouterr().out
    match = re.search(r"Generated password:\s*(\S+)", out)
    assert match is not None, f"no 'Generated password:' line in output: {out!r}"
    printed_password = match.group(1)

    after = db.get_responsible_by_login("test_iv_hank")["password_hash"]
    assert not auth.verify_password("old", after)
    # the PRINTED password must actually verify against the newly stored hash
    assert auth.verify_password(printed_password, after)


def test_cli_deactivate_reactivate():
    admin_cli.main(["add", "--login", "test_iv_leo", "--name", "Leo", "--password", "x"])
    assert db.get_responsible_by_login("test_iv_leo")["active"] is True

    admin_cli.main(["deactivate", "--login", "test_iv_leo"])
    assert db.get_responsible_by_login("test_iv_leo")["active"] is False

    admin_cli.main(["reactivate", "--login", "test_iv_leo"])
    assert db.get_responsible_by_login("test_iv_leo")["active"] is True


def test_cli_deactivate_unknown_login_errors():
    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["deactivate", "--login", "test_iv_nope"])
    assert exc.value.code != 0


def test_cli_link_sets_chat_id():
    admin_cli.main(["add", "--login", "test_iv_x", "--name", "Xavier", "--password", "x"])

    admin_cli.main(["link", "--login", "test_iv_x", "--chat-id", "12345"])
    assert db.get_responsible_by_login("test_iv_x")["telegram_chat_id"] == 12345


def test_cli_link_unknown_login_errors():
    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["link", "--login", "test_iv_nope", "--chat-id", "1"])
    assert exc.value.code != 0


def test_cli_add_with_admin_role():
    admin_cli.main(["add", "--login", "test_iv_boss", "--name", "Boss",
                    "--password", "x", "--role", "admin"])
    assert db.get_responsible_by_login("test_iv_boss")["role"] == "admin"


def test_cli_add_defaults_employee():
    admin_cli.main(["add", "--login", "test_iv_emp", "--name", "Emp", "--password", "x"])
    assert db.get_responsible_by_login("test_iv_emp")["role"] == "employee"


def test_cli_add_rejects_bad_role():
    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["add", "--login", "test_iv_bad", "--name", "Bad",
                        "--password", "x", "--role", "superuser"])
    assert exc.value.code != 0
    # the invalid add must not have created a row
    assert db.get_responsible_by_login("test_iv_bad") is None


def test_cli_setrole():
    admin_cli.main(["add", "--login", "test_iv_role", "--name", "Role", "--password", "x"])
    assert db.get_responsible_by_login("test_iv_role")["role"] == "employee"

    admin_cli.main(["setrole", "--login", "test_iv_role", "--role", "admin"])
    assert db.get_responsible_by_login("test_iv_role")["role"] == "admin"

    # an invalid role is rejected with a non-zero exit and leaves the role unchanged
    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["setrole", "--login", "test_iv_role", "--role", "wizard"])
    assert exc.value.code != 0
    assert db.get_responsible_by_login("test_iv_role")["role"] == "admin"


def test_cli_setrole_unknown_login_errors():
    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["setrole", "--login", "test_iv_nope", "--role", "admin"])
    assert exc.value.code != 0


def test_cli_setavail_upserts_single_day_and_preserves_others():
    admin_cli.main(["add", "--login", "test_iv_ivan", "--name", "Ivan", "--password", "x"])
    rid = db.get_responsible_by_login("test_iv_ivan")["id"]

    admin_cli.main(["setavail", "--login", "test_iv_ivan", "--dow", "0",
                     "--start", "09:00", "--end", "17:00"])
    rows = {r["dow"]: r for r in db.get_availability(rid)}
    assert rows[0]["enabled"] is True
    assert rows[0]["start_min"] == 540
    assert rows[0]["end_min"] == 1020
    assert rows[1]["enabled"] is False  # untouched day still filled False

    # setting a second day must preserve the first
    admin_cli.main(["setavail", "--login", "test_iv_ivan", "--dow", "2",
                     "--start", "10:00", "--end", "18:00"])
    rows = {r["dow"]: r for r in db.get_availability(rid)}
    assert rows[0]["enabled"] is True  # preserved
    assert rows[0]["start_min"] == 540
    assert rows[2]["enabled"] is True
    assert rows[2]["start_min"] == 600
    assert rows[2]["end_min"] == 1080


def test_cli_setavail_rejects_out_of_range_dow():
    admin_cli.main(["add", "--login", "test_iv_judy", "--name", "Judy", "--password", "x"])
    rid = db.get_responsible_by_login("test_iv_judy")["id"]

    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["setavail", "--login", "test_iv_judy", "--dow", "7",
                         "--start", "09:00", "--end", "17:00"])
    assert exc.value.code != 0

    # must NOT have silently no-op-succeeded: every day still unset
    rows = db.get_availability(rid)
    assert all(r["enabled"] is False for r in rows)


def test_cli_setavail_rejects_inverted_window():
    admin_cli.main(["add", "--login", "test_iv_karl", "--name", "Karl", "--password", "x"])
    rid = db.get_responsible_by_login("test_iv_karl")["id"]

    with pytest.raises(SystemExit) as exc:
        admin_cli.main(["setavail", "--login", "test_iv_karl", "--dow", "1",
                         "--start", "17:00", "--end", "09:00"])
    assert exc.value.code != 0

    rows = {r["dow"]: r for r in db.get_availability(rid)}
    assert rows[1]["enabled"] is False  # rejected, never written

    # an EQUAL start/end is also invalid (empty window)
    with pytest.raises(SystemExit):
        admin_cli.main(["setavail", "--login", "test_iv_karl", "--dow", "1",
                         "--start", "09:00", "--end", "09:00"])
