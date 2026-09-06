"""Durable accounting for a batch of clean, unique trial assessments."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AttemptEvidence:
    test_id: str
    profile: str
    planned_sections: tuple[str, ...]
    completed_sections: tuple[str, ...]
    answered_questions: int
    captured_questions: int
    audio_captures: int
    skipped_questions: int
    random_answers: int
    completion_evidence: str

    def __post_init__(self) -> None:
        if not self.test_id.strip() or not self.profile.strip():
            raise ValueError("test and profile are required")
        if not self.planned_sections or len(set(self.planned_sections)) != len(self.planned_sections):
            raise ValueError("unique planned sections are required")
        for value in (self.answered_questions, self.captured_questions,
                      self.audio_captures, self.skipped_questions, self.random_answers):
            if type(value) is not int or value < 0:
                raise ValueError("attempt counters must be non-negative integers")

    @property
    def clean_complete(self) -> bool:
        return (
            set(self.completed_sections) == set(self.planned_sections)
            and len(self.completed_sections) == len(self.planned_sections)
            and self.answered_questions > 0
            and self.captured_questions == self.answered_questions
            and self.skipped_questions == 0
            and self.random_answers == 0
            and bool(self.completion_evidence.strip())
        )


class TrialBatchTracker:
    def __init__(self, path: str | Path, targets: tuple[tuple[str, str], ...]):
        if len(targets) != 15 or len({test_id for test_id, _ in targets}) != 15:
            raise ValueError("exactly 15 unique target tests are required")
        if any(not test_id.strip() or not profile.strip() for test_id, profile in targets):
            raise ValueError("test and profile are required")
        self.path = Path(path)
        self.targets = targets
        self._target_map = dict(targets)
        self._completed: dict[str, AttemptEvidence] = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if tuple(map(tuple, payload["targets"])) != self.targets:
                raise ValueError("batch targets changed")
            for item in payload.get("completed", []):
                item["planned_sections"] = tuple(item["planned_sections"])
                item["completed_sections"] = tuple(item["completed_sections"])
                evidence = AttemptEvidence(**item)
                if not evidence.clean_complete:
                    raise ValueError("stored attempt is not clean and complete")
                self._completed[evidence.test_id] = evidence

    def initialize(self) -> None:
        """Create the durable empty ledger without recording a completion."""
        if not self.path.exists():
            self._save()

    def record(self, evidence: AttemptEvidence) -> None:
        if self._target_map.get(evidence.test_id) != evidence.profile:
            raise ValueError("attempt is outside this batch")
        if not evidence.clean_complete:
            raise ValueError("attempt has gaps, random answers, or incomplete evidence")
        previous = self._completed.get(evidence.test_id)
        if previous is not None and previous != evidence:
            raise ValueError("completed attempt evidence changed")
        self._completed[evidence.test_id] = evidence
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "target_count": 15,
            "targets": [list(target) for target in self.targets],
            "completed": [asdict(self._completed[test_id])
                          for test_id, _ in self.targets if test_id in self._completed],
        }
        fd, temporary = tempfile.mkstemp(prefix=".batch-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def summary(self) -> dict:
        completed = len(self._completed)
        return {
            "target": 15,
            "completed": completed,
            "remaining": 15 - completed,
            "completed_test_ids": tuple(test_id for test_id, _ in self.targets
                                        if test_id in self._completed),
            "next_test_id": next((test_id for test_id, _ in self.targets
                                  if test_id not in self._completed), None),
        }
