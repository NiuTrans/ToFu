"""C2 — folder members load & incrementally merge into the sidebar (jsdom).

WHY
---
A folder's members are resolved server-side by their real ``folderId``,
INDEPENDENT of the top-N sidebar window (C1). But "the backend can find them"
is not "the user sees them" — the frontend must, on entering a folder, FETCH
those members and INCREMENTALLY merge them into the in-memory ``conversations``
array so the folder view renders them.

This test drives the REAL shipped ``mergeServerConvShells`` (core/conversations.js)
and ``setActiveFolderId`` / ``loadFolderMembers`` (core/folders.js) under node,
with an in-memory ``conversations`` array that models the top-500 window and
DELIBERATELY does NOT contain the folder's members. It asserts:

  1. entering the folder issued the ``?folderId=`` query;
  2. members were merged in as shells that PASS the sidebar visibility gate
     (``messages.length>0 || _serverMsgCount>0 || _needsLoad``);
  3. an already-present, LIVE conv (streaming / loaded messages) had its
     ``messages`` / ``_serverRev`` / ``activeTaskId`` / ``_needsLoad`` LEFT
     UNTOUCHED (incremental upsert, never clobber);
  4. the auto-migrated "⭐ 置顶" star folder behaves identically.

NEUTER: a copy of folders.js with the ``loadFolderMembers`` fetch stripped
proves the members stay invisible — the test discriminates the fix.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The harness loads the REAL merge helper + folder-view logic, seeds a
# top-window `conversations` array WITHOUT the folder members, then enters the
# folder and inspects the merged result.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── Seed: an in-memory sidebar window (top-500 model) that does NOT contain
//    the folder's members. It DOES contain one LIVE conv already in the folder
//    (streaming, messages loaded) so we can prove the merge never clobbers it.
global.conversations = [
  // A decoy top-window conv (unfoldered).
  { id: 'decoy-1', title: 'decoy', messages: [], _serverMsgCount: 3,
    _needsLoad: true, folderId: null, updatedAt: 9000 },
  // A LIVE member already present: streaming, messages loaded, has a rev + task.
  { id: 'fmem-live', title: 'live member (local)', folderId: 'FLD',
    messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'yo' }],
    _serverMsgCount: 2, _serverRev: 7, activeTaskId: 'task-xyz', _needsLoad: false,
    updatedAt: 9500 },
];
global.activeConvId = null;
global.activeStreams = new Map();

let renderCount = 0;
let folderQueryArgs = null;   // records the folderId the query was called with
global.renderConversationList = () => { renderCount++; };

// _applySettingsToConv: minimal real-shape stand-in — adopts folderId/pinned.
global._applySettingsToConv = (conv, settings) => {
  if (settings && settings.folderId !== undefined) conv.folderId = settings.folderId;
  if (settings && settings.pinned !== undefined) conv.pinned = settings.pinned;
};

global.Api = {
  conversations: {
    listByFolder: async (folderId) => {
      folderQueryArgs = folderId;
      if (folderId === 'FLD') {
        return {
          conversations: [
            // Two OLD members past the window — NOT in memory. Sidebar meta
            // shape uses messageCount; include it so _serverConvCount reads it.
            { id: 'fmem-old-1', title: 'old member 1', messageCount: 5,
              createdAt: 100, updatedAt: 200, settings: { folderId: 'FLD' } },
            { id: 'fmem-old-2', title: 'old member 2', messageCount: 2,
              createdAt: 110, updatedAt: 210, settings: { folderId: 'FLD' } },
            // The live member is ALSO returned by the server query (it's a real
            // member) — merge must NOT clobber its local heavy/live fields.
            { id: 'fmem-live', title: 'live member (SERVER title)', messageCount: 2,
              createdAt: 120, updatedAt: 9400, settings: { folderId: 'FLD' } },
          ],
          hasMore: false, totalCount: 3,
        };
      }
      if (folderId === 'STAR') {
        return {
          conversations: [
            { id: 'star-old-1', title: 'star member 1', messageCount: 4,
              createdAt: 130, updatedAt: 230, settings: { folderId: 'STAR' } },
          ],
          hasMore: false, totalCount: 1,
        };
      }
      return { conversations: [], hasMore: false, totalCount: 0 };
    },
  },
};

// Load the REAL merge helper — post slice-7 it lives in the leaf
// core/conv_merge_shells.js (`_serverConvCount` + `mergeServerConvShells`
// extracted contiguously from conversations.js). Surgical extract keeps the
// harness independent of the rest of the leaf's future companions.
const convSrc = fs.readFileSync(process.argv[2], 'utf8');
// _serverConvCount + mergeServerConvShells are contiguous; grab from the first
// to the end of the second.
const scStart = convSrc.indexOf('function _serverConvCount(');
const mergeStart = convSrc.indexOf('function mergeServerConvShells(');
if (scStart < 0 || mergeStart < 0) { console.log('FAIL extract merge helper not found'); process.exit(0); }
// End of mergeServerConvShells: find its closing brace by scanning braces.
let i = convSrc.indexOf('{', mergeStart), depth = 0, end = -1;
for (; i < convSrc.length; i++) {
  if (convSrc[i] === '{') depth++;
  else if (convSrc[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
eval(convSrc.slice(scStart, end));

// Load the REAL folder-view logic (setActiveFolderId / loadFolderMembers).
eval(fs.readFileSync(process.argv[3], 'utf8'));  // core/folders.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  check('fn_setActiveFolderId', typeof setActiveFolderId === 'function');
  check('fn_mergeServerConvShells', typeof mergeServerConvShells === 'function');

  // Enter the folder.
  setActiveFolderId('FLD');
  // Let the fire-and-forget loadFolderMembers settle.
  for (let k = 0; k < 100; k++) { await Promise.resolve(); }

  check('folder_query_issued', folderQueryArgs === 'FLD');

  const byId = (id) => conversations.find(c => c.id === id);

  // (2) Old members merged in as visibility-gate-passing shells.
  const gate = (c) => !!c && (c.messages.length > 0 || (c._serverMsgCount || 0) > 0 || c._needsLoad);
  const old1 = byId('fmem-old-1'), old2 = byId('fmem-old-2');
  check('old_member_1_merged', !!old1);
  check('old_member_2_merged', !!old2);
  check('old_member_1_passes_visibility_gate', gate(old1));
  check('old_member_2_passes_visibility_gate', gate(old2));
  check('old_member_1_shell_needsLoad', old1 && old1._needsLoad === true);
  check('old_member_1_shell_serverMsgCount', old1 && old1._serverMsgCount === 5);
  check('old_member_1_folderId_adopted', old1 && old1.folderId === 'FLD');

  // (3) The LIVE member's heavy/live fields were NOT clobbered.
  const live = byId('fmem-live');
  check('live_member_messages_untouched', live && live.messages.length === 2);
  check('live_member_serverRev_untouched', live && live._serverRev === 7);
  check('live_member_activeTaskId_untouched', live && live.activeTaskId === 'task-xyz');
  check('live_member_needsLoad_untouched', live && live._needsLoad === false);

  // Only ONE copy of each id (no duplicate shell created for the live member).
  const liveCount = conversations.filter(c => c.id === 'fmem-live').length;
  check('no_duplicate_live_member', liveCount === 1);

  // The folder view now has all 3 members available to filter.
  const inFolder = conversations.filter(c => c.folderId === 'FLD');
  check('all_three_members_visible', inFolder.length === 3);

  // (4) Star folder behaves identically.
  setActiveFolderId('STAR');
  for (let k = 0; k < 100; k++) { await Promise.resolve(); }
  const star1 = byId('star-old-1');
  check('star_member_merged', !!star1);
  check('star_member_passes_visibility_gate', gate(star1));

  console.log(out.join('\n'));
})();
"""


def _run(conv_js: str, folders_js: str):
    harness = os.path.join(HERE, '_folder_members_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, conv_js, folders_js],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_members_load_and_merge():
    conv_js = os.path.join(JS_DIR, 'core', 'conv_merge_shells.js')
    folders_js = os.path.join(JS_DIR, 'core', 'folders.js')
    proc = _run(conv_js, folders_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'C2 folder-member load/merge regression:\n' + output
    # Sanity: the key assertions actually ran.
    for must in ('PASS folder_query_issued',
                 'PASS old_member_1_passes_visibility_gate',
                 'PASS live_member_messages_untouched',
                 'PASS live_member_serverRev_untouched',
                 'PASS all_three_members_visible',
                 'PASS star_member_merged'):
        assert must in output, f'missing assertion {must!r}:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_without_member_fetch_members_stay_invisible(tmp_path):
    """NEUTER: strip the loadFolderMembers fetch call from setActiveFolderId in a
    COPY of folders.js and prove the folder's older members are then NEVER
    merged — i.e. the fetch is load-bearing and the test discriminates it."""
    conv_js = os.path.join(JS_DIR, 'core', 'conv_merge_shells.js')
    folders_js = os.path.join(JS_DIR, 'core', 'folders.js')
    with open(folders_js, encoding='utf-8') as f:
        src = f.read()
    anchor = 'if (_activeFolderId && !_folderMembersLoaded.has(_activeFolderId)) {\n    loadFolderMembers(_activeFolderId);\n  }'
    assert anchor in src, 'neuter anchor (setActiveFolderId fetch call) not found — update the target'
    neutered = src.replace(anchor, '/* NEUTERED: member fetch removed */', 1)
    assert neutered != src, 'neuter did not change the source'
    nfile = tmp_path / 'folders_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    proc = _run(conv_js, str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('old_member_1_merged') is False, (
        'NEUTER did not bite: members were merged even with the fetch stripped '
        '— the test does not discriminate the loadFolderMembers fix.\n' + output)
