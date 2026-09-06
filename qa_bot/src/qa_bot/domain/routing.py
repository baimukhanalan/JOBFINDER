"""Routing is classification, not solving or permission to execute."""
from dataclasses import dataclass
from enum import StrEnum
from qa_bot.domain.common import confidence


class AdapterKind(StrEnum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    NUMERICAL = "numerical"
    VERBAL = "verbal"
    LOGICAL = "logical"
    TABLE_CHART = "table_chart"
    FREE_TEXT = "free_text"
    AUDIO = "audio"
    SPEAKING = "speaking"
    CODING = "coding"
    SJT_PERSONALITY = "sjt_personality"
    UI_SIMULATION = "ui_simulation"


@dataclass(frozen=True)
class RouteDecision:
    adapter: AdapterKind | None
    confidence: float
    reason: str
    question_id: str
    content_hash: str

    def __post_init__(self):
        confidence(self.confidence)
