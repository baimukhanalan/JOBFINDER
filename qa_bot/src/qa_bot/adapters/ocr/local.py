"""Recognize pixels in explicitly aligned synthetic glyph regions, without I/O."""
import asyncio
import math
import time
from qa_bot.domain.ocr import OCRRequest, OCRResult, OCRToken
from qa_bot.adapters.ocr.font import GLYPHS


class LocalOCRProvider:
    async def recognize(self, request: OCRRequest, *, timeout: float = 2) -> OCRResult:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("positive finite timeout required")
        deadline = time.monotonic() + timeout
        image, r = request.image, request.region
        if (r.scale > 8 or r.height != 7 * r.scale or
            r.width % (6 * r.scale) or r.width // (6 * r.scale) > 256):
            raise ValueError("requires bounded aligned synthetic 5x7 cells")
        tokens = []
        for cell in range(r.width // (6 * r.scale)):
            await asyncio.sleep(0)
            if time.monotonic() > deadline:
                raise TimeoutError("OCR deadline")
            x = r.x + cell * 6 * r.scale
            samples = []
            mixed = False
            for row in range(7):
                for col in range(6):
                    block = [image.pixels[(r.y + row * r.scale + dy) * image.width +
                                          x + col * r.scale + dx]
                             for dy in range(r.scale) for dx in range(r.scale)]
                    mixed |= len(set(block)) != 1
                    samples.append(round(sum(block) / len(block)))
            distances = []
            for char, rows in GLYPHS.items():
                template = [int(p) for row in rows for p in row + "0"]
                distances.append((sum(a != b for a, b in zip(samples, template)), char))
            distances.sort()
            distance, char = distances[0]
            certainty = max(0, 1 - distance / 42)
            if mixed or distance == distances[1][0]:
                certainty = 0
            # Nonexact synthetic pixels are never accepted by the merge gate.
            tokens.append(OCRToken(char,
                                   (x, r.y, 5 * r.scale, 7 * r.scale), certainty))
        return OCRResult(image.digest, r.box, tuple(tokens),
                         min(t.confidence for t in tokens))
