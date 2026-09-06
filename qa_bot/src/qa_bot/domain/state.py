"""Run state snapshot; orchestration is intentionally not implemented."""
from dataclasses import dataclass, field
from qa_bot.domain.screen import ScreenType
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.domain.question import QuestionSpec
from enum import StrEnum
from qa_bot.domain.common import nonempty
from qa_bot.domain.common import confidence
from qa_bot.domain.routing import AdapterKind


class RunPhase(StrEnum):
    CREATED = "created"
    DRY_RUN_READY = "dry_run_ready"
    OBSERVING = "observing"
    OBSERVED = "observed"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    ROUTED = "routed"
    SOLVING = "solving"
    VALIDATING = "validating"
    PREPARED = "prepared"
    APPLYING = "applying"
    AWAITING_ACK = "awaiting_ack"
    CONFIRMED = "confirmed"
    RECOVERING = "recovering"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMPLETE = "complete"


@dataclass(frozen=True)
class RunState:
    run_id: str
    phase: RunPhase = RunPhase.CREATED
    mode: str = "dry-run"
    current_question_id: str | None = None
    processed_count: int = 0
    retry_count: int = 0
    last_error: str | None = None
    screen_type: ScreenType = ScreenType.UNKNOWN
    current_section: str | None = None
    detection_reason: str = ""
    observation: BrowserSnapshot | None = field(default=None, repr=False)
    question_spec: QuestionSpec | None = field(default=None, repr=False)
    extraction_errors: tuple[str, ...] = ()
    selected_adapter: AdapterKind | None = None
    routing_confidence: float = 0.0
    routing_reason: str = ""

    def __post_init__(self) -> None:
        nonempty(self.run_id, "run_id")
        confidence(self.routing_confidence)
        if self.selected_adapter is not None and not isinstance(self.selected_adapter, AdapterKind):
            raise ValueError("selected_adapter must be AdapterKind")
        if self.mode != "dry-run":
            raise ValueError("only dry-run is supported by this skeleton")
        if not isinstance(self.phase, RunPhase):
            raise ValueError("phase must be RunPhase")
        if not isinstance(self.screen_type, ScreenType):
            raise ValueError("screen_type must be ScreenType")
        for value in (self.processed_count, self.retry_count):
            if type(value) is not int or value < 0:
                raise ValueError("counters must be non-negative integers")
