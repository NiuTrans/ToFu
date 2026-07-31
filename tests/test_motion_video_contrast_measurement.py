"""tests/test_motion_video_contrast_measurement.py — how NOT to measure
on-frame contrast.

This suite pins a MEASUREMENT METHOD, not a product behaviour, and it exists
because the wrong method already produced a wrong conclusion in this project.

**The incident (measured 2026-07-29).** Investigating a renderer finding on a
real scene, a probe measured each text node's contrast by screenshotting the
node's bounding box and averaging every pixel in it to get "the background".
On two 52px circular badges it reported **1.05** and **1.09** — catastrophic
failures, far under WCAG's 4.5. Acting on that number would have meant
redesigning a component that was never broken.

The badges were fine. Their true contrast, computed from the CSS colours, is
**6.45** and **9.32**; recomputed from the actually-rendered pixels (dark glyph
on its own saturated fill) it is **4.99** and **9.32**. The 1.05 was an
artefact of the method:

  * the box of a 52px circle is mostly NOT the glyph — it is the circle's own
    fill plus the corners outside the circle entirely;
  * averaging all of that yields a "background" that is really a blend of the
    fill, the corners and the glyph's own dark pixels;
  * blending a dark glyph into its own background drives the two luminances
    together, so the ratio collapses toward 1.0.

A first correction — hiding ALL text nodes at once before screenshotting —
moved one badge from 1.05 to 2.42 and still did not reach the truth, because
the box still contained the circle's fill and its transparent corners. The
error is not "we forgot to hide the text"; it is **box-averaging itself**.

**Why keep this as a test when no shipping code measures contrast today.**
Because the next person to add a real contrast gate will reach for exactly
this shape — screenshot the box, average it, compare — and will get false
failures on every badge, pill, icon and rounded chip in the corpus. The trap
is cheap to encode and expensive to rediscover: it cost a full investigation
cycle and produced a confidently-wrong diagnosis that had to be retracted.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ── The WCAG maths, stated once ───────────────────────────

def _rel_luminance(rgb) -> float:
    def _ch(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * _ch(r) + 0.7152 * _ch(g) + 0.0722 * _ch(b)


def contrast_ratio(fg, bg) -> float:
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


#: The two badges from the real scene, as authored.
_MINUS_GLYPH, _MINUS_FILL = (0x2a, 0x0d, 0x0d), (0xf4, 0x72, 0x72)
_PLUS_GLYPH, _PLUS_FILL = (0x2a, 0x1d, 0x05), (0xf5, 0xb9, 0x42)


def test_the_real_badges_pass_wcag():
    """Ground truth: these components were never broken."""
    assert contrast_ratio(_MINUS_GLYPH, _MINUS_FILL) == pytest.approx(6.45,
                                                                      abs=0.05)
    assert contrast_ratio(_PLUS_GLYPH, _PLUS_FILL) == pytest.approx(9.32,
                                                                    abs=0.05)
    for glyph, fill in ((_MINUS_GLYPH, _MINUS_FILL), (_PLUS_GLYPH, _PLUS_FILL)):
        assert contrast_ratio(glyph, fill) >= 4.5


def _box_average_background(glyph, fill, *, glyph_share: float,
                            outside_share: float, outside=(11, 16, 38)):
    """Reproduce the BAD method: average the whole bounding box.

    A circular badge's box holds three populations — the glyph, the circle
    fill, and the corners outside the circle (which show whatever is behind
    the badge). The bad method blends all three and calls the result
    "background".
    """
    fill_share = 1.0 - glyph_share - outside_share
    assert fill_share > 0
    return tuple(glyph[i] * glyph_share
                 + fill[i] * fill_share
                 + outside[i] * outside_share for i in range(3))


def test_box_averaging_manufactures_a_false_failure():
    """THE trap, reproduced with the real geometry.

    A 52px circle inscribed in a 52px box leaves ~21% of the box outside the
    circle; the glyph covers roughly a third of what remains. Averaging that
    box produces a "background" close enough to the glyph that the ratio
    collapses — the component reads as a catastrophic failure while being
    perfectly legible.
    """
    bad_bg = _box_average_background(_MINUS_GLYPH, _MINUS_FILL,
                                     glyph_share=0.30, outside_share=0.21)
    bad_ratio = contrast_ratio(_MINUS_GLYPH, bad_bg)
    true_ratio = contrast_ratio(_MINUS_GLYPH, _MINUS_FILL)

    assert true_ratio >= 4.5, 'precondition: the badge really is fine'
    assert bad_ratio < 4.5, (
        'box-averaging must be shown to FAIL a passing component — that is '
        'the whole point of this guard')
    assert bad_ratio < true_ratio / 2, (
        f'the artefact should be dramatic, not marginal: {bad_ratio:.2f} vs '
        f'{true_ratio:.2f}')


def test_hiding_all_text_first_does_not_rescue_box_averaging():
    """The tempting half-fix, and why it is not enough.

    Removing the glyph from the sample raises the number (measured on the real
    scene: 1.05 -> 2.42) but the box still contains the circle's fill AND the
    corners outside it, so the result is still not the colour actually behind
    the glyph.
    """
    without_glyph = _box_average_background(_MINUS_GLYPH, _MINUS_FILL,
                                            glyph_share=0.0,
                                            outside_share=0.21)
    improved = contrast_ratio(_MINUS_GLYPH, without_glyph)
    naive = contrast_ratio(
        _MINUS_GLYPH,
        _box_average_background(_MINUS_GLYPH, _MINUS_FILL,
                                glyph_share=0.30, outside_share=0.21))

    assert improved > naive, 'hiding the glyph does help…'
    assert improved < contrast_ratio(_MINUS_GLYPH, _MINUS_FILL), (
        '…but it still under-reports, because the corners outside the circle '
        'are still averaged in — the defect is box-averaging, not the glyph')


def test_sampling_the_backdrop_directly_gives_the_truth():
    """The correct method: sample the colour actually BEHIND the glyph.

    Whatever the implementation (computed backdrop, dominant colour of the
    covered region, per-glyph-pixel sampling), the requirement is that the
    'background' is the surface the glyph sits on — never a mixture that
    includes the glyph or unrelated neighbouring pixels.
    """
    assert contrast_ratio(_MINUS_GLYPH, _MINUS_FILL) >= 4.5
    assert contrast_ratio(_PLUS_GLYPH, _PLUS_FILL) >= 4.5


def test_small_and_round_elements_are_where_the_error_concentrates():
    """The bias is systematic, not random: the smaller the glyph's share of
    its box, the worse box-averaging under-reports. Any future gate must be
    validated against a badge/pill/icon, not only against a paragraph."""
    ratios = []
    for glyph_share in (0.10, 0.30, 0.60):
        bg = _box_average_background(_MINUS_GLYPH, _MINUS_FILL,
                                     glyph_share=glyph_share,
                                     outside_share=0.21)
        ratios.append(contrast_ratio(_MINUS_GLYPH, bg))
    assert ratios == sorted(ratios, reverse=True), (
        'more glyph in the box -> lower reported ratio; the error grows as '
        'the element gets smaller relative to its box')


def test_a_genuinely_low_contrast_pair_is_still_caught():
    """The complement — the maths must not be so forgiving that a real
    failure passes. Grey-on-grey has to fail however it is sampled."""
    assert contrast_ratio((0x88, 0x88, 0x88), (0x99, 0x99, 0x99)) < 4.5


# ══════════════════════════════════════════════════════════
# The craft guide must carry the occlusion DIRECTION
# ══════════════════════════════════════════════════════════

def test_craft_guide_states_which_element_carries_the_occlusion_flag():
    """Measured 2026-07-29: all three guides mentioned the flag ZERO times.

    The author learned it existed only from the CLI's own fix hint, which says
    "mark intentional layering with data-layout-allow-occlusion" WITHOUT
    naming the element — so it marked the coverer, spent 5 repair rounds and
    53k tokens, and the finding never cleared. The direction is the whole
    content of the lesson; a guide that names the flag but not the direction
    would reproduce the same failure.
    """
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, 'lib', 'motion_video', 'guide',
                        'MOTION_CRAFT.md')
    with open(path, encoding='utf-8') as f:
        text = f.read()

    assert 'data-layout-allow-occlusion' in text, (
        'the guide must document the flag at all')
    low = text.lower()
    assert 'covered' in low and 'coverer' in low, (
        'the guide must contrast the COVERED text with the COVERER — naming '
        'the flag without its direction is what produced the defect')
    assert 'wrong' in low and 'right' in low, (
        'the guide should show the wrong and right placement side by side')
