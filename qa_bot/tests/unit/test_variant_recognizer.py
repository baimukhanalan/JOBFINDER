import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path

from qa_bot.knowledge.variants import (
    ExactVariantRecognizer,
    materialize_audio_prefetch,
)


class VariantRecognizerTests(unittest.TestCase):
    def recognizer(self, records, answers=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        questions = root / "questions.jsonl"
        questions.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        answer_file = root / "answers.csv"
        with answer_file.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("id", "recommended_answer"))
            writer.writeheader()
            writer.writerows(answers or [])
        return ExactVariantRecognizer.from_files(questions, answer_file)

    @staticmethod
    def record(identity, test_id, number, text, *, section="Section A: Read and Speak",
               prompt="Read the given sentence out loud."):
        return {
            "id": identity,
            "test_id": test_id,
            "section": section,
            "question_number": number,
            "practice": False,
            "prompt": prompt,
            "text": text,
            "options": [],
        }

    def test_identical_profile_sequences_are_one_variant(self):
        records = []
        for test_id in ("TP-001", "TP-002"):
            records.extend([
                self.record(test_id + "-1", test_id, 1, "Same first sentence."),
                self.record(test_id + "-2", test_id, 2, "Same second sentence."),
            ])
        recognizer = self.recognizer(records)
        result = recognizer.identify(
            ["Same first sentence.", "Same second sentence."],
            section="SVAR - Read and Speak"
        )
        self.assertTrue(result.recognized)
        self.assertEqual(result.variant.source_test_ids, ("TP-001", "TP-002"))

    def test_second_exact_question_resolves_ambiguous_first_question(self):
        recognizer = self.recognizer([
            self.record("a1", "A", 1, "Shared."),
            self.record("a2", "A", 2, "Variant A."),
            self.record("a3", "A", 3, "Next A."),
            self.record("b1", "B", 1, "Shared."),
            self.record("b2", "B", 2, "Variant B."),
            self.record("b3", "B", 3, "Next B."),
        ])
        first = recognizer.identify(["Shared."], section="Read and Speak")
        self.assertEqual(first.status, "ambiguous")
        self.assertEqual(recognizer.plan_audio_prefetch(first), ())

        second = recognizer.identify(
            ["Shared.", "Variant A."], section="Section A: Read and Speak"
        )
        self.assertTrue(second.recognized)
        tasks = recognizer.plan_audio_prefetch(
            second, replay_lookup=lambda question, answer: "cached.wav"
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].question_text, "Next A.")
        self.assertEqual(tasks[0].answer_text, "Next A.")
        self.assertEqual(tasks[0].strategy, "exact_replay")

    def test_unique_first_question_is_tentative_until_second_observation(self):
        recognizer = self.recognizer([
            self.record("a1", "A", 1, "Unique first."),
            self.record("a2", "A", 2, "Second."),
        ])
        first = recognizer.identify(["Unique first."], section="Read and Speak")
        self.assertEqual(first.status, "tentative")
        self.assertEqual(recognizer.plan_audio_prefetch(first), ())
        second = recognizer.identify(
            ["Unique first.", "Second."], section="Read and Speak"
        )
        self.assertTrue(second.recognized)

    def test_changed_punctuation_is_unknown(self):
        recognizer = self.recognizer([
            self.record("a1", "A", 1, "Read this sentence."),
        ])
        result = recognizer.identify(
            ["Read this sentence!"], section="Read and Speak"
        )
        self.assertEqual(result.status, "unknown")
        self.assertEqual(recognizer.plan_audio_prefetch(result), ())

    def test_conflicting_answers_disable_entire_prefetch_plan(self):
        records = [
            self.record("a1", "A", 1, "First.", section="Spoken English",
                        prompt="Listen to the question and answer."),
            self.record("a2", "A", 2, "Second.", section="Spoken English",
                        prompt="Listen to the question and answer."),
            self.record("a3", "A", 3, "Third.", section="Spoken English",
                        prompt="Listen to the question and answer."),
            self.record("b1", "B", 1, "First.", section="Spoken English",
                        prompt="Listen to the question and answer."),
            self.record("b2", "B", 2, "Second.", section="Spoken English",
                        prompt="Listen to the question and answer."),
            self.record("b3", "B", 3, "Third.", section="Spoken English",
                        prompt="Listen to the question and answer."),
        ]
        answers = [
            {"id": "a3", "recommended_answer": "Answer one."},
            {"id": "b3", "recommended_answer": "Answer two."},
        ]
        recognizer = self.recognizer(records, answers)
        result = recognizer.identify(
            ["First.", "Second."], section="Spoken English"
        )
        self.assertTrue(result.recognized)
        self.assertEqual(recognizer.plan_audio_prefetch(result), ())

    def test_parallel_materialization_preserves_question_order(self):
        recognizer = self.recognizer([
            self.record("a1", "A", 1, "First."),
            self.record("a2", "A", 2, "Second."),
            self.record("a3", "A", 3, "Third."),
        ])
        result = recognizer.identify(
            ["First.", "Second."], section="Read and Speak"
        )
        tasks = recognizer.plan_audio_prefetch(result)

        async def prepare(task):
            await asyncio.sleep(0)
            return task.position

        prepared = asyncio.run(materialize_audio_prefetch(tasks, prepare, concurrency=2))
        self.assertEqual(prepared, (3,))

    def test_current_corpus_is_fully_resolved_by_two_question_prefix(self):
        qa_root = Path(__file__).resolve().parents[2]
        corpus_root = qa_root.parent
        recognizer = ExactVariantRecognizer.from_files(
            corpus_root / "data/questions.jsonl",
            corpus_root / "SHL_answers_all.csv",
        )
        metrics = {metric.prefix_size: metric for metric in recognizer.prefix_metrics()}
        self.assertEqual(metrics[1].variant_groups, 28)
        self.assertEqual(metrics[1].resolved_variant_groups, 26)
        self.assertEqual(metrics[1].resolved_source_assignments, 27)
        self.assertEqual(metrics[2].resolved_variant_groups, 28)
        self.assertEqual(metrics[2].resolved_source_assignments, 39)
        self.assertEqual(metrics[3].resolved_variant_groups, 28)
        self.assertEqual(metrics[3].resolved_source_assignments, 39)


if __name__ == "__main__":
    unittest.main()
