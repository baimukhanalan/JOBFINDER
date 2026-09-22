"""Unit tests for the Avature self-verify probe + the Maximus cron's live-tenant gate (no DB/net/browser)."""
from backend.tools import avature_probe_promote as ap
from backend.tools import mass_hiring_apply_cron as mc


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


_MAXIMUS_URL = "https://maximus.avature.net/careers/Job-Application?folderId=42"
_TRANSCOM_URL = "https://apply.careers.transcom.com/en_US/careers/JobDetail/x/13462"


# ---- host union (tenant mapping) + exclude filter (the Maximus cron the probe promotes into) -----

def test_live_sources_union(monkeypatch):
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: set())
    assert mc.live_sources() == {"maximus"}
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: {"transcom"})
    assert mc.live_sources() == {"maximus", "transcom"}


def test_avature_tenant_host_mapping():
    assert mc._avature_tenant(_TRANSCOM_URL) == "transcom"
    assert mc._avature_tenant(_MAXIMUS_URL) == "maximus"
    assert mc._avature_tenant("https://foo.avature.net/x") == "maximus"


def test_maximus_ids_gates_then_admits_and_excludes(monkeypatch):
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    rows = [(1, _MAXIMUS_URL), (2, _TRANSCOM_URL)]
    # unverified: transcom gated out, maximus byte-identical
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: set())
    monkeypatch.setattr(mc.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert mc.maximus_ids() == [1]
    # verified: both admitted
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: {"transcom"})
    monkeypatch.setattr(mc.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert mc.maximus_ids() == [1, 2]
    # exclude the base (the catch-all cron)
    monkeypatch.setattr(mc.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert mc.maximus_ids(exclude={"maximus"}) == [2]


def test_add_verified_source_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "avature_verified_sources.json"
    monkeypatch.setattr(mc, "_VERIFIED_SOURCES_PATH", str(path))
    assert mc._read_verified_sources() == set()
    mc.add_verified_source("transcom")
    assert mc._read_verified_sources() == {"transcom"}
    mc.add_verified_source("transcom")                   # idempotent
    assert mc._read_verified_sources() == {"transcom"}


# ---- newest-active-row pick / empty no-op --------------------------------------------------------

def test_newest_active_job_pick(monkeypatch):
    monkeypatch.setattr(ap.mail_db, "conn", lambda: _FakeConn([(13462,)]))
    assert ap.newest_active_job("transcom") == 13462


def test_newest_active_job_none_when_no_rows(monkeypatch):
    monkeypatch.setattr(ap.mail_db, "conn", lambda: _FakeConn([]))
    assert ap.newest_active_job("transcom") is None


def test_next_pending(monkeypatch):
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: set())
    assert ap.next_pending() == "transcom"
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: {"transcom"})
    assert ap.next_pending() is None


# ---- verdict parser (confirmed → promote, non-ack → pending) ------------------------------------

def test_classify_confirmed():
    assert ap.classify({"confirmed": True, "clicked": True})[0] == "confirmed"


def test_classify_error():
    assert ap.classify({"error": "TimeoutError: x"})[0] == "error"


def test_classify_clicked_no_ack():
    assert ap.classify({"clicked": True, "confirmed": None})[0] == "clicked_no_ack"


def test_classify_pending_incomplete():
    # the documented dead-lane symptom: a required screener leaves it unsubmitted (no click)
    assert ap.classify({"clicked": None, "confirmed": None, "unfilled": ["Referred by"]})[0] == "pending"
    assert ap.classify({})[0] == "pending"


# ---- run_once: promote on confirmed, stay pending otherwise, no-op when no active rows -----------

def test_run_once_promotes_on_confirmed(monkeypatch):
    monkeypatch.setattr(ap.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ap, "next_pending", lambda: "transcom")
    monkeypatch.setattr(ap, "newest_active_job", lambda s: 13462)
    monkeypatch.setattr(ap, "_drive", lambda job: {"confirmed": True, "clicked": True})
    promoted = []
    monkeypatch.setattr(mc, "add_verified_source", lambda s: promoted.append(s))
    out = ap.run_once()
    assert out["verdict"] == "confirmed"
    assert promoted == ["transcom"]


def test_run_once_stays_pending_on_incomplete(monkeypatch):
    monkeypatch.setattr(ap.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ap, "next_pending", lambda: "transcom")
    monkeypatch.setattr(ap, "newest_active_job", lambda s: 13462)
    monkeypatch.setattr(ap, "_drive", lambda job: {"clicked": None, "confirmed": None})
    promoted = []
    monkeypatch.setattr(mc, "add_verified_source", lambda s: promoted.append(s))
    out = ap.run_once()
    assert out["verdict"] == "pending"
    assert promoted == []


def test_run_once_noop_when_no_active_rows(monkeypatch):
    monkeypatch.setattr(ap.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ap, "next_pending", lambda: "transcom")
    monkeypatch.setattr(ap, "newest_active_job", lambda s: None)

    def _boom(job):
        raise AssertionError("must not drive when the tenant has 0 active rows")

    monkeypatch.setattr(ap, "_drive", _boom)
    assert ap.run_once() == {"noop": True, "source": "transcom"}


def test_run_once_dry_run_does_not_drive(monkeypatch):
    monkeypatch.setattr(ap.wpp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(ap, "next_pending", lambda: "transcom")
    monkeypatch.setattr(ap, "newest_active_job", lambda s: 13462)

    def _boom(job):
        raise AssertionError("dry-run must not drive")

    monkeypatch.setattr(ap, "_drive", _boom)
    out = ap.run_once(dry=True)
    assert out["dry"] is True and out["job"] == 13462


def test_run_once_skips_when_busy(monkeypatch):
    monkeypatch.setattr(ap.wpp, "box_is_quiet", lambda: (False, "load1=15"))
    monkeypatch.setattr(ap, "next_pending", lambda: "transcom")
    monkeypatch.setattr(ap, "newest_active_job", lambda s: 13462)

    def _boom(job):
        raise AssertionError("must not drive on a busy box")

    monkeypatch.setattr(ap, "_drive", _boom)
    assert ap.run_once().get("skipped") is True
