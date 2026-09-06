"""DOM-first, atomic OCR merge for explicit synthetic region bindings only."""
import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from qa_bot.domain.common import confidence
from qa_bot.domain.ocr import OCRRequest, OCRResult
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.perception.html_profile import ProfileParser
from qa_bot.perception.extractor import QuestionExtractor, ExtractionResult, rehash_spec, CATALOG
from qa_bot.ports.ocr import OCRProvider


@dataclass(frozen=True)
class OCRPolicy:
    min_confidence: float = 0.95

    def __post_init__(self):
        confidence(self.min_confidence)
        if self.min_confidence < 0.8:
            raise ValueError("OCR gate cannot be lower than 0.8")


def validate_ocr(result: OCRResult, request: OCRRequest,
                 policy: OCRPolicy = OCRPolicy()) -> str | None:
    if not isinstance(result, OCRResult):
        return "ocr_invalid_result"
    if result.image_hash != request.image.digest or result.region != request.region.box:
        return "ocr_stale_or_wrong_region"
    if not result.tokens or not result.text:
        return "ocr_empty"
    threshold = 1 if result.provider == "synthetic-5x7-v1" else policy.min_confidence
    if result.confidence < threshold or any(t.confidence < threshold for t in result.tokens):
        return "ocr_low_confidence"
    r = request.region
    if result.provider == "synthetic-5x7-v1" and len(result.tokens) != r.width // (6 * r.scale):
        return "ocr_incomplete"
    for i, token in enumerate(result.tokens):
        x, y, w, h = token.box
        if x < r.x or y < r.y or x + w > r.x + r.width or y + h > r.y + r.height:
            return "ocr_invalid_coordinates"
        if result.provider == "synthetic-5x7-v1" and token.box != (
            r.x + i * 6 * r.scale, r.y, 5 * r.scale, 7 * r.scale
        ):
            return "ocr_invalid_coordinates"
    return None


async def extract_with_ocr(snapshot: BrowserSnapshot, provider: OCRProvider,
                           requests: Mapping[str, OCRRequest], *, timeout: float = 2,
                           policy: OCRPolicy = OCRPolicy()) -> ExtractionResult:
    """requests bind DOM data-ocr-id to in-memory pixels; no media downloads."""
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        return ExtractionResult(None, ("ocr_invalid_timeout",))
    extractor = QuestionExtractor()
    parser = ProfileParser()
    parser.feed(snapshot.question_html)
    roots = parser.root.find(lambda n: "data-qa-profile" in n.attrs)
    if (snapshot.incomplete or len(roots) != 1 or
        roots[0].attrs.get("data-qa-profile") != "qa-v1" or
        roots[0].attrs.get("data-type-id") not in CATALOG):
        return extractor.extract(snapshot)
    nodes = roots[0].find(lambda n: "data-ocr-id" in n.attrs)
    if not nodes or all(n.text() for n in nodes):
        return extractor.extract(snapshot)
    ids = [n.attrs["data-ocr-id"] for n in nodes]
    if any(not key for key in ids) or len(set(ids)) != len(ids) or len(nodes) > 32:
        return ExtractionResult(None, ("ocr_invalid_bindings",))
    for node in nodes:
        allowed = (node.tag in ("th", "td") or "data-option-id" in node.attrs or
                   node.attrs.get("data-field") in ("text", "instruction", "context"))
        if not allowed or node.find(lambda n: "data-ocr-id" in n.attrs):
            return ExtractionResult(None, ("ocr_unsupported_target",))
    evidence = []
    try:
        async with asyncio.timeout(timeout):
            for node, key in zip(nodes, ids):
                request = requests.get(key)
                if not isinstance(request, OCRRequest):
                    return ExtractionResult(None, ("ocr_missing_binding",))
                result = await provider.recognize(request, timeout=timeout)
                error = validate_ocr(result, request, policy)
                if error:
                    return ExtractionResult(None, (error,))
                dom = " ".join(node.text().split())
                text = " ".join(result.text.split())
                if dom and dom != text:
                    return ExtractionResult(None, ("ocr_dom_conflict",))
                if not dom:
                    node.children.append(text)  # preserve input/image children
                evidence.append(result)
    except Exception:
        return ExtractionResult(None, ("ocr_provider_failed",))
    try:
        extracted = extractor._extract(snapshot, parsed_root=parser.root)
    except (ValueError, TypeError, KeyError):
        return ExtractionResult(None, ("invalid_profile_data",))
    if not extracted.ready:
        return extracted
    spec = replace(extracted.spec, ocr_evidence=tuple(evidence),
                   extraction_confidence=min(extracted.confidence, *(e.confidence for e in evidence)),
                   provenance=extracted.spec.provenance +
                   tuple("ocr:" + e.provider for e in evidence))
    return replace(extracted, spec=rehash_spec(spec), confidence=spec.extraction_confidence)
