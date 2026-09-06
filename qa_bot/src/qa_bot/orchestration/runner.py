"""Strict assessment loop with evidence gates and an append-only audit journal."""
from __future__ import annotations

import inspect
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PageState:
    kind: str
    identity: str
    section: str = ""
    question_number: int | None = None
    payload: object | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"question", "complete", "blocked"}:
            raise ValueError("invalid page state")
        if not self.identity.strip():
            raise ValueError("page identity required")


@dataclass(frozen=True)
class AppliedAnswer:
    question_id: str
    content_hash: str
    answer_fingerprint: str
    accepted: bool
    evidence: str


@dataclass(frozen=True)
class TransitionEvidence:
    advanced: bool
    next_identity: str
    evidence: str


@dataclass(frozen=True)
class RunOutcome:
    status: str
    processed: int
    sections: tuple[str, ...]
    reason: str = ""


class AssessmentDriver(Protocol):
    async def observe(self) -> PageState: ...
    async def extract(self, state: PageState): ...
    async def solve(self, question): ...
    async def validate(self, question, answer) -> tuple[str, ...]: ...
    async def apply(self, question, answer) -> AppliedAnswer: ...
    async def confirm_transition(
        self, before: PageState, receipt: AppliedAnswer
    ) -> TransitionEvidence: ...


class RunJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields) -> None:
        record = {"event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                if self.path.exists():
                    stream.write(self.path.read_bytes())
                stream.write(line.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise


class StrictAssessmentRunner:
    """Process every question exactly once or stop with a recorded reason."""

    def __init__(self, driver: AssessmentDriver, journal: RunJournal, *, max_questions: int = 500):
        if type(max_questions) is not int or not 1 <= max_questions <= 5000:
            raise ValueError("invalid question limit")
        self.driver = driver
        self.journal = journal
        self.max_questions = max_questions

    async def _call(self, name: str, *args):
        result = getattr(self.driver, name)(*args)
        return await result if inspect.isawaitable(result) else result

    def _stop(self, processed: int, sections: list[str], reason: str) -> RunOutcome:
        self.journal.append("stopped", processed=processed, reason=reason)
        return RunOutcome("stopped", processed, tuple(sections), reason)

    async def run(self) -> RunOutcome:
        processed = 0
        sections: list[str] = []
        seen: set[str] = set()
        self.journal.append("started", max_questions=self.max_questions)
        while processed < self.max_questions:
            state = await self._call("observe")
            self.journal.append("observed", **asdict(state))
            if state.kind == "complete":
                self.journal.append("completed", processed=processed, sections=sections)
                return RunOutcome("complete", processed, tuple(sections))
            if state.kind == "blocked":
                return self._stop(processed, sections, state.reason or "page_blocked")
            if state.identity in seen:
                return self._stop(processed, sections, "question_repeated_without_transition")
            if not state.section.strip() or state.question_number is None:
                return self._stop(processed, sections, "question_position_missing")
            question = await self._call("extract", state)
            if question is None or not getattr(question, "completeness", False):
                return self._stop(processed, sections, "question_incomplete")
            if getattr(question, "unresolved_regions", ()):
                return self._stop(processed, sections, "question_has_unresolved_regions")
            if getattr(question, "extraction_confidence", 0.0) < 0.8:
                return self._stop(processed, sections, "question_confidence_below_threshold")
            answer = await self._call("solve", question)
            if answer is None:
                return self._stop(processed, sections, "answer_unavailable")
            errors = tuple(await self._call("validate", question, answer))
            if errors:
                return self._stop(processed, sections, "answer_invalid:" + ",".join(errors))
            receipt = await self._call("apply", question, answer)
            if (not receipt.accepted or receipt.question_id != question.question_id
                    or receipt.content_hash != question.content_hash or not receipt.evidence.strip()):
                return self._stop(processed, sections, "answer_not_acknowledged")
            transition = await self._call("confirm_transition", state, receipt)
            if (not transition.advanced or transition.next_identity == state.identity
                    or not transition.evidence.strip()):
                return self._stop(processed, sections, "transition_not_confirmed")
            seen.add(state.identity)
            processed += 1
            if state.section not in sections:
                sections.append(state.section)
            self.journal.append(
                "question_confirmed", identity=state.identity, section=state.section,
                question_number=state.question_number, question_id=receipt.question_id,
                content_hash=receipt.content_hash,
                answer_fingerprint=receipt.answer_fingerprint,
                apply_evidence=receipt.evidence, transition_evidence=transition.evidence,
                next_identity=transition.next_identity,
            )
        return self._stop(processed, sections, "question_limit_reached")
