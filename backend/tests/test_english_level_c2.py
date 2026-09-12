"""An owner-declared english_level (a CEFR code) is matched in the option text and BACKED, so the
required 'Your English level' radio is answered without a review gate; the synthetic default
('Fluent') still substring-matches the C1 'fluent' option."""
from backend.services.tailor import choices as ch
from backend.tools import synth_persona as sp

SALMON_OPTS = [
    "A1/A2 - Basic (can understand simple words and phrases)",
    "B1 - Intermediate (can handle everyday work communcation)",
    "B2 - Upper-Intermediate (comfortable in meetings and discussions)",
    "C1 - Advanced (fluent, can work fully in English)",
    "C2 - Proficient/ Native",
]


def test_c2_declared_picks_c2_backed():
    idx, backed = ch._language_pick("Your English level", SALMON_OPTS, {"english_level": "C2"})
    assert idx == 4 and backed is True


def test_fluent_default_picks_c1_backed():
    idx, backed = ch._language_pick("Your English level", SALMON_OPTS, {"english_level": "Fluent"})
    assert idx == 3 and backed is True


def test_no_level_falls_back_to_b2_unbacked():
    idx, backed = ch._language_pick("Your English level", SALMON_OPTS, {})
    assert idx == 2 and backed is False


def test_build_candidate_threads_english_level():
    raw = {"full_name": "Dana Erlan", "headline": "Data Analyst", "city": "Almaty"}
    job = {"title": "Data Analyst", "location": "Kazakhstan"}
    c2 = sp._build_candidate(raw, "kz", job, english_level="C2")
    default = sp._build_candidate(raw, "kz", job)
    assert c2["facts"]["english_level"] == "C2"
    assert default["facts"]["english_level"] == "Fluent"
