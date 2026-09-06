import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from qa_bot.app import main


class BootstrapTests(unittest.TestCase):
    def test_dry_run_has_no_network_or_actions(self):
        output = io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            with redirect_stdout(output):
                self.assertEqual(main([]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["actions_executed"], 0)
        self.assertEqual(report["adapters_initialized"], [])
        self.assertEqual(report["state"]["phase"], "dry_run_ready")

    def test_live_mode_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["--mode", "live"])
        self.assertEqual(error.exception.code, 2)
