"""Typed staging actions only; model commands and production origins are not accepted."""
from dataclasses import dataclass
from qa_bot.execution.validator import validate_answer
from qa_bot.domain.question import ResponseKind


@dataclass(frozen=True)
class ExecutionReceipt:
    status: str
    reason: str = ""


class ActionExecutor:
    def __init__(self, port, *, allow_final_submit=False):
        self.port = port
        self.allow_final_submit = allow_final_submit

    async def apply(self, expected, proposal):
        current = await self.port.current()
        errors = validate_answer(current, proposal)
        if errors or current.content_hash != expected.content_hash or current.question_id != expected.question_id:
            return ExecutionReceipt("blocked", ",".join(errors) or "screen_changed")
        if proposal.kind in (ResponseKind.SINGLE_CHOICE, ResponseKind.MULTI_CHOICE):
            if any(s.role for s in proposal.selections):
                return ExecutionReceipt("blocked", "role_controls_not_supported")
            await self.port.select(tuple(s.option_id for s in proposal.selections))
            verified = await self.port.selected()
            good = set(verified) == {s.option_id for s in proposal.selections}
        elif proposal.kind == ResponseKind.TEXT:
            await self.port.fill(proposal.text)
            good = await self.port.value() == proposal.text
        else:
            return ExecutionReceipt("blocked", "unsupported_action_kind")
        after = await self.port.current()
        if after.question_id != current.question_id or after.content_hash != current.content_hash:
            return ExecutionReceipt("unknown", "screen_changed_during_action")
        return ExecutionReceipt("applied" if good else "failed", "" if good else "value_not_confirmed")

    async def navigate(self, expected, direction):
        if direction not in ("next", "back", "submit"):
            return ExecutionReceipt("blocked", "unsupported_navigation")
        if direction == "submit" and not self.allow_final_submit:
            return ExecutionReceipt("blocked", "final_submit_disabled")
        current = await self.port.current()
        if current.question_id != expected.question_id or current.content_hash != expected.content_hash:
            return ExecutionReceipt("blocked", "screen_changed")
        if not any(n.action == direction and n.enabled for n in current.navigation):
            return ExecutionReceipt("blocked", "navigation_unavailable")
        await self.port.navigate(direction)
        after = await self.port.current()
        changed = after.question_id != current.question_id or after.content_hash != current.content_hash
        return ExecutionReceipt("applied" if changed else "unknown",
                                "" if changed else "transition_not_confirmed")
