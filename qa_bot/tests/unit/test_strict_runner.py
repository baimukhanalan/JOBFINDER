import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from qa_bot.orchestration.runner import (
    AppliedAnswer, PageState, RunJournal, StrictAssessmentRunner, TransitionEvidence,
)


@dataclass(frozen=True)
class Question:
    question_id: str
    content_hash: str
    completeness: bool = True
    unresolved_regions: tuple = ()
    extraction_confidence: float = 1.0


class Driver:
    def __init__(self, pages, *, errors=(), accepted=True, advance=True):
        self.pages = iter(pages)
        self.errors = errors
        self.accepted = accepted
        self.advance = advance

    async def observe(self):
        return next(self.pages)

    async def extract(self, state):
        return state.payload

    async def solve(self, question):
        return {"answer": question.question_id}

    async def validate(self, question, answer):
        return self.errors

    async def apply(self, question, answer):
        return AppliedAnswer(question.question_id, question.content_hash, "answer-sha",
                             self.accepted, "dom-selected")

    async def confirm_transition(self, before, receipt):
        return TransitionEvidence(self.advance, before.identity + "-next", "new-question-id")


class StrictRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_completes_only_after_every_confirmed_transition(self):
        pages = [
            PageState("question", "q1", "A", 1, Question("q1", "h1")),
            PageState("question", "q2", "B", 1, Question("q2", "h2")),
            PageState("complete", "done"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            journal = RunJournal(Path(directory) / "run.jsonl")
            outcome = await StrictAssessmentRunner(Driver(pages), journal).run()
            records = [json.loads(line) for line in journal.path.read_text().splitlines()]
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.processed, 2)
        self.assertEqual(outcome.sections, ("A", "B"))
        self.assertEqual([r["event"] for r in records].count("question_confirmed"), 2)
        self.assertEqual(records[-1]["event"], "completed")

    async def test_invalid_answer_stops_without_applying_or_skipping(self):
        page = PageState("question", "q1", "A", 1, Question("q1", "h1"))
        driver = Driver([page], errors=("wrong_kind",))
        driver.apply_calls = 0

        async def count_apply(*args):
            driver.apply_calls += 1
            raise AssertionError("must not apply")

        driver.apply = count_apply
        with tempfile.TemporaryDirectory() as directory:
            outcome = await StrictAssessmentRunner(
                driver, RunJournal(Path(directory) / "run.jsonl")
            ).run()
        self.assertEqual(outcome.status, "stopped")
        self.assertIn("answer_invalid", outcome.reason)
        self.assertEqual(driver.apply_calls, 0)

    async def test_unconfirmed_transition_stops_and_is_not_counted(self):
        page = PageState("question", "q1", "A", 1, Question("q1", "h1"))
        with tempfile.TemporaryDirectory() as directory:
            outcome = await StrictAssessmentRunner(
                Driver([page], advance=False), RunJournal(Path(directory) / "run.jsonl")
            ).run()
        self.assertEqual(outcome.processed, 0)
        self.assertEqual(outcome.reason, "transition_not_confirmed")
