"""Pure unit tests for the Workday/Phenom mass-hiring apply drivers — job-id SQL shape + the
_pick_state-based eligibility helper. No DB, no network, no browser."""
import os
import sys
import types
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, workday_recon, phenom_recon  # noqa: E402


class _FakeCursor:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def execute(self, q, params=None):
        self._sink["sql"] = q
        self._sink["params"] = params

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows, sink):
        self._cur = _FakeCursor(rows, sink)

    def cursor(self):
        return self._cur


def _patch_conn(monkeypatch, rows, sink):
    @contextlib.contextmanager
    def fake_conn():
        yield _FakeConn(rows, sink)
    monkeypatch.setattr(mail_db, "conn", fake_conn)


def test_workday_job_ids_sql_shape(monkeypatch):
    sink = {}
    _patch_conn(monkeypatch, [(101,), (102,), (103,)], sink)
    ids = workday_recon.workday_job_ids()
    assert ids == [101, 102, 103]
    # source ANY(all 5 Workday-CxS mass-hiring tenants) + the wd5|wd1 host filter + active
    assert "source = ANY(%s)" in sink["sql"]
    assert "active" in sink["sql"].lower()
    assert sink["params"][0] == ["centene", "cigna", "humana", "cvshealth", "concentrix"]
    assert sink["params"][1] == r"\.(wd5|wd1)\.myworkdayjobs\.com"


def test_workday_job_ids_limit(monkeypatch):
    sink = {}
    _patch_conn(monkeypatch, [(1,)], sink)
    workday_recon.workday_job_ids(limit=5)
    assert "LIMIT %s" in sink["sql"]
    assert sink["params"][-1] == 5


def test_phenom_job_ids_sql_shape(monkeypatch):
    sink = {}
    _patch_conn(monkeypatch, [(7,), (8,)], sink)
    ids = phenom_recon.phenom_job_ids()
    assert ids == [7, 8]
    assert "source='conduent'" in sink["sql"].replace(" ", "").replace('"', "'").replace("=", "=")
    assert "conduent" in sink["sql"].lower()


def test_expected_state_from_location():
    # Humana / Centene state-scoped postings -> the persona resides in that state (truthful screener)
    assert workday_recon.expected_state("Customer Service Rep", "Remote, Oklahoma, United States") == ("OK", "Oklahoma")
    assert workday_recon.expected_state("CSR", "Remote, Kentucky") == ("KY", "Kentucky")
    assert workday_recon.expected_state("Care Coordinator", "Remote, South Carolina") == ("SC", "South Carolina")
    # Centene spread AR/MO/MS/OK/WI — all real US states, all reachable
    assert workday_recon.expected_state("Rep", "MO, United States")[0] == "MO"
    assert workday_recon.expected_state("Rep", "WI")[0] == "WI"


def test_expected_state_default_ohio_when_no_state():
    # "Remote, United States" / "Nationwide" names no concrete state -> default (Ohio), any-state OK
    assert workday_recon.expected_state("CSR", "Remote, United States") == ("OH", "Ohio")
    assert workday_recon.expected_state("CSR", "Remote, Nationwide") == ("OH", "Ohio")


def test_confirmed_false_on_missing_mailbox():
    # a mailbox that doesn't exist -> not confirmed (never raises)
    assert workday_recon._confirmed("nobody.here9999@takhet.com", 0.0) is False
    assert phenom_recon._confirmed("nobody.here9999@takhet.com", 0.0) is False


def test_pick_strategy_routes_workday(monkeypatch):
    # a Humana wd5 apply URL routes to a real registered strategy (not the generic fallback)
    strat = workday_recon._pick_strategy(
        "https://humana.wd5.myworkdayjobs.com/Humana_External_Career_Site/job/x/apply")
    assert strat is not None
    assert type(strat).__name__ != "GenericStrategy"


def test_connector_auto_status_conduent_humana_unitedhealth():
    # Conduent (Phenom → Oracle HCM guest apply) is LIVE-PROVEN full-auto → 'auto' (the board shows
    # «Авто» + the phenom lane drives it). Humana is Workday-CxS behind a register-captcha probe →
    # 'needs_laptop' (the workday_probe_promote cron promotes it, no misleading «Авто»). UnitedHealth
    # is a hard Azure-AD-SSO identity wall (no candidate self-registration) → genuinely 'blocked'.
    from backend.tools.mass_hiring import auto_status
    assert auto_status("conduent") == "auto"
    assert auto_status("humana") == "needs_laptop"
    assert auto_status("unitedhealth") == "blocked"


def test_phenom_recon_pay_orders_the_pool(monkeypatch):
    # phenom_recon.main() must pay-order the WHOLE Conduent pool via offer_priority.plan_mh_batch
    # (high-pay-first + stop-on-response), THEN apply --limit, so --limit keeps the top-N
    # highest-paying still-open jobs — never a blind SQL LIMIT over id order.
    from backend.tools import offer_priority

    monkeypatch.setattr(phenom_recon, "phenom_job_ids", lambda *a, **k: [11, 22, 33, 44])
    seen = {}

    def fake_plan(ids, **kw):
        seen["ids"] = list(ids)
        seen["kw"] = kw
        return [44, 33, 22, 11]          # pretend pay-desc order

    monkeypatch.setattr(offer_priority, "plan_mh_batch", fake_plan)
    # drive nothing: stub the per-job driver + row lookup so main() only exercises selection
    monkeypatch.setattr(phenom_recon, "_row", lambda jid: None)
    monkeypatch.setenv("PHENOM_ADVANCE", "0")
    import sys
    monkeypatch.setattr(sys, "argv", ["phenom_recon", "--limit", "2"])
    phenom_recon.main()
    assert seen["ids"] == [11, 22, 33, 44]          # the WHOLE pool was handed to the planner
    assert seen["kw"].get("rounds") == 1
