from dataclasses import dataclass
from enum import StrEnum


class ScreenType(StrEnum):
    START = "start"
    INSTRUCTION = "instruction"
    QUESTION = "question"
    SECTION_TRANSITION = "section_transition"
    REVIEW = "review"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ScreenDetection:
    screen_type: ScreenType = ScreenType.UNKNOWN
    section: str | None = None
    reason: str = "no_matching_rule"
