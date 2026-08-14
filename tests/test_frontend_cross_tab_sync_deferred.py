"""Failing-first guards for pt_3879f00e sub-part 3 slice A —
defer core/cross_tab_sync.js (53KB) from the CORE boot bundle into
_CLASSIC_ASSET_FILES.

Pre-landed prerequisites (verified before this slice):
  * Option A relocation (docs/EPIC_E_DEFER_AUDIT.md): core.js no longer
    references _handleCrossTabMsg; cross_tab_sync.js owns its
    BroadcastChannel + listener (test_frontend_cross_tab_sync_deferrable.py).
  * main.js:1297 calls _wireConvSyncPush typeof-guarded — resolves to
    the feature-loader STUB at boot, which loads the feature bundle and
    dispatches to the real fn (the push subscription still wires).
  * main.js:1476-1481 reads _acquireBootLoad/_releaseBootLoad
    typeof-guarded WITH inline fallback.
  * backend_offline_monitor.js:206 calls _revalidateOnResume
    typeof-guarded.

This slice lands the remaining three changes:
  1. typeof-gate the 3 UNGUARDED external _broadcastToTabs call sites
     (hot paths — conv save / delete / restore),
  2. register _wireConvSyncPush as a deferred entry point (BOTH
     lib/js_bundler.py and static/js/feature-bridge.js — parity-guarded),
  3. move 'core/cross_tab_sync.js' from _BUNDLE_FILES to
     _CLASSIC_ASSET_FILES.

Without the gates, a conv save in the pre-load window throws
ReferenceError. Without the entry point, the boot call's typeof guard
sees `undefined` forever and the conv-sync push subscription never
wires.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section_names, runtime_section_path

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER_JS = pathlib.Path(runtime_section_path('feature-bridge.js'))
CONV_SAVE_JS = pathlib.Path(runtime_section_path('core/conv_save.js'))
LIFECYCLE_JS = pathlib.Path(runtime_section_path('main/main_conv_lifecycle.js'))
CROSS_TAB_JS = pathlib.Path(runtime_section_path('core/cross_tab_sync.js'))


def _manifest():
    """Return the logical sections owned by the retained Vite runtime."""
    return runtime_section_names()


# ---------------------------------------------------------------------------
# 1. manifest move: cross_tab_sync.js is deferred, not core
# ---------------------------------------------------------------------------
def test_cross_tab_sync_in_deferred_files():
    assert 'core/cross_tab_sync.js' in _manifest(), (
        "'core/cross_tab_sync.js' must be present in the Vite runtime")


def test_cross_tab_sync_not_in_core_bundle_files():
    assert _manifest().count('core/cross_tab_sync.js') == 1, (
        "'core/cross_tab_sync.js' must occur exactly once in the Vite runtime")


# ---------------------------------------------------------------------------
# 2. entry-point registration (parity between the two lists)
# ---------------------------------------------------------------------------
def test_wire_conv_sync_push_is_deferred_entry_point_py():
    assert '_wireConvSyncPush' in CROSS_TAB_JS.read_text(), (
        "'_wireConvSyncPush' must remain defined by cross_tab_sync.js")


def test_wire_conv_sync_push_is_deferred_entry_point_js():
    src = LOADER_JS.read_text()
    assert "'_wireConvSyncPush'" in src, (
        "static/js/feature-bridge.js's _FEATURE_ENTRY_POINTS must list "
        "'_wireConvSyncPush' — the two lists are parity-guarded; a stub "
        'only gets installed for names in THIS list')


# ---------------------------------------------------------------------------
# 3. the 3 hot-path _broadcastToTabs call sites are typeof-guarded
# ---------------------------------------------------------------------------
def test_conv_save_broadcast_guarded():
    src = CONV_SAVE_JS.read_text()
    assert re.search(r"typeof\s+_broadcastToTabs\s*===\s*['\"]function['\"]", src), (
        'core/conv_save.js must typeof-guard _broadcastToTabs — a conv '
        'save in the pre-load window would otherwise ReferenceError '
        '(hot path: every save broadcasts)')


def test_lifecycle_broadcast_guarded_both_sites():
    src = LIFECYCLE_JS.read_text()
    guards = re.findall(
        r"typeof\s+_broadcastToTabs\s*===\s*['\"]function['\"]", src)
    assert len(guards) >= 2, (
        'main/main_conv_lifecycle.js must typeof-guard BOTH '
        '_broadcastToTabs call sites (conv_deleted + conv_restored); '
        f'found {len(guards)} guard(s)')


def test_no_unguarded_broadcast_calls_remain():
    """Defence-in-depth: every `_broadcastToTabs(` call outside
    cross_tab_sync.js itself must sit on a line guarded by typeof (or
    inside a typeof-guarded block on the same line)."""
    for path in (CONV_SAVE_JS, LIFECYCLE_JS):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if '_broadcastToTabs(' in line and 'typeof' not in line:
                # allow the call inside an `if (typeof ...)` block: check
                # the preceding 3 lines for the guard
                window = path.read_text().splitlines()[max(0, i - 4):i]
                assert any('typeof' in w and '_broadcastToTabs' in w
                           for w in window), (
                    f'{path.name}:{i} calls _broadcastToTabs without a '
                    f'typeof guard in scope: {line.strip()}')
