"""Generate candidate-c-pixel.svg — a strict-grid pixel-art tofu cube.

The current tofu-welcome.svg is a VTracer trace of a raster, so its staircases
are irregular and its internal edges wobble. This rebuilds the same pixel
identity analytically on a 32x32 grid (1 pixel = 2 viewBox units):

  top rhombus 24 wide x 12 tall (2:1 dimetric), vertical edges 13 (== slant),
  1px silhouette outline AND 1px internal face edges, crispEdges rendering.

Face classification is done analytically per cell center against the three
face planes, so the geometry is exact; features are then stamped in grid
coords chosen against the same planes.
"""

P = 2          # pixel size in viewBox units
OUTLINE = '#1F1C25'
TOP_C = '#FCF2DA'
LEFT_C = '#F6E5C2'
RIGHT_C = '#E7CFA6'
BLUSH = '#F79E95'
SHEEN = '#FFFBF0'
WHITE = '#FFFFFF'

# Cube vertices (float grid coords, y-down), symmetric about x=15.5
CX = 15.5
APEX_Y, EQ_Y, CTR_Y = 3.0, 9.0, 15.0      # top vertex / equator / center vertex
SIDE_BOT_Y, BOT_Y = 22.0, 28.0            # side bottom edge / bottom vertex
LX, RX = 3.5, 27.5                        # equator left / right x
HW, HH = 12.0, 6.0                        # rhombus half width / half height
VERT = SIDE_BOT_Y - EQ_Y                  # 13 vertical edge


def face_of(px, py):
    """Classify a point into 'top' | 'left' | 'right' | None (outside)."""
    if abs(px - CX) / HW + abs(py - EQ_Y) / HH <= 1.0 and py <= CTR_Y:
        return 'top'
    slope = HH / HW                              # 0.5
    y_top = CTR_Y - slope * abs(px - CX)         # rhombus bottom edges
    y_bot = BOT_Y - slope * abs(px - CX)         # cube bottom edges
    if LX <= px <= RX and y_top <= py <= y_bot:
        return 'left' if px <= CX else 'right'
    return None


cells = {}
for y in range(0, 32):
    for x in range(0, 32):
        f = face_of(x + 0.5, y + 0.5)
        if f:
            cells[(x, y)] = f


def at(x, y):
    return cells.get((x, y))


color = {}
for (x, y), f in cells.items():
    outside = any(at(x + dx, y + dy) is None
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    internal = (f == 'top' and any(at(x + dx, y + dy) in ('left', 'right')
                                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))) \
        or (f == 'left' and at(x + 1, y) == 'right')
    if outside or internal:
        color[(x, y)] = OUTLINE
    else:
        color[(x, y)] = {'top': TOP_C, 'left': LEFT_C, 'right': RIGHT_C}[f]

# Face on the front-left face — placed inside the plane's measured INTERIOR
# (cells whose 4-neighbours are all left-face, i.e. excluding the ring that
# becomes outline). Measured: x 4..14, 11 interior rows per column, each
# column sliding down as x grows. Features are addressed as (column, row-index
# within that column) so nothing can land on the silhouette or on the centre
# edge no matter how the cube parameters change.
_col = {}
for (_x, _y), _f in cells.items():
    if _f != 'left':
        continue
    if all(cells.get((_x + dx, _y + dy)) == 'left'
           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
        _col.setdefault(_x, []).append(_y)
for _x in _col:
    _col[_x].sort()

XS = sorted(_col)                      # 4..14
X0 = XS[0]
NROW = min(len(v) for v in _col.values())


def _c(i):
    """Column i steps in from the interior's left edge."""
    return X0 + i


def _r(x, k):
    """Row k (0-based, top of the interior) inside column x."""
    return _col[x][k]


# Eyes: 2-wide, 4 rows tall, symmetric about the interior's middle column.
for _i in (1, 2):
    for _k in range(2, 6):
        color[(_c(_i), _r(_c(_i), _k))] = OUTLINE
for _i in (7, 8):
    for _k in range(2, 6):
        color[(_c(_i), _r(_c(_i), _k))] = OUTLINE
color[(_c(2), _r(_c(2), 2))] = WHITE          # sparkles
color[(_c(8), _r(_c(8), 2))] = WHITE
# ω smile between and below the eyes.
for _i, _k in ((3, 7), (4, 8), (5, 8), (6, 7)):
    color[(_c(_i), _r(_c(_i), _k))] = OUTLINE
# Blush: one column outboard of each eye, still inside the interior.
for _i, _k in ((0, 4), (0, 5), (9, 4), (9, 5)):
    color[(_c(_i), _r(_c(_i), _k))] = BLUSH
# Sheen sits on the TOP face (different plane) — unchanged.
for _s in ((11, 6), (10, 7), (9, 8)):
    color[_s] = SHEEN

# Run-length encode per row: consecutive same-color cells -> one rect
rects = []
for y in sorted({y for _, y in color}):
    xs = sorted(x for (x, yy) in color if yy == y)
    runs = []
    start = prev = xs[0]
    for x in xs[1:]:
        if x == prev + 1 and color[(x, y)] == color[(prev, y)]:
            prev = x
            continue
        runs.append((start, prev, color[(prev, y)]))
        start = prev = x
    runs.append((start, prev, color[(prev, y)]))
    for x0, x1, c in runs:
        rects.append(f'<rect x="{x0 * P}" y="{y * P}" width="{(x1 - x0 + 1) * P}" height="{P}" fill="{c}"/>')

svg = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" shape-rendering="crispEdges">\n'
    + '\n'.join(rects)
    + '\n</svg>\n'
)

import os
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'candidate-c-pixel.svg')
with open(out, 'w', encoding='utf-8') as fh:
    fh.write(svg)
print(f'wrote {out} ({len(svg)} bytes, {len(rects)} rects)')
