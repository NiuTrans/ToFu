#!/usr/bin/env python3
"""Derive crop coordinates for the authored 4x4 office character sheets.

The WebP sprites are the source of truth. This script only measures alpha and
emits TypeScript metadata; it never repaints or rescales the game artwork.
Run: python3 scripts/generate_evolution_game_frames.py [--check]
"""
import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "frontend/src/features/evolution/assets"
OUTPUT = ROOT / "frontend/src/features/evolution/office-game-frames.ts"
ROLES = ("executor", "reviewer", "coordinator", "researcher", "engineer", "writer", "analyst", "general")


def bounds(image: Image.Image, box: tuple[int, int, int, int]) -> list[int]:
    alpha = image.crop(box).getchannel("A").point(lambda value: 255 if value > 28 else 0)
    local = alpha.getbbox()
    if local is None:
        raise ValueError(f"Empty sprite cell {box}")
    return [box[0] + local[0], box[1] + local[1], local[2] - local[0], local[3] - local[1]]


def generate() -> str:
    lines = [
        "/** Measured alpha bounds for authored pixel-art sheets. Source: assets/game-*.webp.",
        " * Regenerate with scripts/generate_evolution_game_frames.py; never edit these coordinates by hand.",
        " * Rows: down, left, right, up. Columns: walk, stand, walk, stand.",
        " */",
        "export const GAME_FRAMES = {",
    ]
    for role in ROLES:
        image = Image.open(ASSETS / f"game-{role}.webp").convert("RGBA")
        rows = []
        for row in range(4):
            cells = []
            for column in range(4):
                cell = (
                    round(column * image.width / 4),
                    round(row * image.height / 4),
                    round((column + 1) * image.width / 4),
                    round((row + 1) * image.height / 4),
                )
                cells.append("[" + ",".join(map(str, bounds(image, cell))) + "]")
            rows.append("[" + ",".join(cells) + "]")
        lines.append(f"  {role}: [" + ",\n    ".join(rows) + "],")
    chair = Image.open(ASSETS / "game-chair.webp").convert("RGBA")
    lines.extend(["} as const;", "export const CHAIR_FRAME = " + str(bounds(chair, (0, 0, chair.width, chair.height))).replace(" ", "") + " as const;", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    expected = generate()
    if options.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != expected:
            raise SystemExit("office-game-frames.ts is out of date")
    else:
        OUTPUT.write_text(expected)
