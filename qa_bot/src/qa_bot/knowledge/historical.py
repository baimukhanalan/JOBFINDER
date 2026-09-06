"""Resolve exact historical questions without treating similarity as an answer."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from qa_bot.domain.answer import AnswerProposal, Selection
from qa_bot.domain.question import ResponseKind
from qa_bot.knowledge.bank import normalize


def _key(section: str, instruction: str, text: str, options) -> tuple:
    return (normalize(section), normalize(instruction), normalize(text),
            tuple(normalize(value) for value in options))


@dataclass(frozen=True)
class HistoricalResolution:
    proposal: AnswerProposal
    occurrence_count: int
    source_ids: tuple[str, ...]
    confidence_label: str
    official_answer_key: bool
    action_plan: str | None = None


@dataclass(frozen=True)
class _Entry:
    answer: str
    source_ids: tuple[str, ...]
    confidence_label: str
    official_answer_key: bool
    raw_answer: str = ""


class HistoricalAnswerResolver:
    """Exact corpus lookup with conflict detection and option binding."""

    def __init__(self, entries: dict[tuple, _Entry]):
        self.entries = dict(entries)

    @classmethod
    def from_files(cls, questions_jsonl: str | Path, answers_csv: str | Path):
        with Path(answers_csv).open(encoding="utf-8-sig", newline="") as stream:
            recommendations = {row["id"]: row for row in csv.DictReader(stream)}
        grouped = defaultdict(list)
        seen_ids = set()
        with Path(questions_jsonl).open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                identity = record["id"]
                if identity in seen_ids:
                    raise ValueError("duplicate historical question id")
                seen_ids.add(identity)
                answer = recommendations.get(identity)
                if answer is None or not answer.get("recommended_answer", "").strip():
                    continue
                options = record.get("options") or ()
                if any(not isinstance(option, str) for option in options):
                    # Image-only choices need byte hashes and a separate media binding.
                    continue
                key = _key(record.get("section", ""), record.get("prompt", ""),
                           record.get("text", ""), options)
                grouped[key].append((identity, normalize(answer["recommended_answer"]),
                                     answer.get("confidence", ""),
                                     answer.get("official_answer_key", "").strip().lower()
                                     in {"1", "true", "yes"},answer["recommended_answer"]))
        entries = {}
        for key, values in grouped.items():
            answers = {value[1] for value in values}
            if len(answers) != 1:
                raise ValueError("conflicting answers for an exact historical question")
            entries[key] = _Entry(
                answer=values[0][1],
                source_ids=tuple(value[0] for value in values),
                confidence_label=values[0][2],
                official_answer_key=all(value[3] for value in values),
                raw_answer=values[0][4],
            )
        return cls(entries)

    def lookup(self, question) -> HistoricalResolution | None:
        key = _key(question.section, question.instruction, question.question_text,
                   (option.label for option in question.options))
        entry = self.entries.get(key)
        if entry is None:
            return None
        kind = question.response_contract.kind
        evidence = ("exact_historical_match",) + tuple(
            "source:" + identity for identity in entry.source_ids
        )
        if kind == ResponseKind.SINGLE_CHOICE:
            matches = [option for option in question.options
                       if normalize(option.label) == entry.answer]
            if len(matches) != 1:
                return None
            proposal = AnswerProposal(
                question.question_id, question.content_hash, kind, 1.0,
                selections=(Selection(matches[0].id),), evidence=evidence,
            )
        elif kind == ResponseKind.MULTI_CHOICE:
            markers = "|".join(re.escape(normalize(role))
                               for role in question.response_contract.roles)
            pattern = re.compile(
                rf"(?:^|\s)({markers}):\s*(.*?)(?=\s+(?:{markers}):|$)",
                re.IGNORECASE,
            )
            parts = {normalize(match.group(1)).casefold(): normalize(match.group(2))
                     for match in pattern.finditer(entry.answer)}
            selections = []
            for role in question.response_contract.roles:
                expected = parts.get(normalize(role).casefold())
                matches = [option for option in question.options
                           if expected is not None and normalize(option.label) == expected]
                if len(matches) != 1:
                    return None
                selections.append(Selection(matches[0].id, role=role))
            proposal = AnswerProposal(
                question.question_id, question.content_hash, kind, 1.0,
                selections=tuple(selections), evidence=evidence,
            )
        elif kind == ResponseKind.TEXT:
            proposal = AnswerProposal(
                question.question_id, question.content_hash, kind, 1.0,
                text=entry.raw_answer or entry.answer, evidence=evidence,
            )
        elif kind == ResponseKind.UI_ACTION:
            plan_id = "historical:" + hashlib.sha256(entry.answer.encode()).hexdigest()
            proposal = AnswerProposal(
                question.question_id, question.content_hash, kind, 1.0,
                plan_id=plan_id, evidence=evidence,
            )
        else:
            return None
        return HistoricalResolution(
            proposal=proposal,
            occurrence_count=len(entry.source_ids),
            source_ids=entry.source_ids,
            confidence_label=entry.confidence_label,
            official_answer_key=entry.official_answer_key,
            action_plan=entry.answer if kind == ResponseKind.UI_ACTION else None,
        )

    def stats(self) -> dict[str, int]:
        return {
            "exact_groups": len(self.entries),
            "repeated_groups": sum(len(entry.source_ids) > 1 for entry in self.entries.values()),
            "source_occurrences": sum(len(entry.source_ids) for entry in self.entries.values()),
        }
