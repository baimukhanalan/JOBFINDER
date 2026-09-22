"""Unit tests for the Oracle-ORC self-verifying probe→promote lane.

Pure logic only — NO network, NO browser, NO real Playwright drive, NO submission. Covers:
  * mass_hiring_apply_orc_cron.live_sources() unions the gitignored verified file with the base,
  * orc_recon.orc_job_ids(only=/exclude=) gates on live_sources() (drops unverified sources),
  * orc_probe_promote._probe_job_for() picks the newest live job / no-ops on an empty source,
  * orc_probe_promote.classify() maps a confirmed-ack log → promote and a non-ack → leave-pending,
  * run_once() promotes ONLY on a confirmed ack (writes the verified file), leaves pending otherwise.
"""
import pytest

from backend.tools import mass_hiring_apply_orc_cron as oc
from backend.tools import orc_probe_promote as opp
from backend.tools import orc_recon


# ---- fake DB (the WHERE is applied in SQL/Postgres; the fake returns rows verbatim so the Python
#      source-gate in orc_job_ids / _probe_job_for is what's exercised) --------------------------

class _FakeCur:
    def __init__(self, rows):
        self.rows = rows
        self.captured = []

    def execute(self, sql, params=None):
        self.captured.append((sql, params))

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCur(rows)

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- live_sources() unions the verified file ---------------------------------

def test_live_sources_base_only(monkeypatch):
    monkeypatch.setattr(oc, "_read_verified", lambda: set())
    assert oc.live_sources() == {"alorica"}


def test_live_sources_unions_verified_file(monkeypatch):
    # a probe promotion appends to the gitignored verified file -> live_sources() picks it up
    monkeypatch.setattr(oc, "_read_verified", lambda: {"molina"})
    assert oc.live_sources() == {"alorica", "molina"}
    monkeypatch.setattr(oc, "_read_verified", lambda: {"molina", "hilton"})
    assert oc.live_sources() == {"alorica", "molina", "hilton"}


def test_add_verified_roundtrip(tmp_path, monkeypatch):
    vpath = tmp_path / "orc_verified_sources.json"
    monkeypatch.setattr(oc, "_VERIFIED_PATH", str(vpath))
    assert oc._read_verified() == set()          # absent file -> empty (never raises)
    oc.add_verified("molina")
    assert oc._read_verified() == {"molina"}
    oc.add_verified("molina")                     # idempotent
    oc.add_verified("hilton")
    assert oc._read_verified() == {"molina", "hilton"}
    # base stays unioned in
    monkeypatch.setattr(oc, "_read_verified", lambda: {"molina", "hilton"})
    assert oc.live_sources() == {"alorica", "molina", "hilton"}


# ---- orc_job_ids gates on live_sources() + honors only/exclude ---------------

def _wire_jobids(monkeypatch, rows, live):
    from backend.tools import synth_persona
    monkeypatch.setattr(orc_recon.mail_db, "conn", lambda: _FakeConn(rows))
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    monkeypatch.setattr(oc, "live_sources", lambda: set(live))


def test_orc_job_ids_drops_unverified_sources(monkeypatch):
    rows = [(153, "CSR", "alorica"), (200, "Pharmacy CSR", "molina"), (300, "Reservations", "hilton")]
    _wire_jobids(monkeypatch, rows, {"alorica"})
    assert orc_recon.orc_job_ids() == [153]                     # molina/hilton unverified -> dropped


def test_orc_job_ids_includes_promoted_source(monkeypatch):
    rows = [(153, "CSR", "alorica"), (200, "Pharmacy CSR", "molina"), (300, "Reservations", "hilton")]
    _wire_jobids(monkeypatch, rows, {"alorica", "molina"})
    assert orc_recon.orc_job_ids() == [153, 200]               # hilton still unverified -> dropped


def test_orc_job_ids_exclude_drops_named(monkeypatch):
    rows = [(153, "CSR", "alorica"), (200, "Pharmacy CSR", "molina")]
    _wire_jobids(monkeypatch, rows, {"alorica", "molina"})
    # the catch-all cron passes --exclude alorica -> only the promoted source is driven
    assert orc_recon.orc_job_ids(exclude={"alorica"}) == [200]
    # exclude is case-insensitive
    assert orc_recon.orc_job_ids(exclude={"ALORICA"}) == [200]


def test_orc_job_ids_only_pins_one_source(monkeypatch):
    rows = [(153, "CSR", "alorica"), (200, "Pharmacy CSR", "molina")]
    _wire_jobids(monkeypatch, rows, {"alorica", "molina"})
    assert orc_recon.orc_job_ids(only="alorica") == [153]      # the dedicated alorica cron pin
    assert orc_recon.orc_job_ids(only="molina") == [200]


# ---- _probe_job_for picks the newest live job / no-ops on empty --------------

def test_probe_job_for_picks_newest_staffable(monkeypatch):
    from backend.tools import mail_db, synth_persona
    # rows already id-DESC (the SQL orders DESC); the newest staffable wins.
    rows = [(999, "Reservations Coordinator"), (500, "Customer Service Rep")]
    monkeypatch.setattr(mail_db, "conn", lambda: _FakeConn(rows))
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    assert opp._probe_job_for("molina") == 999


def test_probe_job_for_skips_unstaffable(monkeypatch):
    from backend.tools import mail_db, synth_persona
    rows = [(999, "French Bilingual CSR"), (500, "Customer Service Rep")]
    monkeypatch.setattr(mail_db, "conn", lambda: _FakeConn(rows))
    # only the plain-English row is staffable -> the newest STAFFABLE id, not the raw newest
    monkeypatch.setattr(synth_persona, "job_is_staffable",
                        lambda j: "french" not in (j.get("title") or "").lower())
    assert opp._probe_job_for("molina") == 500


def test_probe_job_for_empty_source_is_noop(monkeypatch):
    from backend.tools import mail_db, synth_persona
    monkeypatch.setattr(mail_db, "conn", lambda: _FakeConn([]))   # hilton has 0 active rows today
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    assert opp._probe_job_for("hilton") is None


# ---- next_pending skips verified/blocked/no-job sources ----------------------

def test_next_pending_skips_verified_and_jobless(monkeypatch):
    monkeypatch.setattr(oc, "_read_verified", lambda: {"molina"})   # molina already promoted
    monkeypatch.setattr(opp, "_read_json", lambda p: set())
    # molina skipped (verified); hilton has a live job -> it's next.
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: 42 if s == "hilton" else None)
    assert opp.next_pending() == "hilton"
    # if neither has a live job -> None (nothing to probe)
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: None)
    assert opp.next_pending() is None


def test_next_pending_skips_blocked(monkeypatch):
    monkeypatch.setattr(oc, "_read_verified", lambda: set())
    monkeypatch.setattr(opp, "_read_json", lambda p: {"molina"})    # molina recorded blocked
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: 7)
    assert opp.next_pending() == "hilton"


# ---- classify: confirmed ack -> promote, anything else -> leave pending -------

def test_classify_confirmed_from_maildir_ack():
    log = ("=== Oracle ORC apply: job 200 [molina] — Pharmacy CSR\n"
           "[submit clicked: ...]\n"
           "[application CONFIRMED — Oracle receipt in the Maildir]\n=== orc apply done")
    assert opp.classify(log)[0] == "confirmed"


def test_classify_incomplete_reached_form_no_ack():
    log = ("persona: Tyler Lawson <t@takhet.com> Columbus, OH\n"
           "[filled: unfilled=['Postal Code'] review_items=0 page_type=application_form at_submit=True]\n"
           "[no confirmation within --keep ...]\n=== orc apply done")
    v, _ = opp.classify(log)
    assert v == "incomplete"                      # NOT promoted on partial evidence


def test_classify_wall_on_login_or_captcha():
    log = "[filled: unfilled=[] review_items=0 page_type=login_required at_submit=False]"
    assert opp.classify(log)[0] == "wall"


def test_classify_error_when_form_never_reached():
    assert opp.classify("[run error: TimeoutError: ...]")[0] == "error"


# ---- run_once: promote ONLY on a confirmed ack -------------------------------

def _confirmed_log(source, job):
    return (f"=== Oracle ORC apply: job {job} [{source}]\n"
            "[application CONFIRMED — Oracle receipt in the Maildir]\n=== orc apply done")


def test_run_once_promotes_on_confirmed(monkeypatch):
    promoted = []
    monkeypatch.setattr(opp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: 200)
    monkeypatch.setattr(opp, "_drive", lambda job: _confirmed_log("molina", job))
    monkeypatch.setattr(oc, "add_verified", lambda s: promoted.append(s))
    res = opp.run_once(force_source="molina")
    assert res["verdict"] == "confirmed"
    assert promoted == ["molina"]                 # the ONLY path that writes the verified file


def test_run_once_leaves_pending_on_incomplete(monkeypatch):
    promoted = []
    monkeypatch.setattr(opp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: 200)
    monkeypatch.setattr(opp, "_drive",
                        lambda job: "[filled: unfilled=['Postal Code'] page_type=application_form]")
    monkeypatch.setattr(oc, "add_verified", lambda s: promoted.append(s))
    res = opp.run_once(force_source="molina")
    assert res["verdict"] == "incomplete"
    assert promoted == []                          # NOT promoted — honest by design


def test_run_once_skips_when_box_busy(monkeypatch):
    driven = []
    monkeypatch.setattr(opp, "box_is_quiet", lambda: (False, "load1=12.0"))
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: 200)
    monkeypatch.setattr(opp, "_drive", lambda job: driven.append(job) or "")
    res = opp.run_once(force_source="molina")
    assert res.get("skipped") is True
    assert driven == []                            # never drives a headful :98 fill under load


def test_run_once_noop_when_source_has_no_job(monkeypatch):
    monkeypatch.setattr(opp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: None)      # hilton: 0 active rows
    res = opp.run_once(force_source="hilton")
    assert res.get("noop") is True


def test_run_once_dry_run_drives_nothing(monkeypatch):
    driven = []
    monkeypatch.setattr(opp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(opp, "_probe_job_for", lambda s: 200)
    monkeypatch.setattr(opp, "_drive", lambda job: driven.append(job) or "")
    res = opp.run_once(force_source="molina", dry=True)
    assert res.get("dry") is True
    assert driven == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
