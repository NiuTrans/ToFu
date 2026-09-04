"""tests/test_css_tokens.py — P3: no dark literals outside :root, themes resolve.

Why these guards exist
----------------------
trading.css was a dark-only design system: 215 hardcoded colours outside its
:root block (measured: 28 hex + 187 rgba). Any rule that writes a literal
instead of a token fights every non-dark theme: white glass overlays are
invisible on a light page, the old accent glow clashes with the host accent,
#fff text is unreadable. The P3 pass replaced every one with a token
reference or a color-mix() derivation.

These tests pin three contracts:

  1. RATCHET — trading.css outside :root contains ZERO hex/rgba literals.
     Semantic hues may live only in :root token definitions.
  2. THEME CHAIN — for key chrome elements, resolving the token chain
     (trading.css :root → theme-bridge.css theme blocks) under
     data-theme=light/tofu must not yield a dark-surface value. jsdom cannot
     resolve var()/color-mix (measured: it returns rgba(0,0,0,0)), and the
     host lacks a runnable chromium — so the chain is resolved statically,
     which is exactly what the browser does, minus paint.
  3. BOOTSTRAP — trading.html sets data-theme BEFORE its first stylesheet
     link, so the first painted frame is already the right theme.
"""

import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, '..')
_CSS = os.path.join(_ROOT, 'tofu_trading', 'static', 'trading.css')
_BRIDGE = os.path.join(_ROOT, 'tofu_trading', 'static', 'theme-bridge.css')
_HTML = os.path.join(_ROOT, 'tofu_trading', 'templates', 'trading.html')

HEX_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b')
RGBA_RE = re.compile(r'rgba?\([^)]*\)')


# ═══════════════════════════════════════════════════════════
#  CSS block parsing helpers
# ═══════════════════════════════════════════════════════════

def _blocks(src):
    """Yield (selector, body) for every top-level ruleset.

    Comments are stripped FIRST — otherwise a header comment lands inside the
    captured selector and exact-match comparisons silently find nothing
    (measured: ':root' not in sels when the block follows a banner comment).
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    i, n = 0, len(src)
    while i < n:
        m = re.search(r'([^{}]+)\{', src[i:])
        if not m:
            break
        sel = m.group(1).strip()
        start = i + m.end()
        depth, j = 1, start
        while j < n and depth:
            if src[j] == '{':
                depth += 1
            elif src[j] == '}':
                depth -= 1
            j += 1
        yield sel, src[start:j - 1]
        i = j


def _tokens_from(body):
    """{--name: value} from a ruleset body (strips comments)."""
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    out = {}
    for m in re.finditer(r'(--[\w-]+)\s*:\s*([^;]+);', body):
        out[m.group(1)] = m.group(2).strip()
    return out


def _theme_tokens(bridge_src, theme):
    """Merged tokens for a theme: bridge :root+derived, then theme block."""
    dark = {}
    themed = {}
    derived = {}
    for sel, body in _blocks(bridge_src):
        sels = [s.strip() for s in sel.split(',')]
        toks = _tokens_from(body)
        if ':root' in sels and '[data-theme="dark"]' in sels:
            dark.update(toks)
        elif sels == [f'[data-theme="{theme}"]']:
            themed.update(toks)
        elif sels == [':root']:
            derived.update(toks)   # the derived (color-mix) token families
    merged = dict(dark)
    merged.update(themed)
    merged.update(derived)
    return merged


def _resolve(value, tokens, _depth=0):
    """Resolve var(--x) / color-mix(...var(--x)...) down to literal colours."""
    if _depth > 12:
        return value

    def _sub(m):
        name = m.group(1)
        fallback = m.group(2)
        if name in tokens:
            return _resolve(tokens[name], tokens, _depth + 1)
        return fallback.strip() if fallback else ''

    prev = None
    while prev != value:
        prev = value
        value = re.sub(r'var\((--[\w-]+)\s*(?:,\s*([^)]*))?\)', _sub, value)
    return value


# ═══════════════════════════════════════════════════════════
#  1. Ratchet: zero literals outside :root
# ═══════════════════════════════════════════════════════════

def _outside_root_lines(src):
    """Line numbers (1-based) that are NOT inside the :root block."""
    lines = src.splitlines()
    rs = next(i for i, l in enumerate(lines) if l.strip().startswith(':root'))
    depth, re_ = 0, None
    for i in range(rs, len(lines)):
        depth += lines[i].count('{') - lines[i].count('}')
        if depth <= 0:
            re_ = i
            break
    return [(i + 1, l) for i, l in enumerate(lines) if not (rs <= i <= re_)]


@pytest.mark.unit
def test_no_literals_outside_root():
    """★ The P3 ratchet. Semantic hues may live only in :root token defs."""
    src = open(_CSS, encoding='utf-8').read()
    violations = []
    for lineno, line in _outside_root_lines(src):
        # var() fallbacks that carry literals are still literals.
        for m in HEX_RE.findall(line) + RGBA_RE.findall(line):
            violations.append(f'L{lineno}: {m}  ::  {line.strip()[:80]}')
    assert not violations, (
        f'{len(violations)} literal colour(s) outside :root:\n  '
        + '\n  '.join(violations[:30]))


@pytest.mark.unit
def test_neuter_ratchet_catches_a_new_literal(tmp_path):
    """The ratchet must bite: a planted literal outside :root is found."""
    src = open(_CSS, encoding='utf-8').read()
    planted = src + '\n.foo{color:#fff}\n'
    found = []
    for lineno, line in _outside_root_lines(planted):
        found += HEX_RE.findall(line)
    assert '#fff' in found, 'ratchet is inert: planted literal not detected'


# ═══════════════════════════════════════════════════════════
#  2. Theme chain: key chrome resolves away from dark values
# ═══════════════════════════════════════════════════════════

# Dark-theme values that must NOT appear once light/tofu themes resolve.
_DARK_SURFACE_VALUES = {
    '#06080d', '#0a0d14', '#0f1219', '#151922', '#1c212e',
    'rgba(255,255,255,.06)', 'rgba(255,255,255,.08)',
    'rgba(255,255,255,.05)', 'rgba(255,255,255,.1)',
}
# A resolved colour counts as "light-theme-safe" if it is not in the dark set
# AND (for text) not a near-white meant for dark backgrounds.


def _base_hex(resolved):
    """First hex literal in a resolved declaration (the color-mix base)."""
    m = HEX_RE.search(resolved)
    return m.group(0) if m else None


def _luminance(hexcolor):
    """Relative luminance 0..1 (sRGB, WCAG approximation)."""
    h = hexcolor.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _decl_map(css_src, selector):
    """All property: value pairs across every ruleset matching selector."""
    out = {}
    for sel, body in _blocks(css_src):
        if selector in [s.strip() for s in sel.split(',')]:
            for m in re.finditer(r'([\w-]+)\s*:\s*([^;]+);', body):
                out[m.group(1)] = m.group(2).strip()
    return out


def _trading_root_tokens(css_src):
    for sel, body in _blocks(css_src):
        if sel.strip() == ':root':
            return _tokens_from(body)
    return {}


@pytest.mark.unit
@pytest.mark.parametrize('theme', ['light', 'tofu'])
def test_chrome_resolves_light(theme):
    """★ Under light/tofu, key chrome elements must not resolve to dark values."""
    css = open(_CSS, encoding='utf-8').read()
    bridge = open(_BRIDGE, encoding='utf-8').read()
    tokens = _theme_tokens(bridge, theme)
    # trading.css :root supplies anything the bridge does not override
    base = _trading_root_tokens(css)
    merged = dict(base)
    merged.update(tokens)

    # kind:
    #   surface — a bg-family panel; its base colour must be LIGHT under
    #             light/tofu themes (catches a deleted theme block).
    #   overlay — a t1-derived wash; correct on light themes means LOW alpha
    #             (subtle shading), not the base colour being light (the base
    #             is ink by design).
    probes = [
        ('.top-bar', 'background', 'surface', 'dark navy bar'),
        ('.top-bar', 'border-bottom', 'overlay', 'white-glass line'),
        ('.panel', 'background', 'surface', 'dark panel'),
        ('.btn-outline', 'background', 'overlay', 'white-glass button'),
        ('.btn-outline', 'border-color', 'overlay', 'white-glass border'),
        ('.data-table td', 'border-bottom', 'overlay', 'white-glass row line'),
        ('.form-input', 'background', 'overlay', 'white-glass input'),
    ]
    failures = []
    for selector, prop, kind, why in probes:
        decls = _decl_map(css, selector)
        if prop not in decls:
            failures.append(f'{selector} has no {prop} declaration at all')
            continue
        resolved = _resolve(decls[prop], merged)
        if resolved in _DARK_SURFACE_VALUES:
            failures.append(f'{selector} {prop} resolves to dark value '
                            f'{resolved} under {theme} ({why})')
        # white-glass detection: any resolved white-with-alpha overlay
        if re.search(r'rgba?\(255,\s*255,\s*255', resolved):
            failures.append(f'{selector} {prop} still resolves to white glass '
                            f'{resolved} under {theme} ({why})')

        base = _base_hex(resolved)
        if kind == 'surface' and base and _luminance(base) < 0.45:
            failures.append(
                f'{selector} background resolves to a DARK surface '
                f'{base} (luminance {_luminance(base):.2f}) under {theme} '
                f'— the {theme} palette is not reaching this element')
        if kind == 'overlay':
            m = re.search(r'(\d+(?:\.\d+)?)%\s*,\s*transparent', resolved)
            if m:
                if float(m.group(1)) > 40:
                    failures.append(
                        f'{selector} {prop} overlay alpha {m.group(1)}% reads '
                        f'as a solid block, not a wash, under {theme} ({why})')
            elif base:
                # No alpha channel: acceptable only as a VISIBLE mid-tone
                # solid (e.g. a real border token). Near-white disappears on
                # a light page; near-black is a heavy slab.
                lum = _luminance(base)
                if lum > 0.85 or lum < 0.05:
                    failures.append(
                        f'{selector} {prop} resolves to extreme solid {base} '
                        f'(luminance {lum:.2f}) under {theme} ({why})')
            else:
                failures.append(
                    f'{selector} {prop} overlay unresolvable: {resolved} '
                    f'under {theme} ({why})')
    assert not failures, 'theme chain resolves to dark values:\n  ' + '\n  '.join(failures)


@pytest.mark.unit
def test_dark_theme_still_resolves():
    """The default dark palette must still produce the dark design."""
    css = open(_CSS, encoding='utf-8').read()
    bridge = open(_BRIDGE, encoding='utf-8').read()
    tokens = _theme_tokens(bridge, 'dark')
    base = _trading_root_tokens(css)
    merged = dict(base)
    merged.update(tokens)
    top = _decl_map(css, '.top-bar')
    resolved = _resolve(top.get('background', ''), merged)
    assert resolved and '255,255,255' not in resolved, (
        f'dark top-bar background broke: {resolved!r}')


# ═══════════════════════════════════════════════════════════
#  3. Bootstrap: theme set before paint
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_theme_bootstrap_runs_before_stylesheets():
    html = open(_HTML, encoding='utf-8').read()
    i_set = html.index("setAttribute('data-theme'")
    i_css = html.index('rel="stylesheet"')
    assert i_set < i_css, (
        'data-theme is set AFTER the first stylesheet link — the first '
        'painted frame would use the wrong theme (dark flash)')
    assert "localStorage.getItem('claude_ui_theme')" in html, (
        'bootstrap must read the host storage key so the two surfaces agree')


@pytest.mark.unit
def test_no_undefined_token_references():
    """★ Every var(--x) a rule uses must be defined in SOME loaded stylesheet.

    Catches the class P3's literal-ratchet structurally cannot see: a rule
    referencing a token that exists NOWHERE. --profit/--loss were used by 5
    rules for a long time while defined in no file, so win/loss numbers
    silently rendered as plain text (measured after P3).
    """
    files = (_CSS, _BRIDGE,
             os.path.join(_ROOT, 'tofu_trading', 'static', 'reconcile.css'))
    defined, used = set(), set()
    for path in files:
        # Strip comments: a token MENTIONED in prose (e.g. the bridge's own
        # doc comment naming the host's --bg-primary) is not a rule reference.
        src = re.sub(r'/\*.*?\*/', '', open(path, encoding='utf-8').read(),
                     flags=re.S)
        defined |= set(re.findall(r'(--[\w-]+)\s*:', src))
        used |= set(re.findall(r'var\((--[\w-]+)', src))
    undefined = sorted(used - defined)
    assert not undefined, (
        f'token(s) referenced by rules but defined in no stylesheet: {undefined}')


@pytest.mark.unit
def test_js_canvas_uses_theme_tokens_not_literals():
    """★ The canvas chart bypasses the token system entirely (canvas cannot
    read CSS vars, so JS hardcoded dark-only literals). This pins that the
    simulator's chart reads the live theme instead."""
    js = open(os.path.join(_ROOT, 'tofu_trading', 'static', 'js', 'trading',
                           'simulator.js'), encoding='utf-8').read()
    assert 'getComputedStyle' in js, (
        'equity chart must read theme tokens via getComputedStyle')
    assert '_chartTheme' in js, 'chart palette must come from _chartTheme()'
    # The dark-only literals the old code painted with must not appear as
    # paint values (they may remain as _chartTheme FALLBACKS, which is correct).
    for stale in ("ctx.fillStyle = 'rgba(6,8,13",
                  "ctx.strokeStyle = 'rgba(255,255,255",
                  "? '#00E59B' : '#FF4D6A'"):
        assert stale not in js, (
            f'dark-only literal still painted directly: {stale!r}')


@pytest.mark.unit
def test_neuter_profit_loss_undefined_fails_guard():
    """NEUTER: stripping the --profit definition must fail the undefined-ref guard."""
    src = open(_CSS, encoding='utf-8').read()
    assert '--profit:' in src, 'precondition: --profit is defined'
    neutered = src.replace('--profit:    var(--success);', '')
    defined = set(re.findall(r'(--[\w-]+)\s*:', neutered))
    used = set(re.findall(r'var\((--[\w-]+)', neutered))
    assert '--profit' in (used - defined), (
        'guard is inert: --profit still resolves after its definition was removed')


@pytest.mark.unit
def test_bridge_defines_all_three_themes():
    bridge = open(_BRIDGE, encoding='utf-8').read()
    for theme in ('dark', 'light', 'tofu'):
        toks = _theme_tokens(bridge, theme)
        for needed in ('--bg1', '--t1', '--accent', '--border', '--success',
                       '--danger'):
            assert needed in toks, f'bridge theme {theme} missing {needed}'
