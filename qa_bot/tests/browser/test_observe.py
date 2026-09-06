import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from qa_bot.adapters.browser.controller import BrowserController
from qa_bot.domain.state import RunState, RunPhase
from qa_bot.domain.screen import ScreenType
from qa_bot.orchestration.observe import observe_once
from qa_bot.perception.detector import ScreenDetector
from qa_bot.reporting.observation_log import state_event
from qa_bot.app import main


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://not-allowed.invalid/")
            self.end_headers()
            return
        return super().do_GET()


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def test_local_network_permission_is_explicit_and_page_scoped(self):
        controller = BrowserController(
            ("https://assessment.example", "http://127.0.0.1:8769"),
            local_network_origins=("https://assessment.example",),
        )
        self.assertEqual(
            controller.local_network_origins,
            ("https://assessment.example:443",),
        )
        with self.assertRaisesRegex(ValueError, "must be allowlisted"):
            BrowserController(
                ("https://assessment.example",),
                local_network_origins=("https://foreign.example",),
            )
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            BrowserController(
                ("http://127.0.0.1:8769",),
                local_network_origins=("http://127.0.0.1:8769",),
            )

    async def test_login_classified_as_authentication_gate(self):
        state = await observe_once(self.base + "/login.html", (self.base,), RunState("test"))
        self.assertEqual(state.screen_type, ScreenType.START)
        self.assertEqual(state.detection_reason, "authentication_required")
        self.assertEqual(state.processed_count, 0)

    async def test_open_shadow_content_observed_but_requires_profile(self):
        controller = BrowserController((self.base,))
        try:
            await controller.open(self.base + "/unknown.html")
            await controller._page.set_content('<div id="host"></div>')
            await controller._page.evaluate("document.querySelector('#host').attachShadow({mode:'open'}).innerHTML = '<h1>Candidate Online Assessment Data Protection Notice</h1><button>Continue</button>'")
            snapshot = await controller.observe()
            self.assertIn("Data Protection Notice", snapshot.dom_text)
            self.assertTrue(any(e.name == "Continue" for e in snapshot.elements))
            self.assertEqual(ScreenDetector().detect(snapshot).reason, "shadow_dom_requires_profile")
        finally:
            await controller.close()

    async def test_delayed_content_and_blocked_origin_metadata(self):
        controller = BrowserController((self.base,))
        try:
            await controller.open(self.base + "/unknown.html")
            await controller._page.set_content('<body><script>setTimeout(() => {document.body.innerHTML = "Ready"}, 100)</script></body>')
            self.assertTrue(await controller.wait_for_content(timeout=2))
            await controller._page.evaluate("fetch('https://blocked.invalid/?token=private').catch(() => {})")
            snapshot = await controller.observe()
            self.assertIn("Ready", snapshot.dom_text)
            self.assertEqual(snapshot.blocked_origins, ("https://blocked.invalid:443",))
            self.assertNotIn("private", json.dumps(state_event(RunState("test", observation=snapshot))))
        finally:
            await controller.close()

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "fixtures" / "screens"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                        partial(QuietHandler, directory=str(root)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    async def test_seven_fixture_screens(self):
        for kind in ScreenType:
            with self.subTest(screen=kind):
                state = await observe_once(f"{self.base}/{kind.value}.html",
                                           (self.base,), RunState("test"))
                self.assertEqual(state.screen_type, kind)
                self.assertEqual(state.phase, RunPhase.OBSERVED)
                self.assertEqual(state.processed_count, 0)

    async def test_dom_elements_and_close(self):
        controller = BrowserController((self.base,))
        try:
            await controller.open(self.base + "/question.html")
            self.assertTrue(controller.is_open)
            snapshot = await controller.observe()
            self.assertEqual(snapshot.title, "QA fixture")
            self.assertIn("Question 1", snapshot.dom_text)
            self.assertTrue(snapshot.document_ref.endswith("/question.html"))
            self.assertEqual(ScreenDetector().detect(snapshot).section, "Basic Analytical Ability")
            self.assertTrue(any(e.name == "Choice A" for e in snapshot.elements))
            self.assertFalse(any(e.ref == "hidden" for e in snapshot.elements))
            self.assertFalse(next(e for e in snapshot.elements if e.ref == "disabled").enabled)
        finally:
            await controller.close()
        self.assertFalse(controller.is_open)
        await controller.close()
        with self.assertRaises(RuntimeError):
            await controller.observe()

    async def test_origin_rejected_before_launch(self):
        controller = BrowserController((self.base,))
        with self.assertRaises(ValueError):
            await controller.open("https://not-allowed.invalid/")
        self.assertFalse(controller.is_open)

    async def test_redirect_failure_closes_browser(self):
        controller = BrowserController((self.base,))
        with self.assertRaises(Exception):
            await controller.open(self.base + "/redirect", timeout=5)
        self.assertFalse(controller.is_open)

    async def test_frame_is_unknown(self):
        state = await observe_once(self.base + "/frame.html", (self.base,), RunState("test"))
        self.assertEqual(state.screen_type, ScreenType.UNKNOWN)

    async def test_ambiguous_headings(self):
        state = await observe_once(self.base + "/ambiguous.html", (self.base,), RunState("test"))
        self.assertEqual(state.screen_type, ScreenType.UNKNOWN)
        self.assertEqual(state.detection_reason, "conflicting_rules")

    async def test_secret_not_in_log(self):
        state = await observe_once(self.base + "/question.html?token=synthetic-secret",
                                   (self.base,), RunState("test"))
        encoded = json.dumps(state_event(state))
        self.assertNotIn("synthetic-secret", encoded)
        self.assertNotIn("question.html", encoded)
        self.assertNotIn("Question 1", encoded)
        self.assertNotIn("synthetic-secret", repr(state))

    async def test_cli_observe_journals_once(self):
        def invoke():
            output = io.StringIO()
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "events.jsonl"
                with patch.dict(os.environ, {"QA_TEST_URL": self.base + "/start.html"}):
                    with redirect_stdout(output):
                        result = main(["--observe", "--url-env", "QA_TEST_URL",
                                       "--allow-origin", self.base, "--log", str(path)])
                event = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(result, 0)
                self.assertEqual(event["screen_type"], "start")
                self.assertEqual(event["actions_executed"], 0)
                self.assertEqual(json.loads(output.getvalue()), event)
        await asyncio.to_thread(invoke)
