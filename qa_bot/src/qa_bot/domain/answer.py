"""AnswerProposal is the architecture's AnswerCandidate contract."""
from dataclasses import dataclass
from qa_bot.domain.common import confidence, nonempty
from qa_bot.domain.question import ResponseKind


@dataclass(frozen=True)
class Selection:
    option_id: str
    role: str | None = None

    def __post_init__(self) -> None:
        nonempty(self.option_id, "option_id")


@dataclass(frozen=True)
class AnswerProposal:
    question_id: str
    content_hash: str
    kind: ResponseKind
    confidence: float
    selections: tuple[Selection, ...] = ()
    text: str | None = None
    audio_ref: str | None = None
    plan_id: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        nonempty(self.question_id, "question_id")
        nonempty(self.content_hash, "content_hash")
        confidence(self.confidence)
        if not isinstance(self.kind, ResponseKind):
            raise ValueError("kind must be ResponseKind")
        payloads = (bool(self.selections), self.text is not None,
                    self.audio_ref is not None, self.plan_id is not None)
        if sum(payloads) != 1:
            raise ValueError("exactly one payload must be supplied")
        if self.kind in (ResponseKind.SINGLE_CHOICE, ResponseKind.MULTI_CHOICE):
            if not self.selections:
                raise ValueError("choice response needs selections")
            ids = [item.option_id for item in self.selections]
            if len(ids) != len(set(ids)):
                raise ValueError("selected option IDs must be distinct")
            if self.kind == ResponseKind.SINGLE_CHOICE and len(ids) != 1:
                raise ValueError("single choice requires exactly one selection")
            roles = [item.role for item in self.selections if item.role is not None]
            if len(roles) != len(set(roles)):
                raise ValueError("selection roles must be distinct")
        else:
            name = {ResponseKind.TEXT: "text", ResponseKind.AUDIO: "audio_ref",
                    ResponseKind.UI_ACTION: "plan_id"}[self.kind]
            nonempty(getattr(self, name), name)
