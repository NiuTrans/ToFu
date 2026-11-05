"""Rendered tofu palette: low chroma, flat ground and readable metadata.

Loads the shipped cascade through the shared isolated browser fixture. Fault
injection adds page-local overriding declarations; it never edits stylesheets.
"""
from __future__ import annotations

import math

import pytest

from tests.support.frontend_visual import (
    frontend_visual_origin,  # noqa: F401
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual

GROUND_CHROMA_CEILING = 0.020
PANEL_CHROMA_CEILING = 0.024
SHEET_CHROMA_CEILING = 0.010
TERTIARY_FLOOR_ON_GROUND = 4.0
TERTIARY_FLOOR_ON_PANEL = 3.5
ACCENT_AA = 4.5


@pytest.fixture
def paper_page(frontend_visual_page):
    page = frontend_visual_page
    page.emulate_media(reduced_motion='reduce')
    page.evaluate("""() => {
      document.documentElement.dataset.theme = 'tofu';
      document.body.innerHTML = `
        <div id="panel" style="background:var(--bg-secondary)"></div>
        <div class="input-area"><div class="input-box"></div></div>
        <span id="tertiary" style="color:var(--text-tertiary)">Metadata</span>
        <a id="accent" style="color:var(--accent)">Link</a>`;
    }""")
    return page


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hex6: str) -> tuple[int, int, int]:
    return tuple(int(hex6[i:i + 2], 16) for i in (0, 2, 4))


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


def _colors(page, selector, *, foreground=False):
    """Resolve browser-computed paints, including every gradient colour stop."""
    colors = page.locator(selector).evaluate(r"""(element, foreground) => {
      const style = getComputedStyle(element);
      const image = style.backgroundImage;
      const values = foreground ? [style.color] : [style.backgroundColor,
        ...(image.match(/(?:rgba?|hsla?|oklch|oklab|lch|lab|color)\([^)]*\)/g) || [])];
      if (!foreground && image !== 'none' && values.length === 1)
        throw new Error('Unmeasured background image: ' + image);
      const context = document.createElement('canvas').getContext('2d');
      return values.map(value => {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = value;
        context.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
        if (a === 0) return null;
        if (a !== 255) throw new Error('Expected opaque palette paint: ' + value);
        return [r, g, b].map(channel => channel.toString(16).padStart(2, '0')).join('');
      }).filter(Boolean);
    }""", foreground)
    assert colors, f'{selector} has no measurable paint'
    return colors


def _assert_chroma(page, selector, ceiling):
    for color in _colors(page, selector):
        chroma = _oklab_chroma(color)
        assert chroma <= ceiling, f'{selector} chroma {chroma:.4f} exceeds {ceiling}'


def _assert_flat_ground(page):
    image = page.locator('body').evaluate('el => getComputedStyle(el).backgroundImage')
    assert image == 'none', f'ambient ground must be flat: {image}'
    assert _colors(page, 'body')


def _assert_contrast(page, ink, ground, floor):
    foreground, = _colors(page, ink, foreground=True)
    background, = _colors(page, ground)
    ratio = _contrast(foreground, background)
    assert ratio >= floor, f'{ink} on {ground}: contrast {ratio:.2f}:1 below {floor}:1'


@pytest.mark.parametrize('selector,ceiling', [
    ('body', GROUND_CHROMA_CEILING), ('#panel', PANEL_CHROMA_CEILING),
    ('.input-box', SHEET_CHROMA_CEILING),
])
def test_rendered_surfaces_stay_low_chroma(paper_page, selector, ceiling):
    _assert_chroma(paper_page, selector, ceiling)


@pytest.mark.parametrize('stops', [
    '#FDF8EC 0%, #FFFFFF 100%', '#FFFFFF 0%, #FDF8EC 100%',
])
def test_sheet_chroma_guard_rejects_either_tinted_stop(paper_page, stops):
    paper_page.add_style_tag(content=(
        f'.input-area .input-box {{background:linear-gradient(180deg,{stops}) !important}}'))
    with pytest.raises(AssertionError, match='chroma'):
        _assert_chroma(paper_page, '.input-box', SHEET_CHROMA_CEILING)


def test_surface_guard_observes_later_cascade_overrides(paper_page):
    paper_page.add_style_tag(content='html[data-theme="tofu"] {--bg-primary:#F5DDB0 !important}')
    with pytest.raises(AssertionError, match='chroma'):
        _assert_chroma(paper_page, 'body', GROUND_CHROMA_CEILING)


def test_rendered_ground_is_flat(paper_page):
    _assert_flat_ground(paper_page)


def test_flat_ground_guard_rejects_ambient_gradient(paper_page):
    paper_page.add_style_tag(content=(
        'body {background:radial-gradient(#FFFDF6, transparent),var(--bg-primary) !important}'))
    with pytest.raises(AssertionError, match='flat'):
        _assert_flat_ground(paper_page)


@pytest.mark.parametrize('ground,floor', [
    ('body', TERTIARY_FLOOR_ON_GROUND), ('#panel', TERTIARY_FLOOR_ON_PANEL),
])
def test_rendered_metadata_is_readable(paper_page, ground, floor):
    _assert_contrast(paper_page, '#tertiary', ground, floor)


def test_rendered_accent_clears_aa(paper_page):
    _assert_contrast(paper_page, '#accent', 'body', ACCENT_AA)


def test_contrast_guard_rejects_pale_metadata(paper_page):
    paper_page.add_style_tag(content='html[data-theme="tofu"] {--text-tertiary:#9A8F79 !important}')
    with pytest.raises(AssertionError, match='contrast'):
        _assert_contrast(paper_page, '#tertiary', 'body', TERTIARY_FLOOR_ON_GROUND)
