"""Strict extraction only. Unsupported or incomplete input never yields a spec."""
from dataclasses import dataclass, asdict, replace
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.domain.question import (
    QuestionSpec, OptionSpec, AssetRef, TableSpec, ResponseContract, ResponseKind,
    FieldConstraint, NavigationButton,
)
from qa_bot.perception.html_profile import ProfileParser

CATALOG = {f"ANA-{i:02}": ("Basic Analytical Ability", ResponseKind.SINGLE_CHOICE)
           for i in range(1, 6)}
CATALOG.update({f"COMP-{i:02}": ("Basic Computer Literacy Simulation", ResponseKind.UI_ACTION)
                for i in range(1, 17)})
CATALOG.update({
    "PERS-01": ("Personality", ResponseKind.SINGLE_CHOICE),
    "SALES-01": ("Sales Competency Test", ResponseKind.MULTI_CHOICE),
    "WRITEX-01": ("WriteX", ResponseKind.TEXT),
    "SVAR-01": ("SVAR", ResponseKind.AUDIO),
})


@dataclass(frozen=True)
class ExtractionResult:
    spec: QuestionSpec | None
    errors: tuple[str, ...] = ()
    confidence: float = 0.0
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.spec is not None and not self.errors


def parse_timer(value: str) -> int:
    if value.isdecimal():
        return int(value)
    parts = value.split(":")
    if len(parts) not in (2, 3) or not all(p.isdecimal() for p in parts):
        raise ValueError("invalid timer")
    numbers = list(map(int, parts))
    if any(n >= 60 for n in numbers[1:]):
        raise ValueError("invalid timer")
    return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))


class QuestionExtractor:
    def extract(self, snapshot: BrowserSnapshot) -> ExtractionResult:
        if snapshot.shadow_root_count:
            return ExtractionResult(None, ("shadow_dom_requires_profile",), 0.0)
        if snapshot.incomplete:
            return ExtractionResult(None, ("unsupported_frames",))
        try:
            return self._extract(snapshot)
        except (ValueError, TypeError, KeyError):
            return ExtractionResult(None, ("invalid_profile_data",))

    def _extract(self, snapshot, *, parsed_root=None):
        parser = ProfileParser()
        if parsed_root is None:
            parser.feed(snapshot.question_html)
        else:
            parser.root = parsed_root
        roots = parser.root.find(lambda n: "data-qa-profile" in n.attrs)
        if len(roots) != 1 or roots[0].attrs["data-qa-profile"] != "qa-v1":
            return ExtractionResult(None, ("unknown_format",))
        root = roots[0]
        errors = []
        warnings = []
        if root.find(lambda n: "data-ocr-id" in n.attrs and not n.text()):
            errors.append("ocr_required")

        def one(name, required=True):
            nodes = root.find(lambda n: n.attrs.get("data-field") == name)
            if len(nodes) != 1:
                if required or nodes:
                    errors.append("missing_or_duplicate:" + name)
                return None
            return nodes[0]

        def value(name):
            node = one(name)
            result = node.text() if node else ""
            if not result:
                errors.append("empty:" + name)
            return result

        type_id = root.attrs.get("data-type-id", "")
        if type_id not in CATALOG:
            return ExtractionResult(None, ("unsupported_type",))
        section, expected_kind = CATALOG[type_id]
        section_text = value("section")
        if section_text != section:
            errors.append("section_type_mismatch")
        instruction, text = value("instruction"), value("text")
        attrs = root.attrs
        question_id, attempt_id = attrs.get("data-question-id", ""), attrs.get("data-attempt-id", "")
        number = int(attrs.get("data-question-number", "0"))
        if not question_id or not attempt_id or number < 1:
            errors.append("missing_identity")
        response = one("response")
        if response is None:
            return ExtractionResult(None, tuple(errors))
        kind = ResponseKind(response.attrs.get("data-kind", ""))
        if kind != expected_kind:
            errors.append("response_type_mismatch")
        options = []
        assets = []
        media_nodes = root.find(lambda n: n.tag in ("img", "audio", "video")
                               or "data-media" in n.attrs)
        for index, node in enumerate(media_nodes):
            source = node.attrs.get("src") or node.attrs.get("href")
            if not source:
                nested = node.find(lambda n: n.tag == "source")
                source = nested[0].attrs.get("src") if nested else None
            if not source:
                errors.append("missing_media_url")
                continue
            absolute = urljoin(snapshot.document_ref, source)
            parsed = urlsplit(absolute)
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
                errors.append("unsupported_media_url")
                continue
            mime = node.attrs.get("data-media-type") or {
                "img": "image/unknown", "audio": "audio/unknown", "video": "video/unknown"
            }.get(node.tag, "application/octet-stream")
            assets.append(AssetRef(node.attrs.get("id") or f"media-{index+1}", mime, absolute))
        option_nodes = root.find(lambda n: "data-option-id" in n.attrs)
        for index, node in enumerate(option_nodes):
            images = node.find(lambda n: n.tag == "img")
            image_ref = images[0].attrs.get("id") if images else None
            if images and not image_ref:
                errors.append("option_image_missing_id")
            options.append(OptionSpec(node.attrs["data-option-id"], index + 1,
                                      node.text(), image_ref))
        choice = kind in (ResponseKind.SINGLE_CHOICE, ResponseKind.MULTI_CHOICE)
        if choice:
            expected_count = response.attrs.get("data-option-count")
            if expected_count is None or int(expected_count) != len(options):
                errors.append("incomplete_options")
            if kind == ResponseKind.SINGLE_CHOICE:
                limits = (1, 1)
            else:
                limits = (int(response.attrs.get("data-min-selections", "0")),
                          int(response.attrs.get("data-max-selections", "0")))
            roles = tuple(response.attrs.get("data-roles", "").split())
            if type_id == "SALES-01" and (limits != (2, 2) or roles != ("best", "worst")):
                errors.append("invalid_best_worst_contract")
        else:
            limits, roles = (0, 0), ()
            if options:
                errors.append("unexpected_options")
        fields = response.find(lambda n: n.tag in ("textarea", "input", "select"))
        constraints = []
        for field in fields:
            for key in ("required", "minlength", "maxlength", "min", "max", "step", "pattern"):
                if key in field.attrs:
                    constraints.append(FieldConstraint(key, field.attrs[key] or "true"))
            low, high = field.attrs.get("minlength"), field.attrs.get("maxlength")
            if low and high and int(low) > int(high):
                errors.append("conflicting_field_limits")
        min_words = int(response.attrs.get("data-min-words", "0"))
        if kind == ResponseKind.TEXT and (len(fields) != 1 or min_words < 30):
            errors.append("invalid_text_field")
        contract = ResponseContract(kind, *limits, roles, min_words)
        tables = []
        for table in root.find(lambda n: n.tag == "table"):
            if table.find(lambda n: n.attrs.get("rowspan", "1") != "1"
                          or n.attrs.get("colspan", "1") != "1"):
                errors.append("unsupported_merged_table")
                continue
            headers = tuple(n.text() for n in table.find(lambda n: n.tag == "th"))
            rows = []
            for row in table.find(lambda n: n.tag == "tr"):
                cells = row.find(lambda n: n.tag == "td")
                if cells:
                    rows.append(tuple(n.text() for n in cells))
            tables.append(TableSpec(headers, tuple(rows)))
        if type_id == "ANA-02" and not tables:
            errors.append("missing_table")
        if type_id in ("ANA-03", "ANA-04") and not any(a.media_type.startswith("image/") for a in assets):
            errors.append("missing_image")
        if root.find(lambda n: n.tag in ("canvas", "iframe") or "data-unresolved" in n.attrs):
            errors.append("unsupported_content")
        timer = one("timer", required=False)
        remaining = parse_timer(timer.text()) if timer else None
        if timer is None:
            warnings.append("timer_unavailable")
        if remaining == 0:
            errors.append("time_expired")
        navigation = tuple(NavigationButton(
            node.attrs.get("id", ""), node.text(), node.attrs["data-nav"],
            "disabled" not in node.attrs and node.attrs.get("aria-disabled") != "true"
        ) for node in root.find(lambda n: "data-nav" in n.attrs))
        if not navigation or any(not n.id or not n.label or n.action not in
                                 ("submit", "next", "back", "skip", "record") for n in navigation):
            errors.append("missing_or_unknown_navigation")
        if len({n.id for n in navigation}) != len(navigation):
            errors.append("duplicate_navigation_id")
        if len({a.id for a in assets}) != len(assets):
            errors.append("duplicate_media_id")
        interaction = attrs.get("data-interaction", "")
        if interaction not in ("submit", "submit_next", "auto_advance"):
            errors.append("unknown_interaction")
        if errors:
            return ExtractionResult(None, tuple(errors), 0.0, tuple(warnings))
        confidence = 0.9 if warnings else 1.0
        spec = QuestionSpec(
            question_id, attempt_id, section_text, type_id, text, contract, "pending",
            schema_version="1.1", question_number=number, instruction=instruction,
            options=tuple(options), tables=tuple(tables), assets=tuple(assets),
            context=tuple(n.text() for n in root.find(lambda n: n.attrs.get("data-field") == "context")),
            interaction_contract=interaction, field_constraints=tuple(constraints),
            remaining_seconds=remaining, navigation=navigation,
            extraction_confidence=confidence, completeness=True,
            provenance=("dom:qa-v1",), source_quality=tuple(warnings))
        return ExtractionResult(rehash_spec(spec), confidence=confidence,
                                warnings=tuple(warnings))


def rehash_spec(spec):
    content = asdict(spec)
    for key in ("question_id", "attempt_id", "question_number", "content_hash",
                "remaining_seconds", "navigation", "extraction_confidence", "source_quality",
                "provenance"):
        content.pop(key)
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return replace(spec, content_hash=digest)
