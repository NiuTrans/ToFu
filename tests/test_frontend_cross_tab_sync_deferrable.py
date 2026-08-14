#!/usr/bin/env python3
"""Regression guard for pt_3879f00e sub-part 3 — cross_tab_sync deferability.

Companion to test_frontend_health_stream_timer_deferrable.py. This locks the
"Option A" relocation the audit documented in docs/EPIC_E_DEFER_AUDIT.md:
move the ``_syncChannel = new BroadcastChannel(...)`` creation + its
``onmessage = _handleCrossTabMsg`` listener registration OUT of ``core.js``
(where it hard-refers ``_handleCrossTabMsg`` at module load) and INTO
``core/cross_tab_sync.js`` itself.

**Why this matters for deferability**: ``core/cross_tab_sync.js`` is 53KB.
The audit flagged it as a candidate for ``_CLASSIC_ASSET_FILES``, blocked by the
fact that ``core.js:132`` had ``_handleCrossTabMsg(e.data)`` inside the
BroadcastChannel callback — if the module is deferred, that symbol is
undefined at boot and the first cross-tab message throws ReferenceError.

**The relocation**: after this commit, ``_handleCrossTabMsg`` is only
referenced inside the module that defines it — so deferring the module
means simply "no cross-tab sync until the module lands", not
"ReferenceError at boot". This is the exact same self-containment pattern
the health_stream_timer strangler sweep achieved with typeof-gates.

**Contract enforced**:

  1. ``core.js`` MUST NOT reference ``_handleCrossTabMsg`` anywhere
     (module load or callback body). The old ``_syncChannel.onmessage =
     (e) => _handleCrossTabMsg(...)`` block is GONE.
  2. ``core/cross_tab_sync.js`` MUST create ``_syncChannel`` and register
     its ``onmessage`` handler — the audit's "Option A" move.
  3. ``TAB_ID`` (needed by cross_tab_sync's postMessage AND by main.js's
     boot log line) STAYS in ``core.js`` — it's a leaf constant with
     multiple consumers.
  4. ``_syncChannel`` MUST remain window-scoped (both files can read it
     with a bare identifier) — accessed via ``if (!_syncChannel)`` in
     cross_tab_sync.js's ``_broadcastToTabs``.
"""

from __future__ import annotations

import os
import re
import sys

from tests._runtime_sections import runtime_section_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORE_JS = runtime_section_path('core.js')
_CROSS_TAB_JS = runtime_section_path('core/cross_tab_sync.js')


@_unit
def test_core_js_does_not_reference_handle_cross_tab_msg():
    """The deferability blocker was ``_handleCrossTabMsg(...)`` inside
    the ``_syncChannel.onmessage`` callback at core.js:132. After the
    relocation, core.js must have ZERO reference to that symbol."""
    with open(_CORE_JS, encoding='utf-8') as f:
        src = f.read()
    assert '_handleCrossTabMsg' not in src, (
        'static/js/core.js must NOT reference _handleCrossTabMsg — a bare '
        'reference at module load would ReferenceError once '
        'core/cross_tab_sync.js is moved to _CLASSIC_ASSET_FILES. The audit\'s '
        'Option A move relocates the BroadcastChannel listener registration '
        'into cross_tab_sync.js itself so _handleCrossTabMsg only appears '
        'in the module that defines it.'
    )


@_unit
def test_cross_tab_sync_creates_sync_channel_and_registers_listener():
    """After the relocation, ``core/cross_tab_sync.js`` MUST own the
    ``new BroadcastChannel(...)`` construction + ``onmessage`` handler
    registration. Verified by grepping for both a BroadcastChannel
    construction with the right channel name AND an onmessage handler that
    dispatches to _handleCrossTabMsg."""
    with open(_CROSS_TAB_JS, encoding='utf-8') as f:
        src = f.read()
    assert 'new BroadcastChannel(' in src, (
        'core/cross_tab_sync.js must construct the BroadcastChannel — '
        'the audit\'s Option A move relocated this from core.js.'
    )
    assert '"claude_dialogue_sync"' in src or "'claude_dialogue_sync'" in src, (
        'core/cross_tab_sync.js must use the exact channel name '
        '"claude_dialogue_sync" — byte-parity with the pre-relocation '
        'core.js BroadcastChannel construction.'
    )
    # The onmessage handler must exist and dispatch to _handleCrossTabMsg.
    # Match ``.onmessage`` and the same tab-id filter the old code had.
    assert re.search(r'\.onmessage\s*=', src), (
        'core/cross_tab_sync.js must register a .onmessage handler on '
        'the BroadcastChannel — otherwise cross-tab events silently drop.'
    )
    assert '_handleCrossTabMsg' in src, (
        'core/cross_tab_sync.js must reference _handleCrossTabMsg — '
        'this is where the dispatch lives now.'
    )
    # The tab-id filter (skip our own messages) must be preserved.
    assert 'sourceTab' in src and 'TAB_ID' in src, (
        'core/cross_tab_sync.js must preserve the sourceTab !== TAB_ID '
        'filter — byte-parity with the pre-relocation core.js block.'
    )


@_unit
def test_tab_id_stays_in_core_js():
    """``TAB_ID`` is a leaf constant used by both cross_tab_sync AND
    main.js's boot log line. It stays in core.js so cross_tab_sync can
    remain window-scope + deferable without dragging TAB_ID with it."""
    with open(_CORE_JS, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'\bconst\s+TAB_ID\s*=', src), (
        'static/js/core.js must still declare TAB_ID — it is a leaf '
        'boot-time constant with multiple consumers (main.js logs it, '
        'cross_tab_sync.js filters incoming messages on it).'
    )


@_unit
def test_sync_channel_still_declared_somewhere():
    """``_syncChannel`` is checked by cross_tab_sync.js's ``_broadcastToTabs``
    (``if (!_syncChannel) return``). After the relocation, its declaration
    must live SOMEWHERE reachable at the ``_broadcastToTabs`` call site —
    the natural home is cross_tab_sync.js's own top."""
    with open(_CROSS_TAB_JS, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'\b(let|var)\s+_syncChannel\b', src), (
        'core/cross_tab_sync.js must declare _syncChannel — after the '
        'relocation it lives in the module that uses it.'
    )


if __name__ == '__main__':
    for fn in [
        test_core_js_does_not_reference_handle_cross_tab_msg,
        test_cross_tab_sync_creates_sync_channel_and_registers_listener,
        test_tab_id_stays_in_core_js,
        test_sync_channel_still_declared_somewhere,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
