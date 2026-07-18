"""tests/test_frontend_dup_model_view_btn.py — one "model view" entry per row.

Bug (owner-reported): a tool row rendered TWO "模型原文" (model view) buttons.
Root cause: while a round's ``toolContent`` was still empty (e.g. a tool
pre-executed during streaming), ``_rowModelViewBtn`` (tool_rounds.js) emits a
SYNTHESIZED fallback button ``<button ... data-tc-preview-text=...>`` showing a
placeholder (``{"snippet":"Pre-executed during streaming"}``). When the real
``toolContent`` later arrives, the streaming sync append branch in
streaming_ui.js added the real ``_tcPreviewBtn`` (``data-tc-preview``) — but its
guard only checked ``!slot.querySelector('[data-tc-preview]')``, which does NOT
match the fallback's ``data-tc-preview-text`` attribute. So the row ended up
with BOTH buttons: the stale placeholder AND the real one.

The model view must show ONLY the verbatim bytes actually sent to the model
(``round.toolContent``), exactly once. The fix removes the stale
``data-tc-preview-text`` fallback before appending the real button.

Source-level guard (streaming_ui.js can't be eval'd standalone — heavy deps),
mirroring test_frontend_rejected_round_terminal.py's source-guard style.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def test_streaming_append_drops_stale_fallback_before_real_model_view():
    """The toolContent-arrived append branch must remove any synthesized
    fallback model-view button (data-tc-preview-text) before inserting the
    real _tcPreviewBtn, so the row keeps exactly ONE model-view entry."""
    src = open(os.path.join(JS_DIR, 'ui', 'streaming_ui.js'), encoding='utf-8').read()

    # Locate the branch that appends the real model-view button once
    # toolContent has arrived.
    m = re.search(
        r"round\.toolContent && !slot\.querySelector\('\[data-tc-preview\]'\)"
        r".*?insertAdjacentHTML\('beforeend', _tcPreviewBtn\(round\)\)",
        src, re.S)
    assert m, 'could not find the toolContent model-view append branch'
    block = m.group(0)

    # Inside that block, the stale synthesized fallback must be removed first.
    assert 'data-tc-preview-text' in block, (
        "the append branch must reference the fallback's data-tc-preview-text "
        'attribute so it can be removed')
    assert re.search(r"querySelector\('\[data-tc-preview-text\]'\)", block), (
        'the append branch must query for a stale [data-tc-preview-text] '
        'fallback button before adding the real one')
    assert '.remove()' in block, (
        'the stale fallback button must be .remove()d before inserting the '
        'real _tcPreviewBtn — otherwise the row shows two model-view buttons')

    # Order sanity: the removal must come BEFORE the real-button insertion.
    rm_idx = block.index("querySelector('[data-tc-preview-text]')")
    add_idx = block.index('insertAdjacentHTML')
    assert rm_idx < add_idx, (
        'stale-fallback removal must precede the real-button insertion')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
