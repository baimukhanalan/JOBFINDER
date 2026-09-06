from typing import Protocol
from dataclasses import dataclass
from qa_bot.domain.question import QuestionSpec
from qa_bot.domain.routing import AdapterKind


@dataclass(frozen=True)
class AdapterResult:
    adapter: AdapterKind
    question_id: str
    content_hash: str
    status: str = "not_implemented"
    answer: None = None


class QuestionAdapter(Protocol):
    kind: AdapterKind

    async def handle(self, question: QuestionSpec) -> AdapterResult: ...
