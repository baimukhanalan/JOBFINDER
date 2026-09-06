from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.domain.question import ResponseContract, ResponseKind
from qa_bot.domain.routing import AdapterKind as A
from qa_bot.domain.state import RunState, RunPhase
from qa_bot.perception.extractor import QuestionExtractor, CATALOG
from qa_bot.solvers.router import QuestionRouter, EXTENSIONS, route_state
from qa_bot.adapters.questions.stubs import default_adapters

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "questions"


def spec(name="ANA-01"):
    html = (FIXTURES / (name + ".html")).read_text(encoding="utf-8")
    return QuestionExtractor().extract(BrowserSnapshot(
        "obs", "http://127.0.0.1:8000/question.html", question_html=html)).spec


class RouterTests(unittest.TestCase):
    def test_all_catalog_types(self):
        expected = {
            "ANA-01": A.NUMERICAL, "ANA-02": A.TABLE_CHART,
            "ANA-03": A.NUMERICAL, "ANA-04": A.TABLE_CHART,
            "ANA-05": A.LOGICAL, "PERS-01": A.SJT_PERSONALITY,
            "SALES-01": A.SJT_PERSONALITY, "WRITEX-01": A.FREE_TEXT,
            "SVAR-01": A.SPEAKING,
            **{f"COMP-{i:02}": A.UI_SIMULATION for i in range(1, 17)},
        }
        self.assertEqual(set(expected), set(CATALOG))
        for name, adapter in expected.items():
            with self.subTest(type=name):
                self.assertEqual(QuestionRouter().route(spec(name)).adapter, adapter)

    def test_all_extension_families(self):
        for name, (adapter, kind) in EXTENSIONS.items():
            with self.subTest(type=name):
                base = spec("SALES-01" if name == "GEN-SJT" else "ANA-02")
                contract = (base.response_contract if name == "GEN-SJT" else
                            ResponseContract(kind, 1, 2) if kind == ResponseKind.MULTI_CHOICE else
                            ResponseContract(kind, 1, 1) if kind == ResponseKind.SINGLE_CHOICE else
                            ResponseContract(kind))
                question = replace(base, type_id=name, response_contract=contract,
                                   assets=spec("SVAR-01").assets)
                self.assertEqual(QuestionRouter().route(question).adapter, adapter)

    def test_ana05_subtypes_and_ambiguity(self):
        for text, expected in (("Find the matching word pair.", A.VERBAL),
                               ("Which is unlike the others?", A.LOGICAL),
                               ("A generic task.", None),
                               ("An analogy: which is unlike the others?", None)):
            with self.subTest(text=text):
                self.assertEqual(QuestionRouter().route(
                    replace(spec("ANA-05"), question_text=text)).adapter, expected)

    def test_contract_overrides_incidental_media(self):
        question = replace(spec("PERS-01"), tables=spec("ANA-02").tables,
                           assets=spec("SVAR-01").assets)
        self.assertEqual(QuestionRouter().route(question).adapter, A.SJT_PERSONALITY)
        self.assertEqual(QuestionRouter().route(spec("SVAR-01")).adapter, A.SPEAKING)

    def test_fail_closed(self):
        for changes in ({"type_id": "unknown"}, {"section": "wrong"},
                        {"completeness": False}, {"unresolved_regions": ("canvas",)},
                        {"extraction_confidence": 0.79}, {"remaining_seconds": 0},
                        {"response_contract": ResponseContract(ResponseKind.TEXT)}):
            with self.subTest(changes=changes):
                decision = QuestionRouter().route(replace(spec(), **changes))
                self.assertIsNone(decision.adapter)
                self.assertEqual(decision.confidence, 0)

    def test_required_inputs(self):
        for question, reason in (
            (replace(spec("ANA-02"), tables=()), "missing_table"),
            (replace(spec("ANA-03"), assets=()), "missing_image"),
            (replace(spec("WRITEX-01"), type_id="GEN-AUDIO"), "missing_input_audio"),
            (replace(spec("SALES-01"), response_contract=ResponseContract(
                ResponseKind.MULTI_CHOICE, 1, 2)), "invalid_sjt_roles"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(QuestionRouter().route(question).reason, reason)

    def test_confidence_cap(self):
        question = replace(spec(), extraction_confidence=0.8)
        self.assertEqual(QuestionRouter().route(question).confidence, 0.8)
        self.assertEqual(QuestionRouter().route(spec("ANA-05")).confidence, 0.85)

    def test_registry_and_missing_adapter(self):
        self.assertEqual(set(default_adapters()), set(A))
        self.assertEqual(QuestionRouter({}).route(spec()).reason, "adapter_unavailable")
        with self.assertRaises(ValueError):
            QuestionRouter({A.VERBAL: default_adapters()[A.NUMERICAL]})

    def test_resolve_revalidates(self):
        router, question = QuestionRouter(), spec()
        decision = router.route(question)
        self.assertEqual(router.resolve(decision, question).kind, A.NUMERICAL)
        for changed in (replace(question, question_id="new"),
                        replace(question, content_hash="changed"),
                        replace(question, completeness=False)):
            with self.assertRaises(ValueError):
                router.resolve(decision, changed)
        blocked = replace(question, type_id="unknown")
        with self.assertRaises(ValueError):
            router.resolve(router.route(blocked), blocked)

    def test_stub_is_offline_and_produces_no_answer(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            for kind, adapter in default_adapters().items():
                # asyncio.run itself may create a local event-loop socket on Windows;
                # invoke this coroutine directly: stubs must not suspend for I/O.
                coroutine = adapter.handle(spec())
                with self.assertRaises(StopIteration) as done:
                    coroutine.send(None)
                result = done.exception.value
                self.assertEqual(result.adapter, kind)
                self.assertEqual(result.status, "not_implemented")
                self.assertIsNone(result.answer)

    def test_run_state_success_and_stop(self):
        state = route_state(RunState("run", question_spec=spec()))
        self.assertEqual(state.phase, RunPhase.ROUTED)
        self.assertEqual(state.selected_adapter, A.NUMERICAL)
        self.assertEqual(state.processed_count, 0)
        for changes in ({"question_spec": None}, {"extraction_errors": ("bad",)},
                        {"question_spec": replace(spec(), type_id="unknown")}):
            stopped = route_state(replace(state, **changes))
            self.assertEqual(stopped.phase, RunPhase.REVIEW_REQUIRED)
            self.assertIsNone(stopped.selected_adapter)
            self.assertEqual(stopped.routing_confidence, 0)

    def test_state_validation(self):
        for changes in ({"routing_confidence": float("nan")},
                        {"routing_confidence": 1.1}, {"selected_adapter": "numerical"}):
            with self.assertRaises(ValueError):
                RunState("run", **changes)
