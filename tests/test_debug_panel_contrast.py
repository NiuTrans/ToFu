"""Static guard: the debug/request-inspector message renderer stays READABLE.

WHY (2026-07-29, owner report on the inline tool-row panel): "the font size of
this ri-state-panel is a bit too small, and the contrast is a bit too low."

Measuring it found the defect was NOT in the panel's own rules — it was that
`.debug-msg-*`, the SHARED renderer the panel and the drawer both use, had a
palette tuned for a #111 ground and, for the ROLE LABELS, no light/tofu
override at ALL. Measured before this guard existed:

  | token                          | light   | tofu    |
  |--------------------------------|---------|---------|
  | role-user / -assistant / -tool | 1.67-1.99 | 1.75-2.08 |
  | debug-str / -num / -null       | 3.69-4.26 | 2.75-3.40 |
  | .debug-msg-summary (tertiary)  | 3.05    | (dark 3.55) |

i.e. the TOOL label on tofu paper was 2.08:1 — technically painted, not
actually legible. Nothing in the suite measured any of it, which is exactly
why it shipped and survived three theme passes.

WHAT THIS PINS
  1. Every role label and JSON syntax colour clears 4.5:1 against the ground
     it is actually painted on (each theme's own --bg-secondary, since the
     panel and the drawer both sit on that surface) — computed, not asserted
     as a golden string, so a re-pick that keeps the hue family still passes
     while a regression to the dark palette fails.
  2. The role labels have a per-theme override at all. The failure mode here
     is SILENT INHERITANCE: adding a theme without re-picking these colours
     produces washed-out text, never an error.
  3. The block summary (byte + token counts — the numbers the panel is opened
     to read) is not on --text-tertiary, which fails in dark AND light.
  4. The inline panel's body type is at least as large as the drawer's dense
     row list. The panel is READ (JSON payloads); the list is SCANNED.

NATURE: static analysis of styles.css + a real WCAG computation. If a
deliberate redesign moves these tokens, re-run the numbers and update the
floors IN THE SAME COMMIT — do not delete the guard.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_STYLES = os.path.join(ROOT, 'static', 'styles.css')

# WCAG AA for normal-size text. The renderer's type is small (10-11.5px), so
# the large-text 3:1 allowance explicitly does NOT apply here.
_AA = 4.5

_ROLES = ('system', 'user', 'assistant', 'tool', 'tools')
_SYNTAX = ('key', 'str', 'num', 'null')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _luminance(hexcolor: str) -> float:
    h = hexcolor.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _theme_bg(css: str, theme: str) -> str:
    """The --bg-secondary of one theme — the ground .ri-state-panel and the
    .ri-drawer are both painted on, so it is the correct comparison surface."""
    if theme == 'dark':
        m = re.search(r':root\{([^}]*)\}', css)
    else:
        m = re.search(r'\[data-theme="' + theme + r'"\]\{(.*?)\n\}', css, re.S)
    assert m, f'{theme} token block not found'
    bg = re.search(r'--bg-secondary\s*:\s*(#[0-9a-fA-F]{3,6})', m.group(1))
    assert bg, f'{theme} does not declare --bg-secondary'
    return bg.group(1)


def _declared(css: str, selector: str, *, unthemed: bool) -> str | None:
    """Last declared colour for an exact selector (later rules win).

    `unthemed=True` (the dark/base lookup) must NOT match the same selector
    sitting behind a `[data-theme="x"] ` prefix — theme prefixes always end
    with `] `, so a fixed-width lookbehind excludes them. Without this the
    base lookup silently reads the LAST theme's colour and the guard grades
    every theme against the wrong ground.
    """
    pat = (r'(?<!\] )' if unthemed else '') + re.escape(selector) + \
        r'\{[^}]*?color\s*:\s*(#[0-9a-fA-F]{3,6})'
    hits = re.findall(pat, css)
    return hits[-1] if hits else None


def _role_color(css: str, theme: str, role: str) -> str | None:
    prefix = '' if theme == 'dark' else f'[data-theme="{theme}"] '
    return _declared(css, f'{prefix}.debug-msg-header .role-{role}',
                     unthemed=(theme == 'dark'))


def _syntax_color(css: str, theme: str, token: str) -> str | None:
    prefix = '' if theme == 'dark' else f'[data-theme="{theme}"] '
    return _declared(css, f'{prefix}.debug-msg-body pre .debug-{token}',
                     unthemed=(theme == 'dark'))


@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_role_labels_are_legible_on_their_own_theme_ground(theme):
    """ROLE labels (SYSTEM/USER/ASSISTANT/TOOL/TOOLS) must clear AA on the
    surface they are painted on. These are the panel's primary navigation —
    the user scans them to find the message they want."""
    css = _read(_STYLES)
    bg = _theme_bg(css, theme)
    for role in _ROLES:
        color = _role_color(css, theme, role)
        assert color, (
            f'.role-{role} has no colour declared for the {theme} theme. '
            f'Silent inheritance of the dark palette measured 1.7-2.1:1 on '
            f'paper grounds — barely legible, and it raises no error.')
        ratio = _contrast(color, bg)
        assert ratio >= _AA, (
            f'{theme}: .role-{role} {color} on {bg} is {ratio:.2f}:1, '
            f'below AA {_AA}:1')


@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_json_syntax_colors_are_legible_on_their_own_theme_ground(theme):
    """The JSON syntax palette (key/str/num/null) is the payload body itself —
    the thing the panel exists to show."""
    css = _read(_STYLES)
    bg = _theme_bg(css, theme)
    for token in _SYNTAX:
        color = _syntax_color(css, theme, token)
        assert color, f'.debug-{token} has no colour for the {theme} theme'
        ratio = _contrast(color, bg)
        assert ratio >= _AA, (
            f'{theme}: .debug-{token} {color} on {bg} is {ratio:.2f}:1, '
            f'below AA {_AA}:1')


@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_axis_chip_ink_is_legible_on_its_own_theme_ground(theme):
    """The panel's axis chip ("Result state" / "Request") must clear AA too.

    This case exists because of a real miss: the chip shipped on
    `var(--accent)` and the static suite passed, because it only graded the
    pre-existing role/syntax tokens. The real-browser pass then measured
    3.49 / 3.71 / 3.00:1 — --accent is a FILL colour (button grounds, borders),
    not a text colour. So the guard now grades the panel's OWN chrome as well,
    and via the --ri-kind-ink custom property, which is the seam that lets a
    theme re-pick it.
    """
    css = _read(_STYLES)
    bg = _theme_bg(css, theme)
    sel = ('.ri-state-panel-kind' if theme == 'dark'
           else f'[data-theme="{theme}"] .ri-state-panel-kind')
    # Same anchoring rule as _declared: the base lookup must not match the
    # SAME selector behind a `[data-theme="x"] ` prefix, or it silently reads
    # the last theme's ink and grades it against the dark ground.
    pat = (r'(?<!\] )' if theme == 'dark' else '') + re.escape(sel) + \
        r'\{[^}]*?--ri-kind-ink\s*:\s*(#[0-9a-fA-F]{3,6})'
    hits = re.findall(pat, css)
    assert hits, (
        f'{theme}: the axis chip declares no --ri-kind-ink. Falling back to '
        f'var(--accent) measured 3.0-3.7:1 as text in every theme.')
    ratio = _contrast(hits[-1], bg)
    assert ratio >= _AA, (
        f'{theme}: axis chip ink {hits[-1]} on {bg} is {ratio:.2f}:1, '
        f'below AA {_AA}:1')


def test_axis_chip_fallback_state_is_visually_distinct():
    """A request-axis FALLBACK render must not look like the post-tool mirror.

    The panel shows one view; when a round has no mirror it shows the request
    instead. If both states painted identically the user could not tell which
    axis they are reading — the exact off-axis confusion the two-tab design
    existed to prevent, which collapsing to one view must not re-introduce."""
    css = _read(_STYLES)
    m = re.search(r'\.ri-state-panel-kind\.ri-kind-fallback\{([^}]*)\}', css)
    assert m, 'the .ri-kind-fallback state has no styles'
    body = m.group(1)
    assert 'color' in body and 'background' in body, (
        'the fallback chip must differ in BOTH ink and ground — one alone is '
        'easy to miss at 10px')


def test_block_summary_is_not_on_the_tertiary_token():
    """--text-tertiary measures 3.55:1 (dark) / 3.05:1 (light) on
    --bg-secondary, so the byte/token counts in each block header were below
    AA in two of three themes. They carry the numbers the panel is opened to
    read, so they belong on --text-secondary (6.6:1 dark / 6.7:1 light)."""
    css = _read(_STYLES)
    m = re.search(r'\.debug-msg-summary\{([^}]*)\}', css)
    assert m, '.debug-msg-summary rule not found'
    assert 'var(--text-tertiary)' not in m.group(1), (
        '.debug-msg-summary is back on --text-tertiary, which fails AA on '
        'both the dark and light grounds')


def test_theme_secondary_token_clears_aa_on_the_panel_ground():
    """The panel title + summary resolve through --text-secondary, so that
    token itself must clear AA on every theme's --bg-secondary. Pinned
    separately because a theme-token tweak elsewhere would otherwise silently
    degrade this panel."""
    css = _read(_STYLES)
    for theme in ('dark', 'light', 'tofu'):
        if theme == 'dark':
            block = re.search(r':root\{([^}]*)\}', css).group(1)
        else:
            block = re.search(r'\[data-theme="' + theme + r'"\]\{(.*?)\n\}',
                              css, re.S).group(1)
        sec = re.search(r'--text-secondary\s*:\s*(#[0-9a-fA-F]{3,6})', block)
        assert sec, f'{theme} does not declare --text-secondary'
        bg = _theme_bg(css, theme)
        ratio = _contrast(sec.group(1), bg)
        assert ratio >= _AA, (
            f'{theme}: --text-secondary {sec.group(1)} on {bg} is '
            f'{ratio:.2f}:1, below AA {_AA}:1')


def test_inline_panel_body_type_is_not_smaller_than_the_drawer_row_list():
    """The inline panel is READ (JSON payloads, character by character); the
    drawer's task/round list is SCANNED for a match. So the panel's body type
    must be at least as large as the list's — the owner's "font size a bit too
    small" complaint was the panel inheriting the LIST's 10.5px scale."""
    css = _read(_STYLES)
    pre = re.search(r'\.ri-state-body \.debug-msg-body pre\{([^}]*)\}', css)
    assert pre, (
        'the inline panel declares no body font-size — it falls back to the '
        'base .debug-msg-body pre 10.5px, the size that was reported as too '
        'small to read')
    size = re.search(r'font-size\s*:\s*([0-9.]+)px', pre.group(1))
    assert size and float(size.group(1)) >= 11.0, (
        f'inline panel body type is {size.group(1) if size else "?"}px; the '
        f'drawer row list uses 10-10.5px and this surface must read larger')
    line = re.search(r'line-height\s*:\s*([0-9.]+)', pre.group(1))
    assert line and float(line.group(1)) >= 1.5, (
        'JSON payload lines need >=1.5 line-height to be scannable when they '
        'wrap (word-break:break-all makes wrapping the norm here)')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
