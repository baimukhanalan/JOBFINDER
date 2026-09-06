"""Single-question pipeline, never an autonomous assessment loop."""
from dataclasses import dataclass
import hashlib
import json
from qa_bot.audio.service import attach_transcript


@dataclass(frozen=True)
class PipelineResult:
    status: str
    source: str
    reason: str = ""


class AnswerPipeline:
    def __init__(self, engine, *, speech=None, executor=None, media=None):
        self.engine, self.speech, self.executor, self.media = engine, speech, executor, media

    async def process(self, question, *, synthetic=False, input_audio=None, apply=False):
        if input_audio is not None:
            if self.speech is None:
                return PipelineResult("abstain", "stt", "speech_provider_missing")
            transcript = await self.speech.transcribe(input_audio, synthetic=synthetic)
            question = attach_transcript(question, transcript)
        result = await self.engine.propose(question, synthetic=synthetic)
        if result.proposal is None:
            return PipelineResult("abstain", result.source, result.reason)
        if result.source not in {"approved", "historical_exact"}:
            return PipelineResult("review_required", "candidate")
        if not apply:
            return PipelineResult("ready", result.source, result.reason)
        if not synthetic or self.executor is None:
            return PipelineResult("blocked", result.source, "staging_executor_required")
        receipt = await self.executor.apply(question, result.proposal)
        return PipelineResult(receipt.status, result.source, receipt.reason)

    async def speak_to_mock(self, question, approved_text, *, voice,
                            approval_question_id, approval_content_hash):
        # Explicit review-bound draft; never accepts arbitrary model commands.
        if (approval_question_id != question.question_id or
            approval_content_hash != question.content_hash or
            self.speech is None or self.media is None):
            raise ValueError("current reviewed speech draft and staging services required")
        audio = await self.speech.synthesize(approved_text, voice=voice, synthetic=True)
        return await self.media.put_response(
            question.question_id, audio,
            idempotency_key=hashlib.sha256(json.dumps(
                [question.content_hash, question.question_id, approved_text, voice]).encode()).hexdigest())
