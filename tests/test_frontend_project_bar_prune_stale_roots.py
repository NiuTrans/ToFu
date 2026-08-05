"""Regression guard: stale extra roots must not leak onto the project bar.

WHY
---
The project bar reflects the PROCESS-GLOBAL root registry
(``lib/project_mod/config.py:_roots``), surfaced via ``get_state()`` →
``GET /api/v1/project/status``. A background task's absolute-path write
auto-registers a NEW extra root into that global registry
(``_resolve_write_path`` §2), and a prior conversation's extras also live
there. Because ``set_project()`` has a ``same_primary`` idempotence guard that
PRESERVES existing global extras, restoring a single-root conversation via the
single-path ``POST /api/v1/project/set`` (``setPath``) leaves those stale
extras in place — so e.g. ``tofu-search`` shows up on a ``chatui``-only
conversation. Confirmed in logs/app.log:

    [tofu-sync_N] set_project chatui (same_primary=True,
        old_roots=['chatui','tofu-search'], new_roots=['chatui','tofu-search'])

Only the multi-path ``PUT /api/v1/project/paths`` (``setPaths``) PRUNES global
extras not in the caller's list. The fix: every conversation-restore path uses
``setPaths(savedPaths, savedReadOnly)`` UNCONDITIONALLY, making the
conversation's saved set the single source of truth for the bar.

This suite locks that in at the source level (no node needed): the two restore
functions must NOT branch to ``setPath`` and must call ``setPaths``.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
# Epic-E sub-7 (2026-08-01): _restoreConvProject / loadProjectStatus moved
# to project_state.js (core state subset); project.js is now the deferred
# panel. Read state-first with a panel fallback — loud when in neither.
PROJECT_STATE_SRC = os.path.join(ROOT, 'static', 'js', 'project_state.js')
PROJECT_SRC = os.path.join(ROOT, 'static', 'js', 'project.js')


def _state_or_panel_src(header: str) -> str:
    for path in (PROJECT_STATE_SRC, PROJECT_SRC):
        src = open(path, encoding='utf-8').read()
        if header in src:
            return src
    raise AssertionError(
        f'{header} not found in project_state.js or project.js — the '
        'sub-7 split moved it; update this harness')


def _fn_body(src: str, header: str) -> str:
    """Return the body of a function from its ``header`` to the next top-level
    ``\\n}`` at column 0 (functions in project.js are module-level)."""
    start = src.index(header)
    end = src.index('\n}', start)
    return src[start:end]


def test_restore_conv_project_uses_pruning_setpaths_only():
    src = _state_or_panel_src('async function _restoreConvProject(')
    body = _fn_body(src, 'async function _restoreConvProject(')
    assert 'Api.project.setPaths(' in body, \
        '_restoreConvProject must reconcile via the pruning setPaths endpoint'
    assert 'Api.project.setPath(' not in body, \
        ('_restoreConvProject must NOT use single-path setPath — its backend '
         'same_primary guard preserves stale global extra roots (the '
         'tofu-search-on-a-chatui-conv leak).')


def test_load_project_status_uses_pruning_setpaths_only():
    src = _state_or_panel_src('async function loadProjectStatus(')
    body = _fn_body(src, 'async function loadProjectStatus(')
    assert 'Api.project.setPaths(' in body, \
        'loadProjectStatus restore branch must reconcile via setPaths'
    assert 'Api.project.setPath(' not in body, \
        ('loadProjectStatus must NOT use single-path setPath (see '
         '_restoreConvProject rationale).')


def test_load_project_status_prunes_extras_even_with_zero_saved_extras():
    """The primary-matches branch must re-hydrate when the SERVER shows extras
    the conversation never saved — i.e. the ``!extrasMatch`` guard must NOT be
    gated behind ``savedExtras.length > 0`` (that gate is exactly what let a
    stale global extra survive on a conv that saved no extras)."""
    src = _state_or_panel_src('async function loadProjectStatus(')
    body = _fn_body(src, 'async function loadProjectStatus(')
    assert not re.search(r'!extrasMatch\s*&&\s*savedExtras\.length\s*>\s*0', body), \
        ('loadProjectStatus must trigger re-hydration on extras mismatch even '
         'when the conversation saved zero extras — otherwise stale global '
         'extras persist on the bar.')
    assert 'if (!extrasMatch || !roMatch)' in body, \
        'expected the un-gated extras/RO drift check'
