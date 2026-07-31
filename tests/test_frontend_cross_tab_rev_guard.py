"""Ground-truth (cross-tab, step iii): two tabs sharing ONE server must not
clobber each other's messages. This is the single most common real-world
clobber — two tabs share IndexedDB and both PUT.

THE SCENARIO (the failure mode the owner named)
-----------------------------------------------
Tab A and Tab B both loaded conv at rev=5. Tab A sends a message → server rev=6
and the server now holds Tab A's message. Tab B (still baseRev=5) then syncs its
OWN un-acked message. Under the OLD wall-clock tiebreaker Tab B could win and
erase Tab A's message. Under CAS: Tab B's PUT carries the stale baseRev=5 →
server 409 `blocked_rev_conflict` → Tab B rebases (GET + append-missing-tail) →
final server state holds BOTH messages.

WHY THIS IS COVERED BY THE SAME MECHANISM
-----------------------------------------
The cross_tab_sync.js trace shows it has NO message-history PUT of its own: its
handlers either PULL (loadConversationsFromServer) or do a local read-adopt
(_recoverOfflineConversations). The ONLY server writer is
syncConversationToServer, which now carries baseRev + rebases on 409. So a
cross-tab write inherits CAS + rebase for free — this test PROVES it end-to-end
rather than assuming it.

Runs the REAL shipped syncConversationToServer under node. TWO in-memory "tabs"
(two conv objects) share ONE stateful CAS server stub (same object across both).
Asserts Tab B's write does not clobber Tab A's message, and the neuter (blind
re-PUT) shows the clobber returning.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._conv_bundle_sources import JS_DIR, source_argv, sources_defining

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── ONE server shared by both tabs, enforcing CAS like the backend. ──
// Both tabs loaded at rev=5 with the shared prefix [u0, a1].
const server = {
  messages: [
    { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
    { role: 'assistant', content: 'a1', timestamp: 1001, _msgId: 'm-a1' },
  ],
  rev: 5,
  title: 't',
};
const calls = { put: 0, get: 0 };

global.Api = {
  conversations: {
    put: async (id, body) => {
      calls.put += 1;
      if (body.baseRev !== undefined && body.baseRev !== null && body.baseRev !== server.rev) {
        return { ok: false, status: 409, clone() { return this; },
                 json: async () => ({ ok: false, error: 'blocked_rev_conflict',
                                      serverRev: server.rev, serverMsgCount: server.messages.length }) };
      }
      server.messages = body.messages.map(m => Object.assign({}, m));
      server.rev += 1;
      const newRev = server.rev;
      return { ok: true, status: 200, clone() { return this; },
               json: async () => ({ ok: true, rev: newRev }) };
    },
    get: async () => {
      calls.get += 1;
      return { messages: server.messages.map(m => Object.assign({}, m)),
               title: server.title, rev: server.rev };
    },
  },
};
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.config = {};
global.activeConvId = null;
global.renderChat = function() {};

/* Eval every shipped file the bundle needs, in production order (see
 * tests/_conv_bundle_sources.py). Hard-coding core/conversations.js broke when
 * the persist/rebase cluster was extracted to core/conv_persist_helpers.js. */
for (let i = 2; i < process.argv.length; i++) {
  eval(fs.readFileSync(process.argv[i], 'utf8'));
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  // Tab A: baseRev=5, appended its own message A. Syncs first → server rev=6.
  const tabA = {
    id: 'c-xtab', title: 't', _serverRev: 5, _serverMsgCount: 2,
    createdAt: 1, updatedAt: 2,
    messages: [
      { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
      { role: 'assistant', content: 'a1', timestamp: 1001, _msgId: 'm-a1' },
      { role: 'user', content: 'from-tab-A', timestamp: 3000, _msgId: 'm-tabA' },
    ],
  };
  const okA = await syncConversationToServer(tabA);
  check('tabA_synced', okA === true && server.rev === 6);
  check('tabA_msg_on_server', server.messages.some(m => m._msgId === 'm-tabA'));

  // Tab B: STILL at baseRev=5 (never saw A's write), holds its OWN un-acked
  // message B. It syncs → stale baseRev → 409 → rebase → both survive.
  const tabB = {
    id: 'c-xtab', title: 't', _serverRev: 5, _serverMsgCount: 2,
    createdAt: 1, updatedAt: 2,
    messages: [
      { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
      { role: 'assistant', content: 'a1', timestamp: 1001, _msgId: 'm-a1' },
      { role: 'user', content: 'from-tab-B', timestamp: 3500, _msgId: 'm-tabB' },
    ],
  };
  const okB = await syncConversationToServer(tabB);
  check('tabB_synced', okB === true);

  const ids = server.messages.map(m => m._msgId);
  // The load-bearing assertion: Tab A's message SURVIVES Tab B's write.
  check('tabA_msg_NOT_clobbered', ids.includes('m-tabA'));
  check('tabB_msg_present', ids.includes('m-tabB'));
  // Both users' messages coexist — no data loss on a concurrent two-tab write.
  check('both_present', ids.includes('m-tabA') && ids.includes('m-tabB'));
  check('tabB_adopted_rev', tabB._serverRev === server.rev);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_threw ' + e.message + '\n' + e.stack); });
"""

_FIXED_MARKER = 'const rebased = _rebaseUnackedTail(serverMsgs, conv.messages);'
_NEUTER_REPLACEMENT = 'const rebased = conv.messages;  // NEUTER: blind re-PUT'


def _run_harness(js_source_paths):
    if isinstance(js_source_paths, str):
        js_source_paths = [js_source_paths]
    harness = os.path.join(HERE, '_xtab_rev_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, *js_source_paths],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_cross_tab_stale_write_does_not_clobber():
    proc = _run_harness(source_argv(
        'syncConversationToServer', '_rebaseUnackedTail', '_clearPendingSyncMarkers'))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'cross-tab clobber failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_cross_tab_neuter_restores_clobber(tmp_path):
    """NEUTER: blind re-PUT (skip append-missing-tail) → Tab B's stale write
    OVERWRITES the server with its own message set, ERASING Tab A's message →
    the not-clobbered assertion fails. Proves the rebase prevents the cross-tab
    clobber."""
    sync_file = sources_defining('syncConversationToServer')[0]
    with open(sync_file, encoding='utf-8') as f:
        src = f.read()
    assert _FIXED_MARKER in src, 'fixed rebase marker not found — update the neuter target'
    nfile = tmp_path / 'conversations_neutered.js'
    nfile.write_text(src.replace(_FIXED_MARKER, _NEUTER_REPLACEMENT, 1), encoding='utf-8')

    rel = os.path.relpath(sync_file, JS_DIR).replace(os.sep, '/')
    proc = _run_harness(source_argv(
        'syncConversationToServer', '_rebaseUnackedTail', '_clearPendingSyncMarkers',
        override={rel: str(nfile)}))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('tabA_msg_NOT_clobbered') is False, (
        'NEUTER did not bite: Tab A message survived even a blind re-PUT — '
        'the test does not discriminate the rebase.\n' + output)
    assert lines.get('tabB_msg_present') is True, (
        'unexpected: neuter also dropped Tab B message:\n' + output)
