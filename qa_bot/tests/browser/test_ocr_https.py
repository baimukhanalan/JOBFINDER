"""Real Chromium with intercepted HTTPS, NOT a real token/platform test."""
import json
import unittest
from unittest.mock import patch
from qa_bot.adapters.browser.controller import BrowserController, origin
from qa_bot.adapters.ocr.local import LocalOCRProvider
from qa_bot.domain.state import RunState, RunPhase
from qa_bot.orchestration.observe import observe_once
from qa_bot.reporting.observation_log import state_event
from unit.test_ocr import snapshot, render


class HTTPSOCRTests(unittest.IsolatedAsyncioTestCase):
    async def test_https_token_url_ocr_without_secret_logging(self):
        html = snapshot().question_html
        async def fixture_route(controller, route):
            # No external network: preserve the controller's origin gate.
            if origin(route.request.url) not in controller.allowed_origins:
                await route.abort()
            else:
                await route.fulfill(status=200, content_type="text/html", body=html)
        with patch.object(BrowserController, "_route", fixture_route):
            state = await observe_once(
                "https://assessment.example.invalid/?token=synthetic-only",
                ("https://assessment.example.invalid",), RunState("https-test"),
                route=True, ocr_provider=LocalOCRProvider(),
                ocr_requests={"text": render("TOTAL 42")})
        self.assertEqual(state.phase, RunPhase.ROUTED)
        event = state_event(state)
        self.assertEqual(event["origin"], "https://assessment.example.invalid:443")
        self.assertEqual(event["ocr_region_count"], 1)
        self.assertEqual(event["actions_executed"], 0)
        self.assertNotIn("synthetic-only", json.dumps(event))
        self.assertNotIn("TOTAL 42", json.dumps(event))
