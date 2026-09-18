"""Fix 2b: mail_indexer.run_once must prune ONLY rows whose Maildir file is gone from disk — never
a row whose mailbox merely fell out of candidates() (a lost register_demo_persona write) while its
.eml file is intact. Before the fix, a clobbered-out persona had its already-indexed rows DELETED
every 300s sweep, permanently losing real «Complete your assessment» mail."""
from __future__ import annotations

import sys
import types
from pathlib import Path

from email.message import EmailMessage

# stand-ins for the Postgres adapter + health backstop (run_once only needs a handful of calls).
# Attributes are present so monkeypatch can override them whether the real or the fake module loads.
fake_db = types.ModuleType("backend.tools.mail_db")
fake_db.get_row = lambda _mid: None
fake_db.delete_paths = lambda _ids: 0
fake_db.all_path_hashes = lambda: set()
fake_db.get_meta = lambda _k: None
fake_db.set_meta = lambda *a, **k: None
fake_db.upsert_message = lambda **f: None
fake_db.conn = lambda: (_ for _ in ()).throw(RuntimeError("no db in unit test"))
sys.modules.setdefault("backend.tools.mail_db", fake_db)
fake_health = types.ModuleType("backend.tools.mail_health")
fake_health.heartbeat = lambda: None
fake_health.record_fallback = lambda *a, **k: None
sys.modules.setdefault("backend.tools.mail_health", fake_health)

from backend.tools import mail_indexer, mailcrm  # noqa: E402


def _write_msg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = "Recruiter <recruiter@example.com>"
    msg["To"] = "cand@takhet.com"
    msg["Subject"] = "Complete your assessment"
    msg.set_content("Body")
    path.write_bytes(msg.as_bytes())


def test_run_once_keeps_rows_whose_file_survives_registry_loss(tmp_path, monkeypatch):
    # one message that STILL exists on disk, plus one row whose file was genuinely deleted.
    live = tmp_path / "cand" / "cur" / "1700000000.M1.host:2,S"
    _write_msg(live)
    hash_live = mailcrm._pid(str(live))
    hash_gone = "deadbeef" * 5
    path_gone = str(tmp_path / "cand" / "cur" / "9999999999.Mgone.host:2,S")  # never created

    deleted: list = []
    # (another test file may register a minimal fake mail_db first, so patch with raising=False.)
    monkeypatch.setattr(mail_indexer.mailcrm, "candidates", lambda: [])  # mailbox dropped out
    monkeypatch.setattr(mail_indexer.mail_db, "all_path_hashes",
                        lambda: {hash_live, hash_gone}, raising=False)
    # refresh_kinds False (no re-parse) — return the current classifier version
    monkeypatch.setattr(mail_indexer.mail_db, "get_meta",
                        lambda _k: mailcrm.classifier_version(), raising=False)
    monkeypatch.setattr(mail_indexer, "_db_paths_for",
                        lambda hs: {hash_live: str(live), hash_gone: path_gone})
    monkeypatch.setattr(mail_indexer.mail_db, "delete_paths",
                        lambda ids: deleted.extend(ids) or len(ids), raising=False)
    monkeypatch.setattr(mail_indexer.mail_db, "set_meta", lambda *a, **k: None, raising=False)

    updated, pruned = mail_indexer.run_once()

    # the surviving file's row is NOT deleted; only the genuinely-gone file is pruned.
    assert hash_live not in deleted
    assert deleted == [hash_gone]
    assert pruned == 1


def test_file_present_follows_new_cur_rename(tmp_path):
    # a read moves new/<name> -> cur/<name>:2,S (path changes, _pid hash does not). The DB path may
    # still point at the vanished new/ path — _file_present must still find the file in cur/.
    new_path = tmp_path / "cand" / "new" / "1700000001.M2.host"
    _write_msg(new_path)
    h = mailcrm._pid(str(new_path))
    # simulate the read: move to cur/ with a flag suffix
    cur_path = tmp_path / "cand" / "cur" / "1700000001.M2.host:2,S"
    cur_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.rename(cur_path)

    # stored (stale) path is the old new/ path that no longer exists, but the same-hash file lives
    # in cur/ — the rescan must find it.
    assert mail_indexer._file_present(str(new_path), h) is True
    # a hash with NO matching file anywhere in the mailbox is reported gone.
    absent = "0" * 40
    assert mail_indexer._file_present(str(tmp_path / "cand" / "cur" / "nope:2,S"), absent) is False
