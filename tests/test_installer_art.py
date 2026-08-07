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


# ═══════════════════════════════════════════════════════════════════
#  The Z-order contract: the art is the BACKGROUND
# ═══════════════════════════════════════════════════════════════════
# The 2026-08-05 blank-wizard failure class (owner's real-machine
# acceptance of the 2026-08-04 redesign): the band+cards rendered (the
# baked bitmap) while EVERY live control — labels, the path edit, the
# browse button, the checkboxes — stayed invisible. A full-page art
# control that sits ABOVE a sibling in Z-order starves that sibling of
# WM_PAINT entirely, so "the art is the bottom-most control" must be an
# EXPLICIT runtime invariant, not an implicit creation-order assumption.

def test_page_art_macro_pins_bitmap_to_z_bottom():
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    macro = tmpl.split('!macro TOFU_PAGE_ART')[1].split('!macroend')[0]
    assert 'SetWindowPos' in macro, (
        'TOFU_PAGE_ART lost its HWND_BOTTOM SetWindowPos — the art must '
        'be pinned to the bottom of the Z-order explicitly; creation '
        'order alone is an assumption nobody can see (2026-08-05 blank '
        'wizard: band+cards visible, every control dead)')


def test_art_is_created_before_any_control_on_every_page():
    """Companion belt to the HWND_BOTTOM pin: the creation order stays
    art-first too, so the background invariant holds on two levels."""
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page in _PAGE_CARDS:
            body = script.split(f'Function {page}')[1] \
                         .split('FunctionEnd')[0]
            art_at = body.find('!insertmacro TOFU_PAGE_ART')
            assert art_at != -1, (target, page)
            first_ctl = min(
                (i for i in (body.find('${NSD_Create'),
                             body.find('!insertmacro TOFU_LABEL'))
                 if i != -1),
                default=-1)
            assert first_ctl == -1 or art_at < first_ctl, (
                f'{target} {page}: a control is created BEFORE the page '
                'art — the art would sit above it in Z-order and hide it')


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



# ═══════════════════════════════════════════════════════════════════
#  The opacity contract: labels are OPAQUE statics (2026-08-06 round 2)
# ═══════════════════════════════════════════════════════════════════
# Stock ${NSD_CreateLabel} inherits __NSD_Label_EXSTYLE =
# WS_EX_TRANSPARENT from nsDialogs.nsh — the documented "label invisible
# until a forced redraw" failure class: a transparent static's first
# paint is deferred behind its siblings and can simply never arrive.
# This design never needs transparency — every label sits on a #F0F0F0
# card baked into the page art, exactly the COLOR_3DFACE an opaque
# static paints behind its text — so the transparent style only carries
# risk. TOFU_LABEL must create the STATIC directly, exstyle 0 (the
# official nsDialogs welcome.nsi shape).

def test_labels_are_opaque_statics_not_nsd_transparent():
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    raw = tmpl.split('!macro TOFU_LABEL')[1].split('!macroend')[0]
    # Comments may NAME the forbidden things (they document why); only
    # code lines count.
    macro = '\n'.join(line for line in raw.splitlines()
                      if not line.lstrip().startswith(';'))
    assert '${NSD_CreateLabel}' not in macro, (
        'TOFU_LABEL rides NSD_CreateLabel again — it carries '
        'WS_EX_TRANSPARENT (the invisible-label class)')
    assert 'WS_EX_TRANSPARENT' not in macro
    assert re.search(r'nsDialogs::CreateControl STATIC ', macro), (
        'TOFU_LABEL must create the STATIC directly (official-example '
        'shape) so the exstyle is explicit')
    assert ' 0 ${x} ${y} ${w} ${h} ' in macro, (
        'the exstyle operand just before the placement quad must stay 0')


def test_progress_status_label_handle_is_captured_from_r0():
    """A second `Pop $StatusCtl` after TOFU_LABEL underflows the stack:
    the macro already popped the HWND into $0, so the extra Pop left
    $StatusCtl empty and every (un.)DoInstall status update hit its
    `<> ""` guard as a no-op — the progress text could never change."""
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page in ('ProgressPageCreate', 'un.ProgressPageCreate'):
            body = script.split(f'Function {page}')[1] \
                         .split('FunctionEnd')[0]
            assert 'StrCpy $StatusCtl $0' in body, (target, page)
            assert 'Pop $StatusCtl' not in body, (target, page)



def test_diag_seam_is_wired_into_the_shared_macros():
    """The TOFU_DIAG measurement seam (2026-08-06): the log calls live
    INSIDE the shared macros so every page (install + uninstall) is
    covered, production compiles expand them to nothing, and the seam
    cannot be dropped without this pin going red."""
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    art_macro = tmpl.split('!macro TOFU_PAGE_ART')[1].split('!macroend')[0]
    label_macro = tmpl.split('!macro TOFU_LABEL')[1].split('!macroend')[0]
    assert '!insertmacro TOFU_DIAG_PAGE' in art_macro, (
        'page-level diag (dialog/art/image-handle) fell out of '
        'TOFU_PAGE_ART')
    assert '!insertmacro TOFU_DIAG_HW "label" $0' in label_macro, (
        'label-level diag fell out of TOFU_LABEL')
    assert '!insertmacro TOFU_DIAG_TEXT "label" $0' in label_macro, (
        'the label text-length probe fell out of TOFU_LABEL')
    fonts_macro = tmpl.split('!macro TOFU_CREATE_FONTS')[1] \
                      .split('!macroend')[0]
    assert '!insertmacro TOFU_DIAG_FONTS' in fonts_macro, (
        'the font-pipeline probe fell out of TOFU_CREATE_FONTS')
    assert '!ifdef TOFU_DIAG' in tmpl and 'OnTofuDiagProbe' in tmpl, (
        'the diag block / live-page probe timer went missing')
    # The empty-expansion else branch is what keeps production builds
    # byte-clean of the seam.
    for name in ('TOFU_DIAG_WRITE', 'TOFU_DIAG_HW', 'TOFU_DIAG_PAGE',
                 'TOFU_DIAG_HEADER', 'TOFU_DIAG_ART_CHECK',
                 'TOFU_DIAG_TEXT', 'TOFU_DIAG_FONTS', 'TOFU_DIAG_MARK',
                 'TOFU_DIAG_RETITLE'):
        assert tmpl.count(f'!macro {name}') == 2, (
            f'{name} must exist in BOTH an ifdef and an empty-else form')


# ═══════════════════════════════════════════════════════════════════
#  The page-function probe coverage (2026-08-07 round 2 of the seam)
# ═══════════════════════════════════════════════════════════════════
# The owner's screenshot showed even the nav-button RETITLE absent on
# the welcome page — the widened seam must log every page reaching
# nsDialogs::Show and audit the retitle lane end to end.

_PAGES = ('WelcomePageCreate', 'DirPageCreate', 'ProgressPageCreate',
          'FinishPageCreate', 'un.ConfirmPageCreate',
          'un.ProgressPageCreate', 'un.FinishPageCreate')
_RETITLING = {'WelcomePageCreate': 'welcome', 'FinishPageCreate': 'finish',
              'un.ConfirmPageCreate': 'un.confirm',
              'un.FinishPageCreate': 'un.finish'}


def test_every_page_logs_reached_show_before_show():
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page in _PAGES:
            body = script.split(f'Function {page}')[1] \
                         .split('FunctionEnd')[0]
            assert 'reached Show' in body, (target, page)
            assert body.index('reached Show') < \
                body.index('nsDialogs::Show'), (target, page)


def test_retitling_pages_probe_the_button_lane():
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page, tag in _RETITLING.items():
            body = script.split(f'Function {page}')[1] \
                         .split('FunctionEnd')[0]
            assert f'TOFU_DIAG_RETITLE "{tag}"' in body, (target, page)
