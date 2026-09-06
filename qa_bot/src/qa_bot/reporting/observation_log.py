"""Metadata-only journal. Never serialize raw RunState with asdict."""
from datetime import datetime, timezone
import json
from pathlib import Path
from qa_bot.domain.state import RunState
from qa_bot.adapters.browser.controller import origin


def state_event(state: RunState) -> dict:
    snapshot = state.observation
    return {
        "event": "screen_observed", "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": state.run_id, "phase": state.phase.value,
        "screen_type": state.screen_type.value, "section": state.current_section,
        "reason": state.detection_reason,
        "origin": origin(snapshot.document_ref) if snapshot else None,
        "observation_id": snapshot.observation_id if snapshot else None,
        "text_length": len(snapshot.dom_text) if snapshot else 0,
        "element_count": len(snapshot.elements) if snapshot else 0,
        "blocked_origins": snapshot.blocked_origins if snapshot else (),
        "shadow_root_count": snapshot.shadow_root_count if snapshot else 0,
        "page_error_count": snapshot.page_error_count if snapshot else 0,
        "failed_request_count": snapshot.failed_request_count if snapshot else 0,
        "title_length": len(snapshot.title) if snapshot else 0,
        "actions_executed": 0,
        "selected_adapter": state.selected_adapter,
        "routing_confidence": state.routing_confidence,
        "routing_reason": state.routing_reason,
        "extraction_errors": state.extraction_errors,
        "extraction_ready": state.question_spec is not None,
        "ocr_region_count": len(state.question_spec.ocr_evidence) if state.question_spec else 0,
        "extraction_confidence": state.question_spec.extraction_confidence if state.question_spec else 0,
    }


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
