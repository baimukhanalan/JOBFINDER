"""Unit tests for the iCIMS self-verify probe + the TP cron's live-source gate (no DB/network/browser)."""
from backend.tools import icims_probe_promote as ip
from backend.tools import mass_hiring_apply_tp_cron as tp


# ---- fake DB (cursor supports execute + fetchone/fetchall, conn is a context manager) ----
class _FakeCur:
    def __init__(self, rows):
        self.rows = rows
        self.captured = []

    def execute(self, sql, params=None):
        self.captured.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCur(rows)

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- source union + exclude filter (the TP cron helpers the probe promotes into) ----------------

def test_live_sources_union(monkeypatch):
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: set())
    assert tp.live_sources() == {"teleperformance"}
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: {"cotiviti"})
    assert tp.live_sources() == {"teleperformance", "cotiviti"}


def test_tp_job_ids_gates_then_admits_and_excludes(monkeypatch):
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    rows = [(500, "teleperformance"), (16254, "cotiviti")]
    # unverified: cotiviti gated out
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: set())
    monkeypatch.setattr(tp.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert tp.tp_job_ids() == [500]
    # verified: both admitted
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: {"cotiviti"})
    monkeypatch.setattr(tp.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert tp.tp_job_ids() == [500, 16254]
    # exclude the base (the catch-all cron)
    monkeypatch.setattr(tp.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert tp.tp_job_ids(exclude={"teleperformance"}) == [16254]


def test_add_verified_source_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "icims_verified_sources.json"
    monkeypatch.setattr(tp, "_VERIFIED_SOURCES_PATH", str(path))
    assert tp._read_verified_sources() == set()          # absent file → empty
    tp.add_verified_source("cotiviti")
    assert tp._read_verified_sources() == {"cotiviti"}
    tp.add_verified_source("cotiviti")                   # idempotent
    assert tp._read_verified_sources() == {"cotiviti"}


# ---- newest-active-row pick / empty no-op --------------------------------------------------------

def test_newest_active_job_pick(monkeypatch):
    monkeypatch.setattr(ip.mail_db, "conn", lambda: _FakeConn([(16254,)]))
    assert ip.newest_active_job("cotiviti") == 16254


def test_newest_active_job_none_when_no_rows(monkeypatch):
    monkeypatch.setattr(ip.mail_db, "conn", lambda: _FakeConn([]))
    assert ip.newest_active_job("cotiviti") is None


def test_next_pending(monkeypatch):
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: set())
    assert ip.next_pending() == "cotiviti"
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: {"cotiviti"})
    assert ip.next_pending() is None                     # all verified


# ---- verdict parser (confirmed → promote, non-ack → pending) ------------------------------------

def test_classify_confirmed_native_marker():
    log = "... walking ...\n[application CONFIRMED submitted — exiting early]\n"
    assert ip.classify(log)[0] == "confirmed"


def test_classify_confirmed_probe_maildir_marker():
    log = "persona: X <x@takhet.com> ...\nPROBE-CONFIRMED: iCIMS Thank-You-for-Applying ack\n"
    assert ip.classify(log)[0] == "confirmed"


def test_classify_no_form_on_expired():
    assert ip.classify("[no register form after Apply — posting expired — skipping fast]")[0] == "no_form"


def test_classify_pending_when_no_ack():
    assert ip.classify("[step filled + advance=clicked -> waiting for next step]")[0] == "pending"


# ---- run_once: promote on confirmed, stay pending otherwise, no-op when no active rows -----------

def test_run_once_promotes_on_confirmed(monkeypatch):
    monkeypatch.setattr(ip.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ip, "next_pending", lambda: "cotiviti")
    monkeypatch.setattr(ip, "newest_active_job", lambda s: 16254)
    monkeypatch.setattr(ip, "_drive", lambda job: "[application CONFIRMED submitted]")
    promoted = []
    monkeypatch.setattr(tp, "add_verified_source", lambda s: promoted.append(s))
    out = ip.run_once()
    assert out["verdict"] == "confirmed"
    assert promoted == ["cotiviti"]


def test_run_once_stays_pending_on_non_ack(monkeypatch):
    monkeypatch.setattr(ip.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ip, "next_pending", lambda: "cotiviti")
    monkeypatch.setattr(ip, "newest_active_job", lambda s: 16254)
    monkeypatch.setattr(ip, "_drive", lambda job: "[step filled — no ack]")
    promoted = []
    monkeypatch.setattr(tp, "add_verified_source", lambda s: promoted.append(s))
    out = ip.run_once()
    assert out["verdict"] == "pending"
    assert promoted == []                                # NOT promoted on partial evidence


def test_run_once_noop_when_no_active_rows(monkeypatch):
    monkeypatch.setattr(ip.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ip, "next_pending", lambda: "cotiviti")
    monkeypatch.setattr(ip, "newest_active_job", lambda s: None)

    def _boom(job):
        raise AssertionError("must not drive when the tenant has 0 active rows")

    monkeypatch.setattr(ip, "_drive", _boom)
    assert ip.run_once() == {"noop": True, "source": "cotiviti"}


def test_run_once_dry_run_does_not_drive(monkeypatch):
    monkeypatch.setattr(ip.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ip, "next_pending", lambda: "cotiviti")
    monkeypatch.setattr(ip, "newest_active_job", lambda s: 16254)

    def _boom(job):
        raise AssertionError("dry-run must not drive")

    monkeypatch.setattr(ip, "_drive", _boom)
    out = ip.run_once(dry=True)
    assert out["dry"] is True and out["job"] == 16254


def test_run_once_skips_when_busy(monkeypatch):
    monkeypatch.setattr(ip.wpp, "box_is_quiet", lambda: (False, "load1=15"))
    monkeypatch.setattr(ip, "next_pending", lambda: "cotiviti")
    monkeypatch.setattr(ip, "newest_active_job", lambda s: 16254)

    def _boom(job):
        raise AssertionError("must not drive on a busy box")

    monkeypatch.setattr(ip, "_drive", _boom)
    assert ip.run_once().get("skipped") is True
