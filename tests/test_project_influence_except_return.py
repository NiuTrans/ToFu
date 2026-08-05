#!/usr/bin/env python3
"""Guard for the project_brain_influence misplaced-return fix (pt_2d6eb6a0).

Pre-existing drift found during api-contract batch 2 (2026-08-01): the
``except`` block of ``project_brain_influence`` had NO return — its
``return api_internal_error(e, source='api_v1.project.brain_influence')``
sat orphaned after ``project_brain_peer_abort``'s except block as dead
code. Effect: an influence failure fell off the end of the function
(Quart ``None`` return → framework 500), losing the route-level
``source=`` diagnostic field.

Two source-level tripwires (the established shipped-source pattern):
  1. influence's except slice MUST contain its own return.
  2. peer_abort's except slice MUST NOT mention 'brain_influence'
     (the dead stray must stay gone).
"""

from __future__ import annotations

import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'project.py')

pytestmark = pytest.mark.unit


def _slice_after(src: str, marker: str, max_lines: int) -> str:
    idx = src.find(marker)
    if idx < 0:
        return ''
    return '\n'.join(src[idx:].split('\n', max_lines + 1)[:max_lines + 1])


def test_influence_except_returns_its_own_500():
    """The influence handler's except block returns api_internal_error with
    its OWN source= string, not a fall-off-the-end None."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    slc = _slice_after(src, "logger.error('[Project.v1] brain influence failed",
                       4)
    assert slc, 'influence except log line not found — parser drift?'
    assert "return api_internal_error(e, source='api_v1.project.brain_influence')" in slc, (
        'project_brain_influence except block has no return — the handler '
        'falls off the end on failure (framework 500, no source= diagnostic)')


def test_peer_abort_except_has_no_influence_stray():
    """peer_abort's except must not contain the orphaned influence return —
    dead code that pretends to handle influence failures from the wrong
    function."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    slc = _slice_after(src, "logger.error('[Project.v1] peer-abort failed",
                       6)
    assert slc, 'peer-abort except log line not found — parser drift?'
    assert 'brain_influence' not in slc, (
        "dead stray `return api_internal_error(..., source='...brain_influence')` "
        "is back inside peer_abort's except — it belongs to the influence handler")


def test_influence_source_string_appears_exactly_once():
    """The source='...brain_influence' marker exists exactly once in the file
    (a duplicate means the stray was copied, not moved)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    count = len(re.findall(r"source='api_v1\.project\.brain_influence'", src))
    assert count == 1, (
        f"expected exactly 1 occurrence of source='api_v1.project.brain_influence', "
        f'found {count} — the fix must MOVE the stray, not copy it')


if __name__ == '__main__':
    for fn in (test_influence_except_returns_its_own_500,
               test_peer_abort_except_has_no_influence_stray,
               test_influence_source_string_appears_exactly_once):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
