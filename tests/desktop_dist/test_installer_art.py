"""tests/desktop_dist/test_installer_art.py — the wizard header-band art contract.

The custom NSIS wizard renders a HEADER BAND bitmap plus real controls on
the native dialog face. Two failure modes this suite pins:

1. **The art itself** — four BMP header bands at the contract size, purple
   gradient, baked product name. A full-page bitmap is the 2026-08-05..09
   blank-wizard class (band visible, every LangString dead).
2. **The geometry contract** — the template places labels/controls in
   dialog units BELOW the 38 du band. If a control's du rect ever overlaps
   the band, an SS_BITMAP sibling starves it of WM_PAINT. Parsed from the
   RENDERED scripts so the agent's injected autostart checkbox is covered.

Run:  pytest tests/desktop_dist/test_installer_art.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from lib.desktop_dist import installer_art, winbuilder as wb

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent

_PAGES = ('WelcomePageCreate', 'DirPageCreate', 'ProgressPageCreate',
          'FinishPageCreate', 'un.ConfirmPageCreate',
          'un.ProgressPageCreate', 'un.FinishPageCreate')
_RETITLING = {'WelcomePageCreate': 'welcome', 'FinishPageCreate': 'finish',
              'un.ConfirmPageCreate': 'un.confirm',
              'un.FinishPageCreate': 'un.finish'}


@pytest.fixture(scope='module')
def art(tmp_path_factory):
    out = tmp_path_factory.mktemp('art')
    return installer_art.render(str(out), 'Tofu Agent', '0.16.0',
                                autostart=True)


def _px(img, fx, fy):
    """Pixel at fractional coordinates."""
    return img.getpixel((int(img.width * fx), int(img.height * fy)))


def _macro_code(tmpl: str, name: str) -> str:
    raw = tmpl.split(f'!macro {name}')[1].split('!macroend')[0]
    return '\n'.join(line for line in raw.splitlines()
                     if not line.lstrip().startswith(';'))


# ═══════════════════════════════════════════════════════════════════
#  The art itself
# ═══════════════════════════════════════════════════════════════════

def test_four_pages_at_contract_size(art):
    assert set(art) == {'welcome', 'directory', 'progress', 'finish'}
    for path in art.values():
        with Image.open(path) as img:
            assert img.format == 'BMP'
            assert img.size == installer_art.BAND_PX
            assert img.size[1] < installer_art.PAGE_PX[1] // 2
            assert img.mode == 'RGB'


def test_four_pages_are_the_same_header_band(art):
    welcome = Path(art['welcome']).read_bytes()
    for name, path in art.items():
        assert Path(path).read_bytes() == welcome, name


def test_band_is_purple_header_not_a_full_page(art):
    for name, path in art.items():
        img = Image.open(path).convert('RGB')
        r, g, b = _px(img, 0.7, 0.4)
        assert b > 150 and r > 70 and g < 130, (name, (r, g, b))
        bottom = img.getpixel((img.width // 2, img.height - 2))
        assert bottom != (255, 255, 255), name
        assert bottom != (240, 240, 240), name


def test_product_name_is_baked_in_the_band(tmp_path):
    a = installer_art.render(str(tmp_path / 'a'), 'Tofu', '1.0.0')
    b = installer_art.render(str(tmp_path / 'b'), 'Tofu Agent', '1.0.0')
    band_h = min(100, installer_art.BAND_PX[1])
    band_a = Image.open(a['welcome']).convert('RGB') \
        .crop((0, 0, installer_art.BAND_PX[0], band_h))
    band_b = Image.open(b['welcome']).convert('RGB') \
        .crop((0, 0, installer_art.BAND_PX[0], band_h))
    assert band_a.tobytes() != band_b.tobytes(), (
        'the band stopped carrying the product name — the pages are '
        'supposed to differ per target')


def test_autostart_flag_does_not_change_header_bands(tmp_path):
    on = installer_art.render(str(tmp_path / 'on'), 'Tofu Agent', '1.0.0',
                              autostart=True)
    off = installer_art.render(str(tmp_path / 'off'), 'Tofu Agent',
                               '1.0.0', autostart=False)
    for page in ('welcome', 'directory', 'progress', 'finish'):
        assert Path(on[page]).read_bytes() == Path(off[page]).read_bytes()


# ═══════════════════════════════════════════════════════════════════
#  The geometry contract: every control sits BELOW the band
# ═══════════════════════════════════════════════════════════════════

_FULL = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'full',
                       art_dir='/art')
_AGENT = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'agent',
                        art_dir='/art')

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


def test_every_control_sits_below_the_band():
    band_bottom = installer_art.BAND_HEIGHT_DU
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page in _PAGES:
            marker = f'Function {page}'
            assert marker in script, (target, page)
            body = script.split(marker)[1].split('FunctionEnd')[0]
            rects = list(_controls_in(body))
            assert rects, (target, page)
            for rect in rects:
                x0, y0, x1, y1 = rect
                assert y0 >= band_bottom, (
                    f'{target} {page}: control {rect} overlaps the '
                    f'{band_bottom} du header band — an SS_BITMAP sibling '
                    'starves overlapping controls of WM_PAINT (2026-09-09 '
                    'blank wizard)')


# ═══════════════════════════════════════════════════════════════════
#  The Z-order + band-size contract
# ═══════════════════════════════════════════════════════════════════
# The 2026-08-05 blank-wizard class: a full-page art control that sits
# ABOVE a sibling in Z-order starves that sibling of WM_PAINT. HWND_BOTTOM
# did not recover paint on real Windows when the bitmap was 100%×100%.
# The load-bearing pin is therefore "the bitmap is a 38u band"; the
# Z-order pin stays as defense in depth.

def test_page_art_is_a_header_band_not_full_page():
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    code = _macro_code(tmpl, 'TOFU_PAGE_ART')
    assert installer_art.BAND_HEIGHT_DU == 38
    assert '100% 38u' in code, (
        'TOFU_PAGE_ART must place the bitmap as a 38 du header band')
    assert '100% 100%' not in code, (
        'a 100%x100% SS_BITMAP overlays every live control — the '
        '2026-08-05..09 blank wizard (band visible, every label dead)')
    assert 'NSD_SetStretchedImage' in code, (
        'the band must stretch via NSD_SetStretchedImage (/RESIZETOFIT)')


def test_page_art_macro_pins_bitmap_to_z_bottom():
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    macro = tmpl.split('!macro TOFU_PAGE_ART')[1].split('!macroend')[0]
    assert 'SetWindowPos' in macro, (
        'TOFU_PAGE_ART lost its HWND_BOTTOM SetWindowPos — keep the band '
        'pinned under the live controls even if placement drifts')


def test_art_is_created_before_any_control_on_every_page():
    """Companion belt to the HWND_BOTTOM pin: the creation order stays
    art-first too, so the background invariant holds on two levels."""
    for script, target in ((_FULL, 'full'), (_AGENT, 'agent')):
        for page in _PAGES:
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
    assert installer_art.BAND_DU == (0, 0, 266, 38)
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
# This design never needs transparency — every label sits on native
# COLOR_3DFACE below the band — so the transparent style only carries
# risk. TOFU_LABEL must create the STATIC directly, exstyle 0 (the
# official nsDialogs welcome.nsi shape).

def test_labels_are_opaque_statics_not_nsd_transparent():
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    macro = _macro_code(tmpl, 'TOFU_LABEL')
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
                 'TOFU_DIAG_HEADER', 'TOFU_DIAG_TEXT', 'TOFU_DIAG_FONTS',
                 'TOFU_DIAG_MARK', 'TOFU_DIAG_RETITLE'):
        assert tmpl.count(f'!macro {name}') == 2, (
            f'{name} must exist in BOTH an ifdef and an empty-else form')


# ═══════════════════════════════════════════════════════════════════
#  The art-source contract: File → $PLUGINSDIR → SetStretchedImage
# ═══════════════════════════════════════════════════════════════════
# The 2026-08-07 CI pixel run proved the prior exe-resource approach
# was broken: $EXEHANDLE is NOT a real NSIS variable (it expands to
# empty → LoadImage(p 0, i 900, …) returns NULL → image_handle=0 on
# every page), and !packhdr cannot embed RT_RCDATA (it post-processes
# the exehead stub, it does not add resources). The documented NSIS
# pattern is File (extracts bmp into $PLUGINSDIR) +
# NSD_SetStretchedImage (LoadAndSetImage /RESIZETOFIT, LR_LOADFROMFILE).

def test_art_loads_from_pluginsdir_via_stretched_image():
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    code = _macro_code(tmpl, 'TOFU_PAGE_ART')
    assert '$EXEHANDLE' not in tmpl, (
        '$EXEHANDLE is not a real NSIS variable — it expands to empty '
        'and LoadImage returns NULL (root cause of the 2026-08-07 '
        'blank wizard, image_handle=0 on every page)')
    assert 'NSD_SetStretchedImage' in code, (
        'the page art must load from $PLUGINSDIR via NSD_SetStretchedImage')
    assert '$PLUGINSDIR' in code, (
        'SetStretchedImage must reference $PLUGINSDIR (extracted by File '
        'in .onInit)')
    assert '!packhdr' not in tmpl, (
        '!packhdr cannot embed resources — it post-processes the '
        'exehead stub; the dead line was removed')
    calls = re.findall(r'!insertmacro TOFU_PAGE_ART "(\w+\.bmp)"',
                       tmpl)
    assert sorted(calls) == sorted([
        'welcome.bmp', 'welcome.bmp',
        'directory.bmp',
        'progress.bmp', 'progress.bmp',
        'finish.bmp', 'finish.bmp']), calls
    for bmp in ('welcome.bmp', 'directory.bmp',
                'progress.bmp', 'finish.bmp'):
        assert f'File "@ART_DIR@/{bmp}"' in tmpl, (
            f'{bmp} must be extracted via File in .onInit/un.onInit')


def test_no_exehandle_anywhere_in_template():
    """The exe-module-handle pseudo-variable is not a real NSIS variable
    — any occurrence would expand to empty and silently break any API
    call that receives it."""
    tmpl = (_ROOT / 'desktop' / 'installer.nsi.tmpl').read_text(
        encoding='utf-8')
    assert '$EXEHANDLE' not in tmpl, (
        'the exe-handle pseudo-variable appeared in the template — it is '
        'not a valid NSIS variable (see NSIS docs §4.2)')


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
