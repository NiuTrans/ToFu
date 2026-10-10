#!/usr/bin/env python3
"""Regenerate the evolution office prop atlas (assets/props.png).

Every prop is an individually controllable scene object (chair, books). The
art is drawn deterministically with PIL in the office woodwork palette
(sampled from the door and desk art in assets/office.png), so the atlas
matches the AI-generated scenery without hand-edited raster copies. Supersampled
4x and LANCZOS-downscaled for soft illustrated edges. The script measures every
prop's alpha bounds and prints the PROPS constant consumed by
frontend/src/features/evolution/office-objects.ts.

Run: python3 scripts/generate_evolution_props.py [--check] [--frames-only]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ATLAS = ROOT / "frontend/src/features/evolution/assets/props.png"

# One prop per cell, vertical strip. KIND order in office-objects.ts must match.
KINDS = [
    "chair",
    "bookFlat0", "bookFlat1", "bookFlat2", "bookFlat3",
    "bookUp0", "bookUp1", "bookUp2", "bookUp3",
]
CELL_W, CELL_H, MARGIN = 96, 96, 8
SUPERSAMPLE = 4
WIDTH = MARGIN * 2 + CELL_W
HEIGHT = MARGIN * 2 + CELL_H * len(KINDS)

# Woodwork palette shared with the office door (#6b4f35 / #463522) and desk edge.
WOOD_DARK = (70, 53, 34)
WOOD_BASE = (107, 79, 53)
WOOD_LIGHT = (125, 95, 63)
WOOD_EDGE = (135, 88, 55)
PAPER = (233, 221, 199)
PAPER_LINE = (201, 185, 143)
# Book cover variants: muted teal/crimson/amber/indigo to sit in the scene palette.
COVERS = [
    ((77, 122, 114), (52, 87, 80)),
    ((161, 74, 74), (115, 48, 48)),
    ((192, 138, 62), (141, 96, 38)),
    ((90, 95, 140), (63, 67, 105)),
]

# Stored (1x) bounding boxes the props are drawn into; alpha bounds are measured.
CHAIR_BOX = (60, 88)      # drawn height on screen ~ spriteHeight * 0.47
BOOK_FLAT_BOX = (42, 20)  # lying on the desk / shelf stack
BOOK_UP_BOX = (28, 32)    # carried under one arm


def shade(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int,
          fill: tuple[int, int, int], dark: tuple[int, int, int]) -> None:
    """Rounded slab with a darker left/lower edge, matching the scenery light."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=fill + (255,))
    draw.rounded_rectangle((x0, y0, x0 + max(2, (x1 - x0) // 5), y1), radius=radius, fill=dark + (255,))


def draw_chair(size: tuple[int, int]) -> Image.Image:
    """Chair viewed from behind (the seated actors face their desks)."""
    w, h = size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE
    s = SUPERSAMPLE
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1):
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    # Rear posts with finial caps.
    for x0 in (10, 44):
        d.rounded_rectangle(box(x0, 8, x0 + 6, 58), radius=3 * s, fill=WOOD_BASE + (255,))
        d.rounded_rectangle(box(x0, 8, x0 + 2, 58), radius=1 * s, fill=WOOD_DARK + (255,))
        d.ellipse(box(x0 - 1, 3, x0 + 7, 11), fill=WOOD_LIGHT + (255,))
        d.ellipse(box(x0 - 1, 3, x0 + 2, 11), fill=WOOD_BASE + (255,))
    # Two backrest slats.
    for y0 in (13, 30):
        shade(d, box(9, y0, 51, y0 + 9), 4 * s, WOOD_LIGHT, WOOD_BASE)
    # Seat: trapezoid top, darker front face.
    d.polygon([p for pt in [(7, 56), (53, 56), (58, 70), (2, 70)] for p in (pt[0] * s, pt[1] * s)],
              fill=WOOD_EDGE + (255,))
    d.polygon([p for pt in [(2, 70), (58, 70), (56, 77), (4, 77)] for p in (pt[0] * s, pt[1] * s)],
              fill=WOOD_BASE + (255,))
    # Legs and stretcher bar.
    for x0 in (8, 47):
        d.rounded_rectangle(box(x0, 72, x0 + 5, 87), radius=2 * s, fill=WOOD_DARK + (255,))
    d.rounded_rectangle(box(8, 79, 52, 83), radius=2 * s, fill=WOOD_DARK + (255,))
    return image.resize(size, Image.LANCZOS)


def draw_book_flat(size: tuple[int, int], cover: tuple[int, int, int], spine: tuple[int, int, int]) -> Image.Image:
    """Closed book lying flat (desk spot and shelf stack)."""
    w, h = size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE
    s = SUPERSAMPLE
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1):
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    d.rounded_rectangle(box(1, 1, w // s - 1, 13), radius=3 * s, fill=cover + (255,))
    d.rounded_rectangle(box(1, 1, 6, 13), radius=3 * s, fill=spine + (255,))
    d.rectangle(box(5, 12, w // s - 4, 17), fill=PAPER + (255,))
    for y in (13, 15):
        d.line(box(6, y, w // s - 5, y), fill=PAPER_LINE + (255,), width=s)
    d.line(box(1, 13, w // s - 1, 13), fill=spine + (255,), width=s)
    return image.resize(size, Image.LANCZOS)


def draw_book_upright(size: tuple[int, int], cover: tuple[int, int, int], spine: tuple[int, int, int]) -> Image.Image:
    """Closed book carried upright under one arm."""
    w, h = size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE
    s = SUPERSAMPLE
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1):
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    d.rounded_rectangle(box(1, 1, w // s - 4, h // s - 1), radius=3 * s, fill=cover + (255,))
    d.rounded_rectangle(box(1, 1, 7, h // s - 1), radius=3 * s, fill=spine + (255,))
    d.rectangle(box(w // s - 6, 3, w // s - 2, h // s - 3), fill=PAPER + (255,))
    for y in range(6, h // s - 5, 4):
        d.line(box(w // s - 6, y, w // s - 2, y), fill=PAPER_LINE + (255,), width=max(1, s // 2))
    d.rounded_rectangle(box(10, 8, w // s - 10, 12), radius=2 * s, fill=PAPER + (255,))
    return image.resize(size, Image.LANCZOS)


def build() -> tuple[Image.Image, list[list[int]]]:
    canvas = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    bounds: list[list[int]] = []
    for index, kind in enumerate(KINDS):
        if kind == "chair":
            prop = draw_chair(CHAIR_BOX)
        elif kind.startswith("bookFlat"):
            cover, spine = COVERS[int(kind[-1])]
            prop = draw_book_flat(BOOK_FLAT_BOX, cover, spine)
        else:
            cover, spine = COVERS[int(kind[-1])]
            prop = draw_book_upright(BOOK_UP_BOX, cover, spine)
        x = MARGIN + (CELL_W - prop.width) // 2
        y = MARGIN + index * CELL_H + (CELL_H - prop.height) // 2
        canvas[y : y + prop.height, x : x + prop.width] = np.array(prop)
        bounds.append(measure(canvas, x, y, prop.width, prop.height))
    return Image.fromarray(canvas, "RGBA"), bounds


def measure(canvas: np.ndarray, x: int, y: int, w: int, h: int) -> list[int]:
    """Tight alpha bounds of a placed prop (robust against paste drift)."""
    region = canvas[y : y + h, x : x + w, 3]
    cols = np.where(region.max(axis=0) > 10)[0]
    rows = np.where(region.max(axis=1) > 10)[0]
    return [x + int(cols.min()), y + int(rows.min()), int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed atlas drifts from regeneration")
    parser.add_argument("--frames-only", action="store_true", help="print the PROPS constant without writing")
    args = parser.parse_args()
    image, bounds = build()
    if args.check:
        import io

        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=True)
        current = ATLAS.read_bytes()
        if buffer.getvalue() != current:
            print("props.png drifts from regeneration; run scripts/generate_evolution_props.py", file=sys.stderr)
            return 1
        print("props.png matches regeneration")
        return 0
    print("const PROPS = [")
    for rect in bounds:
        print("  " + json_dumps(rect) + ",")
    print("];")
    if not args.frames_only:
        image.save(ATLAS, "PNG", optimize=True)
        print(f"wrote {ATLAS} ({image.size[0]}x{image.size[1]})", file=sys.stderr)
    return 0


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())
