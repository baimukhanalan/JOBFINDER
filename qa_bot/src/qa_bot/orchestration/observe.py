"""Observe once, update RunState, and always close the browser."""
from dataclasses import replace
from qa_bot.adapters.browser.controller import BrowserController
from qa_bot.domain.state import RunState, RunPhase
from qa_bot.perception.detector import ScreenDetector
from qa_bot.ports.ocr import OCRProvider
from qa_bot.domain.ocr import OCRRequest
from collections.abc import Mapping


async def observe_once(url: str, allowed_origins: tuple[str, ...], state: RunState,
                       *, timeout: float = 30, extract: bool = False, route: bool = False,
                       ocr_provider: OCRProvider | None = None,
                       ocr_requests: Mapping[str, OCRRequest] | None = None,
                       wait_for_content: bool = False, screenshot_path=None) -> RunState:
    browser = BrowserController(allowed_origins)
    try:
        await browser.open(url, timeout=timeout)
        if wait_for_content:
            await browser.wait_for_content(timeout=min(timeout, 10), min_characters=80)
        snapshot = await browser.observe(timeout=timeout)
        if screenshot_path is not None:
            await browser.capture(screenshot_path)
        detection = ScreenDetector().detect(snapshot)
        updated = replace(state, phase=RunPhase.OBSERVED, observation=snapshot,
                       screen_type=detection.screen_type, current_section=detection.section,
                       detection_reason=detection.reason, question_spec=None,
                       extraction_errors=(), current_question_id=None,
                       selected_adapter=None, routing_confidence=0.0, routing_reason="")
        if extract or route or ocr_provider is not None:
            from qa_bot.perception.extractor import QuestionExtractor
            if ocr_provider is None:
                result = QuestionExtractor().extract(snapshot)
            else:
                from qa_bot.perception.ocr import extract_with_ocr
                result = await extract_with_ocr(snapshot, ocr_provider, ocr_requests or {},
                                                timeout=timeout)
            updated = replace(updated,
                           phase=RunPhase.EXTRACTED if result.ready else RunPhase.REVIEW_REQUIRED,
                           question_spec=result.spec, extraction_errors=result.errors,
                           current_question_id=result.spec.question_id if result.spec else None,
                           current_section=result.spec.section if result.spec else updated.current_section)
            if route:
                from qa_bot.solvers.router import route_state
                return route_state(updated)
            return updated
        return updated
    finally:
        await browser.close()
