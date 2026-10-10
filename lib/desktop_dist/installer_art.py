"""lib/desktop_dist/installer_art.py — wrap-time NSIS wizard header art.

The modern installer wizard (desktop/installer.nsi.tmpl) renders a HEADER
BAND bitmap (product name + version) plus real controls on the native
dialog face. This module paints that band at wrap time (Pillow) so the
art can carry the build's own facts — Tofu / Tofu Agent and the version
— without committing per-target bitmap blobs to the repo.

Why a band, not a full-page bitmap (measured 2026-08-05, 2026-08-06,
2026-09-09): a 100%×100% SS_BITMAP sibling overlaps every live control.
HWND_BOTTOM and opaque labels did not recover paint on real Windows —
the band and any baked cards showed, every LangString/edit/checkbox
stayed invisible. Live controls therefore sit BELOW the 38 du band on
COLOR_3DFACE and must never overlap it.

Only short brand text is baked into the band ("Tofu Agent", the version)
— distortion-tolerant under RESIZETOFIT. Every sentence is a LangString
so English/Chinese stay a runtime NLF choice.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from lib.log import get_logger

logger = get_logger(__name__)

# ── Geometry contract (dialog units; inner page = 266 × 130) ─────────
# The template places labels/controls with these same du values —
# tests/desktop_dist/test_installer_art.py asserts they sit BELOW the band.
PAGE_DU = (266, 130)
BAND_DU = (0, 0, 266, 38)            # purple gradient band (the bitmap)
BAND_HEIGHT_DU = 38
LOGO_DU = (14, 3, 46, 32.5)          # tofu logo inside the band
NAME_X_DU = 52                       # baked product name starts here

# Control/label du rects consumed by the template (documented here so
# the geometry lives on both sides of the contract):
#   welcome:  title (20,56,236,13) sub (20,73,236,10) body (20,87,236,26)
#   dir:      title (20,54,236,10) edit (20,66,196,12) browse (220,66,28,12)
#   dir thin: checkbox (20,113,226,10)
#   progress: status (20,62,236,10) bar (20,96,226,8)
#   finish:   title (20,56,236,13) sub (20,73,236,10)
#   finish thin: checkbox (20,113,226,10)

# ── Pixel space ──────────────────────────────────────────────────────
# Estimated full inner page ≈ 399×211 px at 96 DPI (MS Shell Dlg 8
# base units (6,13)); the BAND is rendered at 2x of its slice so
# RESIZETOFIT stays crisp. Residual estimate error only squishes the
# gradient, never the live LangString text.
EST_PAGE_PX = (399, 211)
SCALE = 2
PAGE_PX = (EST_PAGE_PX[0] * SCALE, EST_PAGE_PX[1] * SCALE)  # 798×422
_BAND_LINE_PX = 4
BAND_PX = (
    PAGE_PX[0],
    round(BAND_DU[3] / PAGE_DU[1] * PAGE_PX[1]) + _BAND_LINE_PX,
)

# ── Palette (web UI tokens) ──────────────────────────────────────────
_WHITE = (255, 255, 255)
_BAND_TOP = (0x7c, 0x66, 0xd4)
_BAND_BOTTOM = (0x55, 0x3c, 0xa8)
_BAND_LINE = (0xe6, 0xe1, 0xf7)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_LOGO = os.path.join(_ROOT, 'static', 'icons', 'logo.png')

# Baked text is Latin-only (the product name + version), so any decent
# bold sans works. Discovery order (first hit wins): fontconfig (present
# on every Linux with desktop fonts), the matplotlib-bundled DejaVu
# (its mpl-data ttf dir ships DejaVuSans-Bold.ttf), PIL's built-in
# bitmap font (ugly but never crashes a build).
_FONT_FAMILIES = ('DejaVu Sans:style=Bold',
                  'Liberation Sans:style=Bold',
                  'Noto Sans:style=Bold')


def _font_path() -> str | None:
    import shutil
    import subprocess
    fc_match = shutil.which('fc-match')
    if fc_match:
        for fam in _FONT_FAMILIES:
            try:
                out = subprocess.run([fc_match, '-f', '%{file}', fam],
                                     capture_output=True, text=True,
                                     timeout=10)
                path = out.stdout.strip()
                if out.returncode == 0 and path and os.path.isfile(path):
                    return path
            except Exception as e:
                logger.debug('[InstallerArt] fc-match %r failed: %s', fam, e)
                continue
    try:  # matplotlib bundles DejaVuSans-Bold.ttf in mpl-data
        import matplotlib
        path = os.path.join(os.path.dirname(matplotlib.__file__),
                            'mpl-data', 'fonts', 'ttf',
                            'DejaVuSans-Bold.ttf')
        if os.path.isfile(path):
            return path
    except Exception as e:
        logger.debug('[InstallerArt] matplotlib font probe failed: %s', e)
    return None


def _du_to_px(rect) -> tuple:
    """Map a dialog-unit rect onto the full-page pixel space."""
    x0, y0, x1, y1 = rect
    return (round(x0 / PAGE_DU[0] * PAGE_PX[0]),
            round(y0 / PAGE_DU[1] * PAGE_PX[1]),
            round(x1 / PAGE_DU[0] * PAGE_PX[0]),
            round(y1 / PAGE_DU[1] * PAGE_PX[1]))


def _find_font(size: int) -> ImageFont.FreeTypeFont:
    path = _font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError as e:
            logger.debug('[InstallerArt] truetype load of %s failed: %s',
                         path, e)
    logger.warning('[InstallerArt] no truetype font found; brand name '
                   'falls back to PIL bitmap font')
    return ImageFont.load_default()


def _cut_out_background(src: Image.Image) -> Image.Image:
    """Corner flood-fill cutout of the logo's white canvas.

    Same algorithm as scripts/gen_desktop_icons.py::_cut_out_background —
    duplicated on purpose: the CI script and the wrap-time renderer are
    independent build paths and a shared helper would couple them.
    """
    im = src.convert('RGB')
    sentinel = (255, 0, 255)
    for corner in ((0, 0), (im.width - 1, 0),
                   (0, im.height - 1), (im.width - 1, im.height - 1)):
        ImageDraw.floodfill(im, corner, sentinel, thresh=24)
    from PIL import ImageChops
    diff = ImageChops.difference(im, Image.new('RGB', im.size, sentinel))
    alpha = diff.convert('L').point(lambda v: 0 if v == 0 else 255)
    out = im.convert('RGBA')
    out.putalpha(alpha)
    return out


def _base_page() -> Image.Image:
    """White page + purple gradient band + hairline (cropped to the band)."""
    w, h = PAGE_PX
    img = Image.new('RGB', (w, h), _WHITE)
    band_h = round(BAND_DU[3] / PAGE_DU[1] * h)
    px = img.load()
    for y in range(band_h):
        t = y / max(1, band_h - 1)
        row = tuple(round(_BAND_TOP[i] +
                          (_BAND_BOTTOM[i] - _BAND_TOP[i]) * t)
                    for i in range(3))
        for x in range(w):
            px[x, y] = row
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, band_h, w, band_h + _BAND_LINE_PX], fill=_BAND_LINE)
    return img


def _paint_band(img: Image.Image, app_name: str, version: str) -> None:
    """Logo + product name + version inside the band (the baked text)."""
    rgba = img.convert('RGBA')
    lx0, ly0, lx1, ly1 = _du_to_px(LOGO_DU)
    logo_side = min(lx1 - lx0, ly1 - ly0)
    if os.path.isfile(_LOGO):
        src = Image.open(_LOGO).convert('RGBA')
        logo = _cut_out_background(src).resize(
            (logo_side, logo_side), Image.LANCZOS)
        disc = Image.new('RGBA', rgba.size, (0, 0, 0, 0))
        cx, cy = lx0 + logo_side // 2, ly0 + logo_side // 2
        r = round(logo_side * 0.68)
        ImageDraw.Draw(disc).ellipse([cx - r, cy - r, cx + r, cy + r],
                                     fill=(255, 255, 255, 46))
        rgba = Image.alpha_composite(rgba, disc)
        rgba.paste(logo, (lx0, cy - logo_side // 2), logo)
    draw = ImageDraw.Draw(rgba)
    band_h = round(BAND_DU[3] / PAGE_DU[1] * PAGE_PX[1])
    name_x = round(NAME_X_DU / PAGE_DU[0] * PAGE_PX[0])
    name_font = _find_font(round(band_h * 0.34))
    ver_font = _find_font(round(band_h * 0.20))
    name_h = draw.textbbox((0, 0), app_name, font=name_font)[3]
    ver_text = 'v%s' % version
    ver_h = draw.textbbox((0, 0), ver_text, font=ver_font)[3]
    total = name_h + 6 + ver_h
    top = (band_h - total) // 2 + round(band_h * 0.04)
    draw.text((name_x, top), app_name, font=name_font, fill=(255, 255, 255))
    draw.text((name_x + 2, top + name_h + 6), ver_text, font=ver_font,
              fill=(255, 255, 255, 191))
    img.paste(rgba.convert('RGB'), (0, 0))


def _crop_band(img: Image.Image) -> Image.Image:
    return img.crop((0, 0, BAND_PX[0], BAND_PX[1]))


def render(out_dir: str, app_name: str, version: str,
           autostart: bool = False) -> dict:
    """Render the four wizard header-band bitmaps into ``out_dir``.

    Returns {page_name: path}. The four filenames are kept so the
    template's File / TOFU_PAGE_ART mapping stays 1:1; the pixels are
    identical (the band does not vary per page). ``autostart`` is
    accepted for call-site compatibility and does not change the art —
    the agent's autostart checkbox is a live control below the band.
    """
    del autostart
    os.makedirs(out_dir, exist_ok=True)
    img = _base_page()
    _paint_band(img, app_name, version)
    band = _crop_band(img)
    pages = {}
    for name in ('welcome', 'directory', 'progress', 'finish'):
        path = os.path.join(out_dir, '%s.bmp' % name)
        band.save(path, format='BMP')
        pages[name] = path
    logger.info('[InstallerArt] rendered %d header bands for %s v%s into %s',
                len(pages), app_name, version, out_dir)
    return pages


__all__ = ['render', 'PAGE_DU', 'PAGE_PX', 'BAND_DU', 'BAND_PX',
           'BAND_HEIGHT_DU']
