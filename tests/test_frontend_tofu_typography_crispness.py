"""Tofu chat-inner typography crispness drift guard.

Symptoms this guards against (fixed 2026-07-08):

1. BLURRY / GRAYISH TEXT. The tofu AND light themes forced
   `-webkit-font-smoothing:auto` (subpixel AA), overriding the base
   `antialiased`. On dark ink over light paper, subpixel AA produces faint
   color fringing that reads as fuzzy. Fix: BOTH light-background themes
   (tofu + light) now use `antialiased` / `grayscale` (crisp). The base (dark)
   theme already uses `antialiased`.

   INVARIANT: every `[data-theme="tofu"]{…}` AND `[data-theme="light"]{…}`
   rule that sets `-webkit-font-smoothing` sets it to `antialiased` (and
   `-moz-osx-font-smoothing:grayscale`), never `auto`.

2. PHANTOM BODY WEIGHT. `.md-content` used `font-weight:450`, but Plus Jakarta
   Sans / Inter are self-hosted only as discrete/variable weights that snap 450
   to a real master — the intended weight was never rendered deterministically.
   Fix: a real loaded weight (400).

   INVARIANT: the tofu `.md-content` body rule uses an integer weight that is a
   real loaded instance (a multiple of 100), NOT 450.

3. INTER MUST BE REAL, NOT A PHANTOM STACK ENTRY. Inter is now the first Latin
   body face in `--sans-body` and MUST be self-hosted (an `@font-face` block),
   never left as a name with no matching file.

   INVARIANT: `--sans-body` lists 'Inter' before 'Plus Jakarta Sans' and ends in
   `var(--cjk-fallback)`; the local font CSS defines an `@font-face` for Inter.

Env-independent: parses the CSS source directly. NEUTERs included.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')
FONT_CSS = os.path.join(ROOT, 'static', 'vendor', 'google-fonts-local.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _theme_smoothing_values(css: str, theme: str) -> list[str]:
    """Every `-webkit-font-smoothing:<v>` inside a rule whose selector list
    contains `[data-theme="<theme>"]`."""
    compact = re.sub(r'\s+', '', css)
    needle = '[data-theme="%s"]' % theme
    out = []
    for m in re.finditer(r'([^{}]*)\{([^{}]*)\}', compact):
        sel, body = m.group(1), m.group(2)
        if needle not in sel:
            continue
        wm = re.search(r'-webkit-font-smoothing:([a-z-]+)', body)
        if wm:
            out.append(wm.group(1))
    return out


def _tofu_smoothing_values(css: str) -> list[str]:
    return _theme_smoothing_values(css, 'tofu')


def _tofu_md_content_weight(css: str) -> int | None:
    """The font-weight declared on the tofu `.md-content` body rule."""
    m = re.search(
        r'\[data-theme="tofu"\]\s*\.md-content\s*\{([^{}]*)\}', css)
    if not m:
        return None
    wm = re.search(r'font-weight:(\d+)', m.group(1))
    return int(wm.group(1)) if wm else None


def _sans_body_value(css: str) -> str | None:
    m = re.search(r'--sans-body:([^;]+);', css)
    return m.group(1).strip() if m else None


# ── #1: crisp smoothing ──

def test_tofu_uses_antialiased_not_subpixel():
    css = _read(CSS)
    vals = _tofu_smoothing_values(css)
    assert vals, 'no tofu -webkit-font-smoothing rule found (structure changed?)'
    bad = [v for v in vals if v != 'antialiased']
    assert not bad, (
        'tofu still forces subpixel font-smoothing (blurry on paper); every '
        f'tofu rule must use antialiased. Offending values: {bad}')
    # the grayscale companion must be present too
    assert '-moz-osx-font-smoothing:grayscale' in re.sub(r'\s+', '', css), (
        'missing -moz-osx-font-smoothing:grayscale companion on tofu')


def test_light_uses_antialiased_not_subpixel():
    css = _read(CSS)
    vals = _theme_smoothing_values(css, 'light')
    assert vals, 'no light -webkit-font-smoothing rule found (structure changed?)'
    bad = [v for v in vals if v != 'antialiased']
    assert not bad, (
        'light theme still forces subpixel font-smoothing (blurry on paper); '
        'it is also a dark-ink-on-light-paper surface and must use antialiased. '
        f'Offending values: {bad}')
    assert '-moz-osx-font-smoothing:grayscale' in re.sub(r'\s+', '', css), (
        'missing -moz-osx-font-smoothing:grayscale companion')


# ── #2: real loaded body weight ──

def test_tofu_body_weight_is_real_loaded_instance():
    css = _read(CSS)
    w = _tofu_md_content_weight(css)
    assert w is not None, 'tofu .md-content font-weight not found'
    assert w != 450, (
        'tofu body still uses the phantom font-weight:450 (no matching master)')
    assert w % 100 == 0, (
        f'tofu body weight {w} is not a real loaded instance (multiple of 100)')


# ── #3: Inter is first AND self-hosted, CJK tail intact ──

def test_inter_is_first_body_face_with_cjk_tail():
    css = _read(CSS)
    sb = _sans_body_value(css)
    assert sb, '--sans-body token not found'
    assert sb.index("'Inter'") < sb.index("'Plus Jakarta Sans'"), (
        "'Inter' must come before 'Plus Jakarta Sans' in --sans-body")
    assert 'var(--cjk-fallback)' in sb, (
        '--sans-body must keep the shared --cjk-fallback tail (single-CJK rule)')


def test_inter_is_self_hosted():
    font_css = _read(FONT_CSS)
    blocks = re.findall(r'@font-face\s*\{[^}]*\}', font_css)
    inter = [b for b in blocks if "font-family: 'Inter'" in b or "font-family:'Inter'" in b]
    assert inter, "Inter is not self-hosted — must have an @font-face block"
    assert any('.woff2' in b for b in inter), (
        'Inter @font-face does not reference a self-hosted woff2 file')
    for name in ('Inter-latin.woff2',):
        assert os.path.exists(os.path.join(ROOT, 'static', 'vendor', 'fonts', name)), (
            f'self-hosted Inter font file missing: {name}')


# ── NEUTERs: prove each guard is load-bearing ──

def test_nc_subpixel_smoothing_is_flagged():
    css = _read(CSS)
    assert not [v for v in _tofu_smoothing_values(css) if v != 'antialiased'], \
        'real CSS not clean; fix before NC'
    poisoned = css.replace(
        '[data-theme="tofu"]{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}',
        '[data-theme="tofu"]{-webkit-font-smoothing:auto;-moz-osx-font-smoothing:auto}',
        1)
    assert poisoned != css, 'NC anchor not found — tofu smoothing rule drifted'
    assert [v for v in _tofu_smoothing_values(poisoned) if v != 'antialiased'], (
        'guard did NOT catch a subpixel-smoothing tofu rule')


def test_nc_light_subpixel_smoothing_is_flagged():
    css = _read(CSS)
    assert not [v for v in _theme_smoothing_values(css, 'light') if v != 'antialiased'], \
        'real CSS not clean; fix before NC'
    poisoned = css.replace(
        '[data-theme="light"]{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}',
        '[data-theme="light"]{-webkit-font-smoothing:auto;-moz-osx-font-smoothing:auto}',
        1)
    assert poisoned != css, 'NC anchor not found — light smoothing rule drifted'
    assert [v for v in _theme_smoothing_values(poisoned, 'light') if v != 'antialiased'], (
        'guard did NOT catch a subpixel-smoothing light rule')


def test_nc_phantom_weight_is_flagged():
    css = _read(CSS)
    poisoned = css.replace(
        'font-size:15px;line-height:1.7;letter-spacing:0.005em;font-weight:400;',
        'font-size:15px;line-height:1.7;letter-spacing:0.005em;font-weight:450;',
        1)
    assert poisoned != css, 'NC anchor not found — tofu .md-content rule drifted'
    assert _tofu_md_content_weight(poisoned) == 450, (
        'neuter did not reintroduce the phantom 450 weight')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
