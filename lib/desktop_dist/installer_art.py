"""lib/desktop_dist/installer_art.py — wrap-time NSIS wizard page art.

The modern installer wizard (desktop/installer.nsi.tmpl, the 2026-08-04
redesign) renders every page as ONE full-page bitmap + a handful of real
controls on top. This module paints those bitmaps at wrap time (Pillow,
server-side) so the art can carry the build's own facts — the product
name (Tofu / Tofu Agent) and the version — without committing per-target
bitmap blobs to the repo.

Why baked bitmaps instead of colored controls (measured 2026-08-04, see
JOURNAL): the conda-forge nsDialogs build ships NO control-coloring
mechanism (no SetCtlColors plugin, no WM_CTLCOLOR callback), so a label
always paints the dialog-default #F0F0F0 behind its text. The design
therefore observes ONE rule:

  * **Only short brand text is baked into art** ("Tofu Agent", the
    version) — distortion-tolerant under RESIZETOFIT.
  * **All sentences are real LangString labels** sitting on #F0F0F0
    "cards" that are part of the page bitmap. Cards are placed in dialog
    units and the labels use the SAME dialog-unit coordinates, so the
    two scale identically at any DPI or language-font metric (the CJK
    dialog-font trap) and the label background always matches the card.

The bitmap is drawn at 2x the estimated page size (the exehead's inner
page is 266x130 dialog units ≈ 399x211 px at 96 DPI) and the template
stretches it to fill (/RESIZETOFIT), so residual estimate error only
squishes flat-color geometry by a few percent — never text.

The card color MUST stay exactly COLOR_3DFACE (#F0F0F0): labels paint
that color behind themselves, and an off-by-one card color would frame
every label in a visible box.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from lib.log import get_logger

logger = get_logger(__name__)

# ── Geometry contract (dialog units; page = 266 × 130) ──────────────
# The template places its labels/controls with THESE SAME du values —
# tests/test_installer_art.py asserts the two sides agree.
PAGE_DU = (266, 130)
BAND_DU = (0, 0, 266, 38)            # purple gradient band
LOGO_DU = (14, 3, 46, 32.5)          # tofu logo inside the band
NAME_X_DU = 52                       # baked product name starts here
CARD_MAIN_DU = (10, 48, 256, 120)    # welcome / progress card
CARD_DIR_DU = (10, 48, 256, 102)     # directory card
CARD_DIR_THIN_DU = (10, 107, 256, 124)   # autostart checkbox card
CARD_FINISH_DU = (10, 48, 256, 102)  # finish card
CARD_FINISH_THIN_DU = (10, 107, 256, 124)  # launch checkbox card

# Control/label du rects consumed by the template (documented here so the
# geometry lives on BOTH sides of the contract and tests can pin it):
#   welcome:  title (20,56,236,13) sub (20,73,236,10) body (20,87,236,26)
#   dir:      title (20,54,236,10) edit (20,66,196,12) browse (220,66,28,12)
#   dir thin: checkbox (20,113,226,10)
#   progress: status (20,62,236,10) bar (20,96,226,8)
#   finish:   title (20,56,236,13) sub (20,73,236,10)
#   finish thin: checkbox (20,113,226,10)

# ── Pixel space ──────────────────────────────────────────────────────
# Estimated page ≈ 399×211 px at 96 DPI (MS Shell Dlg 8 base units
# (6,13)); rendered at 2x for HiDPI crispness under RESIZETOFIT.
EST_PAGE_PX = (399, 211)
SCALE = 2
PAGE_PX = (EST_PAGE_PX[0] * SCALE, EST_PAGE_PX[1] * SCALE)  # 798×422

# ── Palette (web UI tokens; card = COLOR_3DFACE exactly) ─────────────
_WHITE = (255, 255, 255)
_CARD = (240, 240, 240)          # #F0F0F0 — MUST equal COLOR_3DFACE
_BAND_TOP = (0x7c, 0x66, 0xd4)   # static/styles.css dark accent ramp
_BAND_BOTTOM = (0x55, 0x3c, 0xa8)
_BAND_LINE = (0xe6, 0xe1, 0xf7)  # hairline under the band

_CARD_RADIUS_PX = 12
_BAND_LINE_PX = 4

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
    """Map a dialog-unit rect to bitmap pixels (fractional space)."""
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
    """White page + purple gradient band + hairline."""
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


def _paint_card(img: Image.Image, rect_du) -> None:
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(_du_to_px(rect_du), radius=_CARD_RADIUS_PX,
                           fill=_CARD)


def _render_page(path: str, app_name: str, version: str,
                 cards: list) -> None:
    img = _base_page()
    _paint_band(img, app_name, version)
    for rect in cards:
        _paint_card(img, rect)
    img.save(path, format='BMP')


def render(out_dir: str, app_name: str, version: str,
           autostart: bool = False) -> dict:
    """Render the four wizard pages for one target into ``out_dir``.

    Returns {page_name: path}. The uninstaller reuses welcome/progress/
    finish (its confirm page has the welcome layout) — only the install
    directory page differs per target (the agent's autostart card).
    """
    os.makedirs(out_dir, exist_ok=True)
    pages = {}
    specs = {
        'welcome': [CARD_MAIN_DU],
        'directory': [CARD_DIR_DU] + ([CARD_DIR_THIN_DU] if autostart
                                      else []),
        'progress': [CARD_MAIN_DU],
        'finish': [CARD_FINISH_DU, CARD_FINISH_THIN_DU],
    }
    for name, cards in specs.items():
        path = os.path.join(out_dir, '%s.bmp' % name)
        _render_page(path, app_name, version, cards)
        pages[name] = path
    logger.info('[InstallerArt] rendered %d pages for %s v%s into %s',
                len(pages), app_name, version, out_dir)
    return pages


__all__ = ['render', 'PAGE_DU', 'PAGE_PX', 'CARD_MAIN_DU', 'CARD_DIR_DU',
           'CARD_DIR_THIN_DU', 'CARD_FINISH_DU', 'CARD_FINISH_THIN_DU']
