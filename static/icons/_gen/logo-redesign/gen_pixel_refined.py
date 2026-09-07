"""Pixel-refined candidate — hand-laid on a 32×32 grid, written directly.

Why not reuse gen_candidate_c.py: that generator derives the face position
from the cube's plane equations, and every attempt to re-place features
through those helpers put the face off-centre (measured, then re-measured,
then still off). The plane maths is right for the CUBE; it is the wrong tool
for placing a FACE, because what "centred" means for a face is a visual
judgement about the front plane's visible rhombus, not an arithmetic midpoint
of the column set.

So the cube is still generated analytically (staircases stay regular — that
is the whole point of this candidate), but the face is a literal pixel map
laid out by hand, exactly the way the original pixel art was authored. The
bet: keep the current logo's pixel character and hand-made feel, and fix ONLY
the VTracer artefacts (ragged edges, wobbling internal seams).
"""
import os

P = 2
OUTLINE = '#1F1C25'
TOP_C = '#FCF2DA'
LEFT_C = '#F6E5C2'
RIGHT_C = '#E7CFA6'
BLUSH = '#F79E95'
SHEEN = '#FFFBF0'
WHITE = '#FFFFFF'

CX = 15.5
EQ_Y, CTR_Y, BOT_Y = 9.0, 15.0, 28.0
LX, RX = 3.5, 27.5
HW, HH = 12.0, 6.0


def face_of(px, py):
    if abs(px - CX) / HW + abs(py - EQ_Y) / HH <= 1.0 and py <= CTR_Y:
        return 'top'
    slope = HH / HW
    y_top = CTR_Y - slope * abs(px - CX)
    y_bot = BOT_Y - slope * abs(px - CX)
    if LX <= px <= RX and y_top <= py <= y_bot:
        return 'left' if px <= CX else 'right'
    return None


cells = {}
for y in range(32):
    for x in range(32):
        f = face_of(x + 0.5, y + 0.5)
        if f:
            cells[(x, y)] = f

color = {}
for (x, y), f in cells.items():
    def at(dx, dy):
        return cells.get((x + dx, y + dy))
    outside = (at(1, 0) is None or at(-1, 0) is None
               or at(0, 1) is None or at(0, -1) is None)
    internal = (f == 'top' and any(at(dx, dy) in ('left', 'right')
                                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))) \
        or (f == 'left' and at(1, 0) == 'right')
    color[(x, y)] = OUTLINE if (outside or internal) else \
        {'top': TOP_C, 'left': LEFT_C, 'right': RIGHT_C}[f]

# ── Face: a literal pixel map on the front-left plane ──────────────────
# Rows are written the way you'd draw them in an editor. Each entry is
# (x, y, colour). The front-left rhombus runs roughly x 4..14 with each
# column dropping ~0.5px per step, so these coordinates trace that slope by
# hand and sit visually centred in it.
FACE = []


def _blk(x0, y0, w, h, c=OUTLINE):
    for dx in range(w):
        for dy in range(h):
            FACE.append((x0 + dx, y0 + dy, c))


# Left eye (2×4) and right eye (2×4), the right one one row lower to follow
# the plane's downward slope — same trick the original art uses.
_blk(6, 15, 2, 4)
_blk(11, 17, 2, 4)
FACE.append((7, 15, WHITE))      # sparkle in each eye
FACE.append((12, 17, WHITE))
# ω mouth: dips between the eyes, following the same slope.
for x, y in ((8, 20), (9, 21), (10, 21), (11, 22)):
    FACE.append((x, y, OUTLINE))
# Blush pads, outboard of each eye and one row below its centre.
for x, y in ((4, 17), (5, 17), (4, 18), (5, 18)):
    FACE.append((x, y, BLUSH))
for x, y in ((13, 20), (14, 20), (13, 21), (14, 21)):
    FACE.append((x, y, BLUSH))

for x, y, c in FACE:
    if cells.get((x, y)) == 'left':      # never paint onto outline / other faces
        color[(x, y)] = c

# Sheen on the top face — three cells stepping down the lit edge.
for s in ((11, 6), (10, 7), (9, 8)):
    if cells.get(s) == 'top':
        color[s] = SHEEN

rects = []
for y in sorted({yy for _, yy in color}):
    xs = sorted(x for (x, yy) in color if yy == y)
    runs, start, prev = [], xs[0], xs[0]
    for x in xs[1:]:
        if x == prev + 1 and color[(x, y)] == color[(prev, y)]:
            prev = x
            continue
        runs.append((start, prev, color[(prev, y)]))
        start = prev = x
    runs.append((start, prev, color[(prev, y)]))
    for x0, x1, c in runs:
        rects.append(f'<rect x="{x0 * P}" y="{y * P}" width="{(x1 - x0 + 1) * P}" '
                     f'height="{P}" fill="{c}"/>')

svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
       'shape-rendering="crispEdges">\n' + '\n'.join(rects) + '\n</svg>\n')
out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'candidate-pixel-refined.svg')
with open(out, 'w', encoding='utf-8') as fh:
    fh.write(svg)
print(f'wrote {out} ({len(svg)} bytes, {len(rects)} rects)')
