"""Pixel-only OCR contracts. Coordinates are image-local (x, y, width, height)."""
from dataclasses import dataclass, field
import hashlib
from qa_bot.domain.common import confidence, nonempty


@dataclass(frozen=True)
class RasterImage:
    width: int
    height: int
    pixels: bytes = field(repr=False)  # 0 white, 1 black, row-major

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.width, self.height)):
            raise ValueError("invalid dimensions")
        if self.width * self.height > 1_000_000:
            raise ValueError("image too large")
        if not isinstance(self.pixels, bytes) or len(self.pixels) != self.width * self.height:
            raise ValueError("invalid pixel buffer")
        if any(p not in (0, 1) for p in self.pixels):
            raise ValueError("only binary raster supported")

    @property
    def digest(self):
        return hashlib.sha256(f"{self.width}:{self.height}:".encode() + self.pixels).hexdigest()


@dataclass(frozen=True)
class OCRRegion:
    x: int
    y: int
    width: int
    height: int
    scale: int = 1

    def __post_init__(self):
        if any(type(v) is not int for v in (self.x, self.y, self.width, self.height, self.scale)):
            raise ValueError("integer region required")
        if min(self.x, self.y) < 0 or min(self.width, self.height, self.scale) < 1:
            raise ValueError("invalid region")

    @property
    def box(self):
        return self.x, self.y, self.width, self.height


@dataclass(frozen=True)
class OCRRequest:
    image: RasterImage
    region: OCRRegion

    def __post_init__(self):
        r = self.region
        if r.x + r.width > self.image.width or r.y + r.height > self.image.height:
            raise ValueError("region outside image")


@dataclass(frozen=True)
class OCRToken:
    text: str
    box: tuple[int, int, int, int]
    confidence: float

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text or len(self.text) > 4096:
            raise ValueError("nonempty bounded text per token required")
        confidence(self.confidence)
        if len(self.box) != 4 or any(type(v) is not int for v in self.box):
            raise ValueError("invalid box")
        if min(self.box[:2]) < 0 or min(self.box[2:]) < 1:
            raise ValueError("invalid box")


@dataclass(frozen=True)
class OCRResult:
    image_hash: str
    region: tuple[int, int, int, int]
    tokens: tuple[OCRToken, ...]
    confidence: float
    provider: str = "synthetic-5x7-v1"

    def __post_init__(self):
        nonempty(self.image_hash, "image hash")
        nonempty(self.provider, "provider")
        confidence(self.confidence)

    @property
    def text(self):
        return "".join(t.text for t in self.tokens).strip()
