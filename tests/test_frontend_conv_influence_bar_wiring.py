"""Static wiring guard for the per-conversation Project Brain influence bar.

The always-visible influence bar (``#convInfluenceBar``) is conv-scoped and its
refetch is exposed as ``window.convInfluenceRefresh`` (project-brain.js). But it
was NEVER wired into the conversation-switch path: ``_restoreConvToolState()``
(static/js/main.js) — which runs on every load/switch — only called
``presenceRefresh()`` and ``projectBrainRefresh()``. So the bar only ever
populated while the Brain panel happened to be open AND a push frame fired; on a
plain conversation load it stayed permanently ``hidden``, contradicting
index.html's own comment that it "Updates on conversation switch (main.js →
convInfluenceRefresh)".

This asserts the call is present in the same block as its two siblings — a
pure source-level guard (no node/jsdom needed) with a negative-control that
proves the assertion is load-bearing.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
MAIN_JS = os.path.join(ROOT, 'static', 'js', 'main.js')


def _slice_fn(src: str, signature: str) -> str:
    start = src.index(signature)
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'could not slice {signature}')


def _restore_fn() -> str:
    with open(MAIN_JS, encoding='utf-8') as f:
        return _slice_fn(f.read(), 'function _restoreConvToolState(conv) {')


def test_conv_switch_path_calls_conv_influence_refresh():
    """The conv-switch handler must invoke convInfluenceRefresh() so the
    always-visible influence bar re-pulls for the newly-displayed conv — right
    beside the presenceRefresh / projectBrainRefresh siblings it belongs with."""
    body = _restore_fn()
    assert 'presenceRefresh()' in body, 'sibling presenceRefresh() missing (harness stale)'
    assert 'projectBrainRefresh()' in body, 'sibling projectBrainRefresh() missing (harness stale)'
    assert 'convInfluenceRefresh()' in body, (
        'main.js _restoreConvToolState must call convInfluenceRefresh() on '
        'conversation switch — otherwise #convInfluenceBar never populates on '
        'a plain load (the reported all-clients bug).')


def test_NC_absence_would_fail():
    """Negative control: prove the assertion is load-bearing — a body WITHOUT
    the call must fail the same predicate. (Guards against a vacuous grep that
    would pass even if the call were deleted.)"""
    body = _restore_fn().replace(
        'if (typeof convInfluenceRefresh === \'function\') convInfluenceRefresh();',
        '/* removed */', 1)
    assert 'convInfluenceRefresh()' not in body, (
        'NC: with the call removed the predicate must be false — if it is still '
        'true the real assertion is matching something incidental (e.g. a '
        'comment), not the actual invocation.')
