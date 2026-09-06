from typing import Protocol
from qa_bot.domain.question import QuestionSpec
from qa_bot.domain.answer import AnswerProposal


class SolverPort(Protocol):
    def supports(self, type_id: str) -> bool: ...
    async def solve(self, question: QuestionSpec, *, timeout: float) -> AnswerProposal: ...
