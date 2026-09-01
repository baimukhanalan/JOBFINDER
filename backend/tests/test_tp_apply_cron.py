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
