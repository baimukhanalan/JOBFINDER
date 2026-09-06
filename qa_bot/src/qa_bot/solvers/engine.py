"""Approved lookup first; model proposes only for explicitly synthetic unknowns."""
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
    def __init__(self, bank, client, *, historical=None):
        self.bank, self.client, self.historical = bank, client, historical

    async def propose(self, q, *, synthetic=False, timeout=30):
        if not q.completeness or q.unresolved_regions or q.extraction_confidence < 0.8:
            return EngineResult(None, "abstain", "incomplete_question")
        try:
            cached = self.bank.lookup(q)
        except ValueError:
            return EngineResult(None, "abstain", "unverified_media")
        if cached:
            return EngineResult(cached, "approved")
        if self.historical is not None:
            resolution = self.historical.lookup(q)
            if resolution is not None:
                return EngineResult(
                    resolution.proposal,
                    "historical_exact",
                    f"occurrences={resolution.occurrence_count};official_key="
                    f"{str(resolution.official_answer_key).lower()}",
                )
        if not synthetic:
            return EngineResult(None, "abstain", "only_synthetic_model_input_allowed")
        if any(not a.media_type.startswith("audio/") or "stt:" + a.sha256 not in q.provenance
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
                    selections = (Selection(match_number(q, value)),)
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
            return EngineResult(proposal, "candidate")
        except Exception:
            return EngineResult(None, "abstain", "model_or_validation_failed")
