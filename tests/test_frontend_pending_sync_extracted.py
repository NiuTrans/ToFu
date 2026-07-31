#!/usr/bin/env python3
"""Wire-parity for pt_3879f00e sub-part 2 slice 2 — pending-sync cluster.

Scope: move the 5-function pending-sync retry cluster out of the 2311-line
core/conversations.js into a new leaf module core/pending_sync.js:

  * markConvPendingSync(conv)              — stamp per-msg _pendingSync + start poller
  * _clearPendingSyncMarkers(conv)         — clear all markers after confirmed PUT
  * convHasPendingSync(conv)               — durable marker present?
  * _startPendingSyncPolling()             — start the 12s retry interval
  * _flushPendingSyncs(trigger)            — attempt to sync every marked conv

Plus the two module-local state variables:
  * let _pendingSyncInterval = null
  * const _PENDING_SYNC_POLL_MS = 12000

All 5 functions read their dependencies (ConvCache, Api.health, activeStreams,
conversations, renderConversationList, loadConversationMessages,
syncConversationToServer) at CALL time, not IIFE-load time. Bundler
concatenation is verified plain (`cat a.js b.js`), so top-level `let/const`
declarations share ONE scope across files → the state variables see the
functions and vice versa, byte-identically.

Failing-first: 3 assertions (RED before extraction, GREEN after):

  1. static/js/core/pending_sync.js exists AND declares all 5 functions via
     `function <name>(`/`async function <name>(` AND exposes each on window.*.
  2. static/js/core/conversations.js NO LONGER declares any of the 5.
  3. lib/js_bundler.py::_BUNDLE_FILES lists 'core/pending_sync.js' BEFORE
     'core/conversations.js' (leaf must load before its still-in-file
     writer at line ~614: syncConversationToServer's success branch calls
     _clearPendingSyncMarkers).
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_FUNCS_SYNC = (
    'markConvPendingSync',
    '_clearPendingSyncMarkers',
    'convHasPendingSync',
    '_startPendingSyncPolling',
)
_FUNCS_ASYNC = (
    '_flushPendingSyncs',
)


def _read(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), encoding='utf-8') as f:
        return f.read()


@_unit
def test_pending_sync_leaf_module_exists_and_declares_five():
    """Slice 2: static/js/core/pending_sync.js exists and defines all 5
    functions at their intended def sites AND exposes each on window.*."""
    src = _read('static/js/core/pending_sync.js')
    for name in _FUNCS_SYNC:
        assert re.search(rf'\bfunction\s+{re.escape(name)}\s*\(', src), (
            f'core/pending_sync.js must define `function {name}(...)` '
            f'— extraction incomplete'
        )
    for name in _FUNCS_ASYNC:
        assert re.search(rf'\basync\s+function\s+{re.escape(name)}\s*\(', src), (
            f'core/pending_sync.js must define `async function {name}(...)` '
            f'— extraction incomplete'
        )
    for name in _FUNCS_SYNC + _FUNCS_ASYNC:
        assert f'window.{name} = {name}' in src, (
            f'core/pending_sync.js must expose window.{name} = {name} so '
            f'downstream typeof-guarded reads still resolve'
        )


@_unit
def test_conversations_js_no_longer_declares_pending_sync_cluster():
    """Slice 2: conversations.js MUST NOT still carry a `function <name>(`
    definition for any of the 5 pending-sync functions."""
    src = _read('static/js/core/conversations.js')
    for name in _FUNCS_SYNC:
        m = re.search(rf'\bfunction\s+{re.escape(name)}\s*\(', src)
        assert not m, (
            f'conversations.js must NOT still declare `function {name}(...)` '
            f'— leftover definition would shadow the extracted leaf'
        )
    for name in _FUNCS_ASYNC:
        m = re.search(rf'\basync\s+function\s+{re.escape(name)}\s*\(', src)
        assert not m, (
            f'conversations.js must NOT still declare `async function '
            f'{name}(...)` — leftover definition would shadow the leaf'
        )


@_unit
def test_bundle_manifest_loads_pending_sync_before_conversations():
    """Slice 2: lib/js_bundler.py must list 'core/pending_sync.js' BEFORE
    'core/conversations.js' — the still-in-file syncConversationToServer
    success branch reads _clearPendingSyncMarkers, so the leaf must load
    first."""
    src = _read('lib/js_bundler.py')
    pending_idx = src.find("'core/pending_sync.js'")
    conv_idx = src.find("'core/conversations.js'")
    assert pending_idx > 0, (
        "lib/js_bundler.py::_BUNDLE_FILES must include "
        "'core/pending_sync.js' — bundler ratchet skipped"
    )
    assert conv_idx > 0, (
        "lib/js_bundler.py::_BUNDLE_FILES must still include "
        "'core/conversations.js'"
    )
    assert pending_idx < conv_idx, (
        "'core/pending_sync.js' must appear BEFORE "
        "'core/conversations.js' in _BUNDLE_FILES — leaf must load "
        "before its downstream reader"
    )


if __name__ == '__main__':
    for fn in [
        test_pending_sync_leaf_module_exists_and_declares_five,
        test_conversations_js_no_longer_declares_pending_sync_cluster,
        test_bundle_manifest_loads_pending_sync_before_conversations,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
