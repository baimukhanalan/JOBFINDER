import asyncio
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from qa_bot.diagnostics import read_secrets, inspect_page, validate_budget
from qa_bot.domain.state import RunState
from qa_bot.domain.observation import BrowserSnapshot, PageElement


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_is_authentication_block_not_unknown_question(self):
        state = RunState("test", observation=BrowserSnapshot("o", "https://test.invalid",
                         dom_text="Login", elements=(PageElement("password", "input", input_type="password"),)))
        with patch("qa_bot.diagnostics.observe_once", AsyncMock(return_value=state)):
            result = await inspect_page("https://test.invalid", ("https://test.invalid",))
        self.assertEqual(result["status"], "blocked_authentication")
        self.assertEqual(result["answers_sent"], 0)

    def test_budget_is_positive_without_twenty_minute_limit(self):
        for bad in (0, True, -1):
            with self.assertRaises(ValueError):
                validate_budget(bad)
        self.assertEqual(validate_budget(1200), 1200)
        self.assertEqual(validate_budget(3600), 3600)

    def test_stdin_is_bounded_and_unknown_fields_rejected(self):
        self.assertEqual(read_secrets(io.StringIO('{"url":"https://test.invalid"}\n')), {"url": "https://test.invalid"})
        for value in ('{"command":"run"}', 'null', '{"api_key":5}', 'x' * 17000):
            with self.assertRaises(ValueError):
                read_secrets(io.StringIO(value))

    async def test_consent_stop_and_no_page_content_in_report(self):
        state = RunState("test", observation=BrowserSnapshot("o", "https://test.invalid/?token=secret",
                         dom_text="Candidate Online Assessment Data Protection Notice secret"))
        with patch("qa_bot.diagnostics.observe_once", AsyncMock(return_value=state)):
            result = await inspect_page("https://test.invalid/?token=secret", ("https://test.invalid",))
        self.assertEqual(result["status"], "blocked_consent")
        self.assertEqual(result["actions_executed"], 0)
        self.assertNotIn("secret", json.dumps(result))

    async def test_browser_error_is_sanitized(self):
        with patch("qa_bot.diagnostics.observe_once", AsyncMock(side_effect=ValueError("private token"))):
            result = await inspect_page("https://test.invalid", ("https://test.invalid",))
        self.assertEqual(result["status"], "observation_failed")
        self.assertNotIn("private", json.dumps(result))

    async def test_unknown_is_not_success(self):
        with patch("qa_bot.diagnostics.observe_once", AsyncMock(return_value=RunState("test"))):
            result = await inspect_page("https://test.invalid", ("https://test.invalid",))
        self.assertEqual(result["status"], "blocked_unknown_format")
