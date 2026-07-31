"""Pixel-faithful tofu role family — the owner's '退回修改' revision direction.

The A2 geometric family was rejected together with the A2 main logo. This
generator keeps the CURRENT family's identity (pixel-art cube + the same prop
per role) but rebuilds it on a strict 32x32 grid (1 pixel = 2 viewBox units),
so every staircase is regular, every icon is symmetric, and the set survives
22px orchestration chips — unlike the VTracer traces in use today.

Cube machinery identical to gen_candidate_c.py (analytic 3-face classification,
1px silhouette + internal edges). Face = the main-logo pixel face (3x5 sparkle
eyes, v smile, blush, sheen) with per-role variants (worker grin/brows, critic
flat mouth/brow). Props are bold 4-10 cell pixel stamps in the role's colors.

Output: static/icons/_gen/logo-redesign/family-pixel/tofu-<role>.svg
"""
import os

P = 2
OUT = '#1F1C25'
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


def base_colors():
    color = {}
    for (x, y), f in cells.items():
        def at(dx, dy):
            return cells.get((x + dx, y + dy))
        outside = (at(1, 0) is None or at(-1, 0) is None
                   or at(0, 1) is None or at(0, -1) is None)
        internal = (f == 'top' and any(at(dx, dy) in ('left', 'right')
                                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))) \
            or (f == 'left' and at(1, 0) == 'right')
        if outside or internal:
            color[(x, y)] = OUT
        else:
            color[(x, y)] = {'top': TOP_C, 'left': LEFT_C, 'right': RIGHT_C}[f]
    return color


def stamp_face(color, mouth='v', brows=False, flat_mouth=False):
    for x in (6, 7, 8):
        for y in range(13, 18):
            color[(x, y)] = OUT
    for x in (11, 12, 13):
        for y in range(15, 20):
            color[(x, y)] = OUT
    color[(8, 13)] = WHITE
    color[(13, 15)] = WHITE
    if flat_mouth:
        for m in ((9, 20), (10, 20), (11, 20)):
            color[m] = OUT
    elif mouth == 'grin':
        for x in (9, 10, 11):
            for y in (19, 20):
                color[(x, y)] = WHITE
        for m in ((9, 18), (10, 18), (11, 18), (9, 21), (10, 21), (11, 21)):
            color[m] = OUT
    else:
        for m in ((9, 20), (10, 21), (11, 20)):
            color[m] = OUT
    if brows:
        for b in ((6, 11), (7, 12)):
            color[b] = OUT
        for b in ((13, 13), (14, 14)):
            color[b] = OUT
    for b in ((5, 15), (5, 16), (14, 17), (14, 18)):
        color[b] = BLUSH
    for s in ((12, 3), (11, 4), (10, 5)):
        color[s] = SHEEN


def put(color, pts, c):
    for pt in pts:
        color[pt] = c


NAVY, YELLOW, GOLD, WOOD, WOOD_D = '#3E4C8C', '#F5C542', '#C9A227', '#C89B6A', '#A97B4F'
GRAY, GREEN, TERM, PAPER, BLUE = '#9AA3AD', '#7EE787', '#2A2733', '#FFFFFF', '#4A7BD4'
GLASS = '#BFE3EA'

PROPS = {}


def p_planner(c):
    # beret on top face + clipboard at lower-left
    put(c, [(7, 5), (8, 5), (9, 5), (10, 5), (7, 6), (8, 6), (9, 6), (10, 6),
            (8, 4), (9, 4)], NAVY)
    put(c, [(8, 3)], NAVY)
    put(c, [(1, 22), (2, 22), (3, 22), (4, 22), (1, 23), (2, 23), (3, 23), (4, 23),
            (1, 24), (2, 24), (3, 24), (4, 24), (1, 25), (2, 25), (3, 25), (4, 25)], WOOD_D)
    put(c, [(2, 23), (3, 23), (2, 24), (3, 24)], PAPER)


def p_worker(c):
    # hard hat on top face + wrench lower-right
    put(c, [(18, 4), (19, 4), (20, 4), (21, 4), (17, 5), (18, 5), (19, 5), (20, 5),
            (21, 5), (22, 5), (16, 6), (17, 6), (18, 6), (19, 6), (20, 6), (21, 6),
            (22, 6), (23, 6), (15, 7), (16, 7), (17, 7), (18, 7), (19, 7), (20, 7),
            (21, 7), (22, 7), (23, 7), (24, 7)], YELLOW)
    put(c, [(25, 22), (28, 22), (26, 23), (27, 23), (26, 24), (27, 24),
            (26, 25), (27, 25), (26, 26), (27, 26), (26, 27), (27, 27)], GRAY)


def p_critic(c):
    # gold monocle ring over right eye + watch lower-right
    put(c, [(11, 14), (12, 14), (13, 14), (10, 15), (14, 15), (10, 16), (14, 16),
            (10, 17), (14, 17), (11, 18), (12, 18), (13, 18)], GOLD)
    put(c, [(24, 23), (25, 23), (26, 23), (24, 24), (26, 24), (24, 25), (25, 25),
            (26, 25), (25, 22)], GOLD)
    put(c, [(25, 24)], OUT)


def p_thinking(c):
    # thought cloud upper-right + dots
    put(c, [(22, 1), (23, 1), (24, 1), (21, 2), (25, 2), (20, 3), (26, 3),
            (21, 4), (25, 4), (22, 5), (23, 5), (24, 5)], OUT)
    put(c, [(22, 2), (23, 2), (24, 2), (21, 3), (23, 3), (25, 3), (22, 3), (24, 3),
            (22, 4), (23, 4), (24, 4)], WHITE)
    put(c, [(22, 3), (24, 3)], OUT)
    put(c, [(19, 6), (17, 7)], OUT)


def p_browser(c):
    # browser window lower-right
    put(c, [(x, y) for x in range(22, 29) for y in range(18, 24)], OUT)
    put(c, [(x, y) for x in range(22, 29) for y in range(18, 19)], GRAY)
    put(c, [(x, y) for x in range(23, 28) for y in range(20, 23)], PAPER)
    put(c, [(24, 21), (25, 21), (26, 21), (25, 20), (25, 22)], BLUE)


def p_researcher(c):
    # magnifier lower-left + open book lower-right
    put(c, [(2, 20), (3, 20), (1, 21), (4, 21), (1, 22), (4, 22), (2, 23), (3, 23)], OUT)
    put(c, [(2, 21), (3, 21), (2, 22), (3, 22)], GLASS)
    put(c, [(4, 23), (5, 24)], OUT)
    put(c, [(22, 24), (23, 24), (24, 24), (25, 24), (26, 24), (27, 24),
            (22, 25), (24, 25), (25, 25), (27, 25),
            (22, 26), (23, 26), (24, 26), (25, 26), (26, 26), (27, 26),
            (22, 27), (23, 27), (24, 27), (25, 27), (26, 27), (27, 27)], PAPER)
    put(c, [(24, 25), (25, 25), (25, 26)], OUT)


def p_analyst(c):
    # round glasses over both eyes + chart bars
    put(c, [(23, 22), (23, 23), (23, 24), (25, 21), (25, 22), (25, 23), (25, 24),
            (27, 20), (27, 21), (27, 22), (27, 23), (27, 24)], BLUE)
    put(c, [(22, 25), (23, 25), (24, 25), (25, 25), (26, 25), (27, 25), (28, 25)], GRAY)


def p_coder(c):
    # terminal lower-left
    put(c, [(x, y) for x in range(0, 8) for y in range(19, 25)], TERM)
    put(c, [(1, 20), (2, 20), (3, 20)], GRAY)
    put(c, [(1, 22), (2, 21), (1, 23)], GREEN)
    put(c, [(3, 23), (4, 23), (5, 23)], GREEN)


def p_general(c):
    # 4-point sparkle upper-right
    put(c, [(24, 2), (23, 3), (24, 3), (25, 3), (22, 4), (23, 4), (24, 4), (25, 4),
            (26, 4), (23, 5), (24, 5), (25, 5), (24, 6)], YELLOW)
    put(c, [(27, 6)], YELLOW)


def p_router(c):
    # signpost right side
    put(c, [(26, 15), (27, 15), (26, 16), (27, 16), (26, 17), (27, 17),
            (26, 18), (27, 18), (26, 19), (27, 19), (26, 20), (27, 20),
            (26, 21), (27, 21), (26, 22), (27, 22)], WOOD_D)
    put(c, [(22, 13), (23, 13), (24, 13), (25, 13), (26, 13), (27, 13), (28, 13),
            (24, 14), (25, 14), (26, 14)], WOOD)
    put(c, [(20, 17), (21, 17), (22, 17), (23, 17), (24, 17), (25, 17),
            (21, 18), (22, 18), (23, 18)], WOOD)


def p_synthesizer(c):
    # funnel lower-right
    put(c, [(22, 19), (23, 19), (24, 19), (25, 19), (26, 19), (27, 19)], OUT)
    put(c, [(23, 20), (24, 20), (25, 20), (26, 20)], PAPER)
    put(c, [(24, 21), (25, 21), (24, 22), (25, 22)], PAPER)
    put(c, [(24, 23), (25, 23)], OUT)
    put(c, [(22, 17), (27, 17)], OUT)
    put(c, [(24, 25), (25, 25)], YELLOW)


def p_writer(c):
    # paper lower-left + pencil diagonal
    put(c, [(1, 21), (2, 21), (3, 21), (4, 21), (5, 21),
            (1, 22), (5, 22), (1, 23), (5, 23), (1, 24), (5, 24), (1, 25), (5, 25),
            (2, 26), (3, 26), (4, 26), (5, 26)], OUT)
    put(c, [(2, 22), (3, 22), (4, 22), (2, 23), (3, 23), (4, 23),
            (2, 24), (3, 24), (4, 24), (2, 25), (3, 25), (4, 25)], PAPER)
    put(c, [(2, 23), (3, 23), (2, 24), (3, 24)], GRAY)
    put(c, [(6, 20), (7, 21), (8, 22), (9, 23)], YELLOW)
    put(c, [(9, 23)], OUT)


ROLES = {
    'planner': (p_planner, {}),
    'worker': (p_worker, {'mouth': 'grin', 'brows': True}),
    'critic': (p_critic, {'flat_mouth': True}),
    'thinking': (p_thinking, {}),
    'browser': (p_browser, {}),
    'researcher': (p_researcher, {}),
    'analyst': (p_analyst, {}),
    'coder': (p_coder, {}),
    'general': (p_general, {}),
    'router': (p_router, {}),
    'synthesizer': (p_synthesizer, {}),
    'writer': (p_writer, {}),
}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'family-pixel')
os.makedirs(OUT_DIR, exist_ok=True)
for role, (prop_fn, face_kw) in ROLES.items():
    color = base_colors()
    stamp_face(color, **face_kw)
    prop_fn(color)
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
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" shape-rendering="crispEdges">\n'
           + '\n'.join(rects) + '\n</svg>\n')
    out = os.path.join(OUT_DIR, f'tofu-{role}.svg')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(svg)
    print(f'{role}: {len(svg)}B')
print(f'wrote {len(ROLES)} pixel icons to {OUT_DIR}')
