"""Pure unit tests for the Taleo apply lane (no network, no browser)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.applier.strategies.taleo import resolve_apply_url  # noqa: E402
from backend.tools.recon_ttec import is_licensed, ttec_state, ttec_language  # noqa: E402


# ---- resolve_apply_url (Radancy HTML -> external Taleo URL) --------------------------------------

def test_resolve_prefers_external_careersection_10020():
    html = ('<a href="https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=2384324">internal</a>'
            ' ApplyUrl=https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=2384324')
    assert resolve_apply_url(html) == \
        "https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=2384324"


def test_resolve_ttec_sectionless_url():
    # TTEC embeds a section-less jobapply.ftl (302s to a numbered section at runtime)
    html = 'x <a href="https://ttec.taleo.net/careersection/jobapply.ftl?job=04DW3">apply</a> y'
    assert resolve_apply_url(html) == "https://ttec.taleo.net/careersection/jobapply.ftl?job=04DW3"


def test_resolve_avoids_internal_when_no_external():
    html = 'only https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=999 here'
    # no external -> falls back to the internal (still a valid taleo URL, better than None)
    assert resolve_apply_url(html) == \
        "https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=999"


def test_resolve_none_when_absent():
    assert resolve_apply_url("<html>no taleo link at all</html>") is None
    assert resolve_apply_url("") is None


# ---- taleo_recon._resolve_taleo_html (widened for NAMED careersections: Kaiser) ------------------

def test_recon_resolves_kaiser_named_careersection():
    # Kaiser's Radancy page embeds a NAMED (`external`) careersection the strategy's numeric-only
    # regex misses — the widened taleo_recon fallback catches it (both the raw + &amp; copies match).
    from backend.tools.taleo_recon import _resolve_taleo_html
    html = ('<a href="https://kp.taleo.net/careersection/external/mysubmissions.ftl?lang=en">x</a>'
            '<a href="https://kp.taleo.net/careersection/external/jobapply.ftl?job=1445595&amp;src=JB-10088">Apply</a>')
    assert _resolve_taleo_html(html) == \
        "https://kp.taleo.net/careersection/external/jobapply.ftl?job=1445595"


def test_recon_resolver_keeps_ttec_and_uhg_byte_identical():
    # TTEC (section-less) + UHG (numeric 10020) still resolve via the strategy resolver FIRST — the
    # widened fallback only fires when that returns None, so the proven tenants are unchanged.
    from backend.tools.taleo_recon import _resolve_taleo_html
    assert _resolve_taleo_html(
        '<a href="https://ttec.taleo.net/careersection/jobapply.ftl?job=04DW3">x</a>') == \
        "https://ttec.taleo.net/careersection/jobapply.ftl?job=04DW3"
    assert _resolve_taleo_html(
        '<a href="https://uhg.taleo.net/careersection/10000/jobapply.ftl?job=1">i</a>'
        '<a href="https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=1">e</a>') == \
        "https://uhg.taleo.net/careersection/10020/jobapply.ftl?job=1"
    assert _resolve_taleo_html("<html>nothing</html>") is None
    assert _resolve_taleo_html("") is None


# ---- eligibility: licensed-role skip + state placement ------------------------------------------

def test_licensed_roles_are_skipped():
    assert is_licensed("Licensed Healthcare Insurance Agent - Remote")
    assert is_licensed("Licensed Property & Casualty Insurance Agent")
    assert not is_licensed("Customer Service Representative - Remote")
    assert not is_licensed("Bilingual Healthcare Advocate")


def test_ttec_state_includes_california():
    # TTEC hires WAH in CA (TP's allow-list omits CA) -> its own table must include it
    code, full, city, zc = ttec_state("Bilingual Customer Service Representative - Remote in California")
    assert (code, full) == ("CA", "California")
    assert city and zc


def test_ttec_state_defaults_to_ohio():
    code, full, _city, _zc = ttec_state("Customer Service Representative - Remote")
    assert (code, full) == ("OH", "Ohio")


def test_ttec_language_detects_bilingual():
    assert ttec_language("Bilingual (Spanish) Customer Service Rep") == "Spanish"
    assert ttec_language("Vietnamese Speaking Advocate") == "Vietnamese"
    assert ttec_language("Customer Service Representative") is None


# ---- driver eligibility wiring ------------------------------------------------------------------

def test_licensed_id_denylist_present():
    from backend.tools.taleo_recon import _TTEC_LICENSED_IDS
    assert {506, 511, 513, 529} <= _TTEC_LICENSED_IDS


def test_pick_state_routes_by_source():
    from backend.tools.taleo_recon import _pick_state
    # TTEC -> its own CA-inclusive table
    full, code, _city, _zc = _pick_state("ttec", "CSR - Remote in California", "Remote, United States")
    assert (code, full) == ("CA", "California")
    # UnitedHealth -> generic icims _pick_state reads the location
    full, code, _city, _zc = _pick_state("unitedhealth", "CSR - Remote", "TN, United States")
    assert (code, full) == ("TN", "Tennessee")


# ---- taleo_recon.taleo_job_ids scope (adds Kaiser + Percepta) -----------------------------------

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


def test_taleo_job_ids_scope_includes_kaiser_and_percepta(monkeypatch):
    from backend.tools import taleo_recon
    from backend.tools import synth_persona
    rows = [(100, "Customer Service Rep", "ttec"),
            (200, "Contact Center Specialist I", "kaiser"),
            (300, "Customer Experience Advisor", "percepta")]
    fake = _FakeConn(rows)
    monkeypatch.setattr(taleo_recon.mail_db, "conn", lambda: fake)
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    out = taleo_recon.taleo_job_ids()
    assert out == [100, 200, 300]
    sql, params = fake.cur.captured[0]
    assert "source = ANY(%s)" in sql
    assert params[0] == ["unitedhealth", "ttec", "kaiser", "percepta"]
    assert taleo_recon._TALEO_SOURCES == ("unitedhealth", "ttec", "kaiser", "percepta")
