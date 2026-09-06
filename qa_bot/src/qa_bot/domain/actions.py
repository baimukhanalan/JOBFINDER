"""Plans and receipts are data only; no action execution is implemented."""
from dataclasses import dataclass
from enum import StrEnum
from qa_bot.domain.common import nonempty


class ActionKind(StrEnum):
    SELECT = "select"
    FILL = "fill"
    CLICK = "click"
    ATTACH = "attach"
    SUBMIT = "submit"
    NEXT = "next"


class ActionStatus(StrEnum):
    NOT_EXECUTED = "not_executed"
    APPLIED = "applied"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Action:
    action_id: str
    kind: ActionKind
    target_ref: str
    value: str | None = None

    def __post_init__(self) -> None:
        nonempty(self.action_id, "action_id")
        nonempty(self.target_ref, "target_ref")
        if not isinstance(self.kind, ActionKind):
            raise ValueError("kind must be ActionKind")


@dataclass(frozen=True)
class ActionPlan:
    plan_id: str
    question_id: str
    content_hash: str
    observation_id: str
    actions: tuple[Action, ...] = ()
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key in ("plan_id", "question_id", "content_hash", "observation_id"):
            nonempty(getattr(self, key), key)
        ids = [action.action_id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("action IDs must be unique")


@dataclass(frozen=True)
class ActionResult:
    plan_id: str
    status: ActionStatus = ActionStatus.NOT_EXECUTED
    executed_action_ids: tuple[str, ...] = ()
    submission_confirmed: bool = False
    transition_confirmed: bool = False
    correctness: bool | None = None
    evidence: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        nonempty(self.plan_id, "plan_id")
        if not isinstance(self.status, ActionStatus):
            raise ValueError("status must be ActionStatus")
        if self.status == ActionStatus.NOT_EXECUTED and (
            self.executed_action_ids or self.submission_confirmed
            or self.transition_confirmed
        ):
            raise ValueError("unexecuted result cannot claim actions or confirmation")
