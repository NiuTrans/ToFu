"""A hydrated window never re-arms a full snapshot through the catalog.

The catalog counts every Turn of a conversation; a snapshot loads only its
tail window. The shell's Turn count is projected from the authoritative lane
total, so an unchanged catalog row must keep the warm TurnStore (whose ordered
SSE suffix resumes on the next visit) instead of re-downloading the window.
"""
from pathlib import Path
import shutil
import subprocess
import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_hydrated_window_keeps_catalog_count_and_warm_revisit():
    harness = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const invalidated = [];
const conv = {id: 'big', title: 'Big', _serverTurnCount: 42, _serverRev: 7,
  _turnSnapshotRequired: true, _localOnly: false};
const c = {
  conversations: [conv], activeConvId: 'big',
  runtimeScope: {
    ConversationTurnStore: {
      // TurnRuntime's shell reconciler projects the authoritative lane total.
      hydrateConversation: async (shell) => { shell._serverTurnCount = 42; },
      invalidateConversation: (id) => invalidated.push(id),
    },
    // The snapshot loaded a ten-Turn tail window of 42 Turns.
    ConversationTurnRead: {ordered: () => Array.from({length: 10}, (_, i) => ({turnId: `t${i}`}))},
  },
  withProjectHydration: (_conv, hydrate) => hydrate(),
  createConversationSettingsResolution: () => ({}),
  createConversationCatalogLoader: () => ({destroy(){}}),
  retainedCompositionLifecycle: {add(){}},
  renderConversationList: () => {}, _convSorter: () => 0, console,
};
vm.createContext(c);
vm.runInContext(fs.readFileSync('frontend/src/runtime/sections/core/conversation_catalog.js', 'utf8'), c);
(async () => {
  await c.hydrateConversationRuntime('big');
  assert.equal(conv._turnSnapshotRequired, false);
  assert.equal(conv._serverTurnCount, 42);
  c.mergeConversationCatalogRows([{id: 'big', title: 'Big', msgCount: 42, rev: 7}]);
  assert.equal(conv._turnSnapshotRequired, false, 'an unchanged row keeps the warm store');
  assert.deepEqual(invalidated, []);
  c.mergeConversationCatalogRows([{id: 'big', title: 'Big', msgCount: 43, rev: 8}]);
  assert.equal(conv._turnSnapshotRequired, true, 'a newer revision still requires a snapshot');
  assert.deepEqual(invalidated, ['big']);
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
