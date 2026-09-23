"""Unit tests for the Oracle Recruiting Cloud (ORC) / Candidate Experience apply strategy.

Pure logic only — NO network, NO browser, NO submission. Covers URL routing, the
ORC_ADVANCE gate (must be OFF by default so a plain fill is side-effect-free), the
deterministic truthful screener answers, option matching, and strategy registration.
"""
import asyncio

import pytest

from backend.applier.runner import STRATEGIES, _pick_strategy
from backend.applier.strategies.base import GenericStrategy
from backend.applier.strategies.oracle_orc import OracleORCStrategy, _env_advance
from backend.tools import mass_hiring_apply

# The live sample apply URL from recon (Alorica on Oracle CX).
_ORC_URL = ("https://fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/"
            "CandidateExperience/en/sites/CX_1/job/239440")
# Molina — a DIFFERENT ORC tenant host (`hckd.fa.us2`), same CX apply surface + same strategy.
_MOLINA_URL = ("https://hckd.fa.us2.oraclecloud.com/hcmUI/"
               "CandidateExperience/en/sites/CX_1/job/2039268")


# ---- strategy routing --------------------------------------------------------

def test_matches_orc_hosts():
    assert OracleORCStrategy.matches(_ORC_URL)
    # tolerant of case + a shortened /sites/<CX>/job/<id> shape
    assert OracleORCStrategy.matches(_ORC_URL.upper())
    assert OracleORCStrategy.matches(
        "https://fa-abcd.fa.ocs.oraclecloud.com/sites/CX_2/job/551")


def test_does_not_match_non_orc():
    # a greenhouse / avature / workday form, an empty URL, and a NON-CX oraclecloud host
    # (object storage / APEX / docs) must all NOT route here.
    assert not OracleORCStrategy.matches(
        "https://boards.greenhouse.io/embed/job_app?token=1")
    assert not OracleORCStrategy.matches(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")
    assert not OracleORCStrategy.matches(
        "https://acme.wd1.myworkdayjobs.com/en-US/careers")
    assert not OracleORCStrategy.matches("")
    assert not OracleORCStrategy.matches(
        "https://objectstorage.us.oraclecloud.com/n/foo/b/bucket/o/file.pdf")


def test_registered_and_picked_by_url():
    assert OracleORCStrategy in STRATEGIES
    # registered AFTER the specific ATS host strategies (order-independent — more host
    # strategies are appended over time), and the GenericStrategy fallback stays OUT of the list.
    assert GenericStrategy not in STRATEGIES
    picked = _pick_strategy(_ORC_URL)
    assert isinstance(picked, OracleORCStrategy)
    # a non-ORC URL must not accidentally route to ORC.
    assert not isinstance(_pick_strategy("https://boards.greenhouse.io/x"),
                          OracleORCStrategy)


def test_is_a_generic_subclass_with_name():
    # extends GenericStrategy (so super().prefill resolves to the shared pipeline).
    assert issubclass(OracleORCStrategy, GenericStrategy)
    assert OracleORCStrategy.name == "oracle_orc"


def test_molina_routes_to_orc_and_is_supported():
    # Molina's `hckd.fa.us2` ORC tenant routes to the SAME OracleORCStrategy as Alorica and is an
    # accepted mass-hiring apply host (so the probe can drive its guest apply → Postal fallback).
    assert OracleORCStrategy.matches(_MOLINA_URL)
    assert isinstance(_pick_strategy(_MOLINA_URL), OracleORCStrategy)
    assert mass_hiring_apply.is_supported(_MOLINA_URL)


def test_postal_has_empty_open_first_option_fallback():
    # The molina Postal typeahead is scoped to the auto-cascaded City/County, so the persona's own
    # ZIP never surfaces; the shorten branch now re-opens the field EMPTY and takes the first offered
    # local ZIP. Guard the fallback exists (browser-runtime, confirmed live by the ORC probe).
    import inspect
    src = inspect.getsource(OracleORCStrategy._pick_combobox)
    assert "re-open the field EMPTY" in src or "restore the typed ZIP" in src


def test_mass_hiring_apply_supports_oracle():
    assert mass_hiring_apply.is_supported(_ORC_URL)
    assert mass_hiring_apply.is_supported(
        "https://maximus.avature.net/careers/Job-Application?folderId=1")  # unchanged
    assert not mass_hiring_apply.is_supported(
        "https://boards.greenhouse.io/embed/job_app?token=1")


# ---- ORC_ADVANCE gate (live-submit switch) -----------------------------------

def test_env_advance_default_off(monkeypatch):
    monkeypatch.delenv("ORC_ADVANCE", raising=False)
    assert _env_advance() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_env_advance_truthy(monkeypatch, val):
    monkeypatch.setenv("ORC_ADVANCE", val)
    assert _env_advance() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_env_advance_falsy(monkeypatch, val):
    monkeypatch.setenv("ORC_ADVANCE", val)
    assert _env_advance() is False


def test_advance_off_by_default(monkeypatch):
    # The wizard-advance (which transmits PII + sends the application on the final Submit) must
    # be OFF unless ORC_ADVANCE is explicitly set — a plain fill stays side-effect-free.
    monkeypatch.delenv("ORC_ADVANCE", raising=False)

    class _Fresh(OracleORCStrategy):
        advance_wizard = _env_advance()

    assert _Fresh().advance_wizard is False


# ---- deterministic truthful screeners ----------------------------------------

def test_screener_answer_availability_and_eligibility():
    A = OracleORCStrategy._screener_answer
    # per-posting affirmative screeners a synthetic applicant DESIGNED to fit answers Yes
    assert A("are you interested in seasonal work? (2-4 months)", {}) == ["Yes"]
    assert A("are you able to work an 8 hour shift between 7am-7pm cst?", {}) == ["Yes"]
    assert A("this position requires that you be a current u.s. citizen. do you meet this?", {}) == ["Yes"]
    assert A("do you reside within 75 miles of the alorica remote site?", {}) == ["Yes"]
    assert A("do you have a private and secure workspace away from others?", {}) == ["Yes"]
    assert A("are you at least 18 years of age?", {}) == ["Yes"]
    # sponsorship / conflict are truthfully No for a fresh authorized persona
    assert A("do you require sponsorship to work in the united states?", {}) == ["No"]
    assert A("do you foresee any commitment that would interfere with attendance?", {}) == ["No"]


def test_screener_answer_experience_is_multi_option():
    A = OracleORCStrategy._screener_answer
    exp = A("how much experience do you have as a csr in a call center?", {})
    assert exp and exp[0] == "5+ years"           # strongest believable tier first
    sup = A("how much supervisor or leadership experience do you have?", {})
    assert sup and any("year" in v.lower() for v in sup)


def test_screener_answer_language_and_education():
    A = OracleORCStrategy._screener_answer
    # English → native tier (a US persona)
    assert A("what is your english proficiency?", {})[0] == "Native"
    # Spanish depends on the persona being bilingual (a bilingual role)
    assert A("what is your spanish proficiency?", {"bilingual": True})[0] == "Fluent"
    assert A("what is your spanish proficiency?", {})[0] in ("None", "No proficiency")
    # education uses the persona's fact when present, else a sane default
    assert A("what is your highest level of education?", {"education_level": "Associate"})[0] == "Associate"
    assert A("highest level of education achieved?", {})[0] == "Bachelor"


def test_screener_answer_unknown_returns_none():
    # an unrecognized/behavioral question is LEFT for the human, never guessed
    assert OracleORCStrategy._screener_answer("describe a time you resolved a conflict", {}) is None
    assert OracleORCStrategy._screener_answer("what is your favorite color?", {}) is None


def test_screener_answer_alorica_questions():
    # The exact Alorica (Oracle CX) application-question set that was previously left unanswered.
    A = OracleORCStrategy._screener_answer
    # "Do you HAVE a High School Diploma, GED or equivalent?" is a Yes/No (not a level tier)
    assert A("do you have a high school diploma, ged or equivalent?", {}) == ["Yes"]
    # relatives / other members employed with the company → No (fresh synthetic persona)
    assert A("for security/confidentiality reasons, do you have other members currently "
             "employed with the company?", {}) == ["No"]
    # worked-for / provided-services-before → No
    assert A("have you ever worked for or provided services for alorica?", {}) == ["No"]
    # willing to undergo a background check → Yes
    assert A("upon job offer, are you willing to undergo a background check?", {}) == ["Yes"]
    # legally authorized to work → Yes
    assert A("are you legally authorized to work in the country where this job is located?",
             {}) == ["Yes"]


def test_screener_diploma_yesno_does_not_shadow_education_level():
    # the education-LEVEL tier question must still return a tier (not the diploma Yes/No), since it
    # is checked first — the diploma rule only catches the "do you HAVE a diploma" Yes/No form.
    A = OracleORCStrategy._screener_answer
    assert A("what is your highest level of education?", {})[0] == "Bachelor"
    assert A("highest level of education achieved?", {"education_level": "Associate"})[0] == "Associate"


def test_handle_wotc_optout_off_by_default(monkeypatch):
    # The WOTC opt-out navigates SAME-TAB to the flaky ADP partner; it is OPT-IN, so with no
    # ORC_WOTC_OPTOUT it must short-circuit BEFORE any page use (page=None would raise otherwise) —
    # the WOTC then stays a pending step in `unfilled` (harmless: the phone already blocks Submit).
    monkeypatch.delenv("ORC_WOTC_OPTOUT", raising=False)
    strat = OracleORCStrategy()
    assert asyncio.run(strat._handle_wotc(None, {})) is False


def test_handle_wotc_runs_once_per_fill(monkeypatch):
    # Even OPTED-IN, WOTC runs AT MOST once per fill (it navigates the tab; the form step + the
    # review step both call it, and a 2nd nav would stall) — the guard short-circuits the 2nd call.
    monkeypatch.setenv("ORC_WOTC_OPTOUT", "1")
    strat = OracleORCStrategy()
    strat._wotc_attempted = True
    assert asyncio.run(strat._handle_wotc(None, {})) is False


def test_opt_match_boundary():
    m = OracleORCStrategy._opt_match
    assert m("no", "no") is True
    assert m("no", "none") is False                # short answer needs a boundary
    assert m("yes", "yes, my home internet is hardwired") is True
    assert m("1-3 years", "1-3 years") is True
    assert m("3-5 years", "i do not have any experience") is False
    assert m("", "yes") is False


# ---- synthetic phone (unblocks the ORC lane for free — no owner-controlled number) -----------

def test_synth_phone_deterministic_and_format():
    from backend.tools.orc_recon import _synth_phone, _US_AREA_CODES
    import re as _re
    e = "tyler.lawson1234@takhet.com"
    p = _synth_phone(e)
    assert p == _synth_phone(e)                       # stable per email
    m = _re.fullmatch(r"\+1 \((\d{3})\) (\d)(\d)(\d)-(\d{4})", p)
    assert m, f"unexpected format: {p!r}"
    npa, n = m.group(1), m.group(2)
    assert npa in _US_AREA_CODES                      # a real, assigned area code
    assert n in "23456789"                            # exchange first digit N in 2..9 (never 0/1)


def test_synth_phone_never_reserved():
    # NEVER the fictional 555-01xx range, an N11 service code, or the 555 exchange — across many
    # personas (the reserved patterns are exactly what tripped Oracle's "Enter a valid number").
    from backend.tools.orc_recon import _synth_phone
    import re as _re
    for i in range(4000):
        p = _synth_phone(f"persona.candidate{i}@takhet.com")
        d = _re.sub(r"\D", "", p)[1:]                 # 10 national digits (drop the +1)
        npa, nxx, sub = d[:3], d[3:6], d[6:]
        assert not (nxx == "555" and sub.startswith("01")), p   # fictional 555-01xx
        assert nxx[1:] != "11", p                                # N11 service code
        assert nxx != "555", p                                   # 555 exchange
        assert npa[0] in "23456789", p                           # NPA first digit 2..9


def test_synth_phone_is_libphonenumber_valid():
    # The real proof the lane is unblocked: every synthetic number passes libphonenumber's
    # is_valid_number (the exact check Oracle CX runs). Skipped where the optional lib is absent.
    pn = pytest.importorskip("phonenumbers")
    from backend.tools.orc_recon import _synth_phone
    for i in range(500):
        p = _synth_phone(f"applicant{i}@takhet.com")
        assert pn.is_valid_number(pn.parse(p, "US")), p


def _patch_persona_build(monkeypatch, prof):
    """Stub the DB/mailbox/disk deps of orc_recon._build_persona so only the phone logic is exercised."""
    import json as _json
    from backend.tools import orc_recon
    from backend.tools import mass_hiring_apply
    monkeypatch.setattr(orc_recon, "_pick_state", lambda t, l: ("Ohio", "OH", "Columbus", "43215"))
    monkeypatch.setattr(mass_hiring_apply, "prepare",
                        lambda row, gender=None: ("demo_tyler_lawson1234", "999"))
    monkeypatch.setattr(orc_recon.Path, "read_text",
                        lambda self, **k: _json.dumps({"profile": prof, "facts": {}}))
    return orc_recon


def test_build_persona_uses_synth_phone_when_no_orc_phone(monkeypatch):
    # ORC_PHONE unset -> the persona phone is the valid synthetic number, NOT a reserved 555-01xx one.
    monkeypatch.delenv("ORC_PHONE", raising=False)
    prof = {"full_name": "Tyler Lawson", "first_name": "Tyler", "last_name": "Lawson",
            "email": "tyler.lawson1234@takhet.com", "phone": "+1 (415) 555-0150"}
    orc_recon = _patch_persona_build(monkeypatch, prof)
    p = orc_recon._build_persona({"title": "CSR", "location_raw": "Remote, US"})
    got = p["profile_form"]["phone"]
    assert got == orc_recon._synth_phone(prof["email"])
    assert "555-01" not in got                        # never the reserved fictional range


def test_build_persona_honors_orc_phone_override(monkeypatch):
    monkeypatch.setenv("ORC_PHONE", "+1 216 471 2200")
    prof = {"full_name": "Tyler Lawson", "first_name": "Tyler", "last_name": "Lawson",
            "email": "tyler.lawson1234@takhet.com", "phone": "+1 (415) 555-0150"}
    orc_recon = _patch_persona_build(monkeypatch, prof)
    p = orc_recon._build_persona({"title": "CSR", "location_raw": "Remote, US"})
    assert p["profile_form"]["phone"] == "+1 216 471 2200"


# ---- orc_recon.orc_job_ids scope (Alorica + Molina + Hilton, Alorica first) --------------------

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


def test_orc_job_ids_scope_includes_live_sources(monkeypatch):
    from backend.tools import orc_recon
    from backend.tools import synth_persona
    from backend.tools import mass_hiring_apply_orc_cron as oc
    # Rows the SQL candidate pool returns (id, title, source) — Python gates by live_sources().
    rows = [(153, "Customer Service Representative", "alorica"),
            (200, "Pharmacy Customer Service Rep", "molina"),
            (300, "Reservations Coordinator", "hilton")]
    fake = _FakeConn(rows)
    monkeypatch.setattr(orc_recon.mail_db, "conn", lambda: fake)
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    # With ALL three sources live (as after both probes promote), every staffable row is kept.
    monkeypatch.setattr(oc, "live_sources", lambda: {"alorica", "molina", "hilton"})
    out = orc_recon.orc_job_ids()
    assert out == [153, 200, 300]
    sql, params = fake.cur.captured[0]
    assert "source = ANY(%s)" in sql
    assert params[0] == ["alorica", "molina", "hilton"]
    # Alorica rows drain FIRST (proven tenant before the newer same-ATS tenants).
    assert "ORDER BY (source <> 'alorica')" in sql
    assert orc_recon._ORC_SOURCES == ("alorica", "molina", "hilton")


def test_orc_job_ids_gates_on_live_sources(monkeypatch):
    # The whole point of the gate: a collected-but-unverified tenant is DROPPED until live_sources()
    # includes it (mirrors workday_ids gating on live_tenants). Default live = {alorica} only.
    from backend.tools import orc_recon
    from backend.tools import synth_persona
    from backend.tools import mass_hiring_apply_orc_cron as oc
    rows = [(153, "Customer Service Representative", "alorica"),
            (200, "Pharmacy Customer Service Rep", "molina"),
            (300, "Reservations Coordinator", "hilton")]
    monkeypatch.setattr(orc_recon.mail_db, "conn", lambda: _FakeConn(rows))
    monkeypatch.setattr(synth_persona, "job_is_staffable", lambda j: True)
    monkeypatch.setattr(oc, "live_sources", lambda: {"alorica"})
    # base only -> alorica kept, molina/hilton (unverified) dropped
    assert orc_recon.orc_job_ids() == [153]
    # a promotion adds molina to the live set
    monkeypatch.setattr(oc, "live_sources", lambda: {"alorica", "molina"})
    assert orc_recon.orc_job_ids() == [153, 200]
    # the catch-all excludes the base source, driving only the promoted one
    assert orc_recon.orc_job_ids(exclude={"alorica"}) == [200]
    # `only` pins one source (the dedicated cron)
    assert orc_recon.orc_job_ids(only="alorica") == [153]
