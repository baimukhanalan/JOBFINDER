"""prefill_retention keeps the artifacts of personas with an active hiring event / interview.

The résumé + persona.json live in uploads/prefill/<demo_id>/<jobid>/ and were pruned after 20d
even for a persona with a live «событие найма» / предстоящий собес, so the interviewer card lost
the «Скачать резюме» button (owner report 2026-09-23). The guard `_protected_demo_ids()` marks
those personas; prune() skips them. Pure/filesystem — no DB (the guard is monkeypatched).
"""
from __future__ import annotations

import time

from backend.tools import prefill_retention as pr


def _mk(root, demo_id, jobid, age_days):
    d = root / demo_id / jobid
    d.mkdir(parents=True)
    f = d / "resume.pdf"
    f.write_text("pdf")
    old = time.time() - age_days * 86400
    import os
    os.utime(f, (old, old))
    os.utime(d, (old, old))
    return d


def test_protected_persona_survives_prune(tmp_path, monkeypatch):
    root = tmp_path / "prefill"
    root.mkdir()
    prot = _mk(root, "demo_active_one100", "mh_1", age_days=40)      # old but PROTECTED
    stale = _mk(root, "demo_stale_two200", "mh_2", age_days=40)      # old + not protected
    fresh = _mk(root, "demo_fresh_three300", "mh_3", age_days=1)     # recent, kept anyway

    monkeypatch.setattr(pr, "PREFILL_ROOT", root)
    monkeypatch.setattr(pr, "_protected_demo_ids", lambda: {"demo_active_one100"})

    res = pr.prune(days=20, dry_run=False)

    assert prot.exists(), "a persona with an active interview/hiring-event keeps its résumé"
    assert fresh.exists(), "a recent artifact is untouched"
    assert not stale.exists(), "a genuinely-stale, unprotected artifact is still pruned"
    assert res["removed_jobs"] == 1
    assert res["protected"] == 1


def test_guard_is_best_effort(monkeypatch):
    # any failure inside the guard must degrade to an empty set, never raise into the cron
    import backend.tools.mail_db as mail_db
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(mail_db, "_cur", _boom)
    # still returns a set (possibly non-empty from pool/hiring-events, or empty) — never raises
    assert isinstance(pr._protected_demo_ids(), set)
