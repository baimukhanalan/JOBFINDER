"""Tests for the Teleperformance Virtual Hiring Event capture lane.

Pure (no DB / no network) for the matcher + the body extractors + the Zoom-id
parse; the DB-backed aggregation is exercised live against real mail in the report,
not here. Fixtures are trimmed copies of the two real invite variants (inline-URL
"TODAY!" blast and the anchor-text "You're Invited" blast).
"""
from __future__ import annotations

import json

from backend.tools import hiring_events as he

# ---- fixtures: the two real invite shapes (tokens scrubbed) ----------------------

# Variant A — the "TODAY!" blast: the Zoom link is INLINE in the plain body
# (💻 Zoom:<url>, no space), unsubscribe URL on its own line after "please go to:".
_PLAIN_A = (
    "Hi there,\n \n"
    "Great news! Teleperformance is hiring Remote Customer Service Representatives, "
    "and we’d love to meet you!\n \n"
    "\U0001f3af Join our Virtual Hiring Event and interview with our team!\n \n"
    "\U0001f4c5 Hiring Event: Monday–Friday\n"
    "⏰ Time: 9:30 AM – 5:00 PM ET\n"
    "\U0001f4bb Zoom:https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA\n \n"
    "\U0001f680 Why join?\n"
    "- Remote Customer Service opportunity\n"
    "⏰ Interview spots can fill quickly, so join early to secure your spot.\n \n"
    "This message was sent to madison.harlow5917@takhet.com. If you don't want to "
    "receive these emails from this company in the future, please go to:\n"
    "https://tracking.icims.com/f/a/UNSUB_A~~/AAIB5hA~/unsubtokenA \n"
)
_HTML_A = (
    '<html><body><p>\U0001f4bb Zoom:'
    '<a href="https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA">'
    'https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA</a></p>'
    '<p>please go to: <a href="https://tracking.icims.com/f/a/UNSUB_A~~/AAIB5hA~/unsubtokenA">'
    'unsubscribe</a></p></body></html>'
)

# Variant B — "You're Invited": plain has only anchor TEXT ("Join the Hiring Event
# Now"), the real Zoom link lives only in the HTML anchor.
_PLAIN_B = (
    "Hi there,\n \n"
    "Great news! Teleperformance is hiring Remote Customer Service Representatives, "
    "and we’d love to meet you!\n \n"
    "\U0001f4c5 Hiring Event: Monday–Friday\n"
    "⏰ Time: 9:30 AM – 5:00 PM ET\n"
    "\U0001f4bb Zoom: Join the Hiring Event Now\n \n"
    "This message was sent to samuel.nash3785@takhet.com. If you don't want to "
    "receive these emails from this company in the future, please go to:\n"
    "https://tracking.icims.com/f/a/UNSUB_B~~/AAIB5hA~/unsubtokenB \n"
)
_HTML_B = (
    '<html><body><p>\U0001f4bb Zoom: '
    '<a href="https://tracking.icims.com/f/a/ZOOM_B~~/AAIB5hA~/zoomtokenB">'
    'Join the Hiring Event Now</a></p>'
    '<p><a href="https://tracking.icims.com/f/a/UNSUB_B~~/AAIB5hA~/unsubtokenB">'
    'https://teleperformance.icims.com/icims2</a></p></body></html>'
)


# ---- matcher --------------------------------------------------------------------
def test_is_hiring_event_true():
    assert he.is_hiring_event(
        "\U0001f3af Join our Virtual Hiring Event and interview with our team, TODAY!",
        "teleperformance+email+jibmk-ceee1344bd@talent.icims.com")
    assert he.is_hiring_event(
        "\U0001f389 You're Invited! Virtual Hiring Event – Remote CSR",
        "Loraine Roluna <teleperformance+email+jincl@talent.icims.com>")


def test_is_hiring_event_rejects_non_event():
    # right sender, wrong subject (a plain application confirmation)
    assert not he.is_hiring_event(
        "Thank you for applying to Teleperformance",
        "teleperformance@talent.icims.com")
    # right subject wording, but not a Teleperformance/icims sender
    assert not he.is_hiring_event(
        "You're invited to our Virtual Hiring Event",
        "recruiting@some-other-company.com")


# ---- schedule + role extraction -------------------------------------------------
def test_extract_schedule():
    for plain in (_PLAIN_A, _PLAIN_B):
        date_text, time_text = he.extract_schedule(plain)
        assert date_text == "Monday–Friday"
        assert time_text == "9:30 AM – 5:00 PM ET"


def test_extract_role_from_body():
    assert "Remote Customer Service" in he.extract_role(
        "\U0001f3af Join our Virtual Hiring Event ... TODAY!", _PLAIN_A)


def test_extract_role_from_subject_tail():
    assert he.extract_role(
        "\U0001f389 You're Invited! Virtual Hiring Event – Remote CSR", "") == "Remote CSR"


# ---- Zoom / join link extraction ------------------------------------------------
def test_extract_join_inline_variant():
    join, unsub = he.extract_join(_PLAIN_A, _HTML_A)
    assert join == "https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA"
    assert unsub == "https://tracking.icims.com/f/a/UNSUB_A~~/AAIB5hA~/unsubtokenA"
    assert join != unsub


def test_extract_join_anchor_text_variant():
    # the real Zoom link is ONLY in the HTML anchor whose text is "Join the Hiring Event Now"
    join, unsub = he.extract_join(_PLAIN_B, _HTML_B)
    assert join == "https://tracking.icims.com/f/a/ZOOM_B~~/AAIB5hA~/zoomtokenB"
    assert unsub == "https://tracking.icims.com/f/a/UNSUB_B~~/AAIB5hA~/unsubtokenB"
    # must never hand back the unsubscribe link as the join link
    assert join != unsub


def test_extract_join_never_returns_unsub_when_only_unsub_present():
    # a degenerate mail with ONLY the unsubscribe icims link must not surface it as a join link
    plain = "please go to:\nhttps://tracking.icims.com/f/a/ONLYUNSUB~~/AAIB5hA~/t"
    join, unsub = he.extract_join(plain, "")
    assert unsub == "https://tracking.icims.com/f/a/ONLYUNSUB~~/AAIB5hA~/t"
    assert join is None


# ---- zoom meeting id -------------------------------------------------------------
def test_zoom_meeting_id():
    assert he.zoom_meeting_id(
        "https://us06web.zoom.us/j/7436255779?pwd=abc") == "7436255779"
    assert he.zoom_meeting_id("https://tracking.icims.com/f/a/x~~/y/z") is None
    assert he.zoom_meeting_id(None) is None


# ---- candidate résumé + detail (state / ФИО / approximate age) -------------------
def test_estimate_age_from_education_graduation_year():
    # earliest education year → age ≈ now − grad + 22
    r = {"education": [{"degree": "BS", "year": "2010"}]}
    assert he.estimate_age(r, now_year=2026) == 2026 - 2010 + 22  # 38


def test_estimate_age_from_experience_when_no_education():
    r = {"experience": [{"title": "CSR", "dates": "2015 - 2019"}]}
    assert he.estimate_age(r, now_year=2026) == 2026 - 2015 + 22  # 33


def test_estimate_age_uses_earliest_anchor_across_edu_and_exp():
    r = {"education": [{"year": "2018"}], "experience": [{"dates": "2012-2018"}]}
    assert he.estimate_age(r, now_year=2026) == 2026 - 2012 + 22  # earliest=2012 → 36


def test_estimate_age_none_when_no_year_or_implausible():
    assert he.estimate_age({}, now_year=2026) is None
    assert he.estimate_age({"education": [{"year": "n/a"}]}, now_year=2026) is None
    # a stray year before the 1950 floor is ignored → no plausible anchor → None
    assert he.estimate_age({"education": [{"year": "1900"}]}, now_year=2026) is None
    # an anchor that would imply age > 75 is rejected (guess band 18–75)
    assert he.estimate_age({"education": [{"year": "1955"}]}, now_year=2026) is None


def test_candidate_detail_reads_state_name_age_and_omits_missing():
    persona = {"profile": {"full_name": "Jane Doe", "state": "Ohio",
                           "resume": {"education": [{"year": "2016"}]}}}
    det = he.candidate_detail(persona)
    assert det["full_name"] == "Jane Doe"
    assert det["state"] == "Ohio"
    assert det["age"] == he.estimate_age(persona["profile"]["resume"])
    # nameless / unresolved → the fallback name, no state, no age
    det2 = he.candidate_detail(None, fallback_name="Fallback Name")
    assert det2 == {"full_name": "Fallback Name", "state": "", "age": None}


def test_prefill_resolution_resume_path_and_filename(tmp_path):
    # a fake prefill tree; id_resolver=None-returning forces the deterministic demo-id guess
    # (`jane.doe1@…` → `demo_jane_doe1`), so the test never touches the real registry/disk.
    root = tmp_path
    d = root / "demo_jane_doe1" / "mh_5"
    d.mkdir(parents=True)
    persona = {"profile": {"full_name": "Jane Doe", "state": "Texas",
                           "resume": {"personal_info": {"name": "Jane Doe"},
                                      "education": [{"degree": "BS", "year": "2016"}],
                                      "experience": [{"title": "CSR",
                                                      "dates": "2018-Present"}]}}}
    (d / "persona.json").write_text(json.dumps(persona), encoding="utf-8")
    (d / "resume.pdf").write_bytes(b"%PDF-1.4 test resume")
    noid = lambda _e: None  # noqa: E731 — force the localpart-guess branch

    got = he.prefill_dir_for("jane.doe1@takhet.com", root=str(root), id_resolver=noid)
    assert got == str(d)
    assert he.resume_pdf_path("jane.doe1@takhet.com", root=str(root),
                              id_resolver=noid) == str(d / "resume.pdf")
    loaded = he.load_persona("jane.doe1@takhet.com", root=str(root), id_resolver=noid)
    assert he.candidate_detail(loaded)["state"] == "Texas"
    # filename is the candidate's name (persona passed → no disk lookup)
    assert he.resume_filename("jane.doe1@takhet.com", loaded) == "Jane Doe - resume.pdf"
    # an unknown persona resolves to nothing (button hidden / 404)
    assert he.prefill_dir_for("nobody.here9@takhet.com", root=str(root),
                              id_resolver=noid) is None
    assert he.resume_pdf_path("nobody.here9@takhet.com", root=str(root),
                              id_resolver=noid) is None


def test_resume_filename_falls_back_to_localpart():
    assert he.resume_filename("someone.new42@takhet.com", persona={}) == \
        "someone.new42 - resume.pdf"
