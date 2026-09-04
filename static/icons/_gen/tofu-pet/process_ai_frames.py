#!/usr/bin/env python3
"""process_ai_frames.py — turn the AI-generated raw poses into shipped frames.

PIPELINE (raw 1024² chroma-green PNG → shipped transparent PNG)
───────────────────────────────────────────────────────────────
The raw poses live beside this script at ai/raw_<pose>.png (generated with the
image tool, one img2img edit per pose off ai/hero_v1.png — see JOURNAL
2026-07-30). Each raw file is a cream tofu cube on a near-pure green backdrop.

★ ONE GLOBAL SCALE, ONE SHARED ANCHOR (2026-07-31 rewrite — read this before
  touching any step). The previous pipeline trimmed EACH frame to its OWN alpha
  bbox and scaled it so ITS OWN longest side hit MAX_SIDE. That makes the scale
  factor a FUNCTION OF THE POSE, which broke the character in three separate
  ways, all measured on the shipped frames:

    · SIZE WOBBLE. Scale spanned 0.1837–0.2319 (26%). A rigid facial feature
      (blush cheek-to-cheek span) therefore varied 16.5% across the frames that
      share the character's stance, and 9.7% within walk1..4 alone — i.e. the
      character breathed a tenth of its own width 13 times a second while
      walking. Read as "glitching". (Same-stance is the population that matters:
      scratch1/scratch2 are deliberate stretch/squash extremes whose size SHOULD
      differ. Over all 22 frames the figure was 73.8% before / 50.0% after, but
      most of that is pose amplitude the artist drew, not wobble — see the
      scale-fidelity note below.)
    · LATERAL TELEPORT. Re-centring on the INK bbox means asymmetric FX (the
      thinking bubble, alert/celebrating sparkles) shove the BODY sideways: the
      body centre sat −2.20..+0.06 px off sprite centre, an 8%-of-width jump
      fired by a mere MOOD change, with nothing actually moving.
    · FX PAY FOR THEMSELVES IN BODY SIZE. A pose whose FX enlarged the ink box
      got scaled DOWN to fit, shrinking the body to make room for its sparkles.

  The raw masters are NOT at fault and must not be redrawn: body width across
  walk1..4 varies only 1.2% in the raws vs 6.4% shipped. The defect was 100%
  downstream, so the fix is here.

  THE CORRECTNESS CRITERION IS SCALE FIDELITY, NOT SIZE EQUALITY. Poses are
  allowed to differ in size — the artist drew scratch1 turned/compressed and
  scratch2 squashed wide. What must hold is that every shipped frame is its
  master times the SAME constant: measured shipped/master body-width ratio is
  0.17113..0.17288 against S=0.171674 (max 0.70% deviation), and the two
  extreme poses deviate +0.10% / -0.31% — LESS than the ordinary frames — so
  their 0.56 and 1.56 aspect ratios are handled correctly rather than being
  mangled. Any size difference that survives this check is the drawings'.

  Both fixes are one idea: measure the CHARACTER, not the ink. For every frame
  we locate the BODY (the largest opaque connected component — the tofu cube,
  which excludes detached FX) and derive two anchors from it: the body's centre
  X and the ink's foot line Y. Then ONE scale, computed once from the extremes
  across ALL frames, maps every raw into an IDENTICAL canvas with those anchors
  at fixed coordinates. Size constancy and registration are then STRUCTURAL —
  properties of the layout, not a coincidence that holds until someone adds a
  pose with bigger sparkles.

For every ENGINE frame name in FRAME_SOURCES we:

  1. CHROMA-KEY the green out. Distance in "greenness" space
     (g - max(r, b)) with a soft ramp, so the dark outline keeps its edge
     instead of being eaten, then DESPILL (clamp g to max(r, b)) so the cream
     body doesn't carry a green fringe.
  2. MEASURE the anchors: body centre X (largest opaque component, so FX are
     excluded) and foot line Y (bottom of ALL ink — the feet are the lowest
     thing drawn, and a cast shadow would key out with the backdrop).
  3. COMPOSITE onto the SHARED canvas at ONE global scale, with body centre on
     the canvas midline and the foot line on the canvas bottom. No per-frame
     trim, no per-frame normalisation. The canvas is symmetric about the body
     centre so the CSS facing mirror (scaleX) pivots on the body, not on
     wherever this pose's FX happened to land.
  4. DERIVED frames: walk5..8 replay walk1..4 (the stride's second half-cycle
     is the same four drawings), groom1/3 are ±6° rotations of groom2 (the
     wobble the pose ticker plays).

Because every frame now shares one canvas, `object-fit:contain` in the CSS is a
NO-OP scale (all frames identical size) rather than a per-frame renormalisation
— which is precisely why the wobble is gone rather than merely reduced.

USAGE
─────
    python3 static/icons/_gen/tofu-pet/process_ai_frames.py            # write frames
    python3 static/icons/_gen/tofu-pet/process_ai_frames.py --check    # CI gate
    python3 static/icons/_gen/tofu-pet/process_ai_frames.py --out-dir DIR   # write/check DIR instead

Output: static/icons/pet/tofu/tofu-<frame>.png (a PRODUCT asset dir).
This workbench lives under _gen/; it is never served.
"""
import argparse
import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[4]
# The 1024² raw poses (~1MB each) are review-only master art: they live under a
# `_candidates` dir segment so export.py strips them from every shipped build
# (the existing 'raw AI-gen asset candidates' exclusion), while this pipeline
# and the shipped frames still ride along.
RAW_DIR = Path(__file__).resolve().parent / '_candidates' / 'ai'
OUT_DIR = REPO / 'static' / 'icons' / 'pet' / 'tofu'

MAX_SIDE = 160          # px cap on the SHARED canvas's longest edge
KEY_T0, KEY_T1 = 28, 78  # greenness ramp: <=T0 opaque, >=T1 transparent
# Opacity above which a pixel counts as "the character" when measuring anchors.
# Deliberately well above the keyer's soft-ramp tail so a halo of half-keyed
# green fringe cannot drag the body centre or the foot line.
INK_A = 60

# engine frame name → (raw pose file, rotate_deg)
FRAME_SOURCES = {
    'idle':        ('hero_v1', 0),
    'happy':       ('raw_happy', 0),
    'sleepy':      ('raw_sleepy', 0),
    'sleeping':    ('raw_sleeping', 0),
    'thinking':    ('raw_thinking', 0),
    'surprised':   ('raw_surprised', 0),
    'sad':         ('raw_sad', 0),
    'celebrating': ('raw_celebrating', 0),
    'alert':       ('raw_alert', 0),
    'walk1':       ('raw_walk1', 0),
    'walk2':       ('raw_walk2', 0),
    'walk3':       ('raw_walk3', 0),
    'walk4':       ('raw_walk4', 0),
    'walk5':       ('raw_walk1', 0),
    'walk6':       ('raw_walk2', 0),
    'walk7':       ('raw_walk3', 0),
    'walk8':       ('raw_walk4', 0),
    'groom1':      ('raw_groom', -6),
    'groom2':      ('raw_groom', 0),
    'groom3':      ('raw_groom', 6),
    'scratch1':    ('raw_scratch1', 0),
    'scratch2':    ('raw_scratch2', 0),
}


def _key_out_green(im):
    """RGBA image with the chroma-green backdrop made transparent + despilled."""
    import numpy as np
    a = np.asarray(im.convert('RGBA')).astype(np.int16)
    r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    greenness = g - np.maximum(r, b)
    fade = np.clip((KEY_T1 - greenness) / (KEY_T1 - KEY_T0), 0.0, 1.0)
    al = (al * fade).astype(np.int16)
    # despill: no surviving pixel may be greener than it is red/blue
    g = np.where(al > 0, np.minimum(g, np.maximum(r, b)), g)
    out = np.stack([r, g, b, al], axis=-1).astype(np.uint8)
    return Image.fromarray(out, 'RGBA')


def _keyed(src_path, rotate_deg):
    """Keyed + rotated RGBA for one raw pose, cropped to its ink (no scaling).

    The crop is a pure translation — it removes empty backdrop only, so it
    cannot change the character's size. All sizing happens once, globally, in
    _layout(); this function must never scale.
    """
    im = _key_out_green(Image.open(src_path))
    bbox = im.getbbox()
    if not bbox:
        raise SystemExit(f'ERROR: {src_path.name} keyed to nothing — thresholds off?')
    im = im.crop(bbox)
    if rotate_deg:
        im = im.rotate(rotate_deg, resample=Image.BICUBIC, expand=True)
        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
    return im


def _largest_component(solid):
    """Boolean mask of the largest 4-connected component of ``solid``.

    Pure-numpy stand-in for ``scipy.ndimage.label`` + ``ndimage.sum`` so the
    pipeline stays runnable without the optional scipy dependency (scipy is
    deliberately absent from the public CI footprint — the test suite skips its
    own body-mask proofs via ``_requires_scipy``). 4-connectivity matches
    ndimage.label's default structure, and the tofu body is by far the largest
    component, so the body mask is identical either way.
    """
    import numpy as np
    from collections import deque

    h, w = solid.shape
    visited = np.zeros((h, w), dtype=bool)
    best_ys = best_xs = None
    best_size = 0
    for sy, sx in zip(*np.nonzero(solid)):
        if visited[sy, sx]:
            continue
        queue = deque([(sy, sx)])
        visited[sy, sx] = True
        cy, cx = [sy], [sx]
        while queue:
            y, x = queue.popleft()
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and solid[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    queue.append((ny, nx))
                    cy.append(ny)
                    cx.append(nx)
        if len(cy) > best_size:
            best_size = len(cy)
            best_ys, best_xs = cy, cx
    if best_ys is None:
        raise SystemExit('ERROR: no connected component found in frame')
    mask = np.zeros((h, w), dtype=bool)
    mask[best_ys, best_xs] = True
    return mask


def _anchors(im):
    """(body_cx, foot_y, ink_left, ink_right, ink_top) in this image's own px.

    body_cx is the horizontal centre of the BODY — the largest opaque connected
    component — NOT of the ink. That distinction is the whole point: the ink box
    includes detached FX (the thinking bubble, alert/celebrating sparkles) which
    sit off to one side, so centring on ink shifts the body by a pose-dependent
    amount and the pet appears to jump sideways when its mood changes.

    foot_y is the bottom of ALL ink: the feet are the lowest thing drawn, and
    the raws carry no cast shadow (it keys out with the backdrop), so the ink
    bottom IS the foot line. Anchoring it puts every pose's feet on one ground
    line, which is what makes a squash pose settle instead of hover.
    """
    import numpy as np

    a = np.asarray(im)
    solid = a[..., 3] > INK_A
    if not solid.any():
        raise SystemExit('ERROR: frame has no opaque ink — keyer thresholds off?')
    body = _largest_component(solid)
    bys, bxs = np.nonzero(body)
    iys, ixs = np.nonzero(solid)
    return ((bxs.min() + bxs.max()) / 2.0,
            float(iys.max()), float(ixs.min()), float(ixs.max()), float(iys.min()))


def _layout(keyed):
    """One global scale + one canvas size for ALL frames.

    Derived from the extremes across every frame so nothing is ever clipped:
    the canvas must reach the furthest ink LEFT and RIGHT of any body centre,
    and the furthest ink ABOVE any foot line. It is made SYMMETRIC about the
    body centre (half-width = max of the two reaches) because the CSS facing
    flip is scaleX on the whole frame: on an asymmetric canvas that mirror
    would pivot on the canvas midline rather than on the body, translating the
    character sideways every time it turns.

    Returns (scale, canvas_w, canvas_h, half_w_raw).
    """
    reach_l = max(bcx - ix0 for bcx, _fy, ix0, _ix1, _iy0 in keyed.values())
    reach_r = max(ix1 - bcx for bcx, _fy, _ix0, ix1, _iy0 in keyed.values())
    height = max(fy - iy0 for _bcx, fy, _ix0, _ix1, iy0 in keyed.values())
    half = max(reach_l, reach_r)
    scale = MAX_SIDE / max(2.0 * half, height)
    return scale, round(2.0 * half * scale), round(height * scale), half


def _render_all():
    """Two passes: measure every frame's anchors, then composite them all onto
    the ONE canvas the measurements imply. Two passes are required — the global
    scale cannot be known until every frame has been measured."""
    keyed, metrics = {}, {}
    for name, (raw, deg) in FRAME_SOURCES.items():
        im = _keyed(RAW_DIR / f'{raw}.png', deg)
        keyed[name] = im
        metrics[name] = _anchors(im)

    scale, cw, ch, half = _layout(metrics)

    out = {}
    for name, im in keyed.items():
        bcx, foot_y, _ix0, _ix1, _iy0 = metrics[name]
        scaled = im.resize((max(1, round(im.width * scale)),
                            max(1, round(im.height * scale))), Image.LANCZOS)
        canvas = Image.new('RGBA', (cw, ch), (0, 0, 0, 0))
        # body centre → canvas midline; foot line → canvas bottom row.
        dx = round(cw / 2.0 - bcx * scale)
        dy = round(ch - foot_y * scale)
        canvas.alpha_composite(scaled, (dx, dy))
        out[name] = canvas
    return out


def _same(a_path, im):
    if not a_path.exists():
        return False
    ref = Image.open(a_path)
    if ref.size != im.size:
        return False
    return ref.convert('RGBA').tobytes() == im.convert('RGBA').tobytes()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='verify on-disk frames match this pipeline (CI gate)')
    ap.add_argument('--out-dir', type=Path, default=OUT_DIR,
                    help='directory to write/check frames in '
                         f'(default: {OUT_DIR.relative_to(REPO)})')
    args = ap.parse_args()
    out_dir = args.out_dir

    frames = _render_all()
    if args.check:
        drift = [n for n, im in frames.items()
                 if not _same(out_dir / f'tofu-{n}.png', im)]
        if drift:
            print(f'DRIFT: {len(drift)} frame(s) differ from the pipeline: '
                  f'{", ".join(sorted(drift))}', file=sys.stderr)
            print('Re-run: python3 static/icons/_gen/tofu-pet/process_ai_frames.py',
                  file=sys.stderr)
            return 1
        print(f'OK: all {len(frames)} frames match the pipeline.')
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, im in frames.items():
        im.save(out_dir / f'tofu-{name}.png', optimize=True)
    try:
        shown = out_dir.relative_to(REPO)
    except ValueError:
        shown = out_dir
    print(f'Wrote {len(frames)} frames → {shown}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
