import unittest

from qa_bot.distribution import sanitized_record, scan_secret_markers, safe_relative


class DistributionTests(unittest.TestCase):
    def test_excludes_operational_logs_and_never_approves(self):
        result = sanitized_record({"id": "q1", "text": "Choose two", "options": ["a", "b"],
            "executed_actions": ["private"], "submitted_to": "private", "source_candidate": "private",
            "answer_status": "approved", "screenshots": [{"path": "assets/q.jpg", "sha256": "a" * 64, "source_call_id": "private"}]})
        self.assertEqual(result["approval_status"], "candidate")
        self.assertNotIn("private", str(result))
        self.assertEqual(result["screenshots"][0]["path"], "assets/q.jpg")

    def test_secret_marker_rejects_without_echo(self):
        for value in ("sk_" + "a" * 48, "token=" + "a" * 150):
            with self.assertRaises(ValueError) as raised:
                scan_secret_markers(value.encode())
            self.assertNotIn(value, str(raised.exception))
        scan_secret_markers(b"No credentials: use protected input.")

    def test_paths_cannot_escape_package(self):
        for value in ("../secret", "/secret", "C:/secret", "assets/../../secret", "assets\\x"):
            with self.assertRaises(ValueError):
                safe_relative(value)
        self.assertEqual(safe_relative("assets/test.jpg"), "assets/test.jpg")
