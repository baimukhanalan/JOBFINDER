#!/usr/bin/env python3
"""Build a reviewable MP4 from redacted page-only evidence frames."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def natural_key(path: Path):
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name))


def annotate(path: Path):
    image = Image.open(path).convert("RGB")
    if image.size != (1280, 720):
        image.thumbnail((1280, 720))
        canvas = Image.new("RGB", (1280, 720), "black")
        canvas.paste(image, ((1280 - image.width) // 2, (720 - image.height) // 2))
        image = canvas
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 676, 1280, 720), fill=(0, 0, 0, 180))
    draw.text((18, 688), path.stem, fill="white", font=ImageFont.load_default(size=18))
    return image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds-per-frame", type=float, default=1.5)
    args = parser.parse_args()
    frames = sorted(args.frames.glob("*.jpg"), key=natural_key)
    if not frames or not 0.2 <= args.seconds_per_frame <= 10:
        raise SystemExit("valid frames and duration required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fps = 10
    with imageio.get_writer(
        args.output, fps=fps, codec="libx264", quality=8,
        output_params=["-movflags", "+faststart"],
    ) as writer:
        for path in frames:
            image = annotate(path)
            for _ in range(round(args.seconds_per_frame * fps)):
                writer.append_data(np.asarray(image))
    print(args.output.resolve())


if __name__ == "__main__":
    main()
