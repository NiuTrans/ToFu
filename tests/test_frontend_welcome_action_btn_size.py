"""Regression guard for the welcome-screen action buttons' size override.

WHY — same bug CLASS as the orphan-resume button, different surface.
``.action-btn`` (static/styles.css) is the project's fixed-size ICON-ONLY square
(``width:32px;height:32px``; global ``box-sizing:border-box`` makes that
authoritative). The connection-error / 404 / load-error welcome screens in
``static/js/core/conversations.js`` render TEXT-bearing buttons (Retry,
"Remove from sidebar", "New Chat") that reuse ``.action-btn`` for its cosmetics.
Without a size override the 32×32 square wins and squashes the text button.
The fix pins ``width:auto;height:auto`` inline on each.

Every ``class="action-btn"`` in this file is such a text button (there is no
icon-only ``.action-btn`` here), so the invariant is simple and total: EVERY
``.action-btn`` occurrence in ``conversations.js`` must carry both
``width:auto`` and ``height:auto`` in its inline style. That covers the whole
class of bug on this surface, not just the buttons that were reported.

NEGATIVE CONTROL: ``test_nc_stripping_override_goes_red`` removes the override
from ONE button in a COPY of the source and asserts the check fails — proving
the guard has teeth. The shipped file is left byte-identical.

Pure source-level (no node/jsdom needed): the bug lives entirely in the static
markup emitted into ``innerHTML``.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CONV_SRC = os.path.join(ROOT, 'static', 'js', 'core', 'conversations.js')

# Capture the inline style attribute that immediately follows a
# `class="action-btn"` in the button markup, e.g.
#   class="action-btn" style="width:auto;height:auto;padding:8px 16px;cursor:pointer"
_ACTION_BTN_STYLE_RE = re.compile(
    r'class="action-btn"\s+style="(?P<style>[^"]*)"')


def _action_btn_styles(source: str) -> list[str]:
    """Return the inline-style string of every `.action-btn` in the source.

    Raises AssertionError if none are found (a structural change this guard
    must be updated for).
    """
    styles = [m.group('style') for m in _ACTION_BTN_STYLE_RE.finditer(source)]
    assert styles, 'no `class="action-btn" style="..."` buttons found — guard is stale'
    return styles


def _missing_override(styles: list[str]) -> list[str]:
    """Return the styles that FAIL to override the fixed 32x32 square."""
    return [s for s in styles
            if 'width:auto' not in s or 'height:auto' not in s]


def test_every_welcome_action_btn_overrides_fixed_square():
    with open(_CONV_SRC, encoding='utf-8') as f:
        source = f.read()
    styles = _action_btn_styles(source)
    # Sanity: the four known welcome buttons (Retry ×2, Remove, New Chat).
    assert len(styles) >= 4, f'expected >=4 welcome action buttons, found {len(styles)}'
    bad = _missing_override(styles)
    assert not bad, (
        'every welcome-screen .action-btn (a text button) must pin '
        'width:auto;height:auto to defeat the fixed 32x32 icon square; '
        f'missing on: {bad}')


def test_nc_stripping_override_goes_red():
    """NC: strip the override from ONE button in a copy; the same check must
    now fail. Shipped file stays byte-identical."""
    with open(_CONV_SRC, encoding='utf-8') as f:
        original = f.read()

    patched = original.replace(
        'class="action-btn" style="width:auto;height:auto;padding:8px 16px;cursor:pointer"',
        'class="action-btn" style="padding:8px 16px;cursor:pointer"',
        1)
    assert patched != original, 'NC patch did not modify the source — anchor stale'

    styles = _action_btn_styles(patched)
    bad = _missing_override(styles)
    assert bad, 'NC precondition: the stripped button should now be missing the override'
    with pytest.raises(AssertionError):
        assert not bad

    with open(_CONV_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped conversations.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
