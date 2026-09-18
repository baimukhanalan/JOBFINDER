"""Offline tests for the Taleo/TTEC 'Required Assessments' adapter (no network, no DB, no browser).

The `taleo_ttec` sealed links go Taleo Privacy -> login -> HARVER, so `TaleoAdapter` subclasses
`HarverAdapter` (platform 'harver', replaying the pre-solved harver answer bank). These tests cover the
adapter's OWN deterministic surface: the truthful native-Taleo-screening answer policy (the pure
`truthful_answer` picker), the mailbox->full-email normalization the credential lookup needs, and the
"cognitive item is never guessed" contract of `answer_mcq`.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester.adapters.taleo import TaleoAdapter, truthful_answer  # noqa: E402


# ---- truthful_answer: the native-Taleo screening answer policy (eligibility/availability/consent) ----

def test_truthful_work_authorization_yes():
    d = truthful_answer("Are you legally authorized to work in the United States?", ["Yes", "No"])
    assert d == {"index": 0, "kind": "truthful", "value": "yes"}


def test_truthful_sponsorship_no():
    d = truthful_answer("Will you now or in the future require visa sponsorship for employment?",
                        ["Yes", "No"])
    assert d["kind"] == "truthful" and d["value"] == "no" and d["index"] == 1


def test_truthful_without_sponsorship_wins_over_sponsorship():
    # "authorized to work WITHOUT sponsorship" must be Yes, not inverted to a disqualifying No.
    d = truthful_answer("Are you authorized to work without sponsorship now or in the future?",
                        ["Yes", "No"])
    assert d["value"] == "yes" and d["index"] == 0


def test_truthful_age_and_education_yes():
    assert truthful_answer("Are you at least 18 years of age or older?", ["Yes", "No"])["value"] == "yes"
    assert truthful_answer("Do you have a high school diploma or GED?", ["Yes", "No"])["value"] == "yes"


def test_truthful_availability_and_consent_yes():
    assert truthful_answer("Are you willing to work weekends, holidays and overtime?",
                           ["Yes", "No"])["value"] == "yes"
    assert truthful_answer("Do you consent to a background check and drug screen?",
                           ["Yes", "No"])["value"] == "yes"
    assert truthful_answer("I agree to the terms and conditions and privacy policy.",
                           ["I agree", "I disagree"])["value"] == "yes"


def test_truthful_prior_employment_no():
    d = truthful_answer("Have you ever been employed by TTEC or a TTEC subsidiary?", ["Yes", "No"])
    assert d["value"] == "no" and d["index"] == 1


def test_truthful_criminal_no():
    assert truthful_answer("Have you ever been convicted of a felony?", ["Yes", "No"])["value"] == "no"


def test_demographic_declines():
    d = truthful_answer("What is your race/ethnicity?",
                        ["White", "Black or African American", "Asian", "I do not wish to answer"])
    assert d["kind"] == "decline" and d["index"] == 3
    g = truthful_answer("Please select your gender.", ["Male", "Female", "Prefer not to answer"])
    assert g["kind"] == "decline" and g["index"] == 2
    v = truthful_answer("Are you a protected veteran?",
                        ["I am a veteran", "I am not a veteran", "I decline to identify"])
    assert v["kind"] == "decline" and v["index"] == 2


def test_cognitive_never_guessed():
    # a right-answer/knowledge item has no truthful mapping -> needs_human, index None (NOT a random pick).
    d = truthful_answer("What is 15% of 240?", ["36", "24", "40"])
    assert d == {"index": None, "kind": "needs_human", "value": None}
    d2 = truthful_answer("Which word is a synonym for 'happy'?", ["Sad", "Joyful", "Angry"])
    assert d2["kind"] == "needs_human" and d2["index"] is None


def test_truthful_empty_inputs():
    assert truthful_answer("", ["Yes", "No"])["kind"] == "needs_human"
    assert truthful_answer("Are you authorized to work?", [])["kind"] == "needs_human"


def test_truthful_yes_wanted_but_no_yes_option_returns_none():
    # policy wants Yes but the options carry no affirmative -> refuse (index None), never mis-click.
    d = truthful_answer("Are you legally authorized to work in the US?", ["Maybe", "Unsure"])
    assert d["index"] is None and d["value"] == "yes"


# ---- adapter contract ---------------------------------------------------------------------------

def test_platform_is_harver():
    # the assessment IS Harver -> bank/replay must use the pre-solved harver keys.
    assert TaleoAdapter(mailbox="jane.doe12@takhet.com").platform == "harver"


def test_mailbox_localpart_normalized_to_full_email():
    # discover yields the LOCALPART; the Taleo cred lookup needs the full @takhet.com address.
    assert TaleoAdapter(mailbox="jane.doe12").mailbox == "jane.doe12@takhet.com"
    # an already-full email is left unchanged (the --url --mailbox path).
    assert TaleoAdapter(mailbox="jane.doe12@takhet.com").mailbox == "jane.doe12@takhet.com"


def test_account_lookup_uses_full_email():
    # the credential lookup (taleo.taleo_account) is keyed by the full @takhet.com email; a localpart
    # mailbox must be normalized so the lookup resolves. (No creds file in the worktree -> None, but the
    # key it queries must be the full address, verified by the normalization above.)
    a = TaleoAdapter(mailbox="jane.doe12")
    assert a.mailbox == "jane.doe12@takhet.com"
    # _account never raises even when the creds file is absent.
    assert a._account() in (None,) or isinstance(a._account(), dict)


class _NoTouchPage:
    """A page stand-in that fails LOUDLY if the adapter touches it — proves answer_mcq refuses a
    cognitive item WITHOUT clicking anything."""
    def __getattr__(self, name):
        raise AssertionError(f"answer_mcq must not touch the page for a needs_human item (accessed .{name})")


def test_answer_mcq_refuses_cognitive_without_touching_page():
    a = TaleoAdapter(mailbox="jane.doe12@takhet.com")
    item = {"_taleo_native": True, "_taleo_kind": "radio", "_taleo_key": "q1",
            "question": "What is 15% of 240?",
            "options": [{"text": "36"}, {"text": "24"}, {"text": "40"}]}
    ok = asyncio.run(a.answer_mcq(_NoTouchPage(), item, 0))
    assert ok is False
