"""Current catalog authority evicts ghosts only from a complete server page.

The retired sync-digest reducer no longer ships. Exercise the actual retained
catalog and invalidation adapters, including active-view and cache disposal.
"""
from pathlib import Path
import shutil
import subprocess
import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_authoritative_catalog_ghost_eviction():
    harness = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const removed = [], disposed = [], opened = [];
let renders = 0, newChats = 0;
const c = {
  conversations: [], activeConvId: 'keep',
  runtimeScope: {ConversationTurnStore: {disposeConversation: id => disposed.push(id)}},
  ConvCache: {remove: id => removed.push(id)},
  createConversationSettingsResolution: () => ({}),
  createConversationCatalogLoader: () => ({destroy(){}}),
  createConversationCatalogRevisionGate: () => ({destroy(){}}),
  retainedCompositionLifecycle: {add(){}},
  renderConversationList: () => renders++, newChat: () => newChats++,
  loadConversation: id => opened.push(id),
  _convSorter: (a,b) => a.id.localeCompare(b.id),
  window: {addEventListener(){}}, document: {addEventListener(){}},
  setTimeout: () => 1, clearTimeout(){}, console,
};
vm.createContext(c);
for (const file of ['conversation_catalog.js','conversation_invalidation.js']) {
 vm.runInContext(fs.readFileSync('frontend/src/runtime/sections/core/'+file,'utf8'), c);
}
const row = id => ({id, messageCount: 1});
c.conversations.push(row('ghost'),row('keep'),{id:'draft',_localOnly:true});
const identity = c.conversations;
c._applyAuthoritativeConversationCatalogRows([row('keep')], 2);
assert.equal(c.conversations.length,3); // A partial page cannot prove deletion.
c._applyAuthoritativeConversationCatalogRows([row('keep')], null);
assert.equal(c.conversations.length,3); // Missing count is unknown, not complete.
for (const [rows,total] of [[[row('keep')],-1], [[row('keep')],0], [[row('keep'),null],2], [[row('keep'),row('keep')],2]]) {
 c._applyAuthoritativeConversationCatalogRows(rows,total);
 assert.equal(c.conversations.length,3, 'inconsistent catalog is not deletion evidence');
}
c._applyAuthoritativeConversationCatalogRows([row('keep')], 1);
assert.equal(c.conversations,identity);
assert.deepEqual(c.conversations.map(x=>x.id),['draft','keep']);
assert.deepEqual(removed,['ghost']); assert.deepEqual(disposed,['ghost']);
assert.equal(opened.length,0); assert.equal(newChats,0);
c.activeConvId='keep';
c._applyAuthoritativeConversationCatalogRows([],0);
assert.deepEqual(opened,['draft']); // Active deletion selects a remaining shell.
assert.equal(c.conversations[0]._localOnly,true);
c.conversations.splice(0,1,row('last')); c.activeConvId='last';
c._applyAuthoritativeConversationCatalogRows([],0);
assert.equal(newChats,1); assert.equal(c.conversations.length,0);
'''
    result = subprocess.run(['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
