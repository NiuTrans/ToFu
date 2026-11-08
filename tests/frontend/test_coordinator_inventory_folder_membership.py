"""Coordinator roster rows adopted into the sidebar keep their stored folder.

The sidebar's Coordinator group reads the project roster and adopts the rows
its catalog lacks as ordinary catalog shells (``presence.js`` ``adoptPage``).
The roster lists every conversation of the project, owner chats included, so a
shell created without its folder lands in the uncategorized view although
storage files it. This drives the REAL ``adoptPage``, catalog merge and
settings adoption under node and checks the resulting membership; a NEUTER run
without the folder pass-through proves the assertion discriminates the fix.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

SECTIONS = Path(runtime_sections_dir())
PASS_THROUGH = ', folderId: item.folderId'

_HARNESS = r"""
const fs = require('fs');
const [applyPath, catalogPath, presencePath] = process.argv.slice(2);
global.window = global;
global.runtimeScope = global;
global.conversations = [
  // Already in the catalog; the owner moved it after this roster page was read.
  { id: 'moved', title: 'moved', folderId: 'f_other', _serverTurnCount: 1, updatedAt: 9000 },
];
global.protectHydratingProjectSettings = (conv, settings) => settings;
global.ConversationTurnStore = { wakeConversation: async () => {} };
global.DEFAULT_ASYNC_POOL_CONCURRENCY = 4;
global.runWithConcurrency = (items, fn) => Promise.all(items.map(fn));
global.debugLog = () => {};
global.renderConversationList = () => {};

function closingBrace(source, open) {
  for (let i = open, depth = 0; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return i + 1;
  }
  return -1;
}

eval(fs.readFileSync(applyPath, 'utf8'));
const catalog = fs.readFileSync(catalogPath, 'utf8');
const catalogStart = catalog.indexOf('function _catalogRevision(');
const mergeStart = catalog.indexOf('function mergeConversationCatalogRows(');
if (catalogStart < 0 || mergeStart < 0) { console.log('FAIL catalog_merge_found'); process.exit(0); }
eval(catalog.slice(catalogStart, closingBrace(catalog, catalog.indexOf('{', mergeStart))));

const presence = fs.readFileSync(presencePath, 'utf8');
const anchor = 'adoptPage: function (page) {';
const at = presence.indexOf(anchor);
if (at < 0) { console.log('FAIL adopt_page_found'); process.exit(0); }
const adoptPage = eval('(' + presence.slice(at + 'adoptPage: '.length,
  closingBrace(presence, at + anchor.length - 1)) + ')');

adoptPage({ nextCursor: null, coverage: 'complete', conversations: [
  { conversationId: 'owner-filed', title: 'Owner chat', turnCount: 2, coordinatorId: null,
    folderId: 'f_project', createdAt: 100, updatedAt: 200 },
  { conversationId: 'worker-filed', title: 'Worker', turnCount: 1, coordinatorId: 'coordinator-1',
    folderId: 'f_project', createdAt: 100, updatedAt: 210 },
  { conversationId: 'owner-unfiled', title: 'Loose chat', turnCount: 1, coordinatorId: null,
    folderId: null, createdAt: 100, updatedAt: 220 },
  { conversationId: 'moved', title: 'moved', turnCount: 1, coordinatorId: null,
    folderId: 'f_project', createdAt: 100, updatedAt: 230 },
] });

const byId = (id) => conversations.find((conversation) => conversation.id === id) || {};
const out = [];
const check = (name, ok) => out.push((ok ? 'PASS ' : 'FAIL ') + name);
check('owner_chat_keeps_stored_folder', byId('owner-filed').folderId === 'f_project');
check('owner_chat_has_no_coordinator', byId('owner-filed')._coordinatorSource === null);
check('worker_keeps_stored_folder', byId('worker-filed').folderId === 'f_project');
check('worker_keeps_coordinator', byId('worker-filed')._coordinatorSource === 'coordinator-1');
check('unfiled_row_stays_unfiled', byId('owner-unfiled').folderId === null);
check('older_page_never_reverts_newer_local_folder', byId('moved').folderId === 'f_other');
check('one_shell_per_conversation', conversations.length === 4);
console.log(out.join('\n'));
"""


def _run(tmp_path: Path, presence: Path) -> dict[str, bool]:
    harness = tmp_path / 'inventory_folder_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), str(SECTIONS / 'core' / 'conv_apply_settings.js'),
         str(SECTIONS / 'core' / 'conversation_catalog.js'), str(presence)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    results = {line.split(' ', 1)[1]: line.startswith('PASS')
               for line in proc.stdout.splitlines() if line.startswith(('PASS ', 'FAIL '))}
    assert results, proc.stdout
    return results


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_adopted_roster_rows_keep_their_stored_folder(tmp_path):
    results = _run(tmp_path, SECTIONS / 'presence.js')
    assert all(results.values()), results
    assert len(results) == 7, results


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_NC_without_folder_pass_through_filed_rows_fall_into_uncategorized(tmp_path):
    source = (SECTIONS / 'presence.js').read_text(encoding='utf-8')
    anchors = source.count(PASS_THROUGH)
    assert anchors == 1, 'neuter anchor (adoptPage folder pass-through) not found'
    neutered = tmp_path / 'presence_neutered.js'
    neutered.write_text(source.replace(PASS_THROUGH, '', 1), encoding='utf-8')
    results = _run(tmp_path, neutered)
    assert results['owner_chat_keeps_stored_folder'] is False, results
    assert results['worker_keeps_stored_folder'] is False, results
