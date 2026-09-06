"""Minimal observation contracts; extraction is not implemented."""
from dataclasses import dataclass, field
from qa_bot.domain.question import AssetRef


@dataclass(frozen=True)
class PageElement:
    ref: str
    tag: str
    role: str = ""
    input_type: str = ""
    name: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class BrowserSnapshot:
    observation_id: str
    document_ref: str = field(repr=False)
    dom_text: str = field(default="", repr=False)
    assets: tuple[AssetRef, ...] = ()
    title: str = field(default="", repr=False)
    headings: tuple[str, ...] = field(default=(), repr=False)
    elements: tuple[PageElement, ...] = field(default=(), repr=False)
    incomplete: bool = False
    question_html: str = field(default="", repr=False)
    blocked_origins: tuple[str, ...] = ()
    shadow_root_count: int = 0
    page_error_count: int = 0
    failed_request_count: int = 0


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None
    audio_ref: str | None = None
