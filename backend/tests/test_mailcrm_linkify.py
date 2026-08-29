r"""Broken-link-in-email fix: _linkify must reproduce the sender's URL EXACTLY.

Root cause (2026-08-29): a plain-text body commonly wraps a URL in angle brackets
"<https://…>" (an RFC-3986 plain-text convention) or ends a sentence right after it
("…<URL>." / "visit URL."). _msg_card html.escape()s the body BEFORE _linkify, so
"<" becomes "&lt;", ">" becomes "&gt;", '"' becomes "&quot;". The old link regex
`https?://[^\s<]+` had no literal "<" left to stop at, so it swallowed the trailing
"&gt;" / "&quot;" / trailing punctuation INTO the href → the browser navigated to
"https://…/token>" and the self-scheduling / Calendly / Zoom link died with
"We can't find that self-scheduling link." A real "&" in a query string is "&amp;"
after escaping and MUST stay in the href (the browser decodes it back to "&").

No network. Uses real-shape scheduling URLs (long query strings, nested URLs, UUIDs).
See mailcrm_ui._linkify / _msg_card.
"""
from html import escape, unescape

from backend.tools import mailcrm, mailcrm_ui


def _href(escaped_text: str) -> str:
    """The single href _linkify emits, HTML-decoded to what the browser navigates to."""
    import re
    out = mailcrm_ui._linkify(escaped_text)
    m = re.search(r'<a href="([^"]+)"', out)
    return unescape(m.group(1)) if m else ""


def _render_href(url: str, surround: str) -> str:
    """Escape 'surround' (containing {u}) like _msg_card does, linkify, return the
    browser-decoded href — the exact end-to-end path a plain-text body takes."""
    return _href(escape(surround.format(u=url)))


# ---- the proven bug: <URL> angle-bracket wrapping --------------------------
def test_angle_bracket_wrapped_scheduling_url_is_clean():
    url = "https://www.gem.com/scheduling/schedule/3b8721ab-34e4-48c4-962d-ea6107a697ac"
    assert _render_href(url, "book a time using this link\n<{u}>\n.") == url


def test_angle_bracket_wrapped_zoom_url_is_clean():
    url = "https://axon.zoom.us/j/94689679934?pwd=gPc8qcfkQwwMWD2K7RbZXQt8mKkljL.1"
    assert _render_href(url, "Join Zoom Meeting\n<{u}>") == url


# ---- query strings with real "&" must survive (regression guard) -----------
def test_greenhouse_selfschedule_query_string_preserved():
    url = ("https://s101.recruiting.eu.greenhouse.io/schedule/uBRLEadRTv-4hGAoZQvDtw"
           "?utm_medium=email&utm_source=SelfScheduleRequest")
    # trailing sentence period after the URL must NOT enter the href
    assert _render_href(url, "Schedule here: {u}.") == url


def test_calendly_nested_url_in_query_preserved():
    url = ("https://calendly.com/charlienorawalshbattle/introductory-call"
           "?utm_content=cd03c504b22d476b12f23638dbb0925c1a39b0a804d8c97ca13b9915872d6ea6"
           "&utm_campaign=calendly-caf3100cb6a33faf48ec767eab924f94"
           "&utm_term=https://app4.greenhouse.io/guides/14420134004/people/163834913004"
           "?application_id=172831781004")
    assert _render_href(url, "Pick a slot: {u}") == url


def test_google_calendar_redirect_query_preserved():
    url = ("https://www.google.com/url?q=https%3A%2F%2Faxon.zoom.us%2Fj%2F94689679934"
           "&sa=D&source=calendar&ust=1788196560000000&usg=AOvVaw15QntCT3jxxq8J3THFw2KP")
    assert _render_href(url, "<{u}>") == url


# ---- quote / paren wrapping + plain punctuation ----------------------------
def test_double_quote_wrapped_url_is_clean():
    url = "https://calendly.com/team/interview"
    assert _render_href(url, 'here: "{u}"') == url


def test_parenthesised_url_is_clean():
    url = "https://www.charliehealth.com/careers"
    assert _render_href(url, "(see {u})") == url


def test_plain_url_without_wrapping_is_unchanged():
    url = "https://boards.greenhouse.io/embed/job_app?for=acme&token=123"
    assert _render_href(url, "apply: {u} today") == url


# ---- end-to-end through _parse_full + _msg_card ----------------------------
def test_msg_card_href_matches_raw_scheduling_url(tmp_path):
    from email.message import EmailMessage
    url = "https://www.gem.com/scheduling/schedule/2407e297-5d89-49bb-aec3-fab406d7e253"
    m = EmailMessage()
    m["From"] = "Recruiter <r@affirm.com>"
    m["To"] = "cand@takhet.com"
    m["Subject"] = "Call with Affirm!"
    m.set_content(f"Please pick your interview time using this link\n<{url}>\n.\n\nThanks")
    p = tmp_path / "msg.eml"
    p.write_bytes(m.as_bytes())
    row = mailcrm._parse_full(str(p), "h1")
    card = mailcrm_ui._msg_card(row, row.get("subject", ""))
    import re
    hrefs = [unescape(h) for h in re.findall(r'<a href="([^"]+)"', card) if h.startswith("http")]
    assert url in hrefs, f"clean scheduling URL must be a rendered href, got {hrefs}"
    assert not any(h.startswith(url) and h != url for h in hrefs), \
        f"no href may append trailing junk to the URL, got {hrefs}"
