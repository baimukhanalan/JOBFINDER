"""Deterministic routing by catalogue and explicit generic task family."""
from dataclasses import replace
from collections.abc import Mapping
from qa_bot.domain.question import QuestionSpec, ResponseKind
from qa_bot.domain.routing import AdapterKind as A, RouteDecision
from qa_bot.domain.state import RunState, RunPhase
from qa_bot.adapters.questions.stubs import default_adapters
from qa_bot.ports.question_adapter import QuestionAdapter

# Explicit synthetic extension IDs. These are not claimed as observed SHL types.
EXTENSIONS = {
    "GEN-SINGLE": (A.SINGLE_CHOICE, ResponseKind.SINGLE_CHOICE),
    "GEN-MULTIPLE": (A.MULTIPLE_CHOICE, ResponseKind.MULTI_CHOICE),
    "GEN-NUMERICAL": (A.NUMERICAL, ResponseKind.TEXT),
    "GEN-VERBAL": (A.VERBAL, ResponseKind.SINGLE_CHOICE),
    "GEN-LOGICAL": (A.LOGICAL, ResponseKind.SINGLE_CHOICE),
    "GEN-TABLE": (A.TABLE_CHART, ResponseKind.SINGLE_CHOICE),
    "GEN-TEXT": (A.FREE_TEXT, ResponseKind.TEXT),
    "GEN-AUDIO": (A.AUDIO, ResponseKind.TEXT),
    "GEN-SPEAKING": (A.SPEAKING, ResponseKind.AUDIO),
    "GEN-CODING": (A.CODING, ResponseKind.TEXT),
    "GEN-SJT": (A.SJT_PERSONALITY, ResponseKind.MULTI_CHOICE),
}


class QuestionRouter:
    def __init__(self, adapters: Mapping[A, QuestionAdapter] | None = None):
        self.adapters = dict(default_adapters() if adapters is None else adapters)
        for kind, adapter in self.adapters.items():
            if not isinstance(kind, A) or adapter.kind != kind:
                raise ValueError("adapter registration does not match its kind")

    def route(self, spec: QuestionSpec) -> RouteDecision:
        def result(adapter, reason, certainty=1.0):
            return RouteDecision(adapter, min(certainty, spec.extraction_confidence)
                                 if adapter else 0.0, reason,
                                 spec.question_id, spec.content_hash)
        if not spec.completeness or spec.unresolved_regions or spec.extraction_confidence < 0.8:
            return result(None, "incomplete_or_low_confidence")
        if spec.remaining_seconds == 0:
            return result(None, "time_expired")
        type_id = spec.type_id
        expected = None
        section = None
        if type_id in EXTENSIONS:
            adapter, expected = EXTENSIONS[type_id]
        elif type_id in ("ANA-01", "ANA-03"):
            adapter, expected, section = A.NUMERICAL, ResponseKind.SINGLE_CHOICE, "Basic Analytical Ability"
        elif type_id in ("ANA-02", "ANA-04"):
            adapter, expected, section = A.TABLE_CHART, ResponseKind.SINGLE_CHOICE, "Basic Analytical Ability"
        elif type_id == "ANA-05":
            # Catalogue combines analogy, odd-one-out and event ordering.
            # Use explicit task phrases; ambiguous/absent phrases require review.
            text = (spec.instruction + " " + spec.question_text).casefold()
            verbal = any(s in text for s in ("word pair", "analogy", "аналог"))
            logical = any(s in text for s in ("unlike the others", "order would",
                                               "последовательност", "лишнее"))
            if verbal == logical:
                return result(None, "ambiguous_ana05_subtype")
            adapter = A.VERBAL if verbal else A.LOGICAL
            expected, section = ResponseKind.SINGLE_CHOICE, "Basic Analytical Ability"
        elif type_id in {f"COMP-{i:02}" for i in range(1, 17)}:
            adapter, expected, section = A.UI_SIMULATION, ResponseKind.UI_ACTION, "Basic Computer Literacy Simulation"
        elif type_id == "PERS-01":
            adapter, expected, section = A.SJT_PERSONALITY, ResponseKind.SINGLE_CHOICE, "Personality"
        elif type_id == "SALES-01":
            adapter, expected, section = A.SJT_PERSONALITY, ResponseKind.MULTI_CHOICE, "Sales Competency Test"
        elif type_id == "WRITEX-01":
            adapter, expected, section = A.FREE_TEXT, ResponseKind.TEXT, "WriteX"
        elif type_id == "SVAR-01":
            adapter, expected, section = A.SPEAKING, ResponseKind.AUDIO, "SVAR"
        else:
            return result(None, "unsupported_type")
        if spec.response_contract.kind != expected or (section and spec.section != section):
            return result(None, "type_contract_conflict")
        if adapter == A.AUDIO and not any(a.media_type.startswith("audio/") for a in spec.assets):
            return result(None, "missing_input_audio")
        if type_id in ("ANA-02", "GEN-TABLE") and not spec.tables:
            return result(None, "missing_table")
        if type_id in ("ANA-03", "ANA-04") and not any(a.media_type.startswith("image/") for a in spec.assets):
            return result(None, "missing_image")
        if type_id in ("SALES-01", "GEN-SJT") and (
            spec.response_contract.roles != ("best", "worst")
            or (spec.response_contract.min_selections, spec.response_contract.max_selections) != (2, 2)
        ):
            return result(None, "invalid_sjt_roles")
        if adapter not in self.adapters:
            return result(None, "adapter_unavailable")
        return result(adapter, "explicit_type_contract", 0.85 if type_id == "ANA-05" else 1.0)

    def resolve(self, decision: RouteDecision, spec: QuestionSpec) -> QuestionAdapter:
        if decision.question_id != spec.question_id or decision.content_hash != spec.content_hash:
            raise ValueError("stale routing decision")
        current = self.route(spec)
        if decision != current or current.adapter is None:
            raise ValueError("routing decision is blocked or changed")
        return self.adapters[current.adapter]


def route_state(state: RunState, router: QuestionRouter | None = None) -> RunState:
    router = router or QuestionRouter()
    if state.question_spec is None or state.extraction_errors:
        return replace(state, phase=RunPhase.REVIEW_REQUIRED, selected_adapter=None,
                       routing_confidence=0.0, routing_reason="no_valid_spec")
    decision = router.route(state.question_spec)
    return replace(state, phase=RunPhase.ROUTED if decision.adapter else RunPhase.REVIEW_REQUIRED,
                   selected_adapter=decision.adapter, routing_confidence=decision.confidence,
                   routing_reason=decision.reason)
