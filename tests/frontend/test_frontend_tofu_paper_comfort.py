"""Tofu "paper room" comfort invariants — a drift guard, not a taste test.

Symptoms this guards against (owner report 2026-10-20: "the background feels a
bit too warm, and somehow slightly uncomfortable to look at"):

1. AMBIENT CHROMA. The palette had pushed every surface to OKLCH C 0.024-0.043
   with the hue pinned at 85-88deg, so the whole viewport was one cream field
   with no neutral to rest on. Warmth must live in HUE at LOW chroma: the ground
   stays under C 0.020, panels under C 0.024, and the stepped-up paper sheet is
   near-neutral (C <= 0.010) so exactly one surface reads clean.

   INVARIANT: chroma of --bg-primary / --bg-secondary / the composer sheet stays
   inside those ceilings.

2. A FULL-VIEWPORT LUMINANCE RAMP. `body` used to paint a 1240x640 highlight
   top-left into a caramel falloff bottom-right. A slow gradient under every
   pixel leaves no flat field to focus on and reads as "unevenly lit".

   INVARIANT: the tofu `body` ground is a flat colour (a token or one hex), never
   a `*gradient()`.

3. INK THAT DISSOLVES INTO THE PAPER. --text-tertiary was #9A8F79 = 2.78:1 on
   --bg-primary, so timestamps, counts and meta labels were below even the 3:1
   floor; --accent #A0602F measured 4.35:1 while documented as 4.6:1.

   INVARIANT: --text-tertiary >= 4.0:1 on the ground and >= 3.5:1 on panels;
   --accent >= 4.5:1 on the ground (it carries accent-as-text).

Env-independent: parses the COMPOSED sheet, so it also catches "edited the
source sections but never recomposed static/styles.css".

NEUTERS are in-memory string mutations — the working tree is never written
(a sibling may read or commit this shared file mid-test).
"""

from __future__ import annotations

import math
import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

GROUND_CHROMA_CEILING = 0.020
PANEL_CHROMA_CEILING = 0.024
SHEET_CHROMA_CEILING = 0.010

APPLE_AA = 4.5
TERTIARY_FLOOR_ON_GROUND = 4.0
TERTIARY_FLOOR_ON_PANEL = 3.5


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _theme_block(css: str, token: str) -> str:
    """The tofu token block that actually declares ``token`` (the composed sheet
    prepends a base layer, so the first ``[data-theme]`` block is not the owner)."""
    for m in re.finditer(r'\[data-theme="tofu"\]\{(.*?)\n\}', css, re.S):
        if re.search(re.escape(token) + r'\s*:', m.group(1)):
            return m.group(1)
    raise AssertionError(f'tofu block declaring {token} not found')


def _token(css: str, name: str) -> str:
    block = _theme_block(css, name)
    m = re.search(re.escape(name) + r'\s*:\s*([^;}]+)', block)
    assert m, f'{name} has no value'
    return m.group(1).strip()


def _hex(value: str) -> str:
    m = re.fullmatch(r'#?([0-9A-Fa-f]{6})', value.strip())
    assert m, f'expected a 6-digit hex, got {value!r}'
    return m.group(1).upper()


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hex6: str) -> tuple[int, int, int]:
    return tuple(int(hex6[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _relative_luminance(hex6: str) -> float:
    r, g, b = (_linear(c) for c in _rgb(hex6))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _oklab_chroma(hex6: str) -> float:
    """OKLab chroma — perceptual saturation, which is what "too warm / stained"
    actually tracks. HSV saturation of a cream is low even when the screen is
    entirely cream, so it cannot express this."""
    r, g, b = (_linear(c) for c in _rgb(hex6))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    a_axis = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b_axis = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return math.hypot(a_axis, b_axis)


def _rule_body(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r'\{([^}]*)\}', css)
    assert m, f'{selector} rule not found'
    return m.group(1)


def _sheet_hexes(body: str) -> list[str]:
    """Every colour painted into the sheet's BACKGROUND — all gradient stops,
    never the border or the inset highlight. Checking only the first stop lets a
    tinted bottom stop smuggle the cream back in while the assertion passes."""
    background = re.search(r'background\s*:\s*([^;]+)', body)
    assert background, f'no background in {body!r}'
    hexes = [h.upper() for h in re.findall(r'#([0-9A-Fa-f]{6})', background.group(1))]
    assert hexes, f'no colour in {background.group(1)!r}'
    return hexes


# ── 1. ambient chroma ───────────────────────────────────────────────────────

@pytest.mark.parametrize('token, ceiling', [
    ('--bg-primary', GROUND_CHROMA_CEILING),
    ('--bg-secondary', PANEL_CHROMA_CEILING),
])
def test_ground_and_panels_stay_low_chroma(token: str, ceiling: float) -> None:
    css = _read(CSS)
    chroma = _oklab_chroma(_hex(_token(css, token)))
    assert chroma <= ceiling, (
        f'{token} chroma {chroma:.4f} exceeds {ceiling}: the theme is drifting '
        f'back into a uniform cream field with no neutral for the eye to rest on')


def test_composer_sheet_is_the_one_near_neutral_surface() -> None:
    """Paper-on-desk needs ONE clean sheet. If the card is as tinted as the
    ground, the two washes read as a stained screen instead of a hierarchy."""
    css = _read(CSS)
    for sheet in _sheet_hexes(_rule_body(css, '[data-theme="tofu"] .input-area .input-box')):
        chroma = _oklab_chroma(_hex(sheet))
        assert chroma <= SHEET_CHROMA_CEILING, (
            f'composer sheet stop {sheet} chroma {chroma:.4f} exceeds '
            f'{SHEET_CHROMA_CEILING}: it is no longer the neutral reference surface')


def test_neuter_tinted_sheet_fails_the_chroma_ceiling() -> None:
    """NEUTER (in-memory): restore the old cream card fill — the assertion must
    fail, proving it is load-bearing rather than comparing two constants."""
    css = _read(CSS)
    current = _rule_body(css, '[data-theme="tofu"] .input-area .input-box')
    cream = re.search(r'linear-gradient\(180deg,#[0-9A-Fa-f]{6} 0%,#[0-9A-Fa-f]{6} 100%\)',
                      current)
    assert cream, 'composer sheet rule changed shape — update the neuter'
    neutered = css.replace(
        cream.group(0),
        'linear-gradient(180deg,#FDF8EC 0%,#FDF8EC 100%)', 1)
    assert neutered != css
    for sheet in _sheet_hexes(_rule_body(neutered, '[data-theme="tofu"] .input-area .input-box')):
        assert _oklab_chroma(_hex(sheet)) > SHEET_CHROMA_CEILING


# ── 2. flat ground ──────────────────────────────────────────────────────────

def test_tofu_body_ground_is_flat() -> None:
    """No ambient full-screen ramp: a low-frequency gradient under every pixel
    leaves no flat field and reads as uneven lighting."""
    css = _read(CSS)
    body = _rule_body(css, '[data-theme="tofu"] body')
    assert 'gradient(' not in body, (
        'the tofu body ground is a gradient again; the owner reported that '
        'ambient ramp as uncomfortable, not warm')
    assert body.strip(), 'the tofu body rule paints nothing'


def test_neuter_body_gradient_fails_the_flat_ground_rule() -> None:
    """NEUTER (in-memory): put a radial highlight back on body."""
    css = _read(CSS)
    m = re.search(r'(\[data-theme="tofu"\] body\{)([^}]*)(\})', css)
    assert m
    neutered = css[:m.start(2)] + (
        'background:radial-gradient(1240px 640px at 16% -14%,#FFFDF6 0%,'
        'rgba(255,253,246,0) 62%),var(--bg-primary)') + css[m.end(2):]
    assert 'gradient(' in _rule_body(neutered, '[data-theme="tofu"] body')


# ── 3. ink that does not dissolve ───────────────────────────────────────────

@pytest.mark.parametrize('ground, floor', [
    ('--bg-primary', TERTIARY_FLOOR_ON_GROUND),
    ('--bg-secondary', TERTIARY_FLOOR_ON_PANEL),
])
def test_tertiary_ink_is_legible_on_its_grounds(ground: str, floor: float) -> None:
    """Timestamps, counts and meta labels live on --text-tertiary; at 2.78:1
    they dissolved into the cream and made the whole screen feel effortful."""
    css = _read(CSS)
    ratio = _contrast(_hex(_token(css, '--text-tertiary')), _hex(_token(css, ground)))
    assert ratio >= floor, (
        f'--text-tertiary on {ground} is {ratio:.2f}:1, below the {floor}:1 '
        f'floor — meta text is dissolving into the paper again')


def test_accent_clears_aa_as_text_on_the_ground() -> None:
    css = _read(CSS)
    ratio = _contrast(_hex(_token(css, '--accent')), _hex(_token(css, '--bg-primary')))
    assert ratio >= APPLE_AA, (
        f'--accent on --bg-primary is {ratio:.2f}:1 — accent-as-text (links, '
        f'counts, inline marks) no longer clears AA')


def test_neuter_pale_tertiary_fails_the_legibility_floor() -> None:
    """NEUTER (in-memory): restore the shipped #9A8F79 and the original ground;
    the contrast assertion must fail."""
    rendered = _read(CSS).replace('--text-tertiary:#7F7360', '--text-tertiary:#9A8F79', 1)
    ratio = _contrast(_hex(_token(rendered, '--text-tertiary')),
                      _hex(_token(rendered, '--bg-primary')))
    assert ratio < TERTIARY_FLOOR_ON_GROUND
