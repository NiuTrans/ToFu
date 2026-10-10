#!/usr/bin/env python3
"""Regenerate the evolution office sprite atlas (assets/people.png).

Source art: the two AI-generated characters already committed in people.png
(executor body, reviewer body). This script derives four more visually
distinct people by hue-rotating clothing and hair segments (skin and outline
masks stay untouched), repacks six characters onto one deterministic grid,
measures every frame's alpha bounds, and prints the FRAMES constant consumed
by frontend/src/features/evolution/office-scene.ts.

Idempotent: when the input file is already the six-character atlas, the two
base characters are recovered from their verbatim block positions first.
Run: python3 scripts/generate_evolution_people.py [--check] [--frames-only]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ATLAS = ROOT / "frontend/src/features/evolution/assets/people.png"

# Alpha bounds of the original two-character atlas (4 directions x 4 frames
# per character; rows 0-3 = executor body, rows 4-7 = reviewer body).
SOURCE_FRAMES = [
    [[137, 16, 74, 152], [438, 16, 74, 147], [741, 16, 75, 152], [1043, 16, 73, 147]],
    [[134, 181, 76, 146], [436, 182, 75, 144], [737, 181, 76, 146], [1042, 182, 75, 144]],
    [[132, 343, 84, 148], [439, 343, 78, 149], [740, 343, 81, 148], [1044, 343, 77, 149]],
    [[135, 504, 75, 148], [438, 504, 71, 144], [741, 504, 75, 148], [1045, 504, 73, 144]],
    [[126, 665, 93, 150], [428, 663, 92, 148], [731, 665, 94, 150], [1034, 665, 93, 146]],
    [[134, 823, 85, 147], [434, 823, 86, 147], [738, 823, 87, 147], [1039, 823, 86, 147]],
    [[126, 980, 88, 143], [429, 980, 89, 142], [733, 980, 89, 143], [1037, 980, 89, 143]],
    [[132, 1124, 84, 130], [432, 1124, 85, 127], [738, 1124, 83, 130], [1040, 1124, 85, 127]],
]
ORIGINAL_HEIGHT = 1254
CELL_W, CELL_H, MARGIN, BLOCK_GAP = 320, 160, 8, 8
BLOCK_H = 4 * CELL_H + BLOCK_GAP
CHARACTER_COUNT = 6
WIDTH = MARGIN + 4 * CELL_W
HEIGHT = CHARACTER_COUNT * BLOCK_H

# Hue masks in PIL HSV units (h: 0-255 maps 0-360 degrees, s/v: 0-255).
# Verified against the source art: each mask matches clothing or hair only,
# never skin (s <= 166, h 7-32) or the dark outline (v < 46).
SUIT_BODY0 = {"h": (66, 146), "s": 71}          # teal suit, hue center ~150 deg
SUIT_BODY1 = {"h": (17, 48), "s": 166}          # amber suit, hue center ~45 deg
HAIR_BODY0 = {"h": (7, 25), "s": 102, "v_max": 115}  # dark brown hair


def hue(degrees: float) -> int:
    return round(degrees * 255 / 360)


# (source body 0/1, suit target hue in degrees or None, grey hair)
VARIANTS = [
    ("executor", 0, None, False),
    ("reviewer", 1, None, False),
    ("coordinator", 1, 250, False),   # indigo suit on the reviewer silhouette
    ("visitor_crimson", 0, 348, False),
    ("visitor_green", 1, 130, False),
    ("visitor_violet", 0, 290, True),  # violet suit, grey hair
]


def placed_rect(variant: int, body: int, direction: int, frame: int) -> list[int]:
    """Map a base body's source rect onto the variant's grid block, preserving
    the intra-row walk bob (per-frame x/y offsets relative to the tightest frame)."""
    row = body * 4 + direction
    sx, sy, sw, sh = SOURCE_FRAMES[row][frame]
    col_ref = min(SOURCE_FRAMES[r][frame][0] for r in range(8))
    row_ref = min(rect[1] for rect in SOURCE_FRAMES[row])
    x = MARGIN + frame * CELL_W + (sx - col_ref)
    y = variant * BLOCK_H + direction * CELL_H + (sy - row_ref)
    return [x, y, sw, sh]


def load_base_frames(atlas: np.ndarray) -> list[list[np.ndarray]]:
    """Return [body][direction*4+frame] RGBA crops of the two unmodified base
    characters, from either the original atlas or a regenerated one."""
    frames: list[list[np.ndarray]] = [[], []]
    for character in (0, 1):
        for direction in range(4):
            for frame in range(4):
                if atlas.shape[0] == ORIGINAL_HEIGHT:
                    sx, sy, sw, sh = SOURCE_FRAMES[character * 4 + direction][frame]
                else:
                    sx, sy, sw, sh = placed_rect(character, character, direction, frame)
                frames[character].append(atlas[sy : sy + sh, sx : sx + sw].copy())
    return frames


def recolor(frame: np.ndarray, body: int, target_hue: float | None, grey_hair: bool) -> np.ndarray:
    """Hue-rotate the body's suit (and optionally desaturate body-0 hair).
    Rotation shifts every masked hue by the same delta, preserving shading."""
    if target_hue is None and not grey_hair:
        return frame
    image = Image.fromarray(frame, "RGBA")
    h, s, v = image.convert("HSV").split()
    hp, sp, vp = (np.array(channel) for channel in (h, s, v))
    alpha = frame[:, :, 3]
    suit = SUIT_BODY0 if body == 0 else SUIT_BODY1
    if target_hue is not None:
        mask = (hp >= suit["h"][0]) & (hp <= suit["h"][1]) & (sp >= suit["s"]) & (alpha > 0)
        center = round(((suit["h"][0] + suit["h"][1]) / 2))
        hp = np.where(mask, (hp.astype(int) + hue(target_hue) - center) % 256, hp).astype(np.uint8)
    if grey_hair:
        mask = (
            (hp >= HAIR_BODY0["h"][0]) & (hp <= HAIR_BODY0["h"][1])
            & (sp >= HAIR_BODY0["s"]) & (vp <= HAIR_BODY0["v_max"]) & (alpha > 0)
        )
        sp = np.where(mask, sp.astype(int) // 5, sp).astype(np.uint8)
        vp = np.where(mask, np.minimum(vp.astype(int) + 24, 255), vp).astype(np.uint8)
    recolored = Image.merge("HSV", (Image.fromarray(hp), Image.fromarray(sp), Image.fromarray(vp))).convert("RGBA")
    result = np.array(recolored)
    result[:, :, 3] = alpha
    return result


def build() -> tuple[Image.Image, list[list[list[int]]]]:
    atlas = np.array(Image.open(ATLAS).convert("RGBA"))
    bases = load_base_frames(atlas)
    canvas = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    frames: list[list[list[int]]] = []
    for character, (_name, body, target, grey) in enumerate(VARIANTS):
        rows: list[list[int]] = []
        for direction in range(4):
            for frame in range(4):
                sprite = recolor(bases[body][direction * 4 + frame], body, target, grey)
                x, y, sw, sh = placed_rect(character, body, direction, frame)
                canvas[y : y + sh, x : x + sw] = sprite
                if frame == 0:
                    rows.append([])
                rows[direction].append(measure(canvas, x, y, sw, sh))
        frames.append(rows)
    return Image.fromarray(canvas, "RGBA"), frames


def measure(canvas: np.ndarray, x: int, y: int, w: int, h: int) -> list[int]:
    """Tight alpha bounds of a placed frame (robust against paste drift)."""
    region = canvas[y : y + h, x : x + w, 3]
    cols = np.where(region.max(axis=0) > 10)[0]
    rows = np.where(region.max(axis=1) > 10)[0]
    return [x + int(cols.min()), y + int(rows.min()), int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1)]


def flatten(frames: list[list[list[int]]]) -> list[list[list[int]]]:
    """FRAMES[row = character*4+direction][frame] for office-scene.ts."""
    return [rect for rows in frames for rect in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed atlas drifts from regeneration")
    parser.add_argument("--frames-only", action="store_true", help="print the FRAMES constant without writing")
    args = parser.parse_args()
    image, frames = build()
    if args.check:
        import io

        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=True)
        current = ATLAS.read_bytes()
        if buffer.getvalue() != current:
            print("people.png drifts from regeneration; run scripts/generate_evolution_people.py", file=sys.stderr)
            return 1
        print("people.png matches regeneration")
        return 0
    rows = flatten(frames)
    print("const FRAMES = [")
    for row in rows:
        print("  " + json_dumps(row) + ",")
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
