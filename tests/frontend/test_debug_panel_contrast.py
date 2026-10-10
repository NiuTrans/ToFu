"""Inspector role, syntax, axis and body readability over authored delivery owners.

Critical tokens and directly imported lazy sheets are inspected together. Theme
roles resolve CSS variables before measuring 4.5:1 on the actual panel ground;
a semantic token is never mistaken for an inherited dark-theme raw paint.
The companion real-browser test verifies delivery, cascade and geometry.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
_STYLES = os.path.join(ROOT, 'static', 'styles.css')

# WCAG AA for normal-size text. The renderer's type is small (10-11.5px), so
# the large-text 3:1 allowance explicitly does NOT apply here.
_AA = 4.5

_ROLES = ('system', 'user', 'assistant', 'tool', 'tools')
_SYNTAX = ('key', 'str', 'num', 'null')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        css = f.read()
    if path == _STYLES:
        owners = Path(ROOT) / 'frontend/src/styles/application'
        css += '\n' + '\n'.join((owners / f'02-inspector-{part}.css').read_text()
                                  for part in ('shell', 'structure', 'drawer', 'inline', 'responsive'))
    return css


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


def _theme_block(css: str, theme: str, token: str) -> str:
    """Return the first theme block that actually declares ``token``.

    The composed sheet prepends a token base layer (00-design-tokens.css)
    that does not carry every value, so the first :root / [data-theme] block
    is no longer guaranteed to be the owning block.
    """
    if theme == 'dark':
        pattern = r':root\{([^}]*)\}'
    else:
        pattern = r'\[data-theme="' + theme + r'"\]\{(.*?)\n\}'
    for m in re.finditer(pattern, css, re.S):
        if token in m.group(1):
            return m.group(1)
    raise AssertionError(f'{theme} token block not found')


def _theme_bg(css: str, theme: str) -> str:
    """The --bg-secondary of one theme — the ground .ri-state-panel and the
    .ri-drawer are both painted on, so it is the correct comparison surface."""
    block = _theme_block(css, theme, '--bg-secondary')
    bg = re.search(r'--bg-secondary\s*:\s*(#[0-9a-fA-F]{3,6})', block)
    assert bg, f'{theme} does not declare --bg-secondary'
    return bg.group(1)


def _resolve(css: str, theme: str, value: str) -> str:
    """Resolve the theme's declared role, falling back to root vocabulary."""
    match = re.fullmatch(r'var\((--[\w-]+)\)', value.strip())
    if not match:
        return value.strip()
    token = match.group(1)
    for current in ([theme, 'dark'] if theme != 'dark' else ['dark']):
        try:
            block = _theme_block(css, current, token)
        except AssertionError:
            continue
        declaration = re.search(re.escape(token) + r'\s*:\s*([^;}]+)', block)
        if declaration:
            return _resolve(css, theme, declaration.group(1))
    raise AssertionError(f'{theme}: unresolved {token}')


def _declared(css: str, selector: str, *, unthemed: bool) -> str | None:
    pat = (r'(?<!\] )' if unthemed else '') + re.escape(selector) + \
        r'\{[^}]*?(?:^|[;{])?color\s*:\s*([^;}]+)'
    hits = re.findall(pat, css)
    return hits[-1] if hits else None


def _role_color(css: str, theme: str, role: str) -> str | None:
    value = (_declared(css, f'[data-theme="{theme}"] .debug-msg-header .role-{role}', unthemed=False)
             if theme != 'dark' else None)
    value = value or _declared(css, f'.debug-msg-header .role-{role}', unthemed=True)
    return _resolve(css, theme, value) if value else None


def _syntax_color(css: str, theme: str, token: str) -> str | None:
    value = (_declared(css, f'[data-theme="{theme}"] .debug-msg-body pre .debug-{token}', unthemed=False)
             if theme != 'dark' else None)
    value = value or _declared(css, f'.debug-msg-body pre .debug-{token}', unthemed=True)
    return _resolve(css, theme, value) if value else None


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
    hits = re.findall(r'\.ri-state-panel-kind\{[^}]*?--ri-kind-ink\s*:\s*([^;}]+)', css)
    assert hits, f'{theme}: the axis chip declares no --ri-kind-ink'
    ink = _resolve(css, theme, hits[-1])
    ratio = _contrast(ink, bg)
    assert ratio >= _AA, f'{theme}: axis chip ink {ink} on {bg} is {ratio:.2f}:1'


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
        block = _theme_block(css, theme, '--text-secondary')
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
    size = re.search(r'font-size\s*:\s*([^;}]+)', pre.group(1))
    assert size and float(_resolve(css, 'dark', size.group(1)).removesuffix('px')) >= 14
    line = re.search(r'line-height\s*:\s*([^;}]+)', pre.group(1))
    assert line and float(_resolve(css, 'dark', line.group(1))) >= 1.6
