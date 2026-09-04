"""tests/test_theme_contrast.py — every theme must be READABLE, not just
render without dark-value leakage.

Why this exists
---------------
``theme-bridge.css`` gives the page three palettes (dark / light / tofu) and
``test_css_tokens.py`` already proves the token CHAIN resolves — light and
tofu do not leak dark surface values. But "resolves to a different colour"
is not "you can read it". The classic failure of a bridge like this is that
the palette is designed against the dark background and the light variants
are derived by eye; the muted text tiers then wash out on a near-white
surface while every existing test stays green.

That is exactly what was measured here (contrast on --bg3, the worst
surface, BEFORE the fix):

    tier    dark     light    tofu
    --t1   14.17    11.28    11.70     primary   — was already fine
    --t2    6.09     6.15     4.76     secondary — fine but thin on tofu
    --t3    3.26     2.81     2.17     muted     — below AA everywhere
    --t4    2.10     1.91     1.70     faint     — below even 3.0 everywhere

--t3 degraded monotonically dark → light → tofu, and **tofu is the DEFAULT**
(the bootstrap in trading.html falls back to 'tofu' when the host has not
written ``claude_ui_theme``). 92 of the 122 muted-tier rules in trading.css
pair --t3/--t4 with a font-size under 14px — small text, which WCAG holds to
the strict 4.5 bar, not the 3.0 large-text bar.

Two structural notes on the fix
-------------------------------
1. **--t4 is now an alias of --t3, not a dimmer value.** It was a
   ``color-mix`` blending --t2/--t3 toward the background, which is why it
   failed on *every* theme including dark. There is no room for a fourth
   readable tier between --t3 and the surface, so the tier was collapsed
   rather than nudged. The token is kept (122 rules reference it).
2. **--t2 was lifted to ≥7.0, not left at AA.** Raising --t3 to 4.5 while
   --t2 sat at 4.76 (tofu) would have made the two tiers indistinguishable —
   satisfying the letter of AA while destroying the visual hierarchy that
   makes the tiers worth having. ``test_tier_hierarchy_is_preserved`` pins
   the separation so a future edit cannot re-collapse them.

Discipline
----------
These assert the RESULT (a measured contrast ratio), never a token's value,
so any palette that is actually readable passes. Do NOT relax a threshold to
match a palette — that makes the guard describe the bug instead of catching
it.
"""

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, '..'))
_BRIDGE = os.path.join(_ROOT, 'tofu_trading', 'static', 'theme-bridge.css')
_CSS = os.path.join(_ROOT, 'tofu_trading', 'static', 'trading.css')
_HTML = os.path.join(_ROOT, 'tofu_trading', 'templates', 'trading.html')

sys.path.insert(0, _HERE)
# Reuse the SHIPPED resolver + luminance from the sibling suite rather than
# re-deriving them: a second copy would drift from the real token chain and
# start asserting against colours the page never renders.
import test_css_tokens as _tok  # noqa: E402

THEMES = ('dark', 'light', 'tofu')
SURFACES = ('--bg', '--bg1', '--bg2', '--bg3')
TEXT_TIERS = ('--t1', '--t2', '--t3', '--t4')

AA = 4.5
# --t2 must stay clearly ahead of --t3 or the two tiers read identically.
SECONDARY_MIN = 7.0
# --t2 : --t3 contrast-ratio quotient. Measured at 1.56x across all three
# themes after the fix; 1.35 leaves headroom for palette tuning without
# letting the tiers merge.
TIER_SEPARATION = 1.35


def _bridge():
    with open(_BRIDGE, encoding='utf-8') as f:
        return f.read()


def _contrast(fg_hex, bg_hex):
    l1, l2 = _tok._luminance(fg_hex), _tok._luminance(bg_hex)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _mix(a_hex, b_hex, weight_a):
    """sRGB blend of two hex colours (weight_a of a, remainder of b)."""
    def rgb(h):
        h = h.lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    a, b = rgb(a_hex), rgb(b_hex)
    return '#' + ''.join(
        f'{round(a[i] * weight_a + b[i] * (1 - weight_a)):02x}' for i in range(3))


def _resolved_hex(value, tokens):
    """Resolve to one literal hex, EVALUATING color-mix().

    ``_base_hex`` returns only the FIRST hex in a declaration, so for
    ``color-mix(in srgb, var(--t3) 75%, var(--bg2))`` it reports the operand
    rather than the mix. These mixes blend TOWARD the background, so every
    number computed that way is optimistic — measured: dark --t4 read 6.09
    by first-operand but 2.10 once the mix is evaluated. Skipping the
    evaluation does not merely misreport: it makes a genuinely failing
    palette PASS, which is worse than having no guard.
    """
    resolved = _tok._resolve(value, tokens)
    m = re.search(r'color-mix\(\s*in\s+srgb\s*,\s*(#[0-9a-fA-F]{3,8})\s*'
                  r'([\d.]+)%\s*,\s*(#[0-9a-fA-F]{3,8})', resolved)
    if m:
        return _mix(m.group(1), m.group(3), float(m.group(2)) / 100.0)
    return _tok._base_hex(resolved)


def _pairs(theme, tiers):
    """(tier, surface, fg_hex, bg_hex, ratio) for a theme's resolved tokens."""
    toks = _tok._theme_tokens(_bridge(), theme)
    out = []
    for t in tiers:
        for s in SURFACES:
            if t not in toks or s not in toks:
                continue
            fg = _resolved_hex(toks[t], toks)
            bg = _resolved_hex(toks[s], toks)
            if fg and bg:
                out.append((t, s, fg, bg, _contrast(fg, bg)))
    return out


def _worst(theme, tier):
    return min(r for _, _, _, _, r in _pairs(theme, (tier,)))


@pytest.mark.unit
def test_scan_surface_report():
    """Print what is measured BEFORE anything asserts on it.

    If a token is renamed or the chain stops resolving to a hex, ``_pairs``
    silently returns fewer rows and every assertion below passes by measuring
    nothing.
    """
    expected = len(TEXT_TIERS) * len(SURFACES)
    for theme in THEMES:
        rows = _pairs(theme, TEXT_TIERS)
        print(f'\n{theme}: {len(rows)} resolved text/surface pair(s)')
        for t, s, fg, bg, r in rows:
            flag = '' if r >= AA else '   << below AA'
            print(f'   {t} on {s}: {fg} / {bg} = {r:.2f}{flag}')
        assert len(rows) == expected, (
            f'{theme}: expected {expected} pairs, got {len(rows)} — a token '
            f'stopped resolving to a literal colour, so the contrast '
            f'assertions would be vacuous')


@pytest.mark.unit
def test_color_mix_is_actually_evaluated():
    """The mix evaluation is load-bearing, not a nicety.

    A ``color-mix`` toward the background is precisely how a tier ends up
    unreadable while its first operand still looks fine. If ``_resolved_hex``
    regresses to returning the operand, the AA gate below passes on a palette
    that genuinely fails — measured: reverting --t4 to its old color-mix AND
    dropping this evaluation left the whole suite green over a 1.70:1 tier.
    """
    probe = {'--x': 'color-mix(in srgb, #000000 50%, #ffffff)'}
    got = _resolved_hex(probe['--x'], probe)
    assert got and got.lower() not in ('#000000', '#000'), (
        f'color-mix returned the first operand ({got}) instead of the blend — '
        f'muted tiers would be scored optimistically and the AA gate would '
        f'pass a failing palette')
    assert 0.15 < _tok._luminance(got) < 0.35, (
        f'50/50 black-white mix resolved to {got}, whose luminance is not '
        f'mid-range — the blend maths is wrong')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_every_text_tier_meets_AA_on_every_surface(theme):
    """All four text tiers must clear WCAG AA, on every surface, every theme.

    AA (not the 3.0 large-text bar) because 92 of the 122 muted-tier rules in
    trading.css set a font-size under 14px.
    """
    bad = [f'{t} on {s}: {fg}/{bg} = {r:.2f}'
           for t, s, fg, bg, r in _pairs(theme, TEXT_TIERS) if r < AA]
    assert not bad, (
        f'theme "{theme}" has text below WCAG AA ({AA}:1):\n  '
        + '\n  '.join(bad) +
        '\n\nFix the tier in theme-bridge.css. Do NOT lower the threshold — '
        'these tiers carry timestamps, units, hints and placeholders at '
        '9-13px, which is exactly the case AA exists for.')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_tier_hierarchy_is_preserved(theme):
    """Complement: AA must not be met by flattening the tiers into one.

    Lifting --t3 to 4.5 while --t2 sits at 4.76 satisfies AA and destroys the
    hierarchy — the reader can no longer tell secondary from muted. Pin both
    the secondary floor and the gap.
    """
    t2, t3 = _worst(theme, '--t2'), _worst(theme, '--t3')
    assert t2 >= SECONDARY_MIN, (
        f'{theme}: --t2 is {t2:.2f}, below the {SECONDARY_MIN} secondary '
        f'floor — with --t3 at {t3:.2f} the two tiers are visually the same')
    assert t2 / t3 >= TIER_SEPARATION, (
        f'{theme}: --t2/--t3 separation is {t2 / t3:.2f}x (need '
        f'{TIER_SEPARATION}x) — the tiers have collapsed into one')


@pytest.mark.unit
def test_themes_are_actually_distinct():
    """Complement: passing by making every theme identical must NOT work.

    The cheapest way to satisfy the contrast assertions is to point all three
    palettes at the dark values — perfect contrast, no light theme.
    """
    seen = {}
    for theme in THEMES:
        toks = _tok._theme_tokens(_bridge(), theme)
        seen[theme] = _resolved_hex(toks['--bg'], toks)
    assert len(set(seen.values())) == len(THEMES), (
        f'themes no longer have distinct backgrounds: {seen} — contrast was '
        f'satisfied by collapsing the palettes instead of fixing them')


@pytest.mark.unit
def test_no_opacity_stacked_on_a_muted_tier():
    """A muted token plus ``opacity`` is a double dimming the tokens cannot see.

    The tier values are chosen to clear AA as rendered. An inline
    ``opacity:.6`` on top silently multiplies the shortfall back in — the
    Beta badge measured ~1.7:1 that way on the default theme while the token
    itself was compliant. Contrast must be a property of the token, so that
    fixing the token actually fixes the pixel.
    """
    with open(_HTML, encoding='utf-8') as f:
        html = f.read()
    bad = []
    for m in re.finditer(r'style="([^"]*)"', html):
        style = m.group(1)
        if re.search(r'var\(--t[234]\)', style) and re.search(r'opacity\s*:', style):
            bad.append(style[:90])
    assert not bad, (
        'inline opacity stacked on a muted text token:\n  ' + '\n  '.join(bad) +
        '\n\nThe token already encodes how dim the text should be. Drop the '
        'opacity, or pick a dimmer token — do not multiply the two.')


@pytest.mark.unit
def test_small_text_blast_radius_is_reported():
    """Report how much small text rides the muted tiers.

    Printed rather than thresholded — the count moves with ordinary UI work,
    so pinning it would be noise. The assertion only guards the scan itself.
    """
    with open(_CSS, encoding='utf-8') as f:
        src = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
    rules = re.findall(r'\{[^}]*var\(--t[34]\)[^}]*\}', src)
    small = [r for r in rules if re.search(r'font-size:\s*(?:9|10|11|12|13)px', r)]
    print(f'\nrules pairing --t3/--t4 with font-size < 14px: {len(small)} '
          f'(of {len(rules)} muted-tier rules)')
    assert rules, 'no muted-tier rules found — the scan regex has gone stale'


# ═══════════════════════════════════════════════════════════
#  Self-tinted badges — text drawn over a tint of ITSELF.
#
#  A whole class the gates above cannot see. `.sr-type-*` render
#  `color: var(--type-stock)` over
#  `background: color-mix(in srgb, var(--type-stock) 15%, transparent)` —
#  foreground and background are the SAME token, so the pair is
#  self-referential: darkening the token darkens its own backdrop too, and
#  contrast is bounded by the tint percentage rather than by the hue.
#
#  Measured before the fix: 1.35 / 1.68 / 2.54 on light and 1.42 / 1.75 /
#  2.67 on tofu — the worst readability figures anywhere on the page, worse
#  than any muted text tier. Root cause: --type-stock/etf/fund were the only
#  CONTENT colours with no per-theme override, so dark-tuned pastels were
#  reused verbatim on a near-white surface.
# ═══════════════════════════════════════════════════════════

SELF_TINTED = ('--type-stock', '--type-etf', '--type-fund')


def _self_tint_pairs(theme):
    """(token, fg, tinted_bg, ratio) for every self-tinted badge."""
    toks = _tok._theme_tokens(_bridge(), theme)
    page = _resolved_hex(toks['--bg2'], toks)
    out = []
    with open(_CSS, encoding='utf-8') as f:
        src = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
    for tok in SELF_TINTED:
        # Read the tint percentage from the RULE, not from a constant here —
        # changing 15% in the CSS must move this measurement.
        m = re.search(
            r'color-mix\(\s*in\s+srgb\s*,\s*var\(' + re.escape(tok) +
            r'\)\s*([\d.]+)%\s*,\s*transparent', src)
        if not m or tok not in toks:
            continue
        fg = _resolved_hex(toks[tok], toks)
        bg = _mix(fg, page, float(m.group(1)) / 100.0)
        out.append((tok, fg, bg, _contrast(fg, bg)))
    return out


@pytest.mark.unit
def test_self_tinted_scan_surface():
    """Confirm the self-tint pairs actually resolve before asserting on them."""
    for theme in THEMES:
        rows = _self_tint_pairs(theme)
        print(f'\n{theme}: {len(rows)} self-tinted badge(s)')
        for tok, fg, bg, r in rows:
            print(f'   {tok} {fg} on its own tint {bg} = {r:.2f}')
        assert len(rows) == len(SELF_TINTED), (
            f'{theme}: expected {len(SELF_TINTED)} self-tinted pairs, got '
            f'{len(rows)} — either a token vanished or the color-mix rule was '
            f'rewritten, so this gate would measure nothing')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_self_tinted_badges_are_readable(theme):
    """Text over a tint of itself must still clear AA.

    This is the case a per-token "is it readable on --bg3" check cannot
    catch: the backdrop moves with the foreground.
    """
    bad = [f'{tok}: {fg} on its own tint {bg} = {r:.2f}'
           for tok, fg, bg, r in _self_tint_pairs(theme) if r < AA]
    assert not bad, (
        f'theme "{theme}" has unreadable self-tinted badges:\n  '
        + '\n  '.join(bad) +
        '\n\nForeground and background are the SAME token here, so contrast '
        'is bounded by the tint percentage. Darken the token for this theme '
        '(they need a per-theme value — the dark pastels do not survive a '
        'light surface), or raise the tint percentage in trading.css.')


@pytest.mark.unit
def test_asset_type_colours_are_themed():
    """Complement: the fix must be a per-theme value, not one global tweak.

    Darkening the shared token until it passes on light would wreck it on
    dark. These three were the only content colours without an override,
    which is exactly how they ended up unreadable on two of three themes.
    """
    per_theme = {}
    for theme in THEMES:
        toks = _tok._theme_tokens(_bridge(), theme)
        per_theme[theme] = tuple(_resolved_hex(toks[t], toks) for t in SELF_TINTED)
    assert len(set(per_theme.values())) > 1, (
        f'asset-type colours are identical across all themes: {per_theme}. '
        f'A single global value cannot be readable on both a near-black and '
        f'a near-white surface — give each theme its own.')



@pytest.mark.unit
def test_raising_a_tint_never_improves_contrast():
    """Pin the direction claimed in comments and in the closed epics.

    Two tickets in a row proposed "the tint is too weak, raise the
    percentage" as the fix for unreadable chips, and a comment I wrote in
    trading.css asserted it was at least valid for the SELF-tinted badges.
    Measured, it is false in both shapes: more tint always pulls the backdrop
    toward the foreground, so contrast falls monotonically. This asserts the
    direction rather than any specific figure, so re-tuning a palette cannot
    make it falsely red — only a genuine reversal of the maths would.
    """
    toks = _tok._theme_tokens(_bridge(), 'light')
    page = _resolved_hex(toks['--bg2'], toks)
    cases = {
        'self-tinted (fg IS the tint source)': (
            _resolved_hex(toks['--type-stock'], toks),
            _resolved_hex(toks['--type-stock'], toks)),
        'different-token chip': (
            _resolved_hex(toks['--accent-text'], toks),
            _resolved_hex(toks['--accent'], toks)),
    }
    for label, (fg, fill) in cases.items():
        series = [(p, _contrast(fg, _mix(fill, page, p / 100.0)))
                  for p in (5, 15, 25, 40, 60)]
        print(f'\n  {label}: ' + '  '.join(f'{p}%={r:.2f}' for p, r in series))
        ratios = [r for _, r in series]
        assert ratios == sorted(ratios, reverse=True), (
            f'{label}: contrast did not fall monotonically as the tint rose '
            f'({series}). If this ever reverses, the "just raise the tint" '
            f'fix would become valid and the comments saying otherwise must '
            f'be corrected — but until then, raising a tint makes chips '
            f'LESS readable, never more.')


# ═══════════════════════════════════════════════════════════
#  Semantic hues — P&L green/red, accent, warning, tags.
#
#  These were outside the gate above and it stayed green over them: light
#  --success measured 2.57 and --danger 2.99, i.e. WORSE than the --t3 that
#  had just been fixed. And they matter more — --t1..--t4 carry timestamps
#  and hints, --success/--danger carry the only number the user opened the
#  page for.
#
#  The bar depends on USE, derived from trading.css rather than assumed:
#  a token rendered as `color:` is text (4.5); one used only for
#  background/border/icon is non-text (3.0, WCAG 1.4.11). Forcing 4.5 on
#  decoration would wreck the chart palette for no readability gain.
# ═══════════════════════════════════════════════════════════

SEMANTIC = ('--success', '--danger', '--warning', '--accent', '--accent-text',
            '--purple', '--cyan', '--teal', '--orange', '--yellow', '--blue')
NON_TEXT_AA = 3.0
# --success vs --danger, against EACH OTHER. Equal-luminance red/green clears
# AA against the surface yet is invisible to a red-green colourblind reader,
# and the naive per-token solve lands exactly there (measured 1.01 on light,
# 1.00 on tofu before this constraint was added).
PNL_SEPARATION = 1.40

# Aliases the P&L path actually renders through. --success/--danger have ZERO
# direct `color:` rules; they reach text via these, so a scan that does not
# follow them concludes "decoration only" and applies the wrong bar.
_TOKEN_ALIASES = {'--profit': '--success', '--loss': '--danger'}


def _semantic_usage():
    """{token: {'text': [selectors], 'nontext': [props]}} from trading.css."""
    with open(_CSS, encoding='utf-8') as f:
        src = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
    usage = {s: {'text': [], 'nontext': []} for s in SEMANTIC}
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', src):
        for m in re.finditer(r'(?:^|;)\s*([-\w]+)\s*:\s*([^;]*)', body):
            prop, val = m.group(1).strip(), m.group(2)
            for tok in re.findall(r'var\((--[\w-]+)', val):
                base = _TOKEN_ALIASES.get(tok, tok)
                if base not in usage:
                    continue
                if prop == 'color':
                    usage[base]['text'].append(sel.strip()[:60])
                else:
                    usage[base]['nontext'].append(prop)
    return usage


@pytest.mark.unit
def test_semantic_usage_scan_surface():
    """Print how each semantic hue is consumed before any bar is applied.

    The alias hop is the load-bearing part: --success/--danger reach text
    ONLY through --profit/--loss. A scan that stops at the base name reports
    them as decoration and would gate the P&L colours at 3.0.
    """
    usage = _semantic_usage()
    print('\nsemantic hue usage in trading.css:')
    for s in SEMANTIC:
        t, nt = usage[s]['text'], usage[s]['nontext']
        bar = 4.5 if t else 3.0
        print(f'  {s:<11} text×{len(t):<3} nontext×{len(nt):<4} → bar {bar}')
        for sel in t[:3]:
            print(f'        text: {sel}')
    assert usage['--success']['text'], (
        '--success resolved to zero text rules — the --profit/--loss alias '
        'hop broke, so the P&L colours would be gated as decoration (3.0) '
        'instead of as text (4.5)')
    assert usage['--danger']['text'], '--danger text usage not found (alias hop broken)'
    # --accent is deliberately a FILL only; its text role lives in
    # --accent-text (see test_accent_serves_both_roles below).
    assert usage['--accent-text']['text'], (
        '--accent-text has no text usage — either the token was dropped or '
        'the link/active rules were pointed back at the fill token')
    assert not usage['--accent']['text'], (
        '--accent is used as text again. It is the FILL that --on-accent sits '
        'on; making it light enough to read on a surface drops white-on-accent '
        'below AA. Use --accent-text for text.')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_accent_serves_both_roles(theme):
    """One hue, two directions — and one token cannot satisfy both.

    --accent is a FILL with --on-accent text on top, so it must stay dark
    enough for that white to clear AA. --accent-text is the same hue rendered
    AS TEXT on a surface, so it must be light enough to clear AA the other
    way. Measured when they were a single token: lifting it to 4.50 against
    --bg3 pushed white-on-accent from 5.39 to 3.84.
    """
    toks = _tok._theme_tokens(_bridge(), theme)
    fill = _resolved_hex(toks['--accent'], toks)
    on_fill = _resolved_hex(toks['--on-accent'], toks)
    r = _contrast(on_fill, fill)
    assert r >= AA, (
        f'{theme}: --on-accent {on_fill} on --accent fill {fill} = {r:.2f}. '
        f'The fill was lightened past what its own label can sit on — that is '
        f'what happens when --accent is tuned for its text role. Tune '
        f'--accent-text instead.')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_semantic_hues_meet_their_use_appropriate_bar(theme):
    """Text-rendered hues at AA; decoration-only hues at the non-text bar."""
    usage = _semantic_usage()
    bad = []
    for tok in SEMANTIC:
        rows = _pairs(theme, (tok,))
        if not rows:
            continue
        bar = AA if usage[tok]['text'] else NON_TEXT_AA
        for t, s, fg, bg, r in rows:
            if r < bar:
                kind = 'text' if usage[tok]['text'] else 'non-text'
                bad.append(f'{t} on {s}: {fg}/{bg} = {r:.2f} (needs {bar}, {kind})')
    assert not bad, (
        f'theme "{theme}" has semantic hues below their bar:\n  '
        + '\n  '.join(bad) +
        '\n\nThese carry meaning — profit/loss, warnings, links. Fix the hue '
        'in theme-bridge.css; do not lower the bar.')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_profit_and_loss_are_distinguishable_from_each_other(theme):
    """Complement: both may clear AA and still be the same shade of grey.

    Solving each token independently against the background does exactly
    that. Red and green must also differ in LUMINANCE so the distinction
    survives red-green colour blindness and greyscale printing.
    """
    toks = _tok._theme_tokens(_bridge(), theme)
    g = _resolved_hex(toks['--success'], toks)
    d = _resolved_hex(toks['--danger'], toks)
    sep = _contrast(g, d)
    assert sep >= PNL_SEPARATION, (
        f'{theme}: --success {g} and --danger {d} are only {sep:.2f} apart. '
        f'Both may clear AA against the surface while being indistinguishable '
        f'from each other — profit and loss would look identical to a '
        f'red-green colourblind reader. Push one further from the surface.')


@pytest.mark.unit
def test_pnl_direction_is_not_carried_by_colour_alone():
    """Colour must never be the ONLY channel stating profit vs loss.

    Every element that gets a pnlClass() tint must also carry the direction
    in text — a sign, or a word. Verified against the shipped renderers, and
    against the CSS for the one cell that showed a positive-only figure
    tinted by a different quantity.
    """
    js_dir = os.path.join(_ROOT, 'tofu_trading', 'static', 'js', 'trading')
    with open(os.path.join(js_dir, 'state.js'), encoding='utf-8') as f:
        state = f.read()
    # fmtPct is the shared formatter for signed percentages.
    m = re.search(r'F\.fmtPct\s*=\s*function[\s\S]{0,240}?\};', state)
    assert m, 'fmtPct not found — the signed-percentage formatter moved'
    assert '"+"' in m.group(0), (
        'fmtPct no longer prefixes positives with "+" — the sign is the '
        'non-colour channel for direction, and dropping it leaves colour '
        'as the only signal')

    with open(_CSS, encoding='utf-8') as f:
        css = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
    m = re.search(r'\.sim-j-equity\s*\{([^}]*)\}', css)
    assert m, '.sim-j-equity rule not found — update this guard, do not drop it'
    assert 'var(--t1)' in m.group(1), (
        '.sim-j-equity must stay neutral: it renders the portfolio VALUE '
        '(always positive) while its pnlClass tint came from the period '
        'return — colour was the only channel carrying that meaning, so a '
        'colourblind reader got nothing and others could misread a healthy '
        'balance as a loss.')


# ═══════════════════════════════════════════════════════════
#  Tinted chips — text over a translucent tint of ANOTHER token.
#
#  The third and last shape, and the one every gate above is structurally
#  blind to. They all measure a token against a FLAT SURFACE (--bg..--bg3).
#  But most chips on this page are
#      background: color-mix(in srgb, var(--accent) 12%, transparent)
#      color:      var(--accent-text)
#  i.e. the backdrop is neither the page nor a solid fill — it is the page
#  with a wash of a THIRD token over it. Measured 43 such pairs below AA
#  while every flat-surface gate was green.
#
#  Two traps this encodes, both hit during the fix:
#
#  1. Compositing is mandatory. `--red-bg` resolves to
#     `color-mix(... var(--red) 6%, transparent)`; scoring that against
#     `--red` without compositing the transparency over the page yields
#     1.00 (a colour against itself), an impossible figure that made an
#     earlier scan report 125 phantom failures.
#
#  2. Raising the tint % makes these WORSE, not better. The ticket that
#     opened this work assumed "6% is too weak, raise it". Measured on the
#     dominant cluster: 0% → 4.90, 8% → 4.38, 20% → 3.67, 50% → 2.26. The
#     tint pulls the backdrop TOWARD the text hue. The real lever is the
#     text token, which had been solved against the bare page — the chip is
#     the stricter constraint and was never in that solve.
#
#  Gradients are skipped on purpose: a two-stop `linear-gradient` has no
#  single backdrop colour, so any number computed for it would be invented.
# ═══════════════════════════════════════════════════════════


def _composite(spec, tokens, page):
    """Resolve a background spec to the colour the EYE sees.

    Returns None for gradients — deliberately unmeasurable, not skipped
    silently: the caller counts them so a regex that starts matching nothing
    cannot masquerade as "all clear".
    """
    resolved = _tok._resolve(spec, tokens)
    if 'gradient' in resolved:
        return None
    m = re.search(r'color-mix\(\s*in\s+srgb\s*,\s*(#[0-9a-fA-F]{6})\s*'
                  r'([\d.]+)%\s*,\s*transparent', resolved)
    if m:
        return _mix(m.group(1), page, float(m.group(2)) / 100.0)
    m = re.search(r'color-mix\(\s*in\s+srgb\s*,\s*(#[0-9a-fA-F]{6})\s*'
                  r'([\d.]+)%\s*,\s*(#[0-9a-fA-F]{6})', resolved)
    if m:
        return _mix(m.group(1), m.group(3), float(m.group(2)) / 100.0)
    m = re.search(r'#[0-9a-fA-F]{6}', resolved)
    return m.group(0) if m else None


def _chip_rules():
    """Every CSS rule that sets BOTH a colour and a background from tokens."""
    with open(_CSS, encoding='utf-8') as f:
        src = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
    out = []
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', src):
        bg = re.search(r'(?:^|;)\s*background(?:-color)?\s*:\s*([^;]*)', body)
        fg = re.search(r'(?:^|;)\s*color\s*:\s*([^;]*)', body)
        if not (bg and fg):
            continue
        if 'var(' not in bg.group(1) and 'var(' not in fg.group(1):
            continue
        out.append((sel.strip()[:60], fg.group(1).strip(), bg.group(1).strip()))
    return out


def _chip_pairs(theme):
    """(selector, fg, composited_bg, ratio) plus the gradient skip count."""
    toks = _tok._theme_tokens(_bridge(), theme)
    page = _resolved_hex(toks['--bg2'], toks)
    rows, skipped = [], 0
    for sel, fspec, bspec in _chip_rules():
        bg = _composite(bspec, toks, page)
        if bg is None:
            skipped += 1
            continue
        fg = _composite(fspec, toks, page)
        if not fg:
            continue
        rows.append((sel, fg, bg, _contrast(fg, bg)))
    return rows, skipped


@pytest.mark.unit
def test_chip_scan_surface():
    """Print the measured chip population before anything asserts on it.

    A regex that stops matching would leave this gate green over the whole
    class, which is exactly how these 43 pairs went unnoticed while the
    flat-surface gates passed.
    """
    total = len(_chip_rules())
    print(f'\nrules setting both colour and background from tokens: {total}')
    for theme in THEMES:
        rows, skipped = _chip_pairs(theme)
        below = [r for r in rows if r[3] < AA]
        print(f'  {theme:<6} {len(rows)} measurable, {skipped} gradient(skipped), '
              f'{len(below)} below AA')
        for sel, fg, bg, r in sorted(below, key=lambda x: x[3])[:8]:
            print(f'      {r:5.2f}  {sel}  {fg} on {bg}')
    assert total >= 60, (
        f'only {total} colour+background rules matched — the chip scan has '
        f'gone stale and the gate below would pass by measuring nothing')


@pytest.mark.unit
@pytest.mark.parametrize('theme', THEMES)
def test_text_on_tinted_chips_meets_AA(theme):
    """Text on a translucent chip must clear AA once the tint is composited."""
    rows, _ = _chip_pairs(theme)
    bad = [f'{sel}: {fg} on {bg} = {r:.2f}' for sel, fg, bg, r in rows if r < AA]
    assert not bad, (
        f'theme "{theme}" has unreadable text on tinted chips:\n  '
        + '\n  '.join(sorted(bad)) +
        '\n\nDo NOT raise the tint percentage — measured, that makes it worse '
        '(the tint pulls the backdrop toward the text hue). Darken the TEXT '
        'token for this theme against the WORST tint it is ever placed on; '
        'solving it against the bare page is what left this gap.')


@pytest.mark.unit
def test_transparency_is_actually_composited():
    """The compositing step is load-bearing, not cosmetic.

    Without it a chip is scored against the raw token, which for
    ``--red`` on ``--red-bg`` gives 1.00 — a colour against itself. An
    earlier version of this scan did exactly that and reported 125 phantom
    failures, three times the real count.
    """
    probe = {'--x': 'color-mix(in srgb, #ff0000 50%, transparent)'}
    got = _composite(probe['--x'], probe, '#ffffff')
    assert got and got.lower() not in ('#ff0000', '#f00'), (
        f'composite returned the raw operand ({got}) instead of blending it '
        f'over the page — chip contrast would be scored against the wrong '
        f'backdrop entirely')
    # 50% red over white is a pink, i.e. strictly lighter than pure red.
    assert _tok._luminance(got) > _tok._luminance('#ff0000'), (
        f'50% red over white resolved to {got}, which is not lighter than '
        f'#ff0000 — the alpha maths is inverted')


@pytest.mark.unit
def test_gradients_are_skipped_not_faked():
    """Complement: a two-stop gradient must be reported as unmeasurable.

    If ``_composite`` ever starts returning some arbitrary stop for a
    gradient, this gate would silently begin asserting a made-up number.
    """
    probe = {'--g': 'linear-gradient(135deg, #000000 0%, #ffffff 100%)'}
    assert _composite(probe['--g'], probe, '#888888') is None, (
        'a gradient resolved to a single colour — there is no one backdrop '
        'for a two-stop gradient, so any contrast figure for it is invented')
    # And the population must actually contain some, or the skip path is dead.
    _, skipped = _chip_pairs('dark')
    assert skipped > 0, (
        'no gradient backgrounds found in the chip population — either they '
        'were all removed (then drop this test) or the scan stopped seeing '
        'them (then the skip path is untested)')
