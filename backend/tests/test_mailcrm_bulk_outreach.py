"""Fix 1: a Teleperformance mass-outreach blast must NEVER be classified as an interview
invitation (it used to match «get you hired today» in the live interview keyword bucket, polluting
the Собес delegation pool + firing false owner Telegram interview alerts). The keyword phrase is
removed live; this covers the belt-and-braces code guard in `_kind_with_done_override`."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# mailcrm imports the Postgres adapter, but these unit tests exercise only the classifier — a tiny
# stand-in keeps local QA dependency-free (mirrors test_mailcrm_inbox.py).
fake_db = types.ModuleType("backend.tools.mail_db")
fake_db.get_row = lambda _mid: None
fake_db.delete_paths = lambda _ids: 0
sys.modules.setdefault("backend.tools.mail_db", fake_db)

from backend.tools import mailcrm  # noqa: E402

# The real live blast (from mail_index): same body to every persona.
TP_SENDER = "LaQuinda.Gatlin@teleperformanceusa.com"
TP_SUBJECT = "Teleperformance-DIRECT HIRE JOB OPPORTUNITY"
TP_BODY = ("Hello Chase , We are looking to hire you today for a position starting on 9/28. "
           "If you are interested, please join zoom so that we can get you hired today! "
           "https://tracking.icims.com/f/a/xyz I look forward to your response! Thank you, LaQuinda")


def test_is_bulk_outreach_is_tightly_scoped():
    # the exact sender+subject pair fires
    assert mailcrm._is_bulk_outreach(TP_SUBJECT, TP_SENDER)
    assert mailcrm._is_bulk_outreach(TP_SUBJECT, "Recruiter <LaQuinda.Gatlin@teleperformanceusa.com>"
                                     .split("<")[-1].rstrip(">"))
    # a genuine 1:1 invite from the same domain is NOT demoted
    assert not mailcrm._is_bulk_outreach("Interview invitation for the Analyst role", TP_SENDER)
    # the blast subject from a DIFFERENT sender is NOT demoted
    assert not mailcrm._is_bulk_outreach(TP_SUBJECT, "recruiter@example.com")
    assert not mailcrm._is_bulk_outreach("", "")


def test_tp_blast_demoted_even_when_live_keyword_still_matches():
    with tempfile.TemporaryDirectory() as td, \
         patch.object(mailcrm, "KEYWORDS_FILE", Path(td) / "kw.json"):
        old = dict(mailcrm._KEYWORDS_CACHE)
        try:
            mailcrm._KEYWORDS_CACHE.update({"mtime": None, "rules": None})
            mailcrm.save_keyword_rules({"interview": ["get you hired today"]})
            # the underlying substring classifier still (wrongly) says interview…
            assert mailcrm.classify(TP_SUBJECT, TP_BODY) == "interview"
            # …but the guard demotes the known TP blast to a non-actionable 'other', so it leaves
            # the Собес pool and can't fire a false interview alert.
            assert mailcrm._kind_with_done_override(
                TP_SUBJECT, TP_BODY, "chase.parker7542@takhet.com", TP_SENDER) == "other"
        finally:
            mailcrm._KEYWORDS_CACHE.clear()
            mailcrm._KEYWORDS_CACHE.update(old)


def test_genuine_interview_invite_is_not_demoted():
    with tempfile.TemporaryDirectory() as td, \
         patch.object(mailcrm, "KEYWORDS_FILE", Path(td) / "kw.json"):
        old = dict(mailcrm._KEYWORDS_CACHE)
        try:
            mailcrm._KEYWORDS_CACHE.update({"mtime": None, "rules": None})
            mailcrm.save_keyword_rules({"interview": ["interview invitation"]})
            subj = "Interview invitation — Software Engineer"
            body = "We would like to invite you to an interview. Pick a time."
            assert mailcrm.classify(subj, body) == "interview"
            # a normal recruiter sender keeps the interview classification
            assert mailcrm._kind_with_done_override(
                subj, body, "cand@takhet.com", "recruiter@bigco.com") == "interview"
        finally:
            mailcrm._KEYWORDS_CACHE.clear()
            mailcrm._KEYWORDS_CACHE.update(old)
