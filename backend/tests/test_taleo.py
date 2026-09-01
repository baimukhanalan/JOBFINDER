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
