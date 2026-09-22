"""Unit tests for the Avature apply strategy + the synth-persona US-state fix (no network)."""
import re

from backend.applier.strategies.avature import AvatureStrategy, _gen_password
from backend.tools.synth_persona import _build_candidate, _us_state_full


# ---- strategy routing --------------------------------------------------------

def test_matches_avature_hosts():
    assert AvatureStrategy.matches("https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert AvatureStrategy.matches("https://foo.AVATURE.net/careers/Register")
    assert not AvatureStrategy.matches("https://boards.greenhouse.io/embed/job_app?token=1")
    assert not AvatureStrategy.matches("")


def test_advance_off_by_default():
    # The wizard-advance (which transmits PII + creates the account on submit) must be OFF unless
    # AVATURE_ADVANCE is explicitly set — a plain fill stays side-effect-free at the employer.
    assert AvatureStrategy().advance_wizard is False


def test_generated_password_meets_complexity():
    for _ in range(20):
        pw = _gen_password()
        assert len(pw) >= 10
        assert re.search(r"[a-z]", pw) and re.search(r"[A-Z]", pw)
        assert re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


# ---- synth-persona US state coherence ---------------------------------------

def test_us_state_full():
    assert _us_state_full("TX") == "Texas"
    assert _us_state_full("ok") == "Oklahoma"
    assert _us_state_full("Florida") == "Florida"
    assert _us_state_full("Ontario") == ""
    assert _us_state_full("") == ""


def _job():
    return {"title": "Customer Service Representative", "location": "United States"}


def test_persona_state_parsed_from_city():
    p = _build_candidate({"full_name": "Jane Doe", "city": "Miami, FL"}, "United States", _job())
    assert p["profile"]["state"] == "Florida"
    assert p["profile"]["city"] == "Miami"


def test_persona_state_backfilled_when_missing():
    # A bare US city with no state -> a coherent (city, state) from the bank, never empty.
    p = _build_candidate({"full_name": "Jane Doe", "city": ""}, "United States", _job())
    assert p["profile"]["state"] in {"Texas", "Colorado", "Ohio", "Washington"}
    assert p["profile"]["city"]


def test_non_us_persona_has_no_state():
    p = _build_candidate({"full_name": "João Silva", "city": "São Paulo"}, "Brazil", _job())
    assert p["profile"]["state"] == ""
    assert p["profile"]["city"] == "São Paulo"


# ---- step-3 radio screeners (job screening questions) -------------------------

def test_screener_answer_availability_and_eligibility():
    A = AvatureStrategy._screener_answer
    # per-posting Compliance-step screeners a synthetic applicant answers affirmatively
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("work an 8 hour shift between 7am-7pm cst. are you able to meet this requirement?", {}) == ["Yes"]
    assert A("obtain a federal clearance (medium risk public trust). able to meet this requirement?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this requirement?", {}) == ["Yes"]
    assert A("do you reside within 75 miles of the maximus lawrence kansas location?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    # a conflict/commitment screener is still truthfully No
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]
    # CSR experience is a multi-option, not Yes/No
    exp = A("how much experience do you have as a tier i csr in a call center?", {})
    assert exp and any("year" in c.lower() for c in exp)


def test_opt_match_boundary():
    m = AvatureStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False               # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False


def test_screener_answer_supervisor_experience():
    A = AvatureStrategy._screener_answer
    # the Supervisor - Call Center role asks leadership experience, not customer-service
    vals = A("how much supervisor or leadership experience do you have?", {})
    assert vals and any("year" in v.lower() for v in vals)
    # the CSR customer-service experience question still resolves
    vals2 = A("how much experience do you have in a customer service environment as a tier i csr?", {})
    assert vals2 and vals2[0] == "5+ years"   # ETALON: strongest tier first


def test_employee_referral_screener_answered_no():
    # Maximus added a REQUIRED "Were you referred by an existing employee?" step-1 select
    # ~2026-09-12; unanswered, it was the lone required-empty field so the wizard never
    # advanced and the whole Maximus/Avature lane went dead (clicked=0). A fresh synthetic
    # persona was not referred -> No, on BOTH the step-1 select path (_SCREENERS) and the
    # radio / later-step fallback (_screener_answer).
    assert ("referred by", "No") in AvatureStrategy._SCREENERS
    A = AvatureStrategy._screener_answer
    assert A("were you referred by an existing employee?", {}) == ["No"]
    assert A("employee referral - name of referring employee", {}) == ["No"]
    # the substring must not steal the "preferred first name" identity field or unrelated Qs
    assert A("preferred first name", {}) is None
    assert A("are you legally authorized to work in the united states?", {}) == ["Yes"]


# ---- maximus_ids scope now also picks up Transcom (Avature on apply.careers.transcom.com) --------

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


_MAXIMUS_URL = "https://maximus.avature.net/careers/Job-Application?folderId=42"
_TRANSCOM_URL = "https://apply.careers.transcom.com/en_US/careers/JobDetail/x/13462"


def test_maximus_ids_drives_maximus_and_gates_transcom(monkeypatch):
    """The SQL matches both the maximus.avature.net host AND the transcom host, but Maximus is
    always driven while an UNVERIFIED Transcom is gated OUT (a different Avature tenant)."""
    from backend.tools import mass_hiring_apply_cron as mc
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: set())
    fake = _FakeConn([(1, _MAXIMUS_URL), (2, _TRANSCOM_URL)])
    monkeypatch.setattr(mc.mail_db, "conn", lambda: fake)
    out = mc.maximus_ids()
    assert out == [1]                          # maximus byte-identical; transcom gated
    sql, params = fake.cur.captured[0]
    assert "apply_url ILIKE %s OR apply_url ILIKE %s" in sql
    assert params == ("%avature%", "%apply.careers.transcom.com%")


def test_maximus_ids_drives_transcom_once_verified(monkeypatch):
    from backend.tools import mass_hiring_apply_cron as mc
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: {"transcom"})   # probe promoted it
    fake = _FakeConn([(1, _MAXIMUS_URL), (2, _TRANSCOM_URL)])
    monkeypatch.setattr(mc.mail_db, "conn", lambda: fake)
    assert mc.maximus_ids() == [1, 2]          # both now live


def test_maximus_ids_only_and_exclude(monkeypatch):
    from backend.tools import mass_hiring_apply_cron as mc
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: {"transcom"})
    rows = [(1, _MAXIMUS_URL), (2, _TRANSCOM_URL)]
    monkeypatch.setattr(mc.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert mc.maximus_ids(only="transcom") == [2]
    monkeypatch.setattr(mc.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert mc.maximus_ids(exclude={"maximus"}) == [2]   # catch-all: base Maximus skipped


def test_avature_tenant_mapping():
    from backend.tools import mass_hiring_apply_cron as mc
    assert mc._avature_tenant(_TRANSCOM_URL) == "transcom"
    assert mc._avature_tenant(_MAXIMUS_URL) == "maximus"
    assert mc._avature_tenant("https://foo.avature.net/careers/Register") == "maximus"
    assert mc._avature_tenant("") == "maximus"


def test_avature_live_sources_union(monkeypatch):
    from backend.tools import mass_hiring_apply_cron as mc
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: set())
    assert mc.live_sources() == {"maximus"}
    monkeypatch.setattr(mc, "_read_verified_sources", lambda: {"transcom"})
    assert mc.live_sources() == {"maximus", "transcom"}
