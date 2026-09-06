"""One inert adapter instance per kind; no solver logic or I/O."""
from dataclasses import dataclass
from types import MappingProxyType
from qa_bot.domain.question import QuestionSpec
from qa_bot.domain.routing import AdapterKind
from qa_bot.ports.question_adapter import AdapterResult


@dataclass(frozen=True)
class StubQuestionAdapter:
    kind: AdapterKind

    async def handle(self, question: QuestionSpec) -> AdapterResult:
        return AdapterResult(self.kind, question.question_id, question.content_hash)


def default_adapters():
    return MappingProxyType({kind: StubQuestionAdapter(kind) for kind in AdapterKind})
