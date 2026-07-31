#!/usr/bin/env python3
"""Wire-parity for pt_3879f00e sub-part 2 slice 5 — _applySettingsToConv.

Scope: ``_applySettingsToConv(conv, settings)`` (~87 lines at
conversations.js L425) — a pure "adopt settings-column fields onto a
conversation object" helper. It reads a settings dict from either
IndexedDB / server-meta / cross-tab-broadcast and writes ~30 defined
sub-fields onto conv (model/thinkingDepth/searchMode/tool-toggle flags,
project scope, autopilot summaries, sidebar shell facts, cross-tab CAS
markers, activeTaskId restore).

Nine CALL sites depend on it: eight inside conversations.js
(mergeServerConvShells / hydrateSidebarFromCache / _openConvMayHoldOrphanGhost /
loadConversationsFromServer / loadConversationMessages / forceRecoverFromServer)
and — critically — ONE cross-file caller at cross_tab_sync.js:387
(``_handleConvNotifyPush``). Both files sit in _BUNDLE_FILES so the bare
name resolves at runtime via the shared bundle scope.

Extract to ``static/js/core/conv_apply_settings.js``, loaded via
``lib/js_bundler.py::_BUNDLE_FILES`` BEFORE BOTH ``core/conversations.js``
AND ``core/cross_tab_sync.js`` — same load-order rule as slices 1-4, with
the added constraint that the cross_tab_sync caller loads at bundle index
451 (before conversations.js at ~492), so the leaf MUST be inserted BEFORE
cross_tab_sync in the manifest.

Failing-first — this test asserts (RED before extraction, GREEN after):
  1. New leaf file exists and defines ``_applySettingsToConv`` at the
     file top level (bundler concatenates files at file scope).
  2. Manifest lists leaf BEFORE conversations.js AND BEFORE cross_tab_sync.js.
  3. conversations.js no longer carries the inline
     ``function _applySettingsToConv(...)`` declaration. All eight call
     sites in conversations.js AND the one in cross_tab_sync.js remain.
  4. Extracted body carries the pivotal field-adoption lines.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEAF = os.path.join(_ROOT, 'static/js/core/conv_apply_settings.js')
_CONV = os.path.join(_ROOT, 'static/js/core/conversations.js')
_CTS = os.path.join(_ROOT, 'static/js/core/cross_tab_sync.js')
_BUNDLER = os.path.join(_ROOT, 'lib/js_bundler.py')


@_unit
def test_leaf_module_exists_and_defines_apply_settings_at_top_level():
    """Slice 5: static/js/core/conv_apply_settings.js exists and
    declares ``_applySettingsToConv`` at the file's top level."""
    assert os.path.exists(_LEAF), (
        f'{_LEAF} missing — leaf file not created after slice 5'
    )
    with open(_LEAF, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'^function\s+_applySettingsToConv\s*\(', src, re.M), (
        'conv_apply_settings.js must declare _applySettingsToConv as a '
        'TOP-LEVEL function declaration — a wrapped def (IIFE / module) '
        'would not leak into the shared bundle scope the 9 call sites '
        '(8 in conversations.js + 1 in cross_tab_sync.js) rely on.'
    )


@_unit
def test_bundler_lists_leaf_before_conversations_and_cross_tab_sync():
    """Slice 5: manifest must list ``core/conv_apply_settings.js`` BEFORE
    both ``core/conversations.js`` AND ``core/cross_tab_sync.js``.

    cross_tab_sync.js is bundled at index ~451 (before conversations.js
    at ~492); the leaf MUST sit above BOTH or the bare-name call in
    ``_handleConvNotifyPush`` (cross_tab_sync.js:387) would see the
    function as undefined at load time.
    """
    with open(_BUNDLER, encoding='utf-8') as f:
        src = f.read()
    leaf_idx = src.find("'core/conv_apply_settings.js'")
    conv_idx = src.find("'core/conversations.js'")
    cts_idx = src.find("'core/cross_tab_sync.js'")
    assert leaf_idx != -1, (
        "lib/js_bundler.py::_BUNDLE_FILES must list "
        "'core/conv_apply_settings.js' after slice 5"
    )
    assert conv_idx != -1, "sanity: 'core/conversations.js' entry missing"
    assert cts_idx != -1, "sanity: 'core/cross_tab_sync.js' entry missing"
    assert leaf_idx < conv_idx, (
        f'conv_apply_settings.js at pos {leaf_idx} must come BEFORE '
        f'conversations.js at pos {conv_idx} in _BUNDLE_FILES.'
    )
    assert leaf_idx < cts_idx, (
        f'conv_apply_settings.js at pos {leaf_idx} must come BEFORE '
        f'cross_tab_sync.js at pos {cts_idx} in _BUNDLE_FILES — its '
        f'_handleConvNotifyPush handler calls _applySettingsToConv bare.'
    )


@_unit
def test_conversations_js_no_longer_declares_apply_settings_inline():
    """Slice 5: conversations.js MUST NOT re-carry the inline
    ``function _applySettingsToConv(`` declaration. All EIGHT call sites
    (bare ``_applySettingsToConv(...)``) MUST still exist — extraction
    is a move, not a removal.

    NOTE: as further slices extract other functions FROM conversations.js
    into their own leaves, some ``_applySettingsToConv(...)`` call sites
    move with their host function into those leaves. The invariant is
    that the TOTAL number of call sites (across every leaf that inherits
    them) never shrinks — otherwise a caller was silently dropped. Slice
    9 (conv_disaster_recovery.js) moved forceRecoverFromServer, which
    carries one such call site; slice 5's original guard would then read
    7 in conversations.js and appear to fail even though the caller is
    just in a new file. Sum across the known migrated leaves.
    """
    with open(_CONV, encoding='utf-8') as f:
        conv_src = f.read()
    assert not re.search(r'\bfunction\s+_applySettingsToConv\s*\(', conv_src), (
        'conversations.js must NOT re-carry inline '
        '``function _applySettingsToConv(...)`` — extracted to '
        'core/conv_apply_settings.js in slice 5.'
    )
    # Sum call sites across conversations.js AND the migrated leaves that
    # inherit them (grew as later slices carved out functions).
    call_pattern = r'_applySettingsToConv\s*\('
    total_hits = len(re.findall(call_pattern, conv_src))
    migrated_leaves = [
        os.path.join(_ROOT, 'static/js/core/conv_disaster_recovery.js'),
    ]
    for leaf in migrated_leaves:
        if os.path.exists(leaf):
            with open(leaf, encoding='utf-8') as f:
                total_hits += len(re.findall(call_pattern, f.read()))
    assert total_hits >= 8, (
        f'the TOTAL number of _applySettingsToConv call sites (across '
        f'conversations.js + migrated leaves) must not shrink below 8 '
        f'(found {total_hits}) — extraction is a move, not a drop.'
    )



@_unit
def test_cross_tab_sync_still_calls_apply_settings():
    """Slice 5: the cross-file caller at cross_tab_sync.js:387
    (_handleConvNotifyPush) MUST still call _applySettingsToConv bare —
    slice 5 only moves the definition, never the call sites."""
    with open(_CTS, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'_applySettingsToConv\s*\(', src), (
        'cross_tab_sync.js must still CALL _applySettingsToConv — the '
        'extracted definition still needs its cross-file caller intact.'
    )


@_unit
def test_leaf_module_carries_the_pivotal_body_lines():
    """Slice 5: the extracted body carries the pivotal field-adoption
    lines. Anchors specific field assignments (a variety across the
    function's body) so a future edit that hollows out the body flips
    this test.
    """
    with open(_LEAF, encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        # Model / preset / effort adoption — early in body
        'conv.model = settings.model',
        # Feature-toggle assignments — mid-body
        'conv.searchMode = settings.searchMode',
        'conv.imageGenEnabled = settings.imageGenEnabled',
        # Autopilot summaries sidecar — late in body
        'conv.autopilotSummaries = settings.autopilotSummaries',
        # Sidebar-shell facts — late in body
        'conv.lastMsgRole = settings.lastMsgRole',
        # Cross-device attach guard: activeTaskId restore
        'settings.activeTaskId && !conv.activeTaskId',
    ):
        assert pivot in src, (
            f'conv_apply_settings.js missing pivot {pivot!r} — the '
            f'extracted body must carry the field-adoption logic '
            f'verbatim; a hollow move is not a valid slice.'
        )


if __name__ == '__main__':
    for fn in [
        test_leaf_module_exists_and_defines_apply_settings_at_top_level,
        test_bundler_lists_leaf_before_conversations_and_cross_tab_sync,
        test_conversations_js_no_longer_declares_apply_settings_inline,
        test_cross_tab_sync_still_calls_apply_settings,
        test_leaf_module_carries_the_pivotal_body_lines,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
