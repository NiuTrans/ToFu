"""Regression guard for the orphan-resume button's CSS class.

WHY
The orphan-resume affordance (rendered under #chatInner when the backend marks
``conv._orphanResumable``) offers a text+icon "继续回答 / Resume" button. It was
mistakenly emitted with TWO classes — ``orphan-resume-btn action-btn`` — but
``.action-btn`` (static/styles.css) is the project's fixed-size ICON-ONLY square
(``width:32px;height:32px``). With the global ``box-sizing:border-box`` that
32×32 square won over the size-less ``.orphan-resume-btn``, squashing the button
into a tiny box: the CJK label wrapped one glyph per line and the rocket icon
overflowed as a broken square. ``.orphan-resume-btn`` is fully self-sufficient
inline-flex styling, so the fix removed the ``action-btn`` class.

This class of bug — a TEXT-bearing button accidentally wearing the icon-only
``.action-btn`` square — silently squashes the button and is easy to
reintroduce. This guard asserts the orphan-resume button markup does NOT carry
``action-btn``.

NEGATIVE CONTROL: ``test_nc_reintroducing_action_btn_goes_red`` re-adds the
``action-btn`` class in a COPY of the source and asserts the same check fails —
proving the guard has teeth. The shipped file is left byte-identical.

Pure source-level (no node/jsdom needed): the bug lives entirely in the static
class string emitted by ``_orphanResumeAffordanceHtml``.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHAT_RENDER_SRC = os.path.join(ROOT, 'static', 'js', 'ui', 'chat_render.js')

# Matches the orphan-resume button's opening tag and captures its class list,
# e.g.  <button class="orphan-resume-btn" onclick="_resumeOrphanTurn(...
_ORPHAN_BTN_RE = re.compile(
    r'<button class="(?P<cls>[^"]*orphan-resume-btn[^"]*)"[^>]*onclick="_resumeOrphanTurn')


def _orphan_button_classes(source: str) -> list[str]:
    """Return the class list of the orphan-resume button in the given JS source.

    Raises AssertionError if the button markup can't be found (a structural
    change that this guard must be updated for).
    """
    m = _ORPHAN_BTN_RE.search(source)
    assert m is not None, 'orphan-resume button markup not found — guard is stale'
    return m.group('cls').split()


def test_orphan_resume_button_has_no_action_btn_class():
    with open(_CHAT_RENDER_SRC, encoding='utf-8') as f:
        source = f.read()
    classes = _orphan_button_classes(source)
    assert 'orphan-resume-btn' in classes, classes
    assert 'action-btn' not in classes, (
        'orphan-resume button must NOT wear the fixed 32x32 .action-btn '
        f'icon-square class (would squash it); classes={classes}')


def test_nc_reintroducing_action_btn_goes_red():
    """NC: re-add ``action-btn`` in a copy of the source; the same check must
    now fail — proving the guard actually catches the regression. Shipped file
    stays byte-identical."""
    with open(_CHAT_RENDER_SRC, encoding='utf-8') as f:
        original = f.read()

    patched = original.replace(
        '<button class="orphan-resume-btn" onclick="_resumeOrphanTurn(',
        '<button class="orphan-resume-btn action-btn" onclick="_resumeOrphanTurn(',
        1)
    assert patched != original, 'NC patch did not modify the source — anchor stale'

    classes = _orphan_button_classes(patched)
    assert 'action-btn' in classes, 'NC precondition: the class was re-added'
    with pytest.raises(AssertionError):
        # Re-run the real guard's assertion against the neutered source.
        assert 'action-btn' not in classes

    # Shipped source untouched (we only patched an in-memory copy).
    with open(_CHAT_RENDER_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped chat_render.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
