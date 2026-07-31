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

2. BODY WEIGHT MUST BE INSIDE THE LOADED VARIABLE AXIS. Inter is self-hosted
   as a VARIABLE font — both `@font-face` blocks in
   `static/vendor/google-fonts-local.css` declare `font-weight: 100 900`, so
   EVERY integer in 100..900 (450, 430, 650, 660, …) is a genuinely renderable
   instance, NOT a synthesized phantom.

   HISTORY — do not re-break this: an earlier version of this guard asserted
   the tofu body weight had to be a MULTIPLE OF 100 and banned 450, on the
   premise that only discrete masters were loaded. That premise is false for a
   variable axis, and the rule additionally anchored on a
   `[data-theme="tofu"] .md-content{font-weight:…}` declaration that the
   chat-column declutter batch later removed — so the guard went red on a
   missing anchor while asserting a fiction. Weights are now checked against
   the AXIS RANGE actually declared by the shipped `@font-face` blocks, which
   is the only fact that decides whether a weight can render.

   INVARIANT: every numeric `font-weight` declared by a tofu-scoped rule falls
   inside the variable axis range the shipped Inter `@font-face` declares.

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


def _inter_axis_range(font_css: str) -> tuple[int, int] | None:
    """The variable-axis weight range declared by the shipped Inter
    `@font-face` blocks, e.g. (100, 900). None when Inter declares only
    discrete weights (then there is no axis to validate against).

    This is the SINGLE source of truth for "can this weight render?" — read
    from the font CSS that actually ships, never hardcoded in the assertion.
    """
    lo = hi = None
    for block in re.findall(r'@font-face\s*\{[^}]*\}', font_css):
        if "font-family: 'Inter'" not in block and "font-family:'Inter'" not in block:
            continue
        m = re.search(r'font-weight:\s*(\d+)\s+(\d+)', block)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
    return (lo, hi) if lo is not None else None


def tofu_declared_weights(css: str) -> list[tuple[str, int]]:
    """Every numeric `font-weight` declared by a tofu-scoped rule, as
    [(selector_tail, weight)].

    Scoped to the whole theme rather than one selector on purpose: the previous
    single-selector anchor was deleted by an unrelated batch, which turned this
    guard red without any real defect. A theme-wide scan follows the
    declarations wherever they move.
    """
    compact = re.sub(r'\s+', '', css)
    needle = '[data-theme="tofu"]'
    out: list[tuple[str, int]] = []
    for m in re.finditer(r'([^{}]*)\{([^{}]*)\}', compact):
        sel, body = m.group(1), m.group(2)
        if needle not in sel:
            continue
        for wm in re.finditer(r'font-weight:(\d+)', body):
            out.append((sel[-80:], int(wm.group(1))))
    return out


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


# ── #2: declared weights live inside the loaded variable axis ──

def test_tofu_body_weight_is_real_loaded_instance():
    """Every tofu-scoped font-weight must be renderable by the shipped font.

    Inter ships as a variable font (`font-weight: 100 900`), so the real
    constraint is the AXIS RANGE — not "multiple of 100" (which would
    false-positive on 450/430/650/660, all of which render exactly).
    """
    css = _read(CSS)
    axis = _inter_axis_range(_read(FONT_CSS))
    assert axis is not None, (
        'shipped Inter @font-face declares no variable weight range — if Inter '
        'was switched back to discrete masters, this guard must be rewritten '
        'to check membership in the discrete set instead of a range')
    lo, hi = axis
    declared = tofu_declared_weights(css)
    assert declared, 'no tofu-scoped font-weight found (structure changed?)'
    outside = [(sel, w) for sel, w in declared if not (lo <= w <= hi)]
    assert not outside, (
        f'tofu rules declare font-weight(s) outside the shipped Inter variable '
        f'axis {lo}..{hi} — these cannot render as written:\n'
        + '\n'.join(f'  {w} in …{sel}' for sel, w in outside)
    )


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
    """NC: a weight OUTSIDE the shipped variable axis must be flagged.

    Poisons a real surviving tofu rule (not a deleted anchor) with 1000, which
    lies outside Inter's 100..900 axis and therefore genuinely cannot render.
    Proves the guard discriminates unrenderable weights from the legitimate
    in-axis ones (450/650/660) it must leave alone.
    """
    css = _read(CSS)
    axis = _inter_axis_range(_read(FONT_CSS))
    assert axis is not None, 'no variable axis to test against'
    lo, hi = axis
    assert not [(s, w) for s, w in tofu_declared_weights(css) if not (lo <= w <= hi)], \
        'real CSS not clean; fix before NC'
    poisoned = css.replace(
        '[data-theme="tofu"] .md-content strong{font-weight:660}',
        '[data-theme="tofu"] .md-content strong{font-weight:1000}',
        1)
    assert poisoned != css, 'NC anchor not found — tofu strong-weight rule drifted'
    outside = [(s, w) for s, w in tofu_declared_weights(poisoned) if not (lo <= w <= hi)]
    assert any(w == 1000 for _s, w in outside), (
        f'guard did NOT catch an out-of-axis weight; outside={outside}')
    # And the in-axis weights the old guard wrongly banned stay ACCEPTED.
    assert all(w != 450 for _s, w in outside), (
        '450 is inside the 100..900 variable axis and must NOT be flagged')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
