from typing import Protocol
from qa_bot.domain.ocr import OCRRequest, OCRResult
from qa_bot.domain.observation import BrowserSnapshot
from collections.abc import Mapping


class OCRProvider(Protocol):
    async def recognize(self, request: OCRRequest, *, timeout: float) -> OCRResult: ...


# Import compatibility; the unused old string-result contract is replaced.
OCRPort = OCRProvider


class OCRInputSource(Protocol):
    """Future screenshot/fixture source; fetching is separate from recognition."""
    async def prepare(self, snapshot: BrowserSnapshot, *, timeout: float) -> Mapping[str, OCRRequest]: ...
