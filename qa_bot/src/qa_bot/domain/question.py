"""Immutable question content; UI bindings belong to observations."""
from dataclasses import dataclass
from enum import StrEnum

from qa_bot.domain.common import nonempty, confidence
from qa_bot.domain.ocr import OCRResult


class ResponseKind(StrEnum):
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    TEXT = "text"
    AUDIO = "audio"
    UI_ACTION = "ui_action"


@dataclass(frozen=True)
class OptionSpec:
    id: str
    position: int
    label: str = ""
    image_ref: str | None = None

    def __post_init__(self) -> None:
        nonempty(self.id, "option.id")
        if type(self.position) is not int or self.position < 1:
            raise ValueError("position must be a positive integer")
        if not self.label.strip() and not self.image_ref:
            raise ValueError("option requires a label or image reference")


@dataclass(frozen=True)
class AssetRef:
    id: str
    media_type: str
    location: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        for key in ("id", "media_type", "location"):
            nonempty(getattr(self, key), key)


@dataclass(frozen=True)
class TableSpec:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not self.headers or any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("table must have headers and rectangular rows")


@dataclass(frozen=True)
class ResponseContract:
    kind: ResponseKind
    min_selections: int = 0
    max_selections: int = 0
    roles: tuple[str, ...] = ()
    min_words: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResponseKind):
            raise ValueError("kind must be ResponseKind")
        for value in (self.min_selections, self.max_selections, self.min_words):
            if type(value) is not int or value < 0:
                raise ValueError("response limits must be non-negative integers")
        if self.max_selections < self.min_selections:
            raise ValueError("invalid selection range")
        if self.kind == ResponseKind.SINGLE_CHOICE:
            if (self.min_selections, self.max_selections) != (1, 1):
                raise ValueError("single choice requires exactly one selection")
        elif self.kind == ResponseKind.MULTI_CHOICE:
            if self.min_selections < 1:
                raise ValueError("multi choice requires a positive selection range")
        elif self.min_selections or self.max_selections or self.roles:
            raise ValueError("non-choice response cannot have selections")
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("roles must be unique")
        if self.roles and len(self.roles) != self.max_selections:
            raise ValueError("role count must match max selections")
        if self.min_words and self.kind != ResponseKind.TEXT:
            raise ValueError("word limit applies only to text")


@dataclass(frozen=True)
class FieldConstraint:
    name: str
    value: str


@dataclass(frozen=True)
class NavigationButton:
    id: str
    label: str
    action: str
    enabled: bool


@dataclass(frozen=True)
class QuestionSpec:
    question_id: str
    attempt_id: str
    section: str
    type_id: str
    question_text: str
    response_contract: ResponseContract
    content_hash: str
    schema_version: str = "1.0"
    subsection: str | None = None
    question_number: int | None = None
    instruction: str = ""
    options: tuple[OptionSpec, ...] = ()
    context: tuple[str, ...] = ()
    tables: tuple[TableSpec, ...] = ()
    assets: tuple[AssetRef, ...] = ()
    eligibility: str = "MAIN"
    provenance: tuple[str, ...] = ()
    unresolved_regions: tuple[str, ...] = ()
    source_quality: tuple[str, ...] = ()
    interaction_contract: str = "unknown"
    field_constraints: tuple[FieldConstraint, ...] = ()
    remaining_seconds: int | None = None
    navigation: tuple[NavigationButton, ...] = ()
    extraction_confidence: float = 0.0
    completeness: bool = False
    ocr_evidence: tuple[OCRResult, ...] = ()

    def __post_init__(self) -> None:
        for name in ("question_id", "attempt_id", "section", "type_id",
                     "question_text", "content_hash", "schema_version"):
            nonempty(getattr(self, name), name)
        confidence(self.extraction_confidence)
        if self.remaining_seconds is not None:
            if type(self.remaining_seconds) is not int or self.remaining_seconds < 0:
                raise ValueError("remaining_seconds must be non-negative")
        if self.question_number is not None:
            if type(self.question_number) is not int or self.question_number < 1:
                raise ValueError("question_number must be positive")
        if self.eligibility not in ("MAIN", "PRACTICE"):
            raise ValueError("invalid eligibility")
        ids = [option.id for option in self.options]
        positions = [option.position for option in self.options]
        if len(ids) != len(set(ids)) or len(positions) != len(set(positions)):
            raise ValueError("option IDs and positions must be unique")
        if self.response_contract.kind in (
            ResponseKind.SINGLE_CHOICE, ResponseKind.MULTI_CHOICE
        ):
            if len(self.options) < self.response_contract.max_selections:
                raise ValueError("not enough options for response contract")
