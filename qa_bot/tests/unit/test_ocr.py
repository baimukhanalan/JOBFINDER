import unittest
from dataclasses import replace
from unittest.mock import AsyncMock
from pathlib import Path

from qa_bot.domain.ocr import RasterImage, OCRRegion, OCRRequest
from qa_bot.domain.ocr import OCRResult, OCRToken
from qa_bot.adapters.ocr.local import LocalOCRProvider
from qa_bot.adapters.ocr.font import GLYPHS
from qa_bot.perception.ocr import extract_with_ocr
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.adapters.ocr.pbm import decode_pbm
from qa_bot.perception.extractor import QuestionExtractor
from qa_bot.orchestration.observe import observe_once
from qa_bot.domain.state import RunState, RunPhase
from unittest.mock import patch


def render(text, *, x=3, y=4, scale=2, chart=False):
    width, height = x + len(text) * 6 * scale + 4, y + 7 * scale + 20
    pixels = [0] * (width * height)
    for i, char in enumerate(text):
        for row, bits in enumerate(GLYPHS[char]):
            for col, bit in enumerate(bits):
                for dy in range(scale):
                    for dx in range(scale):
                        pixels[(y + row * scale + dy) * width +
                               x + (i * 6 + col) * scale + dx] = int(bit)
    if chart:
        for yy in range(y + 7 * scale + 3, height):
            for xx in range(3, width // 2):
                pixels[yy * width + xx] = 1
    image = RasterImage(width, height, bytes(pixels))
    region = OCRRegion(x, y, len(text) * 6 * scale, 7 * scale, scale)
    return OCRRequest(image, region)


def snapshot(text="", other=""):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/questions/ANA-01.html"
    html = fixture.read_text(encoding="utf-8")
    start = html.index('<p data-field="text">')
    end = html.index("</p>", start) + 4
    html = html[:start] + '<p data-field="text" data-ocr-id="text">' + text + "</p>" + html[end:]
    if other:
        html = html.replace("Choice 1</label>", other + "</label>")
        html = html.replace('data-option-id="o1"', 'data-option-id="o1" data-ocr-id="option"')
    return BrowserSnapshot("obs", "http://127.0.0.1:8000/q", question_html=html)


class OCRTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaceable_provider_without_synthetic_grid(self):
        request = OCRRequest(RasterImage(20, 10, bytes(200)), OCRRegion(0, 0, 20, 10))
        provider = AsyncMock()
        provider.recognize.return_value = OCRResult(
            request.image.digest, request.region.box,
            (OCRToken("TOTAL 42", (1, 1, 18, 8), 0.97),), 0.97, "fake-general-engine")
        result = await extract_with_ocr(snapshot(), provider, {"text": request})
        self.assertTrue(result.ready, result.errors)
        self.assertEqual(result.spec.extraction_confidence, 0.97)

    async def test_independent_pbm_table_fixture(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/ocr/table42.pbm"
        image = decode_pbm(path.read_bytes())
        result = await LocalOCRProvider().recognize(
            OCRRequest(image, OCRRegion(3, 3, 12, 7)), timeout=1)
        self.assertEqual(result.text, "42")
        self.assertEqual(result.tokens[1].box, (9, 3, 5, 7))

    async def test_bad_pbm(self):
        for data in (b"P1 2 2 1", b"P1 1 1 9", b"P4 1 1 0", b"\xff"):
            with self.assertRaises(ValueError):
                decode_pbm(data)

    async def test_dom_without_ocr_cannot_claim_complete(self):
        result = QuestionExtractor().extract(snapshot())
        self.assertFalse(result.ready)
        self.assertIn("ocr_required", result.errors)

    async def test_wrong_hash_box_empty_and_incomplete_rejected(self):
        request = render("123")
        good = await LocalOCRProvider().recognize(request, timeout=1)
        cases = (replace(good, image_hash="wrong"),
                 replace(good, tokens=()),
                 replace(good, tokens=good.tokens[:1]),
                 replace(good, tokens=(replace(good.tokens[0], box=(0, 0, 1, 1)),) + good.tokens[1:]))
        for returned in cases:
            provider = AsyncMock()
            provider.recognize.return_value = returned
            result = await extract_with_ocr(snapshot(), provider, {"text": request})
            self.assertFalse(result.ready)

    async def test_observation_state_and_route_reset_on_failure(self):
        browser = AsyncMock()
        browser.observe.return_value = snapshot()
        with patch("qa_bot.orchestration.observe.BrowserController", return_value=browser):
            state = await observe_once("http://127.0.0.1", ("http://127.0.0.1",),
                                       RunState("run"), route=True, ocr_provider=LocalOCRProvider(),
                                       ocr_requests={"text": render("TOTAL")})
            self.assertEqual(state.phase, RunPhase.ROUTED)
            self.assertEqual(state.question_spec.question_text, "TOTAL")
            stopped = await observe_once("http://127.0.0.1", ("http://127.0.0.1",),
                                         state, route=True, ocr_provider=LocalOCRProvider())
            self.assertEqual(stopped.phase, RunPhase.REVIEW_REQUIRED)
            self.assertIsNone(stopped.question_spec)
            self.assertIsNone(stopped.selected_adapter)
            self.assertEqual(browser.close.await_count, 2)

    async def test_text_numbers_and_boxes(self):
        for text in ("TOTAL 123", "-12.50", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789"):
            request = render(text)
            result = await LocalOCRProvider().recognize(request, timeout=1)
            self.assertEqual(result.text, text)
            self.assertEqual(result.confidence, 1)
            self.assertEqual(result.tokens[0].box[:2], (3, 4))
            self.assertEqual(result.image_hash, request.image.digest)

    async def test_chart_label_not_bar_geometry(self):
        request = render("SALES 42", chart=True)
        result = await LocalOCRProvider().recognize(request, timeout=1)
        self.assertEqual(result.text, "SALES 42")

    async def test_dom_complete_skips_provider(self):
        provider = AsyncMock()
        result = await extract_with_ocr(snapshot("DOM TEXT"), provider, {})
        self.assertTrue(result.ready)
        provider.recognize.assert_not_called()

    async def test_fill_and_provenance(self):
        result = await extract_with_ocr(snapshot(), LocalOCRProvider(), {"text": render("TOTAL 123")})
        self.assertTrue(result.ready, result.errors)
        self.assertEqual(result.spec.question_text, "TOTAL 123")
        self.assertEqual(len(result.spec.ocr_evidence), 1)
        self.assertIn("ocr:synthetic-5x7-v1", result.spec.provenance)

    async def test_conflict_stops(self):
        result = await extract_with_ocr(snapshot(other="DOM"), LocalOCRProvider(),
                                       {"text": render("TOTAL"), "option": render("OTHER")})
        self.assertFalse(result.ready)
        self.assertIn("ocr_dom_conflict", result.errors)

    async def test_low_confidence_stops(self):
        request = render("123")
        noisy = bytearray(request.image.pixels)
        # Destroy the first glyph, retaining an unknown nonblank pattern.
        for y in range(4, 18):
            for x in range(3, 13):
                noisy[y * request.image.width + x] = 1
        request = replace(request, image=replace(request.image, pixels=bytes(noisy)))
        result = await extract_with_ocr(snapshot(), LocalOCRProvider(), {"text": request})
        self.assertFalse(result.ready)
        self.assertIn("ocr_low_confidence", result.errors)

    async def test_missing_binding_and_timeout(self):
        result = await extract_with_ocr(snapshot(), LocalOCRProvider(), {})
        self.assertFalse(result.ready)
        provider = AsyncMock()
        provider.recognize.side_effect = TimeoutError
        result = await extract_with_ocr(snapshot(), provider, {"text": render("123")})
        self.assertIn("ocr_provider_failed", result.errors)

    async def test_table_cells(self):
        html = snapshot().question_html.replace('data-type-id="ANA-01"', 'data-type-id="ANA-02"')
        html = html.replace('<p data-field="text" data-ocr-id="text"></p>',
                            '<p data-field="text">Read the table</p><table><tr><th>VALUE</th></tr>'
                            '<tr><td data-ocr-id="cell"></td></tr></table>')
        snap = replace(snapshot(), question_html=html)
        result = await extract_with_ocr(snap, LocalOCRProvider(), {"cell": render("42")})
        self.assertTrue(result.ready, result.errors)
        self.assertEqual(result.spec.tables[0].rows, (("42",),))

    async def test_invalid_image_and_region(self):
        with self.assertRaises(ValueError):
            RasterImage(2, 2, b"bad")
        with self.assertRaises(ValueError):
            OCRRequest(render("1").image, OCRRegion(999, 0, 12, 14, 2))
        with self.assertRaises(ValueError):
            await LocalOCRProvider().recognize(render("1"), timeout=0)

    async def test_unknown_profile_no_ocr(self):
        provider = AsyncMock()
        result = await extract_with_ocr(replace(snapshot(), question_html="<p>unknown</p>"), provider, {})
        self.assertFalse(result.ready)
        provider.recognize.assert_not_called()
