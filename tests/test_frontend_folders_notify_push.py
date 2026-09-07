#!/usr/bin/env python3
"""tests/test_frontend_folders_notify_push.py — apply-side regression for the
event-driven cross-device FOLDER sync handler (``_onFoldersChangedPush`` in
``core/conversation_invalidation.js``).

WHY
---
Folders live in a separate per-install store, so they don't ride the
``conv_changed`` rev signal. The server now emits a dedicated ``folders_changed``
frame on the ``notify`` channel on every folder mutation
(routes/api_v1/folders.py). ``_onFoldersChangedPush`` consumes it so a sibling
tab/device updates its folder tree WITHOUT a manual refresh:

  • create / rename / reorder → debounced ``loadFolders()`` (re-render in place).
  • delete (``deletedFolderId`` present) → unassign local conversations off the
    removed folder ON THIS DEVICE too (the clicking device already did it in
    ``deleteFolder``; this makes a SECOND device reconcile), drop the dead
    folder from the in-memory array, then reload.

This drives the REAL shipped handler under jsdom:
  1. a plain ``folders_changed`` frame schedules a ``loadFolders()`` reload
     (captured timer, then fired) — the create/rename/reorder path;
  2. a delete frame flips a local conv's ``folderId`` off the removed folder and
     removes the folder from ``getFolders()`` — the cross-device unassign.

NEUTER: strip the delete-reconcile loop's assignment (``c.folderId = null`` →
no-op) and assert the conv KEEPS the stale folderId, proving the unassign is
load-bearing (item 2 of the objective).

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest

from tests._jsdom import JS_DIR, node_deps_available, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

_SRC = os.path.join(JS_DIR, 'core', 'conversation_invalidation.js')
_FRAME_OWNER = native_module_path(
    '.native/frame-identity-folders.js',
    Path(__file__).resolve().parents[1] / 'frontend/src/core/frame-identity.ts',
)

# The exact delete-reconcile assignment the fix introduced (neuter target).
_UNASSIGN = (
    'if (conversation?.folderId === deletedFolderId) '
    'conversation.folderId = null;'
)
_NEUTER = (
    'if (conversation?.folderId === deletedFolderId) void 0;'
)

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

// Capture debounced timers so we can fire them deterministically (the shared
// harness neuters setTimeout to a no-op; we need the reload callback).
const _timers = [];
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="convList"></div></body>',
  // argv[4] = the typed frame-identity owner. The retained handler gets only
  // a composition adapter that supplies the current authenticated owner.
  targets: [process.argv[4], process.argv[2]],
  globals: {
    setTimeout: (fn) => { _timers.push(fn); return _timers.length; },
    clearTimeout: () => {},
    _editingMsgIdx: null,
    activeStreams: new Map(),
    conversations: [],
    _currentUserId: 1,
    _frameIsOurs: (frameOwnerId) =>
      frameBelongsToOwner(global._currentUserId, frameOwnerId),
    reconcileConversationCatalogMetadata: () => {
      global.__saved = (global.__saved || 0) + 1;
    },
    renderConversationList: () => { global.__rendered = (global.__rendered || 0) + 1; },
    ConvCache: { put: () => {} },
    // core/conversation_invalidation.js registers the catalog-revision gate's
    // teardown on the retained composition lifecycle (defined by _prelude.js,
    // which this focused harness does not load). The handler under test never
    // drives the lifecycle, so an inert stub is faithful here.
    retainedCompositionLifecycle: { add: () => {}, destroy: () => {}, listen: () => {} },
    debugLog: () => {},
  },
});
function fireTimers() { const t = _timers.splice(0); for (const fn of t) { try { fn(); } catch (e) {} } }

// ── Folder store the handler reads/mutates via getFolders() + loadFolders() ──
let _folders = [
  { id: 'f-keep', name: 'Keep', order: 0 },
  { id: 'f-del', name: 'Doomed', order: 1 },
];
let _loadFolderCalls = 0;
global.getFolders = window.getFolders = () => _folders;
global.loadFolders = window.loadFolders = () => { _loadFolderCalls++; return Promise.resolve(_folders); };

// jsdom is "visible" by default so the debounced reload guard passes.
Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });

// Seed two conversations, one assigned to the folder we will delete.
window.conversations.push(
  { id: 'c1', folderId: 'f-del' },
  { id: 'c2', folderId: 'f-keep' },
);

const NEUTER = process.env.NEUTER === '1';

// ── Scenario A: a create/rename/reorder frame → debounced loadFolders() ──
_onFoldersChangedPush({ type: 'folders_changed', userId: 1 });
check('A_reload_not_sync', _loadFolderCalls === 0);   // debounced, not immediate
fireTimers();
check('A_reload_fired', _loadFolderCalls === 1);       // reload ran after the timer

// ── Scenario B: a DELETE frame → unassign local convs + drop the folder ──
_onFoldersChangedPush({ type: 'folders_changed', deletedFolderId: 'f-del', userId: 1 });
const c1 = window.conversations.find((c) => c.id === 'c1');
const c2 = window.conversations.find((c) => c.id === 'c2');
const stillHasDel = _folders.some((f) => f.id === 'f-del');

if (NEUTER) {
  // With the unassign stripped, the conv that referenced the deleted folder
  // KEEPS its stale folderId → the "second device still shows it in the dead
  // folder until refresh" bug.
  check('B_neuter_conv_keeps_stale_folderId', c1.folderId === 'f-del');
} else {
  check('B_conv_unassigned', c1.folderId === null);        // reconciled off dead folder
  check('B_other_conv_untouched', c2.folderId === 'f-keep'); // unrelated conv intact
  check('B_dead_folder_removed', stillHasDel === false);    // dropped from the tree
  fireTimers();
  check('B_reload_fired_after_delete', _loadFolderCalls === 2);
}

// ── Scenario C: a frame for a DIFFERENT user is ignored (forward-safety) ──
if (!NEUTER) {
  window._currentUserId = 1;
  const before = _loadFolderCalls;
  _onFoldersChangedPush({ type: 'folders_changed', userId: 999 });
  fireTimers();
  check('C_foreign_user_ignored', _loadFolderCalls === before);
  delete window._currentUserId;
}

report();
"""


def test_folders_changed_applies_without_refresh():
    """A folders_changed frame reloads the tree and a delete frame unassigns
    conversations off the removed folder — all in place, no page refresh."""
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    assert _UNASSIGN in src, (
        'delete-reconcile unassign marker not found — did _onFoldersChangedPush change?')
    run_harness(
        target_js=_SRC,
        body_js=_BODY,
        extra_targets=[_FRAME_OWNER],
        min_pass=7,
        label='folders-changed apply',
    )


def test_NC_no_unassign_leaves_stale_folderid():
    """NEUTER: strip the ``c.folderId = null`` unassign → the conv keeps the
    stale folderId, proving the cross-device delete-reconcile is load-bearing."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    assert src.count(_UNASSIGN) == 1, 'expected exactly one unassign site to neuter'
    neutered = src.replace(_UNASSIGN, _NEUTER, 1)
    assert neutered != src

    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=os.path.dirname(_SRC), delete=False, encoding='utf-8'
    ) as fh:
        neutered_path = fh.name
        fh.write(neutered)
    try:
        os.environ['NEUTER'] = '1'
        run_harness(
            target_js=neutered_path,
            body_js=_BODY,
            extra_targets=[_FRAME_OWNER],
            min_pass=3,
            label='folders-changed apply NC',
        )
    finally:
        os.environ.pop('NEUTER', None)
        try:
            os.remove(neutered_path)
        except OSError:
            pass
