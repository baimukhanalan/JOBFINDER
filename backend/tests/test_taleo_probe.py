"""Pure unit tests for the self-verifying Taleo-source probe + auto-promotion (no network/browser).

Mirrors the Workday probe/promote wiring for the Taleo family: the source UNION
(`mass_hiring_apply_taleo_cron.live_sources()` = base {'ttec'} ∪ the gitignored verified file), the
`taleo_ids(only=, exclude=)` catch-all selector, the runtime live-job pick, and the verdict parser
(a confirmed Maildir-receipt log promotes; anything else stays pending).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mass_hiring_apply_taleo_cron as tc  # noqa: E402
from backend.tools import taleo_probe_promote as pp  # noqa: E402


# ---- fakes (same shape as test_taleo.py) --------------------------------------------------------

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


# ---- (1) live_sources() = base {'ttec'} UNION the verified file ----------------------------------

def test_live_sources_base_only(monkeypatch):
    monkeypatch.setattr(tc, "_read_verified_sources", lambda: set())
    assert tc.live_sources() == {"ttec"}


def test_live_sources_unions_verified_file(monkeypatch):
    monkeypatch.setattr(tc, "_read_verified_sources", lambda: {"kaiser"})
    assert tc.live_sources() == {"ttec", "kaiser"}


def test_add_verified_source_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "taleo_verified_sources.json"
    monkeypatch.setattr(tc, "_VERIFIED_SOURCES_PATH", str(path))
    assert tc.live_sources() == {"ttec"}           # file absent -> base only
    tc.add_verified_source("kaiser")
    tc.add_verified_source("kaiser")               # idempotent
    assert tc._read_verified_sources() == {"kaiser"}
    assert tc.live_sources() == {"ttec", "kaiser"}


# ---- (2) taleo_ids(only=, exclude=) catch-all selector ------------------------------------------

def _wire_taleo_ids(monkeypatch, rows, live):
    from backend.tools import synth_persona, mh_settings
    fake = _FakeConn(rows)
    monkeypatch.setattr(tc, "mail_db", type("M", (), {"conn": staticmethod(lambda: fake)}))
    monkeypatch.setattr(tc, "live_sources", lambda: set(live))
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: list(ids))
    return fake


def test_taleo_ids_excludes_ttec_keeps_promoted(monkeypatch):
    # live_sources has ttec + kaiser; the catch-all passes exclude={'ttec'} -> only kaiser ids.
    rows = [(100, "Customer Service Rep", "ttec"),
            (200, "Contact Center Specialist I", "kaiser"),
            (201, "Member Services Advocate", "kaiser")]
    fake = _wire_taleo_ids(monkeypatch, rows, {"ttec", "kaiser"})
    out = tc.taleo_ids(exclude={"ttec"})
    assert out == [200, 201]
    # the query scopes to source = ANY(sorted live_sources())
    sql, params = fake.cur.captured[0]
    assert "source = ANY(%s)" in sql
    assert params[0] == ["kaiser", "ttec"]


def test_taleo_ids_only_restricts_to_one_source(monkeypatch):
    rows = [(100, "CSR", "ttec"), (200, "CSR I", "kaiser"), (300, "CX Advisor", "percepta")]
    _wire_taleo_ids(monkeypatch, rows, {"ttec", "kaiser", "percepta"})
    assert tc.taleo_ids(only="percepta") == [300]


def test_taleo_ids_inert_before_promotion(monkeypatch):
    # before any promotion live_sources()=={'ttec'}; the catch-all's exclude={'ttec'} -> empty.
    rows = [(100, "CSR", "ttec")]
    _wire_taleo_ids(monkeypatch, rows, {"ttec"})
    assert tc.taleo_ids(exclude={"ttec"}) == []


def test_taleo_ids_skips_licensed_role(monkeypatch):
    # a licensed-insurance title is dropped (synthetic persona holds no real license), same as ttec.
    rows = [(200, "Licensed Health Insurance Agent - Remote", "kaiser"),
            (201, "Customer Service Rep", "kaiser")]
    _wire_taleo_ids(monkeypatch, rows, {"ttec", "kaiser"})
    assert tc.taleo_ids(exclude={"ttec"}) == [201]


# ---- (3) live_probe_job(): newest applyable id, or no-op ----------------------------------------

def test_live_probe_job_picks_newest_applyable(monkeypatch):
    from backend.tools import synth_persona
    # ORDER BY id DESC -> rows already newest-first; the licensed newest is skipped, next wins.
    rows = [(300, "Licensed Insurance Agent"), (250, "Contact Center Specialist I"), (100, "CSR")]
    fake = _FakeConn(rows)
    monkeypatch.setattr(pp, "mail_db", type("M", (), {"conn": staticmethod(lambda: fake)}))
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    assert pp.live_probe_job("kaiser") == 250
    sql, params = fake.cur.captured[0]
    assert "ORDER BY id DESC" in sql and params == ("kaiser",)


def test_live_probe_job_none_when_no_rows(monkeypatch):
    from backend.tools import synth_persona
    fake = _FakeConn([])
    monkeypatch.setattr(pp, "mail_db", type("M", (), {"conn": staticmethod(lambda: fake)}))
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    assert pp.live_probe_job("percepta") is None


# ---- (4) verdict parser: confirmed ack promotes, non-ack stays pending ---------------------------

_CONFIRMED_LOG = """\
=== Taleo apply: job 250 [kaiser] — Contact Center Specialist I
persona: Casey Nolan <casey.nolan42@takhet.com> Renton, WA | resume=True | TALEO_ADVANCE=1
[taleo prefill: page_type=application_form advance=True submitted=True]
[application CONFIRMED submitted — Taleo receipt in the Maildir]
=== taleo apply done
"""

_SUBMIT_NO_ACK_LOG = """\
[taleo prefill: page_type=application_form advance=True submitted=True]
[no confirmation within --keep (expected if TALEO_ADVANCE is off, or needs live selector tuning)]
"""

_NO_FORM_LOG = "[skip: could not resolve a taleo.net apply URL from https://x/y]"

_INCOMPLETE_LOG = """\
[taleo prefill: page_type=application_form advance=True submitted=False]
[no confirmation within --keep]
"""


def test_classify_confirmed_promotes():
    verdict, _ = pp.classify(_CONFIRMED_LOG)
    assert verdict == "confirmed"


def test_classify_submit_without_ack_stays_pending():
    verdict, _ = pp.classify(_SUBMIT_NO_ACK_LOG)
    assert verdict == "submitted_no_ack" and verdict != "confirmed"


def test_classify_no_form_stays_pending():
    verdict, _ = pp.classify(_NO_FORM_LOG)
    assert verdict == "no_form" and verdict != "confirmed"


def test_classify_incomplete_stays_pending():
    verdict, _ = pp.classify(_INCOMPLETE_LOG)
    assert verdict == "incomplete" and verdict != "confirmed"


# ---- run_once: promote ONLY on a confirmed drive ------------------------------------------------

def test_run_once_promotes_on_confirmed(monkeypatch):
    promoted = []
    monkeypatch.setattr(pp, "box_is_quiet", lambda: (True, "load1=0.5(<9) taleo_drives=0"))
    monkeypatch.setattr(pp, "next_pending", lambda: "kaiser")
    monkeypatch.setattr(pp, "live_probe_job", lambda s: 250)
    monkeypatch.setattr(pp, "_drive", lambda job: _CONFIRMED_LOG)
    monkeypatch.setattr(tc, "add_verified_source", lambda s: promoted.append(s))
    res = pp.run_once()
    assert res["verdict"] == "confirmed"
    assert promoted == ["kaiser"]


def test_run_once_leaves_pending_without_ack(monkeypatch):
    promoted = []
    monkeypatch.setattr(pp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(pp, "next_pending", lambda: "kaiser")
    monkeypatch.setattr(pp, "live_probe_job", lambda s: 250)
    monkeypatch.setattr(pp, "_drive", lambda job: _SUBMIT_NO_ACK_LOG)
    monkeypatch.setattr(tc, "add_verified_source", lambda s: promoted.append(s))
    res = pp.run_once()
    assert res["verdict"] == "submitted_no_ack"
    assert promoted == []              # NOT promoted without a real ack


def test_run_once_noop_when_no_live_rows(monkeypatch):
    monkeypatch.setattr(pp, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(pp, "next_pending", lambda: "percepta")
    monkeypatch.setattr(pp, "live_probe_job", lambda s: None)
    res = pp.run_once()
    assert res["verdict"] == "no_rows"


def test_run_once_skips_when_busy(monkeypatch):
    monkeypatch.setattr(pp, "box_is_quiet", lambda: (False, "load1=14.0(<9) taleo_drives=2"))
    monkeypatch.setattr(pp, "next_pending", lambda: "kaiser")
    res = pp.run_once()
    assert res.get("skipped") is True and res["next"] == "kaiser"
