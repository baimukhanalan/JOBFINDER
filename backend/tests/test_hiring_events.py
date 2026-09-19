"""Tests for the Teleperformance Virtual Hiring Event capture lane.

Pure (no DB / no network) for the matcher + the body extractors + the Zoom-id
parse; the DB-backed aggregation is exercised live against real mail in the report,
not here. Fixtures are trimmed copies of the two real invite variants (inline-URL
"TODAY!" blast and the anchor-text "You're Invited" blast).
"""
from __future__ import annotations

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
