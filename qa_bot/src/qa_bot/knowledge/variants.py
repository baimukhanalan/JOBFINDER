"""Exact sequence recognition and fail-closed answer prefetching.

The recognizer identifies a *question sequence*, rather than a profile/test ID.
Several profiles can receive the same sequence and therefore belong to the same
variant.  Recognition never uses fuzzy text matching or a majority guess.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import inspect
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from qa_bot.knowledge.bank import normalize


def section_family(section: str) -> str:
    """Map known UI labels to stable section families using explicit tokens."""
    value = normalize(section).casefold()
    if "read and speak" in value:
        return "read_and_speak"
    if "spoken english" in value or value == "svar":
        return "spoken_english"
    if "personality" in value:
        return "personality"
    if "analytical" in value:
        return "analytical"
    if "computer literacy" in value:
        return "computer_literacy"
    if "email writing" in value or "writex" in value:
        return "email_writing"
    if "sales competency" in value:
        return "sales_competency"
    return value


def _normalized_option(option: Any) -> str:
    if isinstance(option, str):
        return normalize(option)
    if isinstance(option, Mapping):
        return json.dumps(option, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    label = getattr(option, "label", None)
    if isinstance(label, str):
        return normalize(label)
    raise ValueError("unsupported option representation")


def _signature(text: str, options: Iterable[Any]) -> tuple[str, tuple[str, ...]]:
    return normalize(text), tuple(_normalized_option(option) for option in options)


def _speech_mode(section: str, prompt: str) -> str | None:
    family = section_family(section)
    instruction = normalize(prompt).casefold()
    if family == "read_and_speak" or (
        "read" in instruction and "out loud" in instruction
    ):
        return "read_aloud"
    if "listen" in instruction and "repeat" in instruction:
        return "listen_repeat"
    if family == "spoken_english" and (
        "speak" in instruction or "answer" in instruction
    ):
        return "spoken_answer"
    return None


@dataclass(frozen=True)
class VariantQuestion:
    question_id: str
    section: str
    position: int
    text: str
    options: tuple[str, ...]
    answer_text: str | None
    answer_conflict: bool
    speech_mode: str | None

    @property
    def signature(self) -> tuple[str, tuple[str, ...]]:
        return _signature(self.text, self.options)


@dataclass(frozen=True)
class QuestionVariant:
    variant_id: str
    section_family: str
    source_test_ids: tuple[str, ...]
    questions: tuple[VariantQuestion, ...]


@dataclass(frozen=True)
class VariantRecognition:
    status: str
    observed_count: int
    section_family: str
    candidate_variant_ids: tuple[str, ...]
    variant: QuestionVariant | None = None

    @property
    def recognized(self) -> bool:
        return self.status == "recognized" and self.variant is not None


@dataclass(frozen=True)
class AudioPrefetchTask:
    variant_id: str
    question_id: str
    position: int
    question_text: str
    answer_text: str
    speech_mode: str
    strategy: str
    replay: Any = None


@dataclass(frozen=True)
class PrefixMetric:
    prefix_size: int
    variant_groups: int
    resolved_variant_groups: int
    source_assignments: int
    resolved_source_assignments: int

    @property
    def variant_coverage(self) -> float:
        return self.resolved_variant_groups / self.variant_groups

    @property
    def assignment_coverage(self) -> float:
        return self.resolved_source_assignments / self.source_assignments


class ExactVariantRecognizer:
    """Recognize a known sequence from its exact observed prefix."""

    def __init__(self, variants: Sequence[QuestionVariant], *, minimum_observations: int = 2):
        if minimum_observations < 1:
            raise ValueError("minimum observations must be positive")
        self.variants = tuple(variants)
        self.minimum_observations = minimum_observations
        grouped: dict[str, list[QuestionVariant]] = defaultdict(list)
        for variant in self.variants:
            grouped[variant.section_family].append(variant)
        self._by_section = {
            section: tuple(sorted(values, key=lambda item: item.variant_id))
            for section, values in grouped.items()
        }

    @classmethod
    def from_files(
        cls,
        questions_jsonl: str | Path,
        answers_csv: str | Path,
        *,
        include_practice: bool = False,
    ) -> "ExactVariantRecognizer":
        with Path(answers_csv).open(encoding="utf-8-sig", newline="") as stream:
            answer_rows = list(csv.DictReader(stream))
        answers: dict[str, str] = {}
        for row in answer_rows:
            identity = row.get("id", "")
            answer = row.get("recommended_answer", "").strip()
            if identity in answers and answers[identity] != answer:
                raise ValueError("conflicting duplicate answer id")
            answers[identity] = answer

        sequences: dict[tuple[str, str], list[dict]] = defaultdict(list)
        seen_ids: set[str] = set()
        with Path(questions_jsonl).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                identity = record["id"]
                if identity in seen_ids:
                    raise ValueError("duplicate question id")
                seen_ids.add(identity)
                if record.get("practice") and not include_practice:
                    continue
                family = section_family(record.get("section", ""))
                sequences[(family, record["test_id"])].append(record)

        # Full identical sequences are one variant, even when several profiles
        # supplied them.  This avoids pretending that a profile ID is observable.
        grouped_sequences: dict[tuple[str, tuple], list[tuple[str, list[dict]]]] = defaultdict(list)
        for (family, test_id), records in sequences.items():
            sequence_key = tuple(
                _signature(record.get("text", ""), record.get("options") or ())
                for record in records
            )
            grouped_sequences[(family, sequence_key)].append((test_id, records))

        variants = []
        for (family, sequence_key), sources in grouped_sequences.items():
            canonical = json.dumps(
                {"version": 1, "section_family": family, "questions": sequence_key},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            variant_id = f"{family}:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"
            questions = []
            for position in range(len(sequence_key)):
                aligned = [records[position] for _, records in sources]
                answer_candidates = set()
                for record in aligned:
                    mode = _speech_mode(record.get("section", ""), record.get("prompt", ""))
                    if mode == "read_aloud":
                        candidate = normalize(record.get("text", ""))
                    else:
                        candidate = answers.get(record["id"], "").strip()
                    if candidate:
                        answer_candidates.add(normalize(candidate))
                conflict = len(answer_candidates) > 1
                answer = next(iter(answer_candidates)) if len(answer_candidates) == 1 else None
                first = aligned[0]
                questions.append(VariantQuestion(
                    question_id=first["id"],
                    section=first.get("section", ""),
                    position=position + 1,
                    text=normalize(first.get("text", "")),
                    options=tuple(_normalized_option(value) for value in first.get("options") or ()),
                    answer_text=answer,
                    answer_conflict=conflict,
                    speech_mode=_speech_mode(
                        first.get("section", ""), first.get("prompt", "")
                    ),
                ))
            variants.append(QuestionVariant(
                variant_id=variant_id,
                section_family=family,
                source_test_ids=tuple(sorted(test_id for test_id, _ in sources)),
                questions=tuple(questions),
            ))
        return cls(variants)

    @staticmethod
    def _observed_signature(observation: Any) -> tuple[str, tuple[str, ...]]:
        if isinstance(observation, str):
            return _signature(observation, ())
        if isinstance(observation, Mapping):
            text = observation.get("question_text", observation.get("question", observation.get("text", "")))
            return _signature(text, observation.get("options") or ())
        text = getattr(observation, "question_text", getattr(observation, "text", ""))
        options = getattr(observation, "options", ())
        labels = [getattr(option, "label", option) for option in options]
        return _signature(text, labels)

    @staticmethod
    def _observation_section(observation: Any) -> str:
        if isinstance(observation, Mapping):
            return str(observation.get("section", ""))
        return str(getattr(observation, "section", ""))

    def identify(
        self,
        observations: Sequence[Any],
        *,
        section: str | None = None,
    ) -> VariantRecognition:
        if not observations:
            family = section_family(section or "")
            return VariantRecognition("insufficient", 0, family, ())
        declared = section_family(section or self._observation_section(observations[0]))
        if not declared:
            return VariantRecognition("unknown", len(observations), "", ())
        for observation in observations:
            observed_section = self._observation_section(observation)
            if observed_section and section_family(observed_section) != declared:
                return VariantRecognition("unknown", len(observations), declared, ())
        prefix = tuple(self._observed_signature(item) for item in observations)
        candidates = tuple(
            variant for variant in self._by_section.get(declared, ())
            if tuple(question.signature for question in variant.questions[:len(prefix)]) == prefix
        )
        candidate_ids = tuple(variant.variant_id for variant in candidates)
        if len(candidates) == 1:
            # One known branch is not enough evidence that an unseen branch
            # cannot share its first question.  Wait for a second observation,
            # except when the observed prefix already covers the entire variant.
            required = min(self.minimum_observations, len(candidates[0].questions))
            if len(prefix) < required:
                return VariantRecognition(
                    "tentative", len(prefix), declared, candidate_ids
                )
            return VariantRecognition(
                "recognized", len(prefix), declared, candidate_ids, candidates[0]
            )
        if candidates:
            return VariantRecognition("ambiguous", len(prefix), declared, candidate_ids)
        return VariantRecognition("unknown", len(prefix), declared, ())

    def plan_audio_prefetch(
        self,
        recognition: VariantRecognition,
        *,
        lookahead: int = 4,
        replay_lookup: Callable[[str, str], Any] | None = None,
    ) -> tuple[AudioPrefetchTask, ...]:
        if lookahead < 1:
            raise ValueError("lookahead must be positive")
        if not recognition.recognized:
            return ()
        upcoming = recognition.variant.questions[
            recognition.observed_count:recognition.observed_count + lookahead
        ]
        tasks = []
        for question in upcoming:
            if question.speech_mode is None:
                continue
            # A missing/conflicting answer invalidates the whole speculative plan.
            if question.answer_conflict or not question.answer_text:
                return ()
            replay = None
            if replay_lookup is not None:
                replay = replay_lookup(question.text, question.answer_text)
            tasks.append(AudioPrefetchTask(
                recognition.variant.variant_id,
                question.question_id,
                question.position,
                question.text,
                question.answer_text,
                question.speech_mode,
                "exact_replay" if replay is not None else "local_tts",
                replay,
            ))
        return tuple(tasks)

    def prefix_metrics(self, prefix_sizes: Sequence[int] = (1, 2, 3)) -> tuple[PrefixMetric, ...]:
        metrics = []
        for size in prefix_sizes:
            if size < 1:
                raise ValueError("prefix sizes must be positive")
            total_groups = resolved_groups = total_assignments = resolved_assignments = 0
            for variants in self._by_section.values():
                prefixes: dict[tuple, list[QuestionVariant]] = defaultdict(list)
                for variant in variants:
                    prefix = tuple(question.signature for question in variant.questions[:size])
                    prefixes[prefix].append(variant)
                    total_groups += 1
                    total_assignments += len(variant.source_test_ids)
                for candidates in prefixes.values():
                    if len(candidates) == 1:
                        resolved_groups += 1
                        resolved_assignments += len(candidates[0].source_test_ids)
            metrics.append(PrefixMetric(
                size, total_groups, resolved_groups,
                total_assignments, resolved_assignments,
            ))
        return tuple(metrics)


async def materialize_audio_prefetch(
    tasks: Sequence[AudioPrefetchTask],
    prepare: Callable[[AudioPrefetchTask], Any],
    *,
    concurrency: int = 3,
) -> tuple[Any, ...]:
    """Prepare independent audio items concurrently while preserving order."""
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(task: AudioPrefetchTask):
        async with semaphore:
            result = prepare(task)
            return await result if inspect.isawaitable(result) else result

    return tuple(await asyncio.gather(*(one(task) for task in tasks)))
