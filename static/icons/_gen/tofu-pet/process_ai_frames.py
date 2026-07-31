#!/usr/bin/env python3
"""process_ai_frames.py — turn the AI-generated raw poses into shipped frames.

PIPELINE (raw 1024² chroma-green PNG → shipped transparent PNG)
───────────────────────────────────────────────────────────────
The raw poses live beside this script at ai/raw_<pose>.png (generated with the
image tool, one img2img edit per pose off ai/hero_v1.png — see JOURNAL
2026-07-30). Each raw file is a cream tofu cube on a near-pure green backdrop.
For every ENGINE frame name in FRAME_SOURCES we:

  1. CHROMA-KEY the green out. Distance in "greenness" space
     (g - max(r, b)) with a soft ramp, so the dark outline keeps its edge
     instead of being eaten, then DESPILL (clamp g to max(r, b)) so the cream
     body doesn't carry a green fringe.
  2. TRIM to the alpha bounding box — NO padding. The engine's CSS
     (object-fit:contain) bottom-aligns the art in the pet's box, so the feet
     of every frame share one baseline and squash/stretch poses keep their
     height contrast. Horizontal centring is what the facing mirror flips
     about, so the trim is re-centred, not left where the raw put it.
  3. DOWNSCALE so the longest side is ≤ MAX_SIDE (the pet ships at 30px; 3×
     that plus headroom for CSS filters).
  4. DERIVED frames: walk5..8 replay walk1..4 (the stride's second half-cycle
     is the same four drawings), groom1/3 are ±6° rotations of groom2 (the
     wobble the pose ticker plays).

USAGE
─────
    python3 static/icons/_gen/tofu-pet/process_ai_frames.py            # write frames
    python3 static/icons/_gen/tofu-pet/process_ai_frames.py --check    # CI gate

Output: static/icons/pet/tofu/tofu-<frame>.png (a PRODUCT asset dir).
This workbench lives under _gen/; it is never served.
"""
import argparse
import math
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

MAX_SIDE = 160          # px cap on a frame's longest edge
KEY_T0, KEY_T1 = 28, 78  # greenness ramp: <=T0 opaque, >=T1 transparent

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


def _process(src_path, rotate_deg):
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
    if max(im.size) > MAX_SIDE:
        s = MAX_SIDE / max(im.size)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                       Image.LANCZOS)
    return im


def _render_all():
    out = {}
    for name, (raw, deg) in FRAME_SOURCES.items():
        out[name] = _process(RAW_DIR / f'{raw}.png', deg)
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
    args = ap.parse_args()

    frames = _render_all()
    if args.check:
        drift = [n for n, im in frames.items()
                 if not _same(OUT_DIR / f'tofu-{n}.png', im)]
        if drift:
            print(f'DRIFT: {len(drift)} frame(s) differ from the pipeline: '
                  f'{", ".join(sorted(drift))}', file=sys.stderr)
            print('Re-run: python3 static/icons/_gen/tofu-pet/process_ai_frames.py',
                  file=sys.stderr)
            return 1
        print(f'OK: all {len(frames)} frames match the pipeline.')
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, im in frames.items():
        im.save(OUT_DIR / f'tofu-{name}.png', optimize=True)
    print(f'Wrote {len(frames)} frames → {OUT_DIR.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
