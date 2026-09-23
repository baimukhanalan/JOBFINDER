"""Pure unit tests for the TP (iCIMS) auto-apply cron helpers — no network, no browser, no DB."""
from backend.tools import mass_hiring_apply_tp_cron as tp


# ---- _is_tp_confirmation -------------------------------------------------------

def test_confirmation_by_icims_autoreply_from():
    assert tp._is_tp_confirmation(
        '"Teleperformance @ icims" <teleperformance+autoreply@talent.icims.com>',
        "Application received") is True


def test_confirmation_by_subject():
    assert tp._is_tp_confirmation(
        "Some Recruiter <noreply@example.com>",
        "Thank You for Applying at Remote (United States)") is True


def test_shl_assessment_invite_is_not_a_confirmation():
    # the SHL invite is a LATER step, not proof the application was submitted
    assert tp._is_tp_confirmation(
        "TP <talentcentral@shl.com>",
        "TP Assessment - Test Login Details") is False


def test_unrelated_mail_is_not_a_confirmation():
    assert tp._is_tp_confirmation("Bank <alerts@bank.com>", "Your statement is ready") is False


def test_empty_headers_are_not_a_confirmation():
    assert tp._is_tp_confirmation("", "") is False
    assert tp._is_tp_confirmation(None, None) is False


# ---- _persona_email_from_output ------------------------------------------------

def test_persona_email_parsed_from_recon_stdout():
    out = ("=== iCIMS recon: job 502 — Healthcare Customer Service Representative - Remote\n"
           "[reusing persona demo_beau_maddox8725 (no LLM)]\n"
           "persona: Beau Maddox <beau.maddox8725@takhet.com> Columbus, OH (Ohio) | resume=True\n"
           "[proxy: DIRECT ...]\n")
    assert tp._persona_email_from_output(out) == "beau.maddox8725@takhet.com"


def test_persona_email_none_when_absent():
    assert tp._persona_email_from_output("no persona line here") is None
    assert tp._persona_email_from_output("") is None


# ---- tp_job_ids scope is host-based (%icims%) so Cotiviti (careers-cotiviti.icims.com) is in ------

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


def test_tp_job_ids_drives_base_tp_and_gates_cotiviti(monkeypatch):
    """The %icims% predicate matches EVERY iCIMS tenant, but only live_sources() are driven: the base
    TP source always, an UNVERIFIED tenant (cotiviti) NEVER — until the probe promotes it."""
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    # DB returns a TP row + an (unverified) cotiviti row; only the TP one is driven.
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: set())
    fake = _FakeConn([(500, "teleperformance"), (16254, "cotiviti")])
    monkeypatch.setattr(tp.mail_db, "conn", lambda: fake)
    out = tp.tp_job_ids()
    assert out == [500]                       # cotiviti gated out
    sql, params = fake.cur.captured[0]
    assert params == ("%icims%",)             # host scope unchanged; the SOURCE filter gates the tenant
    assert "source" in sql.lower()            # query now also selects the source column


def test_tp_job_ids_drives_cotiviti_once_verified(monkeypatch):
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: {"cotiviti"})   # probe promoted it
    fake = _FakeConn([(500, "teleperformance"), (16254, "cotiviti")])
    monkeypatch.setattr(tp.mail_db, "conn", lambda: fake)
    assert tp.tp_job_ids() == [500, 16254]    # both now live


def test_tp_job_ids_only_and_exclude(monkeypatch):
    """`only=` restricts to one source; `exclude=` drops one — the catch-all cron excludes the base."""
    from backend.tools import mh_settings
    monkeypatch.setattr(mh_settings, "drop_spanish", lambda ids: ids)
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: {"cotiviti"})
    rows = [(500, "teleperformance"), (16254, "cotiviti")]
    monkeypatch.setattr(tp.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert tp.tp_job_ids(only="cotiviti") == [16254]
    monkeypatch.setattr(tp.mail_db, "conn", lambda: _FakeConn(list(rows)))
    assert tp.tp_job_ids(exclude={"teleperformance"}) == [16254]   # catch-all: base TP skipped


def test_live_sources_union(monkeypatch):
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: set())
    assert tp.live_sources() == {"teleperformance"}                # base only
    monkeypatch.setattr(tp, "_read_verified_sources", lambda: {"cotiviti"})
    assert tp.live_sources() == {"teleperformance", "cotiviti"}    # base UNION verified


# ---- icims_recon._is_employer_screener (Cotiviti "employed by <us>?" -> No) ----------------------

def test_employer_screener_matches_tenant_name():
    from backend.tools.icims_recon import _is_employer_screener as f
    # names the employer (Cotiviti) — a fresh synthetic persona never worked there -> answer No.
    assert f("Have you ever been employed by Cotiviti?", "Cotiviti")
    assert f("Have you previously worked for Cotiviti, Inc. or a subsidiary?", "Cotiviti")
    # generic self-reference (no name) still counts.
    assert f("Have you ever worked for the Company?", "Cotiviti")
    assert f("Are you currently employed by our organization?", "Cotiviti")


def test_employer_screener_excludes_unrelated_questions():
    from backend.tools.icims_recon import _is_employer_screener as f
    # work-AUTHORIZATION is NOT an employment-history question (must not answer No).
    assert not f("Are you legally authorized to work in the United States?", "Cotiviti")
    # a CSR-experience question must not be mistaken for 'employed by us'.
    assert not f("Do you have experience working in customer service?", "Cotiviti")
    # a third-party employer (no tenant name, no self-reference) is left for the human.
    assert not f("Are you currently employed by a staffing agency?", "Cotiviti")
    # TP's own 'employed by TP' is already answered by the strategy; the tenant-name path here is
    # additive and never contradicts it (TP is the base tenant, kept byte-identical).
    assert not f("", "Cotiviti")
