"""Offline tests for harvester discovery/link parsing + adapter terminal detection (no DB/network)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester import discover  # noqa: E402
from backend.tools.assessment_harvester.adapters.amcat import _TERMINAL_RE  # noqa: E402


def test_amcat_link_from_quoted_printable(tmp_path):
    """The AMCAT autologin link is split by soft line-breaks (=\\r\\n) in the raw email; extraction
    must un-QP it and recover the full token."""
    tok = "eyJ0eXAiOiJKV1Q" + "A" * 300
    # simulate a quoted-printable body wrapping the long URL across lines
    raw = ("Subject: TP Assessment - Test Login Details\r\n\r\n"
           "Please click: https://amcatglobal.aspiringminds.com/?autoLoginVersion=3&t=\r\n"
           "oken=" + tok[:40] + "=\r\n" + tok[40:] + " thanks\r\n")
    p = tmp_path / "msg.eml"
    p.write_bytes(raw.encode())
    # NB the real regex expects &token=; build a faithful sample
    raw2 = ("https://amcatglobal.aspiringminds.com/?autoLoginVersion=3&token=" + tok[:50] + "=\r\n"
            + tok[50:] + "\r\n")
    p.write_bytes(raw2.encode())
    link = discover.link_from_path(str(p), discover.MATCHERS["amcat"]["link_re"])
    assert link is not None
    assert link.startswith("https://amcatglobal.aspiringminds.com/?autoLoginVersion=3&token=")
    assert "\r\n" not in link and " " not in link
    assert (tok[:50] + tok[50:]) in link  # soft-break removed, token intact


def test_shl_sutherland_link_regex():
    raw = ('open <https://talentcentral.us1.shl.com/experience/#/link/JTdCJTIybG9naW4lMjI=> now')
    link = discover.link_from_path.__self__ if hasattr(discover.link_from_path, "__self__") else None
    m = discover.MATCHERS["shl_sutherland"]["link_re"].search(raw)
    assert m and m.group(0).startswith("https://talentcentral.us1.shl.com/experience/#/link/")


def test_amcat_terminal_regex():
    assert _TERMINAL_RE.search("This assessment has either been completed or submitted. Message code TC100.")
    assert _TERMINAL_RE.search("your session has expired")
    assert not _TERMINAL_RE.search("Welcome! Click Start to begin your assessment.")


def test_matchers_present():
    for p in ("amcat", "shl_sutherland", "taleo_ttec"):
        assert p in discover.MATCHERS
        assert isinstance(discover.MATCHERS[p]["link_re"], re.Pattern)
