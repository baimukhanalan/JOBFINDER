from typing import Protocol
from qa_bot.domain.question import QuestionSpec
from qa_bot.domain.answer import AnswerProposal


class LLMPort(Protocol):
    async def propose(self, question: QuestionSpec, *, timeout: float) -> AnswerProposal: ...
