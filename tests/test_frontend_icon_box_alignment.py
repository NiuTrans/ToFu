#!/usr/bin/env python3
"""Icon-alignment utility guard (recurring bug class: SVG-on-baseline drift).

An inline <svg> sits on the text baseline and reserves descender space, so a
standalone icon renders ~2-3px low and floats off-center inside a fixed box.
The robust fix is the shared `.icon-box` utility in the BASE layer of
styles.css:

    .icon-box{display:inline-flex;align-items:center;justify-content:center}
    .icon-box>svg,.icon-box>img{display:block}

The load-bearing line is `>svg{display:block}` — it removes the inline
descender gap that keeps a glyph looking low EVEN inside a centered flex box.
This test parses styles.css with tinycss2 (not regex) and asserts:

  1. the container rule centers via flex (inline-flex + align/justify center);
  2. the child rule forces `display:block` on svg AND img;
  3. the utility is in the BASE layer, not gated behind a [data-theme=...]
     prefix (so it works in dark/light/tofu);
  4. NEUTER — flipping the child to `display:inline` makes assertion (2) fail,
     proving the invariant bites and isn't a tautology;
  5. the concrete offender (paper-reader zoom bar in index.html) no longer uses
     unicode glyphs (⤢ / − / +) and its buttons carry `icon-box` + an <svg>.
  6. the toolbar-toggle count badge (`.submenu-count`) — same bug class —
     centers its digit via flex, uses unitless `line-height:1` in BOTH the base
     and tofu rules, and a neuter proves the flex-centering assertion bites.
"""

from __future__ import annotations

import os

import pytest

try:
    import tinycss2
except ModuleNotFoundError:
    # tinycss2 is a declared TEST dep (pyproject `[test]` extra), so every CI
    # lane that ran `pip install -e ".[test]"` has it; a bare contributor
    # checkout may not. Skip loudly there — and under TOFU_REQUIRE_FRONTEND=1
    # (the lane that PROMISES these suites run) fail instead of silently
    # dropping the module from collection (tests/_jsdom.frontend_required).
    tinycss2 = None

if tinycss2 is None:
    try:
        from tests._jsdom import frontend_required
    except ImportError:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from _jsdom import frontend_required
    if frontend_required():
        pytest.fail(
            'TOFU_REQUIRE_FRONTEND=1 but tinycss2 is not installed '
            '(pip install -e ".[test]")', pytrace=False)
    pytest.skip(
        'tinycss2 not installed (pip install -e ".[test]")',
        allow_module_level=True)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')
HTML_PATH = os.path.join(ROOT, 'index.html')


def _decls_for(css: str, selector: str):
    """Return {prop: value} of the FIRST qualified rule whose prelude
    serializes to exactly `selector` (whitespace-normalized)."""
    rules = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    want = ' '.join(selector.split())
    for rule in rules:
        if rule.type != 'qualified-rule':
            continue
        prelude = tinycss2.serialize(rule.prelude)
        if ' '.join(prelude.split()) != want:
            continue
        out = {}
        for d in tinycss2.parse_declaration_list(
                rule.content, skip_whitespace=True, skip_comments=True):
            if d.type == 'declaration':
                out[d.lower_name] = tinycss2.serialize(d.value).strip()
        return out
    return None


def test_icon_box_container_centers_via_flex():
    css = open(CSS_PATH, encoding='utf-8').read()
    decls = _decls_for(css, '.icon-box')
    assert decls is not None, '.icon-box container rule missing from styles.css'
    assert decls.get('display') == 'inline-flex', (
        f".icon-box display is {decls.get('display')!r}; must be inline-flex — "
        f"vertical-align does NOTHING on a flex child, centering must come from "
        f"the flex box.")
    assert decls.get('align-items') == 'center', '.icon-box must align-items:center'
    assert decls.get('justify-content') == 'center', (
        '.icon-box must justify-content:center')


def test_icon_box_child_is_display_block():
    """The load-bearing line: svg/img children MUST be display:block to kill the
    inline descender gap (a centered flex box alone still looks low without it)."""
    css = open(CSS_PATH, encoding='utf-8').read()
    decls = _decls_for(css, '.icon-box>svg,.icon-box>img')
    assert decls is not None, (
        '.icon-box>svg,.icon-box>img rule missing — the child display:block '
        'rule is what removes the baseline descender gap')
    assert decls.get('display') == 'block', (
        f"icon-box child display is {decls.get('display')!r}; must be 'block'. "
        f"This is the line that actually fixes the low-icon drift.")


def test_icon_box_is_base_layer_not_theme_scoped():
    """The utility must be theme-agnostic (works in dark/light/tofu), i.e. the
    rule head must NOT be prefixed with a [data-theme=...] scope."""
    css = open(CSS_PATH, encoding='utf-8').read()
    rules = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    found = False
    for rule in rules:
        if rule.type != 'qualified-rule':
            continue
        prelude = ' '.join(tinycss2.serialize(rule.prelude).split())
        if prelude == '.icon-box':
            found = True
            # A base-layer rule is exactly '.icon-box' with no ancestor scope.
            assert 'data-theme' not in prelude, (
                '.icon-box must be a base-layer rule, not theme-scoped')
    assert found, '.icon-box base rule not found'


def test_NC_icon_box_child_inline_would_break():
    """NEUTER: flip the child display to inline and confirm the display:block
    assertion FAILS — proving the guard has teeth."""
    css = open(CSS_PATH, encoding='utf-8').read()
    neutered = css.replace(
        '.icon-box>svg,.icon-box>img{display:block}',
        '.icon-box>svg,.icon-box>img{display:inline}', 1)
    assert neutered != css, 'NC pattern did not match — test is stale'
    decls = _decls_for(neutered, '.icon-box>svg,.icon-box>img')
    assert decls.get('display') == 'inline', 'neuter did not take'
    # The real assertion the positive test makes must now be violated.
    assert not (decls.get('display') == 'block'), (
        'the display:block invariant must FAIL on the neutered CSS')


def test_zoom_bar_uses_svg_not_unicode_glyphs():
    """The concrete offender from the screenshot: the paper-reader zoom bar
    controls must be inline SVGs inside .icon-box buttons, NOT unicode glyphs
    (⤢ U+2922, − U+2212, +) which drift off-center and fall back per-platform."""
    html = open(HTML_PATH, encoding='utf-8').read()
    i = html.find('paper-pdf-toolbar')
    assert i != -1, 'paper-pdf-toolbar not found in index.html'
    # Bound the check to the toolbar block.
    block = html[i:html.find('paper-pdf-container', i)]

    for glyph in ('\u2922', '\u2212'):  # ⤢, −
        assert glyph not in block, (
            f'zoom bar still contains unicode glyph U+{ord(glyph):04X} — replace '
            f'with an inline SVG (§3.4)')
    # The three controls (fit-width, zoom-out, zoom-in) must be icon-box SVG buttons.
    for handler in ('paperFitWidth()', 'paperZoomOut()', 'paperZoomIn()'):
        j = block.find(handler)
        assert j != -1, f'{handler} button missing from zoom bar'
        btn = block[block.rfind('<button', 0, j):block.find('</button>', j)]
        assert 'icon-box' in btn, f'{handler} button must carry class="...icon-box"'
        assert '<svg' in btn, f'{handler} button must render an inline <svg>'


def test_submenu_count_badge_centers_via_flex():
    """Same bug class as .icon-box: the count digit in the toolbar-toggle badge
    must be centered by FLEX, not by line-height (line-height centering drifts
    off-center per font). `.submenu-count.visible` is the visible-state rule."""
    css = open(CSS_PATH, encoding='utf-8').read()
    decls = _decls_for(css, '.submenu-count.visible')
    assert decls is not None, '.submenu-count.visible rule missing from styles.css'
    assert decls.get('display') == 'inline-flex', (
        f".submenu-count.visible display is {decls.get('display')!r}; must be "
        f"inline-flex — line-height centering drifts the digit low.")
    assert decls.get('align-items') == 'center', (
        '.submenu-count.visible must align-items:center')
    assert decls.get('justify-content') == 'center', (
        '.submenu-count.visible must justify-content:center')


def test_submenu_count_no_pixel_line_height():
    """Both the base and the tofu-scoped `.submenu-count` rules must use
    `line-height:1` — a PIXEL line-height (e.g. 16px/14px) re-introduces the
    baseline drift the flex centering was added to remove."""
    css = open(CSS_PATH, encoding='utf-8').read()
    for selector in ('.submenu-count', '[data-theme="tofu"] .submenu-count'):
        decls = _decls_for(css, selector)
        assert decls is not None, f'{selector} rule missing from styles.css'
        lh = decls.get('line-height')
        assert lh == '1', (
            f"{selector} line-height is {lh!r}; must be '1' (unitless). A pixel "
            f"line-height re-introduces baseline drift once the digit's font "
            f"metrics don't match the box height.")


def test_NC_submenu_count_inline_block_would_break():
    """NEUTER: revert `.submenu-count.visible` to `display:inline-block` (the
    old line-height-centered form) and confirm the flex assertion FAILS —
    proving this guard bites, so a careless revert can't silently regress."""
    css = open(CSS_PATH, encoding='utf-8').read()
    neutered = css.replace(
        '.submenu-count.visible{display:inline-flex;align-items:center;justify-content:center}',
        '.submenu-count.visible{display:inline-block}', 1)
    assert neutered != css, 'NC pattern did not match — test is stale'
    decls = _decls_for(neutered, '.submenu-count.visible')
    assert decls.get('display') == 'inline-block', 'neuter did not take'
    assert not (decls.get('display') == 'inline-flex'), (
        'the inline-flex invariant must FAIL on the neutered CSS')


if __name__ == '__main__':
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    rc = 0
    for fn in fns:
        try:
            fn()
            print(f'PASS {fn.__name__}')
        except AssertionError as e:
            rc = 1
            print(f'FAIL {fn.__name__}: {e}')
    sys.exit(rc)
