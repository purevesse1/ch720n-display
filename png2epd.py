"""Convert an image into a C header for the epd_test sketch (SSD1681, 200x200, 1 = white).

Uses the same crop/dither as epaper_image.py, so the panel shows what fallback_eink.png previews.

    python png2epd.py                               # fallback.png -> epd_test/fallback.h
    python png2epd.py input.png out.h --name my_img
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from epaper_image import FALLBACK_IMAGE, SCREEN_SIZE, to_epaper

DEFAULT_HEADER = Path(__file__).with_name("epd_test") / "fallback.h"


def image_to_bytes(image: Image.Image) -> bytes:
    """Pack a 1-bit image row-major, MSB = leftmost pixel, bit 1 = white (SSD1681 RAM 0x24 format)."""
    mono = to_epaper(image)
    width, height = mono.size
    assert (width, height) == SCREEN_SIZE and width % 8 == 0
    px = mono.load()
    out = bytearray()
    for y in range(height):
        for xb in range(width // 8):
            b = 0
            for bit in range(8):
                if px[xb * 8 + bit, y]:          # nonzero = white
                    b |= 0x80 >> bit
            out.append(b)
    return bytes(out)


def write_header(data: bytes, path: Path, name: str, source: Path) -> None:
    w, h = SCREEN_SIZE
    lines = [
        f"// Generated from {source.name} by png2epd.py — {w}x{h}, 1 = white. Do not edit by hand.",
        "#pragma once",
        "#include <stdint.h>",
        "",
        f"const uint8_t {name}[{len(data)}] = {{",
    ]
    for i in range(0, len(data), 16):
        lines.append("  " + ", ".join(f"0x{b:02X}" for b in data[i:i + 16]) + ",")
    lines.append("};")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", nargs="?", type=Path, default=FALLBACK_IMAGE)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_HEADER)
    parser.add_argument("--name", default="fallback_img", help="C array name (default: fallback_img)")
    args = parser.parse_args()

    with Image.open(args.input) as opened:
        data = image_to_bytes(opened)
    write_header(data, args.output, args.name, args.input)
    black = sum(8 - bin(b).count("1") for b in data)
    print(f"Wrote {args.output} ({len(data)} bytes, {black} black px)")


if __name__ == "__main__":
    main()
