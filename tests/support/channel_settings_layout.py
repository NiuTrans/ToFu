"""Geometry checks for the channel Settings panels (大象, GitHub) in real Chromium.

The panels share one presentation (frontend/src/features/settings/
channel-settings-view.ts, styles/settings/13-dx-channel.css); these checks
state its alignment contract over a Playwright locator of a panel.
"""
from __future__ import annotations

# Sub-pixel layout rounds differently per edge; one CSS pixel is the tolerance.
_PIXEL = 1


def box(locator) -> dict:
    found = locator.bounding_box()
    assert found is not None, locator
    return found


def _center(found: dict) -> float:
    return found['y'] + found['height'] / 2


def _bottom(found: dict) -> float:
    return found['y'] + found['height']


def _settle_frame_animations(panel) -> None:
    """Wait for the Settings frame's finite animations (its opening scale, tab transitions).

    Each box is a separate measurement, so two boxes taken on different frames of
    a running scale would disagree although their layout agrees.
    """
    panel.evaluate("""(el) => Promise.all(el.closest('.modal').getAnimations({subtree: true})
      .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
      .map((animation) => animation.finished.catch(() => undefined)))""")


def assert_channel_layout_aligned(panel) -> None:
    """Card heads center their aside on the title; the visible controls of an action
    row share one bottom edge and height; every thread cell starts under its header;
    nothing escapes the Settings content scroller to stretch the fixed Settings frame
    (a focus or scroll-into-view would otherwise shift the whole frame)."""
    _settle_frame_animations(panel)
    overflow = panel.evaluate("""(el) => { const frame = el.closest('.modal');
      return {scroll: frame.scrollHeight, client: frame.clientHeight}; }""")
    assert overflow['scroll'] <= overflow['client'] + _PIXEL, overflow
    for head in panel.locator('.channel-card-head').all():
        title, aside = box(head.locator('.channel-card-title')), head.locator(':scope > :not(.channel-card-title)')
        if aside.count():
            chip = box(aside.first)
            assert abs(_center(title) - _center(chip)) <= _PIXEL, (title, chip)
    for row in panel.locator('.channel-actions').all():
        controls = [box(control) for control in row.locator('select, input, button').all() if control.is_visible()]
        if not controls:
            continue
        assert max(map(_bottom, controls)) - min(map(_bottom, controls)) <= _PIXEL, controls
        heights = [control['height'] for control in controls]
        assert max(heights) - min(heights) <= _PIXEL, controls
    for table in panel.locator('.channel-table').all():
        columns = [box(span)['x'] for span in table.locator('.channel-table-head > span').all()]
        for row in table.locator('.channel-row').all():
            cells = [box(row.locator('.channel-row-title')), *map(box, row.locator('.channel-cell').all())]
            assert all(abs(cell['x'] - x) <= _PIXEL for cell, x in zip(cells, columns, strict=True)), (columns, cells)
