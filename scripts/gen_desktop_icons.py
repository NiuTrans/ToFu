#!/usr/bin/env python3
"""Generate platform icons (.ico, .icns) from the source logo.png.

Usage:
    python scripts/gen_desktop_icons.py

Outputs:
    static/icons/tofu.ico   — Windows multi-size icon (16–256px)
    static/icons/tofu.icns  — macOS icon (16–512px @1x/@2x)
    static/icons/installer/wizard-large.bmp — Inno Setup left panel (164×314)
    static/icons/installer/wizard-small.bmp — Inno Setup inner page (55×58)
    static/icons/installer/dmg-background.png    — DMG window art (600×400)
    static/icons/installer/dmg-background@2x.png — retina variant (1200×800)
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
    from PIL import Image, ImageChops, ImageDraw
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


# Inno Setup `modern` wizard geometry — these dimensions are part of the
# Inno contract, not a design choice: WizardImageFile is the left panel of
# the welcome/finish pages, WizardSmallImageFile the top-right of inner pages.
WIZARD_LARGE_SIZE = (164, 314)
WIZARD_SMALL_SIZE = (55, 58)

# Brand accent ramp (static/styles.css --accent / --accent-hover, dark theme).
_ACCENT_TOP = (0x7c, 0x66, 0xd4)
_ACCENT_BOTTOM = (0x55, 0x3c, 0xa8)


def _cut_out_background(src: Image.Image) -> Image.Image:
    """Return an RGBA copy with the EXTERIOR near-white background removed.

    logo.png is RGBA but fully opaque (alpha 255 everywhere) over a uniform
    ~254-white canvas. A global white threshold would also punch holes in the
    cube's interior specular highlight and leave antialiased fringes; a
    flood-fill seeded from the four corners removes ONLY the connected
    exterior region — the closed black outline keeps the interior untouched.
    thresh=24 sits far from both the cream face (~41 away) and any outline
    blend pixel, so the fill cannot leak across the border.
    """
    im = src.convert('RGB')
    sentinel = (255, 0, 255)
    for corner in ((0, 0), (im.width - 1, 0),
                   (0, im.height - 1), (im.width - 1, im.height - 1)):
        ImageDraw.floodfill(im, corner, sentinel, thresh=24)
    # Alpha from the sentinel WITHOUT getdata (deprecated in Pillow 12):
    # difference-against-sentinel is exactly (0,0,0) on sentinel pixels.
    diff = ImageChops.difference(im, Image.new('RGB', im.size, sentinel))
    alpha = diff.convert('L').point(lambda v: 0 if v == 0 else 255)
    out = im.convert('RGBA')
    out.putalpha(alpha)
    return out


def _icon_canvas(src: Image.Image) -> Image.Image:
    """Build the app-icon canvas: the floating cube (white canvas cut out),
    cropped to the artwork with a small uniform margin.

    Feeding the RAW source to the .ico/.icns writers bakes the opaque white
    canvas into every frame — the desktop icon then renders as a white plate
    behind the tofu (owner report 2026-08-03: "white circle around the
    icon"). Two details matter here:

    * RGB under fully-transparent pixels is replaced with a dark neutral.
      _cut_out_background leaves the magenta flood-fill sentinel there, and
      Pillow resizes RGBA WITHOUT premultiplying alpha — so the LANCZOS
      downscale to each icon frame would bleed a purple fringe into the
      cube's dark outline. The outline is near-black, so darkening the
      sub-pixel edge is invisible; magenta is not.
    * The canvas is cropped to the artwork bbox (+6% margin, squared on the
      longer side). The source carries ~30% whitespace, which would render
      the cube needlessly small on the desktop — and illegible in the 16px
      tray frame.
    """
    cut = _cut_out_background(src)
    alpha = cut.getchannel('A')
    transparent = alpha.point(lambda a: 255 if a == 0 else 0)
    rgb = Image.composite(
        Image.new('RGB', cut.size, (43, 39, 51)), cut.convert('RGB'),
        transparent)
    bbox = alpha.getbbox()
    if bbox:
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        side = max(bw, bh) + 2 * round(max(bw, bh) * 0.06)
        side = min(side, min(src.size))
        cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
        left = min(max(cx - side // 2, 0), src.width - side)
        top = min(max(cy - side // 2, 0), src.height - side)
        rgb = rgb.crop((left, top, left + side, top + side))
        alpha = alpha.crop((left, top, left + side, top + side))
    out = rgb.convert('RGBA')
    out.putalpha(alpha)
    return out


def gen_wizard_images(src: Image.Image, out_dir: str):
    """Generate the Inno Setup wizard bitmaps from the source logo.

    wizard-large.bmp: brand accent gradient with the logo on a soft light
    disc — the installer is the FIRST screen a Windows user ever sees of the
    product, and the default Inno blank panel was the only unbranded surface
    left in the install flow. wizard-small.bmp: logo on white (Inno inner
    pages are white). Both are RGB BMPs, the only format ISCC accepts here.
    """
    os.makedirs(out_dir, exist_ok=True)

    # ── Large panel: vertical accent gradient + disc + centered logo ──
    w, h = WIZARD_LARGE_SIZE
    img = Image.new('RGBA', (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        row = tuple(round(_ACCENT_TOP[i] + (_ACCENT_BOTTOM[i] - _ACCENT_TOP[i]) * t)
                    for i in range(3)) + (255,)
        for x in range(w):
            px[x, y] = row

    logo_size = 96
    logo_cx, logo_cy = w // 2, 78 + logo_size // 2
    disc = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse(
        [logo_cx - 62, logo_cy - 62, logo_cx + 62, logo_cy + 62],
        fill=(255, 255, 255, 46))
    img = Image.alpha_composite(img, disc)
    logo = _cut_out_background(src).resize((logo_size, logo_size),
                                           Image.LANCZOS)
    img.paste(logo, (logo_cx - logo_size // 2, logo_cy - logo_size // 2), logo)

    large_path = os.path.join(out_dir, 'wizard-large.bmp')
    img.convert('RGB').save(large_path, format='BMP')
    print(f'  ✓ {large_path} ({os.path.getsize(large_path) // 1024} KB)')

    # ── Small tile: logo on white (inner pages) ──
    sw, sh = WIZARD_SMALL_SIZE
    small = Image.new('RGBA', (sw, sh), (255, 255, 255, 255))
    s_logo = _cut_out_background(src).resize((44, 44), Image.LANCZOS)
    small.paste(s_logo, ((sw - 44) // 2, (sh - 44) // 2), s_logo)
    small_path = os.path.join(out_dir, 'wizard-small.bmp')
    small.convert('RGB').save(small_path, format='BMP')
    print(f'  ✓ {small_path} ({os.path.getsize(small_path) // 1024} KB)')


# DMG window geometry. The icon COORDINATES are pinned here AND in
# build-desktop.yml's create-dmg invocation (--icon / --app-drop-link) — the
# arrow in the artwork is drawn between these two points, so a coordinate
# changed on only one side makes the art point at empty space. The parity
# test in tests/test_desktop_build_workflow.py compares both sides.
DMG_BG_SIZE = (600, 400)
DMG_APP_ICON_POS = (150, 190)
DMG_DROP_ICON_POS = (450, 190)

# Warm paper ramp (static/styles.css [data-theme="light"] --bg-primary/-secondary).
_PAPER_TOP = (0xf4, 0xf2, 0xed)
_PAPER_BOTTOM = (0xe3, 0xe1, 0xda)
_DMG_ACCENT = (0x63, 0x66, 0xf1)  # light-theme --accent


def _dmg_background(src: Image.Image, scale: int) -> Image.Image:
    """Render the DMG window art at ``scale`` (1 or 2 for retina).

    Warm paper gradient + the brand cube + a drag-guidance arrow pointing
    from the app-icon position to the Applications-drop position — the ONE
    instruction a DMG window exists to give, previously implied by nothing
    (the default white Finder window left users to guess the drag).
    """
    w, h = DMG_BG_SIZE[0] * scale, DMG_BG_SIZE[1] * scale
    img = Image.new('RGBA', (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        row = tuple(round(_PAPER_TOP[i] + (_PAPER_BOTTOM[i] - _PAPER_TOP[i]) * t)
                    for i in range(3)) + (255,)
        for x in range(w):
            px[x, y] = row

    draw = ImageDraw.Draw(img)
    app_x, app_y = DMG_APP_ICON_POS[0] * scale, DMG_APP_ICON_POS[1] * scale
    drop_x, _ = DMG_DROP_ICON_POS[0] * scale, DMG_DROP_ICON_POS[1] * scale

    # Drag arrow between the two icon centres (clear of the 100px icons).
    x0, x1, ay = app_x + 80 * scale, drop_x - 90 * scale, app_y
    width = max(2, 3 * scale)
    draw.line([(x0, ay), (x1, ay)], fill=_DMG_ACCENT + (255,), width=width)
    head = 10 * scale
    draw.polygon([(x1, ay - head), (x1, ay + head), (x1 + head * 2, ay)],
                 fill=_DMG_ACCENT + (255,))

    # Brand cube above the arrow, centred between the icons.
    logo = _cut_out_background(src).resize((72 * scale, 72 * scale),
                                           Image.LANCZOS)
    img.paste(logo, ((app_x + drop_x) // 2 - 36 * scale,
                     ay - 120 * scale), logo)
    return img


def gen_dmg_background(src: Image.Image, out_dir: str):
    """Emit dmg-background.png and its @2x retina sibling (create-dmg picks
    the @2x file up automatically when it sits next to the 1x file)."""
    os.makedirs(out_dir, exist_ok=True)
    for scale, name in ((1, 'dmg-background.png'),
                        (2, 'dmg-background@2x.png')):
        path = os.path.join(out_dir, name)
        _dmg_background(src, scale).convert('RGB').save(path, format='PNG')
        print(f'  ✓ {path} ({os.path.getsize(path) // 1024} KB)')


def main():
    if not os.path.isfile(SOURCE_PNG):
        print(f'ERROR: Source logo not found: {SOURCE_PNG}')
        sys.exit(1)

    src = Image.open(SOURCE_PNG).convert('RGBA')
    print(f'Source: {SOURCE_PNG} ({src.width}×{src.height})')

    # The app icons are built from the cut-out + cropped canvas: the raw
    # source bakes the opaque white canvas into every frame (the "white
    # plate" desktop icon). Wizard/DMG art place the logo on their own
    # backgrounds, so they keep using the full-canvas cutout.
    icon_src = _icon_canvas(src)

    ico_path = os.path.join(ICONS_DIR, 'tofu.ico')
    gen_ico(icon_src, ico_path)

    icns_path = os.path.join(ICONS_DIR, 'tofu.icns')
    try:
        gen_icns(icon_src, icns_path)
    except Exception as e:
        print(f'  ! ICNS generation failed ({e}) — run on macOS or use iconutil')

    gen_wizard_images(src, os.path.join(ICONS_DIR, 'installer'))
    gen_dmg_background(src, os.path.join(ICONS_DIR, 'installer'))


if __name__ == '__main__':
    main()
