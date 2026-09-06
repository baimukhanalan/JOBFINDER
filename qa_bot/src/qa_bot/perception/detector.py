"""Conservative structural rules; question body is not a screen heading."""
import re
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.domain.screen import ScreenDetection, ScreenType

SECTIONS = ("Basic Analytical Ability", "Basic Computer Literacy Simulation",
            "Personality", "Sales Competency Test", "WriteX", "SVAR")


class ScreenDetector:
    def detect(self, snapshot: BrowserSnapshot) -> ScreenDetection:
        if snapshot.shadow_root_count:
            return ScreenDetection(reason="shadow_dom_requires_profile")
        if snapshot.incomplete:
            return ScreenDetection(reason="frames_require_profile")
        headings = " | ".join(snapshot.headings).casefold()
        controls = [e for e in snapshot.elements if e.enabled]
        names = " | ".join(e.name.casefold() for e in controls)
        if any(e.input_type == "password" for e in controls):
            return ScreenDetection(ScreenType.START, reason="authentication_required")
        section = next((s for s in SECTIONS if s.casefold() in headings), None)
        candidates = []
        rules = (
            (ScreenType.COMPLETE, r"assessment complete|test completed|тест заверш[её]н"),
            (ScreenType.SECTION_TRANSITION, r"section complete|next section|раздел заверш[её]н"),
            (ScreenType.REVIEW, r"review answers|review your answers|проверка ответов"),
            (ScreenType.INSTRUCTION, r"\binstructions\b|инструкция"),
            (ScreenType.START, r"welcome|start assessment|добро пожаловать"),
        )
        for screen, pattern in rules:
            if re.search(pattern, headings):
                candidates.append(screen)
        response_controls = any(e.input_type in ("radio", "checkbox")
                                or e.tag in ("textarea", "select")
                                or e.role in ("radio", "textbox") for e in controls)
        if re.search(r"question\s+\d+|вопрос\s+\d+", headings) and (
            response_controls or re.search(r"submit|record|отправить", names)
        ):
            candidates.append(ScreenType.QUESTION)
        if len(candidates) != 1:
            return ScreenDetection(section=section,
                                   reason="conflicting_rules" if candidates else "no_matching_rule")
        return ScreenDetection(candidates[0], section, "heading_and_controls")
