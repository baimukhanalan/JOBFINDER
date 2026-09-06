"""Cross-model validation. Never treats model confidence as proof of correctness."""
from qa_bot.domain.question import QuestionSpec, ResponseKind
from qa_bot.domain.answer import AnswerProposal


def validate_answer(q: QuestionSpec, a: AnswerProposal, *, min_confidence=0.9,
                    observation_id=None, expected_observation_id=None) -> tuple[str, ...]:
    errors = []
    if not q.completeness or q.unresolved_regions or q.extraction_confidence < 0.8:
        errors.append("incomplete_question")
    if q.remaining_seconds == 0:
        errors.append("time_expired")
    if a.question_id != q.question_id or a.content_hash != q.content_hash:
        errors.append("stale_answer")
    if observation_id != expected_observation_id:
        errors.append("screen_changed")
    if a.kind != q.response_contract.kind:
        errors.append("response_kind")
    if a.confidence < min_confidence:
        errors.append("low_confidence")
    c = q.response_contract
    if a.kind in (ResponseKind.SINGLE_CHOICE, ResponseKind.MULTI_CHOICE):
        if not c.min_selections <= len(a.selections) <= c.max_selections:
            errors.append("selection_count")
        if any(s.option_id not in {o.id for o in q.options} for s in a.selections):
            errors.append("unknown_option")
        if sorted(s.role or "" for s in a.selections) != sorted(c.roles or ("",) * len(a.selections)):
            errors.append("selection_roles")
    if a.kind == ResponseKind.TEXT:
        text = a.text or ""
        if len(text.split()) < c.min_words:
            errors.append("too_few_words")
        for limit in q.field_constraints:
            if limit.name in ("minlength", "maxlength"):
                try:
                    bound = int(limit.value)
                    if (limit.name == "minlength" and len(text) < bound or
                        limit.name == "maxlength" and len(text) > bound):
                        errors.append(limit.name)
                except ValueError:
                    errors.append("invalid_limit")
            elif limit.name in ("pattern", "min", "max", "step"):
                errors.append("unsupported_field_constraint")
    if a.kind == ResponseKind.UI_ACTION:
        trusted_history = (
            bool(a.plan_id and a.plan_id.startswith("historical:"))
            and "exact_historical_match" in a.evidence
            and any(item.startswith("source:") for item in a.evidence)
        )
        if not trusted_history:
            errors.append("ui_plans_not_accepted_from_model")
    return tuple(errors)
