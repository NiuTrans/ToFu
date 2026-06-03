#!/usr/bin/env python3
"""Generate platform icons (.ico, .icns) from the source logo.png.

Usage:
    python scripts/gen_desktop_icons.py

Outputs:
    static/icons/tofu.ico   — Windows multi-size icon (16–256px)
    static/icons/tofu.icns  — macOS icon (16–512px @1x/@2x)
"""

import os
import sys

# Windows CI consoles default to cp1252, which can't encode the ✓/× glyphs
# in our status prints — that raises UnicodeEncodeError and fails the build.
# Force UTF-8 on the streams (Python 3.7+) so output is portable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

try:
    from PIL import Image
except ImportError:
    print('ERROR: Pillow is required. Install with: pip install Pillow')
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS_DIR = os.path.join(ROOT, 'static', 'icons')
SOURCE_PNG = os.path.join(ICONS_DIR, 'logo.png')

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]

ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def gen_ico(src: Image.Image, out_path: str):
    """Generate a multi-size .ico file."""
    src.save(out_path, format='ICO', sizes=ICO_SIZES)
    print(f'  ✓ {out_path} ({os.path.getsize(out_path) // 1024} KB)')


def gen_icns(src: Image.Image, out_path: str):
    """Generate an .icns file via Pillow (supports basic icns writing)."""
    sizes = [sz for sz in ICNS_SIZES if sz <= src.width]
    imgs = [src.resize((sz, sz), Image.LANCZOS) for sz in sizes]
    imgs[0].save(out_path, format='ICNS', append_images=imgs[1:])
    print(f'  ✓ {out_path} ({os.path.getsize(out_path) // 1024} KB)')


def main():
    if not os.path.isfile(SOURCE_PNG):
        print(f'ERROR: Source logo not found: {SOURCE_PNG}')
        sys.exit(1)

    src = Image.open(SOURCE_PNG).convert('RGBA')
    print(f'Source: {SOURCE_PNG} ({src.width}×{src.height})')

    ico_path = os.path.join(ICONS_DIR, 'tofu.ico')
    gen_ico(src, ico_path)

    icns_path = os.path.join(ICONS_DIR, 'tofu.icns')
    try:
        gen_icns(src, icns_path)
    except Exception as e:
        print(f'  ! ICNS generation failed ({e}) — run on macOS or use iconutil')


if __name__ == '__main__':
    main()
