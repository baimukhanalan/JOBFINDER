from typing import Protocol
from qa_bot.domain.question import AssetRef


class StagingAudioPort(Protocol):
    async def put_response(self, question_id: str, audio: AssetRef, *,
                           idempotency_key: str, timeout: float) -> str: ...
    async def response_status(self, idempotency_key: str, *, timeout: float) -> str: ...
