from typing import Protocol
from qa_bot.domain.question import AssetRef
from qa_bot.domain.observation import Transcript


class STTPort(Protocol):
    async def transcribe(self, audio: AssetRef, *, timeout: float) -> Transcript: ...


class TTSPort(Protocol):
    async def synthesize(self, text: str, *, language: str,
                         timeout: float) -> AssetRef: ...
