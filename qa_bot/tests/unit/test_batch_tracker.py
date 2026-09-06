import tempfile
import unittest
from pathlib import Path

from qa_bot.orchestration.batch import AttemptEvidence, TrialBatchTracker


TARGETS = tuple((f"TP-{number:03d}", f"Profile {number}") for number in range(15, 30))


def evidence(**changes):
    values = dict(
        test_id="TP-015", profile="Profile 15",
        planned_sections=("Speech", "Typing", "Personality"),
        completed_sections=("Speech", "Typing", "Personality"),
        answered_questions=20, captured_questions=20, audio_captures=8,
        skipped_questions=0, random_answers=0,
        completion_evidence="Your test is now complete.",
    )
    values.update(changes)
    return AttemptEvidence(**values)


class BatchTrackerTests(unittest.TestCase):
    def test_counts_only_clean_complete_unique_attempts_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.json"
            tracker = TrialBatchTracker(path, TARGETS)
            tracker.initialize()
            self.assertTrue(path.is_file())
            tracker.record(evidence())
            self.assertEqual(tracker.summary()["completed"], 1)
            self.assertEqual(tracker.summary()["next_test_id"], "TP-016")
            loaded = TrialBatchTracker(path, TARGETS)
            self.assertEqual(loaded.summary(), tracker.summary())

    def test_rejects_skip_random_capture_gap_and_partial_section(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = TrialBatchTracker(Path(directory) / "batch.json", TARGETS)
            for bad in (
                evidence(skipped_questions=1),
                evidence(random_answers=1),
                evidence(captured_questions=19),
                evidence(completed_sections=("Speech", "Typing")),
            ):
                with self.assertRaisesRegex(ValueError, "gaps"):
                    tracker.record(bad)
            self.assertEqual(tracker.summary()["completed"], 0)

    def test_rejects_wrong_profile_and_changed_completion_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = TrialBatchTracker(Path(directory) / "batch.json", TARGETS)
            with self.assertRaisesRegex(ValueError, "outside"):
                tracker.record(evidence(profile="Someone else"))
            tracker.record(evidence())
            with self.assertRaisesRegex(ValueError, "changed"):
                tracker.record(evidence(answered_questions=21, captured_questions=21))
