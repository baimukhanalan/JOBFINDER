"""Fix 2a: register_demo_persona serialises across concurrent PROCESSES, not just threads.

Three OS processes (the dash, apply_campaign_cron, the mass-hiring lane crons) register demo
personas at once. The old bare threading.Lock only covered one process, so cross-process
read-modify-writes of demo_personas.json CLOBBERED each other and lost registrations — a persona
dropped from the registry vanishes from candidates() and its indexed mail is then pruned. The
fcntl flock added in register_demo_persona must keep EVERY registration under process contention.
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import types
from pathlib import Path

# dependency-free stand-in for the Postgres adapter (register_demo_persona never touches it).
fake_db = types.ModuleType("backend.tools.mail_db")
fake_db.get_row = lambda _mid: None
fake_db.delete_paths = lambda _ids: 0
sys.modules.setdefault("backend.tools.mail_db", fake_db)

from backend.tools import mailcrm  # noqa: E402


def _register_batch(demo_file: str, start: int, count: int) -> None:
    """Child-process entrypoint: point the module registry at the shared temp file and register a
    contiguous block of distinct personas."""
    from backend.tools import mailcrm as m
    m.DEMO_FILE = Path(demo_file)
    for i in range(start, start + count):
        m.register_demo_persona(f"user{i}@takhet.com", f"User {i}", f"demo_user{i}")


def test_concurrent_processes_lose_no_registration(tmp_path):
    demo_file = tmp_path / "demo_personas.json"
    demo_file.write_text("{}")
    n_procs, per = 8, 60
    ctx = multiprocessing.get_context("fork")
    procs = []
    for w in range(n_procs):
        p = ctx.Process(target=_register_batch, args=(str(demo_file), w * per, per))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(60)
        assert p.exitcode == 0

    reg = json.loads(demo_file.read_text())
    total = n_procs * per
    assert len(reg) == total, f"lost {total - len(reg)} of {total} registrations"
    for i in range(total):
        assert f"user{i}@takhet.com" in reg


def test_register_is_idempotent(tmp_path):
    orig = mailcrm.DEMO_FILE
    mailcrm.DEMO_FILE = tmp_path / "demo_personas.json"
    try:
        mailcrm.DEMO_FILE.write_text("{}")
        for _ in range(3):
            mailcrm.register_demo_persona("dana@takhet.com", "Dana Erlan", "demo_dana")
        reg = json.loads(mailcrm.DEMO_FILE.read_text())
        assert reg == {"dana@takhet.com": {"id": "demo_dana", "name": "Dana Erlan"}}
        # no stale per-PID tmp file left behind
        assert not list(tmp_path.glob("demo_personas.json.tmp*"))
    finally:
        mailcrm.DEMO_FILE = orig
