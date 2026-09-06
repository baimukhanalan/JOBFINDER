"""Bounded P1 decoder. Accepts bytes, never follows a path or URL."""
import re
from qa_bot.domain.ocr import RasterImage


def decode_pbm(data: bytes) -> RasterImage:
    if not isinstance(data, bytes) or len(data) > 2_100_000:
        raise ValueError("invalid PBM size")
    try:
        parts = re.sub(r"#[^\r\n]*", "", data.decode("ascii")).split()
        if len(parts) < 4 or parts[0] != "P1":
            raise ValueError("only plain binary PBM P1 is supported")
        width, height = int(parts[1]), int(parts[2])
        if any(p not in ("0", "1") for p in parts[3:]):
            raise ValueError("invalid PBM pixels")
        return RasterImage(width, height, bytes(int(p) for p in parts[3:]))
    except (UnicodeError, IndexError) as error:
        raise ValueError("invalid PBM") from error
