"""Inbox regression tests that run with the Python standard library alone."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

# mailcrm imports the Postgres adapter, but these unit tests exercise only the
# classifier/Maildir operations. A tiny stand-in keeps local QA dependency-free.
fake_db = types.ModuleType("backend.tools.mail_db")
fake_db.get_row = lambda _mid: None
fake_db.delete_paths = lambda _ids: 0
sys.modules.setdefault("backend.tools.mail_db", fake_db)

from backend.tools import mailcrm, mailcrm_ui  # noqa: E402


class ClassifierTests(unittest.TestCase):
    def test_application_ack_with_generic_next_steps_is_not_interview(self):
        body = ("We received your application and our recruiting team is screening it. "
                "We will contact you about next steps if your experience is a match.")
        self.assertEqual(mailcrm.classify("Application received", body), "ack")

    def test_russian_application_ack_is_not_interview(self):
        body = "Ваш отклик принят. Мы рассмотрим заявку и сообщим о следующем этапе."
        self.assertEqual(mailcrm.classify("Заявка принята", body), "ack")

    def test_explicit_interview_invitation(self):
        body = "We would like to schedule an interview. Please select a time that works."
        self.assertEqual(mailcrm.classify("Next steps", body), "interview")

    def test_explicit_offer(self):
        self.assertEqual(mailcrm.classify("Offer letter", "We are pleased to offer you the role."),
                         "offer")

    def test_negated_interview_is_rejection(self):
        body = "Unfortunately, we will not be moving forward to an interview."
        self.assertEqual(mailcrm.classify("Your application", body), "rejection")


class ReplyUiTests(unittest.TestCase):
    def test_reply_data_survives_quotes_without_inline_javascript(self):
        page = mailcrm_ui.render_thread({
            "mailbox": "candidate@example.com",
            "candidate": "Candidate",
            "subject": 'Recruiter\'s "next" step',
            "messages": [{
                "id": "abc", "from_email": "recruiter@example.com",
                "from_name": "Recruiter", "message_id": "<id'quoted@example.com>",
                "subject": "Subject", "plain": "Hello", "outbound": False,
            }],
        })
        self.assertIn('class="hbtn reply-action"', page)
        self.assertNotIn('onclick="reply(', page)
        self.assertIn("Recruiter&#x27;s &quot;next&quot; step", page)
        self.assertIn("document.querySelectorAll('.reply-action')", page)

    def test_candidate_search_is_available_on_desktop_and_preserves_filter(self):
        page = mailcrm_ui.render_candidates(
            [{"name": "Dinara", "email": "dinara@example.com", "unread": 0}],
            counts={"interview": 1}, active_filter="interview", total=539,
            query="dinara", has_more=1,
        )
        self.assertIn('class="candidate-tools"', page)
        self.assertIn('value="dinara"', page)
        self.assertIn('name="filter" value="interview"', page)
        self.assertIn('filter=interview&amp;q=dinara', page)

    def test_mobile_css_keeps_filters_and_message_actions_accessible(self):
        page = mailcrm_ui.render_candidates([], total=0)
        self.assertIn(".funnel{flex-wrap:nowrap;overflow-x:auto", page)
        thread = mailcrm_ui.render_thread({"messages": []})
        self.assertIn(".msg-toolbar .reply-action,.msg-toolbar .delete-action", thread)


class DeleteThreadTests(unittest.TestCase):
    @staticmethod
    def _write_message(path: Path, subject: str) -> None:
        msg = EmailMessage()
        msg["From"] = "Recruiter <recruiter@example.com>"
        msg["To"] = "candidate@example.com"
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{path.name}@example.com>"
        msg.set_content("Body")
        path.write_bytes(msg.as_bytes())

    def test_delete_moves_whole_thread_to_recoverable_trash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "candidate"
            (root / "new").mkdir(parents=True)
            (root / "cur").mkdir()
            first = root / "new" / "one"
            second = root / "cur" / "two:2,S"
            self._write_message(first, "Interview update")
            self._write_message(second, "Re: Interview update")
            box = {"id": "p1", "email": "candidate@example.com", "name": "Candidate",
                   "maildir": str(root)}
            deleted = []
            with patch.object(mailcrm, "candidates", return_value=[box]), \
                 patch.object(mailcrm.mail_db, "get_row", return_value=None), \
                 patch.object(mailcrm.mail_db, "delete_paths",
                              side_effect=lambda ids: deleted.extend(ids) or len(ids)):
                result = mailcrm.delete_thread(mailcrm._pid(str(first)))

            self.assertTrue(result["ok"])
            self.assertTrue(result["recoverable"])
            self.assertEqual(result["deleted"], 2)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertEqual(len(list((root / ".Trash" / "cur").iterdir())), 2)
            self.assertEqual(len(deleted), 2)


if __name__ == "__main__":
    unittest.main()
