"""Convert images to 200x200 1-bit e-paper-ready pictures for the CH720N display.

Used as a module by ch720n_media_preview.py, or run on its own:

    python epaper_image.py                                   # fallback.png -> fallback_eink.png
    python epaper_image.py input.jpg                         # -> input_eink.png
    python epaper_image.py input.jpg output.png
    python epaper_image.py fallback.png epd_test/fallback.h  # C header for the epd_test sketch
    python epaper_image.py input.png out.h --name my_img

An output ending in .h gets the panel's raw bitmap (SSD1681 RAM format) as a C array instead of a PNG,
so the panel shows exactly what the _eink.png preview shows.
"""

from __future__ import annotations

import argparse
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

SCREEN_SIZE = (200, 200)
# Picture used when there is no artwork; any format/size, converted to 1-bit automatically.
FALLBACK_IMAGE = Path(__file__).with_name("fallback.png")


def make_fallback_image(source_path: Path | None = FALLBACK_IMAGE) -> Image.Image:
    """Load the fallback picture (fallback.png by default), or draw a ghost if it's unavailable."""
    if source_path is not None:
        try:
            with Image.open(source_path) as opened:
                return opened.convert("RGB")
        except (OSError, ValueError) as exc:
            print(f"Fallback image {source_path} unavailable ({exc}); drawing placeholder.",
                  file=sys.stderr)

    image = Image.new("RGB", SCREEN_SIZE, "white")
    draw = ImageDraw.Draw(image)

    # A tiny ghost with a pixel-ish outline.
    draw.ellipse((52, 30, 148, 143), fill="white", outline="black", width=5)
    draw.rectangle((54, 90, 146, 145), fill="white")
    draw.line((54, 126, 54, 151, 75, 137, 99, 151, 124, 137, 146, 151, 146, 126),
              fill="black", width=5, joint="curve")
    draw.ellipse((78, 76, 91, 94), fill="black")
    draw.ellipse((109, 76, 122, 94), fill="black")
    draw.arc((88, 91, 113, 115), start=10, end=170, fill="black", width=3)
    draw.text((57, 169), "waiting for music...", fill="black")
    return image


def to_epaper(source: Image.Image) -> Image.Image:
    """Crop to a 200x200 square, convert to grayscale and dither to 1-bit."""
    square = ImageOps.fit(
        source.convert("RGB"),
        SCREEN_SIZE,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    grayscale = ImageOps.grayscale(square)
    # Floyd-Steinberg dithering approximates the look of a 1-bit e-paper panel.
    return grayscale.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def make_epaper_preview(artwork_bytes: bytes | None,
                        fallback_path: Path | None = FALLBACK_IMAGE) -> Image.Image:
    """Return a 200x200 monochrome/dithered image ready for an e-paper panel.

    With no artwork, the fallback picture is used (or the ghost if that's missing).
    """
    if not artwork_bytes:
        return to_epaper(make_fallback_image(fallback_path))
    with Image.open(BytesIO(artwork_bytes)) as opened:
        return to_epaper(opened)


def to_epd_bytes(source: Image.Image) -> bytes:
    """Convert any image to 1-bit and pack it in the panel's RAM format (see pack_epd_bytes)."""
    return pack_epd_bytes(to_epaper(source))


def pack_epd_bytes(mono: Image.Image) -> bytes:
    """Pack an already-converted 200x200 1-bit image row-major, MSB = leftmost pixel,
    bit 1 = white (SSD1681 RAM 0x24 format, 5000 bytes)."""
    if mono.mode != "1" or mono.size != SCREEN_SIZE:
        raise ValueError(f"expected a {SCREEN_SIZE} 1-bit image, got {mono.size} {mono.mode}")
    width, height = mono.size
    assert width % 8 == 0
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


def write_c_header(data: bytes, path: Path, name: str, source: Path) -> None:
    """Write packed panel bytes as a `const uint8_t name[]` C header."""
    w, h = SCREEN_SIZE
    lines = [
        f"// Generated from {source.name} by epaper_image.py — {w}x{h}, 1 = white. Do not edit by hand.",
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
    parser = argparse.ArgumentParser(description="Convert an image to a 200x200 1-bit e-paper image.")
    parser.add_argument("input", nargs="?", type=Path, default=FALLBACK_IMAGE,
                        help=f"image to convert (default: {FALLBACK_IMAGE.name})")
    parser.add_argument("output", nargs="?", type=Path,
                        help="where to save the PNG, or a .h for a C header (default: <input>_eink.png)")
    parser.add_argument("--name", default="fallback_img",
                        help="C array name when writing a .h (default: fallback_img)")
    args = parser.parse_args()

    output = args.output or args.input.with_name(f"{args.input.stem}_eink.png")
    with Image.open(args.input) as opened:
        if output.suffix.lower() == ".h":
            data = to_epd_bytes(opened)
            write_c_header(data, output, args.name, args.input)
            black = sum(8 - bin(b).count("1") for b in data)
            print(f"Wrote {output} ({len(data)} bytes, {black} black px)")
            return
        result = to_epaper(opened)
    result.save(output)
    print(f"Saved {output} ({result.size[0]}x{result.size[1]}, 1-bit)")


if __name__ == "__main__":
    main()
