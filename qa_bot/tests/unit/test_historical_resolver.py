import csv
import json
import tempfile
import unittest
from pathlib import Path

from qa_bot.domain.question import OptionSpec, QuestionSpec, ResponseContract, ResponseKind
from qa_bot.knowledge.historical import HistoricalAnswerResolver


def question(*, text="2 + 2?", instruction="Choose.", options=("3", "4"),
             kind=ResponseKind.SINGLE_CHOICE, roles=()):
    option_specs = tuple(OptionSpec(f"o{i}", i, label) for i, label in enumerate(options, 1))
    selections = 1 if kind == ResponseKind.SINGLE_CHOICE else len(roles)
    if kind == ResponseKind.UI_ACTION:
        selections = 0
    return QuestionSpec(
        "live-q", "attempt", "Analytical", "ANA-01", text,
        ResponseContract(kind, selections, selections, roles), "live-hash",
        instruction=instruction, options=option_specs, completeness=True,
        extraction_confidence=1.0,
    )


class HistoricalResolverTests(unittest.TestCase):
    def resolver(self, rows, answers):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        questions = root / "questions.jsonl"
        questions.write_text("".join(json.dumps(row) + "\n" for row in rows))
        csv_path = root / "answers.csv"
        fields = ["id", "recommended_answer", "confidence", "official_answer_key"]
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(answers)
        return HistoricalAnswerResolver.from_files(questions, csv_path)

    def test_exact_repeat_resolves_to_current_option_id(self):
        rows = [
            {"id": "old-1", "section": "Analytical", "prompt": "Choose.",
             "text": "2 + 2?", "options": ["3", "4"]},
            {"id": "old-2", "section": "Analytical", "prompt": "Choose.",
             "text": "2 + 2?", "options": ["3", "4"]},
        ]
        answers = [
            {"id": "old-1", "recommended_answer": "4", "confidence": "high",
             "official_answer_key": "false"},
            {"id": "old-2", "recommended_answer": "4", "confidence": "high",
             "official_answer_key": "false"},
        ]
        resolver = self.resolver(rows, answers)
        result = resolver.lookup(question())
        self.assertEqual(result.proposal.selections[0].option_id, "o2")
        self.assertEqual(result.occurrence_count, 2)
        self.assertFalse(result.official_answer_key)
        self.assertEqual(resolver.stats(), {"exact_groups": 1, "repeated_groups": 1,
                                            "source_occurrences": 2})

    def test_changed_word_or_option_order_is_not_a_match(self):
        resolver = self.resolver(
            [{"id": "old", "section": "Analytical", "prompt": "Choose.",
              "text": "2 + 2?", "options": ["3", "4"]}],
            [{"id": "old", "recommended_answer": "4", "confidence": "high",
              "official_answer_key": "false"}],
        )
        self.assertIsNone(resolver.lookup(question(text="2 plus 2?")))
        self.assertIsNone(resolver.lookup(question(options=("4", "3"))))

    def test_conflicting_exact_history_fails_closed(self):
        rows = [
            {"id": "a", "section": "Analytical", "prompt": "Choose.",
             "text": "2 + 2?", "options": ["3", "4"]},
            {"id": "b", "section": "Analytical", "prompt": "Choose.",
             "text": "2 + 2?", "options": ["3", "4"]},
        ]
        answers = [
            {"id": "a", "recommended_answer": "4", "confidence": "high",
             "official_answer_key": "false"},
            {"id": "b", "recommended_answer": "3", "confidence": "high",
             "official_answer_key": "false"},
        ]
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.resolver(rows, answers)

    def test_best_worst_roles_bind_to_exact_labels(self):
        options = ("Helpful action", "Harmful action", "Neutral action")
        resolver = self.resolver(
            [{"id": "sales", "section": "Analytical", "prompt": "Choose.",
              "text": "Situation", "options": list(options)}],
            [{"id": "sales", "recommended_answer":
              "BEST: Helpful action\nWORST: Harmful action", "confidence": "medium",
              "official_answer_key": "false"}],
        )
        result = resolver.lookup(question(
            text="Situation", options=options, kind=ResponseKind.MULTI_CHOICE,
            roles=("best", "worst"),
        ))
        self.assertEqual([(s.option_id, s.role) for s in result.proposal.selections],
                         [("o1", "best"), ("o2", "worst")])

    def test_ui_action_returns_stable_plan_and_text(self):
        resolver = self.resolver(
            [{"id": "computer", "section": "Analytical", "prompt": "Choose.",
              "text": "Open Notepad.", "options": []}],
            [{"id": "computer", "recommended_answer":
              "Open Start -> Find Notepad -> Open", "confidence": "high",
              "official_answer_key": "false"}],
        )
        result = resolver.lookup(question(
            text="Open Notepad.", options=(), kind=ResponseKind.UI_ACTION,
        ))
        self.assertTrue(result.proposal.plan_id.startswith("historical:"))
        self.assertEqual(result.action_plan, "Open Start -> Find Notepad -> Open")
