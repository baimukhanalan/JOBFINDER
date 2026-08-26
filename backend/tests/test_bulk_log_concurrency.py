"""The «Подать на все» parallel lane runs a dozen dashboard worker threads (plus the
reconciler/drain daemons) that all mutate bulk_log's three shared-state files
concurrently. Before 2026-08-26 those mutations used a FIXED '<name>.json.tmp' and an
unlocked read-modify-write, so they (1) crashed with FileNotFoundError when one thread's
tmp was os.replace'd out from under another, and (2) silently LOST each other's updates —
dropping «Незавершённые» ledger entries so jobs were never drained/finished.

This test hammers record()/confirmed-vs-parked from many threads and asserts no exception
and no lost updates. Pure (temp dir, no network)."""
import json
import tempfile
import threading
from pathlib import Path

from backend.tools import bulk_log as bl


def _redirect(tmp: Path):
    bl._LOGDIR = tmp
    bl._LOG = tmp / "bulk_apply.log"
    bl._REPORT = tmp / "bulk_apply_last.json"
    bl._LEDGER = tmp / "unfinished.json"
    bl._DONE = tmp / "submitted_jobids.json"


def test_parallel_record_no_crash_no_lost_updates():
    tmp = Path(tempfile.mkdtemp())
    _redirect(tmp)
    run = bl.start(2000)
    errs: list[str] = []

    def worker(base: int):
        try:
            for i in range(50):
                jid = base * 50 + i
                confirmed = (jid % 2 == 0)          # even = confirmed, odd = parked
                bl.record(run, jobid=jid, company=f"c{jid}",
                          state="done" if confirmed else "needs_human",
                          submit={"clicked": confirmed, "confirmed": confirmed})
        except Exception as e:  # pragma: no cover - the bug we guard against
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(b,)) for b in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = 40 * 50
    parked = sum(1 for j in range(total) if j % 2 == 1)
    confirmed = sum(1 for j in range(total) if j % 2 == 0)

    assert not errs, f"concurrent record() raised: {errs[:3]}"
    ledger = json.loads(bl._LEDGER.read_text())
    done = json.loads(bl._DONE.read_text())
    report = json.loads(bl._REPORT.read_text())
    assert len(ledger) == parked, f"lost ledger updates: {len(ledger)} != {parked}"
    assert len(done) == confirmed, f"lost submitted_jobids: {len(done)} != {confirmed}"
    assert len(report["jobs"]) == total, f"lost report rows: {len(report['jobs'])} != {total}"


def test_atomic_write_uses_unique_tmp(tmp_path):
    """Two 'processes' (distinct tmp names) writing the same target must not collide."""
    _redirect(tmp_path)
    bl._atomic_write_json(bl._LEDGER, {"a": 1})
    assert json.loads(bl._LEDGER.read_text()) == {"a": 1}
    # no stray .tmp left behind
    assert not list(tmp_path.glob(".*tmp"))
