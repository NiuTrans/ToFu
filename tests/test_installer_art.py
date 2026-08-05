"""tests/test_installer_art.py — the wizard page art contract.

The custom NSIS wizard (2026-08-04 redesign) renders every page as one
full-page bitmap + real controls on top. Two failure modes this suite
pins:

1. **The art itself** — four BMP pages at the contract size, purple band
   on top, white body, #F0F0F0 cards (the card color MUST equal Windows
   COLOR_3DFACE: labels paint that color behind themselves, so an
   off-by-one card color frames every label in a visible box).
2. **The geometry contract** — the template places labels/controls in
   dialog units; the cards are baked into the bitmaps at dialog-unit
   coordinates from installer_art. If a control's du rect ever falls
   OUTSIDE its card, the label paints gray-on-white patches (the exact
   2000s look the redesign killed). Parsed from the RENDERED scripts so
   the agent's injected autostart checkbox is covered too.

Run:  pytest tests/test_installer_art.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from lib.desktop_dist import installer_art, winbuilder as wb

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def art(tmp_path_factory):
    out = tmp_path_factory.mktemp('art')
    return installer_art.render(str(out), 'Tofu Agent', '0.16.0',
                                autostart=True)


def _px(img, fx, fy):
    """Pixel at fractional coordinates."""
    return img.getpixel((int(img.width * fx), int(img.height * fy)))


# ═══════════════════════════════════════════════════════════════════
#  The art itself
# ═══════════════════════════════════════════════════════════════════

def test_four_pages_at_contract_size(art):
    assert set(art) == {'welcome', 'directory', 'progress', 'finish'}
    for path in art.values():
        with Image.open(path) as img:
            assert img.format == 'BMP'
            assert img.size == installer_art.PAGE_PX
            assert img.mode == 'RGB'


def test_band_body_card_colors(art):
    for name, path in art.items():
        img = Image.open(path).convert('RGB')
        # Purple band at the top (blue-dominant violet).
        r, g, b = _px(img, 0.7, 0.05)
        assert b > 150 and r > 70 and g < 130, (name, (r, g, b))
        # White body strip between band and first card (y ≈ 0.34).
        assert _px(img, 0.5, 0.335) == (255, 255, 255), name
        # Card center is EXACTLY COLOR_3DFACE.
        assert _px(img, 0.5, 0.65) == (240, 240, 240), name


def test_card_color_is_color_3dface():
    """The ONE color rule of the whole design — see module docstring."""
    assert installer_art._CARD == (240, 240, 240)


def test_product_name_is_baked_in_the_band(tmp_path):
    a = installer_art.render(str(tmp_path / 'a'), 'Tofu', '1.0.0')
    b = installer_art.render(str(tmp_path / 'b'), 'Tofu Agent', '1.0.0')
    band_a = Image.open(a['welcome']).convert('RGB') \
        .crop((0, 0, installer_art.PAGE_PX[0], 100))
    band_b = Image.open(b['welcome']).convert('RGB') \
        .crop((0, 0, installer_art.PAGE_PX[0], 100))
    assert band_a.tobytes() != band_b.tobytes(), (
        'the band stopped carrying the product name — the pages are '
        'supposed to differ per target')


def test_autostart_flag_changes_only_the_directory_page(tmp_path):
    on = installer_art.render(str(tmp_path / 'on'), 'Tofu Agent', '1.0.0',
                              autostart=True)
    off = installer_art.render(str(tmp_path / 'off'), 'Tofu Agent',
                               '1.0.0', autostart=False)
    for page in ('welcome', 'progress', 'finish'):
        assert open(on[page], 'rb').read() == open(off[page], 'rb').read()
    assert open(on['directory'], 'rb').read() != \
        open(off['directory'], 'rb').read(), (
            'the agent directory page lost its autostart checkbox card')


# ═══════════════════════════════════════════════════════════════════
#  The geometry contract: every control sits on a card
# ═══════════════════════════════════════════════════════════════════

_FULL = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'full',
                       art_dir='/art')
_AGENT = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'agent',
                        art_dir='/art')

_PAGE_CARDS = {
    'WelcomePageCreate': [installer_art.CARD_MAIN_DU],
    'DirPageCreate': [installer_art.CARD_DIR_DU,
                      installer_art.CARD_DIR_THIN_DU],
    'ProgressPageCreate': [installer_art.CARD_MAIN_DU],
    'FinishPageCreate': [installer_art.CARD_FINISH_DU,
                         installer_art.CARD_FINISH_THIN_DU],
    'un.ConfirmPageCreate': [installer_art.CARD_MAIN_DU],
    'un.ProgressPageCreate': [installer_art.CARD_MAIN_DU],
    'un.FinishPageCreate': [installer_art.CARD_FINISH_DU,
                            installer_art.CARD_FINISH_THIN_DU],
}

_RECT_PATTERNS = (
    re.compile(r'TOFU_LABEL (\d+)u (\d+)u (\d+)u (\d+)u'),
    re.compile(r'\$\{NSD_Create(?:Label|CheckBox|DirRequest|BrowseButton|'
               r'ProgressBar)\} (\d+)u (\d+)u (\d+)u (\d+)u'),
)


def _controls_in(page_src: str):
    for pattern in _RECT_PATTERNS:
        for m in pattern.finditer(page_src):
            x, y, w, h = (int(g) for g in m.groups())
            yield (x, y, x + w, y + h)


def _inside(rect, card):
    x0, y0, x1, y1 = rect
    cx0, cy0, cx1, cy1 = card
    return cx0 <= x0 and cy0 <= y0 and x1 <= cx1 and y1 <= cy1


def test_every_control_sits_on_a_card():
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page, cards in _PAGE_CARDS.items():
            marker = f'Function {page}'
            assert marker in script, (target, page)
            body = script.split(marker)[1].split('FunctionEnd')[0]
            for rect in _controls_in(body):
                assert any(_inside(rect, card) for card in cards), (
                    f'{target} {page}: control {rect} falls outside every '
                    f'card {cards} — a label there paints gray-on-white '
                    'patches (the 2000s look the redesign killed)')


def test_page_du_matches_the_art_space():
    """The template's du coordinates only align with the bitmaps if both
    sides assume the SAME page size (266×130 — the exehead's inner page)."""
    assert installer_art.PAGE_DU == (266, 130)
    # The largest du coordinate the template uses must fit the page.
    for script in (_FULL, _AGENT):
        for pattern in _RECT_PATTERNS:
            for m in pattern.finditer(script):
                x, y, w, h = (int(g) for g in m.groups())
                assert x + w <= installer_art.PAGE_DU[0]
                assert y + h <= installer_art.PAGE_DU[1]
