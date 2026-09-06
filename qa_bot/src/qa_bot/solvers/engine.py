"""Approved lookup first; model proposals require explicit QA authorization."""
import asyncio
from dataclasses import dataclass, asdict
from qa_bot.domain.answer import AnswerProposal, Selection
from qa_bot.domain.question import ResponseKind
from qa_bot.execution.validator import validate_answer
from qa_bot.solvers.calculator import calculate, match_number


@dataclass(frozen=True)
class EngineResult:
    proposal: AnswerProposal | None
    source: str
    reason: str = ""


class AnswerEngine:
    def __init__(self, bank, client, *, historical=None, archive=None):
        self.bank, self.client, self.historical = bank, client, historical
        self.archive=archive

    async def propose(self, q, *, synthetic=False, authorized_qa=False, timeout=30, allow_model=True):
        kwargs=dict(synthetic=synthetic,authorized_qa=authorized_qa,timeout=timeout,allow_model=allow_model)
        if self.archive and authorized_qa:
            try:
                async with self.archive.solution_lock(q,timeout=timeout+5):
                    return await self._propose(q,**kwargs)
            except TimeoutError:
                return EngineResult(None,'abstain','shared_answer_preparation_busy')
        return await self._propose(q,**kwargs)

    async def _propose(self, q, *, synthetic=False, authorized_qa=False, timeout=30, allow_model=True):
        if not q.completeness or q.unresolved_regions or q.extraction_confidence < 0.8:
            return EngineResult(None, "abstain", "incomplete_question")
        archive=self.archive if authorized_qa else None
        if archive:archive.observe(q)
        try:
            cached = self.bank.lookup(q)
        except ValueError:
            return EngineResult(None, "abstain", "unverified_media")
        if cached:
            if archive:archive.save(q,cached,'approved')
            return EngineResult(cached, "approved")
        if self.historical is not None:
            resolution = self.historical.lookup(q)
            if resolution is not None:
                if archive:archive.save(q,resolution.proposal,'historical_exact')
                return EngineResult(
                    resolution.proposal,
                    "historical_exact",
                    f"occurrences={resolution.occurrence_count};official_key="
                    f"{str(resolution.official_answer_key).lower()}",
                )
        if archive:
            previous=archive.lookup(q)
            if previous:return EngineResult(previous,'previous_exact','official_key=false')
        if not allow_model:return EngineResult(None,'abstain','unknown_question_in_replay_mode')
        if not synthetic and not authorized_qa:
            return EngineResult(None, "abstain", "only_synthetic_model_input_allowed")
        if any(not (a.media_type.startswith("audio/") and "stt:" + str(a.sha256) in q.provenance)
               and not (a.media_type in ("image/png","image/jpeg") and a.sha256 and getattr(self.client,'supports_images',False))
               for a in q.assets):
            return EngineResult(None, "abstain", "multimodal_transport_not_configured")
        try:
            async with asyncio.timeout(timeout):
                payload = {"question_id": q.question_id, "content_hash": q.content_hash,
                           "section": q.section, "type_id": q.type_id,
                           "instruction": q.instruction, "question_text": q.question_text,
                           "options": [asdict(o) for o in q.options],
                           "tables": [asdict(t) for t in q.tables], "context": q.context,
                           "response_contract": asdict(q.response_contract)}
                images=[asdict(a) for a in q.assets if a.media_type.startswith('image/')]
                if images:payload['_image_attachments']=images
                data = await self.client.complete(payload, timeout=timeout)
            expected = {"question_id", "content_hash", "status", "kind", "confidence",
                        "selections", "text", "calculation"}
            if not isinstance(data, dict) or set(data) != expected:
                raise ValueError("invalid_model_schema")
            if data["question_id"] != q.question_id or data["content_hash"] != q.content_hash:
                raise ValueError("stale_model_answer")
            if data["status"] == "abstain":
                return EngineResult(None, "abstain", "model_abstained")
            if data["status"] != "answer":
                raise ValueError("invalid_status")
            kind = ResponseKind(data["kind"])
            selections = tuple(Selection(**s) for s in data["selections"])
            text = data["text"]
            if data["calculation"] is not None:
                value = calculate(data["calculation"])
                if kind == ResponseKind.SINGLE_CHOICE:
                    try:
                        selections = (Selection(match_number(q, value)),)
                    except ValueError as error:
                        # A comparison or a typeset fraction can have nonnumeric
                        # option labels. Retain the model's explicit selection;
                        # arithmetic alone does not verify that label mapping.
                        if str(error) != 'numeric option format unsupported':
                            raise
                    text = None
                elif kind == ResponseKind.TEXT:
                    text = str(value)
                else:
                    raise ValueError("invalid_numeric_response")
            proposal = AnswerProposal(q.question_id, q.content_hash, kind, data["confidence"],
                                      selections=selections, text=text)
            errors = validate_answer(q, proposal)
            if errors:
                return EngineResult(None, "abstain", ",".join(errors))
            self.bank.save_candidate(q, proposal)
            if archive:archive.save(q,proposal,'candidate')
            return EngineResult(proposal, "candidate")
        except Exception as error:
            return EngineResult(None, "abstain", "model_or_validation_failed:"+type(error).__name__+":"+str(error)[:160])
