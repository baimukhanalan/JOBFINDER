import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
import tempfile
import os
import io
import json
from unittest.mock import patch
from contextlib import redirect_stdout

from browser.test_observe import QuietHandler
from qa_bot.adapters.browser.controller import BrowserController
from qa_bot.domain.state import RunState, RunPhase
from qa_bot.perception.extractor import QuestionExtractor, CATALOG
from qa_bot.orchestration.observe import observe_once
from qa_bot.app import main


class ExtractionBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "fixtures" / "questions"
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

    async def test_all_25_browser_fixtures(self):
        for name in CATALOG:
            with self.subTest(type=name):
                state = await observe_once(self.base + "/" + name + ".html",
                                           (self.base,), RunState("test"), extract=True)
                self.assertEqual(state.phase, RunPhase.EXTRACTED, state.extraction_errors)
                self.assertEqual(state.question_spec.type_id, name)
                self.assertEqual(state.processed_count, 0)
                self.assertEqual(state.current_question_id, "q-" + name)

    async def test_unknown_stops_and_clears_spec(self):
        first = await observe_once(self.base + "/ANA-01.html", (self.base,),
                                   RunState("test"), extract=True)
        stopped = await observe_once(self.base + "/unknown.html", (self.base,),
                                     first, extract=True)
        self.assertEqual(stopped.phase, RunPhase.REVIEW_REQUIRED)
        self.assertIsNone(stopped.question_spec)
        self.assertIsNone(stopped.current_question_id)

    async def test_css_hidden_option_excluded(self):
        state = await observe_once(self.base + "/hidden.html", (self.base,),
                                   RunState("test"), extract=True)
        self.assertEqual(state.phase, RunPhase.EXTRACTED, state.extraction_errors)
        self.assertEqual(len(state.question_spec.options), 4)

    async def test_route_then_unknown_clears_selection(self):
        first = await observe_once(self.base + "/ANA-01.html", (self.base,),
                                   RunState("test"), route=True)
        self.assertEqual(first.phase, RunPhase.ROUTED)
        self.assertEqual(first.selected_adapter.value, "numerical")
        stopped = await observe_once(self.base + "/unknown.html", (self.base,),
                                     first, route=True)
        self.assertIsNone(stopped.selected_adapter)
        self.assertEqual(stopped.phase, RunPhase.REVIEW_REQUIRED)
        observed = await observe_once(self.base + "/ANA-01.html", (self.base,), first)
        self.assertIsNone(observed.selected_adapter)

    async def test_route_cli_metadata_and_no_actions(self):
        def invoke():
            for name, expected_code in (("ANA-01", 0), ("unknown", 3)):
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "log.jsonl"
                    with patch.dict(os.environ, {"QA_TEST_URL": self.base + "/" + name + ".html"}):
                        with redirect_stdout(io.StringIO()):
                            code = main(["--route", "--url-env", "QA_TEST_URL",
                                         "--allow-origin", self.base, "--log", str(path)])
                    self.assertEqual(code, expected_code)
                    event = json.loads(path.read_text())
                    self.assertEqual(event["selected_adapter"],
                                     "numerical" if expected_code == 0 else None)
                    self.assertEqual(event["actions_executed"], 0)
        await asyncio.to_thread(invoke)

    async def test_cli_failure_exit_and_log(self):
        def invoke():
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "log.jsonl"
                with patch.dict(os.environ, {"QA_TEST_URL": self.base + "/unknown.html"}):
                    with redirect_stdout(io.StringIO()):
                        code = main(["--extract", "--url-env", "QA_TEST_URL",
                                     "--allow-origin", self.base, "--log", str(path)])
                self.assertEqual(code, 3)
                event = json.loads(path.read_text())
                self.assertFalse(event["extraction_ready"])
                self.assertEqual(event["actions_executed"], 0)
        await asyncio.to_thread(invoke)
