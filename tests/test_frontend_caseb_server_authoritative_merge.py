"""Regression (separation-of-concerns): `initActiveTasks` Case-B recovery must
decide the local-vs-server winner from the SERVER-ISSUED task STATUS, never from
a frontend content-length compare.

WHY
---
Case-B recovery polls `/api/chat/poll/<taskId>` for a conv whose task finished
while the client was away, then merges the poll result into the trailing
assistant message and re-PUTs via `syncConversationToServer`. The old code used
a frontend referee:

    if (localContentLen > serverContentLen) { /* KEEP LOCAL */ }
    else                                     { am.content = td.content; }

That is the exact "looks-longer wins" data-conflict class the project already
retired for the sidebar reconcile (the `localNewest > serverNewest` wall-clock
tiebreaker). A stale-but-longer local buffer (e.g. an IDB cache from a prior
partial render) would WIN over a freshly-committed `done` server tail and then
get PUT back — re-clobbering fresh server truth. Conflict resolution is a
backend concern; the frontend must execute the server's verdict, not invent one.

THE FUNDAMENTAL FIX
-------------------
Key the merge on `td.status`:
  * `status === 'done'`  → settled TERMINAL verdict → adopt `td.content` /
    `td.thinking` VERBATIM even when a stale local buffer is longer.
  * NOT cleanly settled (`interrupted` / crash) → keep-longer-local survives as
    the ONE legitimate OFFLINE RESCUE (local SSE buffer may hold un-acked stream
    content the checkpoint never captured).

This test drives the REAL shipped `initActiveTasks` end-to-end under node:
  • conv-done   : local buffer is LONGER, server poll says status='done' with a
                  SHORTER (canonical) tail → server MUST win (verbatim adopt).
  • conv-interr : local buffer is LONGER, server poll says status='interrupted'
                  with a shorter checkpoint → local MUST be kept (offline rescue).

NEUTER (below): revert the merge to the old length-referee in a COPY of
main_init_tasks.js and prove conv-done now WRONGLY keeps the stale-longer local
— i.e. the test genuinely discriminates the fix. Real file untouched.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

MAIN_INIT_TASKS = runtime_section_path('main/main_init_tasks.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# Two convs, both LOADED, each with an activeTaskId whose task is NOT in the
# running set (→ Case B batch-poll). The poll stub returns a DIFFERENT payload
# per conv keyed on the taskId, so we can exercise both the settled and the
# interrupted branch in one run.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// Local buffers are deliberately LONGER than the server tails so a
// content-length referee would KEEP LOCAL in BOTH cases. Only a status-keyed
// merge distinguishes them.
const LOCAL_LONG   = 'LOCAL-BUFFER-content-that-is-quite-long-abcdefghij';   // 50 chars
const SERVER_DONE  = 'SERVER-done-tail';                                     // shorter, canonical
const SERVER_INTER = 'SRV-cp';                                               // shorter checkpoint

function _seedConvs() {
  return [
    {
      id: 'conv-done', title: 'done', _needsLoad: false, activeTaskId: 'task-done',
      messages: [
        { role: 'user', content: 'q1', timestamp: 1000 },
        { role: 'assistant', content: LOCAL_LONG, thinking: LOCAL_LONG, toolRounds: [], timestamp: 1001, _taskId: 'task-done' },
      ],
      _serverMsgCount: 2,
    },
    {
      id: 'conv-interr', title: 'interr', _needsLoad: false, activeTaskId: 'task-interr',
      messages: [
        { role: 'user', content: 'q2', timestamp: 2000 },
        { role: 'assistant', content: LOCAL_LONG, thinking: LOCAL_LONG, toolRounds: [], timestamp: 2001, _taskId: 'task-interr' },
      ],
      _serverMsgCount: 2,
    },
  ];
}

let conversations = [];
global.__reseed = () => { conversations = _seedConvs(); global.conversations = conversations; };
global.__reseed();
global.activeConvId = null;

global.loadConversationsFromServer = async () => {};
global.loadFolders = async () => {};
global.loadConversationMessages = async () => {};

// Poll stub: keyed by taskId. Both return a SHORTER tail than the local buffer.
const _pollPayload = {
  'task-done':   { status: 'done',        content: SERVER_DONE,  thinking: SERVER_DONE,  finishReason: 'stop' },
  'task-interr': { status: 'interrupted', content: SERVER_INTER, thinking: SERVER_INTER },
};
global.Api = {
  chat: {
    activeResponse: async () => ({ ok: true, json: async () => [] }),  // NO running tasks → all go Case B
    poll: async (taskId) => ({ ok: true, json: async () => _pollPayload[taskId] }),
    active: async () => [],
  },
  conversations: { get: async () => null, put: async () => ({ ok: true }) },
};
global.startAssistantResponse = () => {};
global.connectToTask = () => {};
global.syncConversationToServer = async () => {};
global.saveConversations = () => {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.getActiveConv = () => null;
global.ConvCache = { put() {}, remove() {} };
global.debugLog = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = (e) => e;
global.errorEnvelopeKind = () => '';
global._ensureMsgId = (m) => m;
global._migratePinnedToFolder = () => {};
global._refreshServerQueue = () => {};
global.isBranchTaskId = () => false;
global.initBranchReconnect = () => {};
global._recoverTimerPolls = () => {};
global.config = { model: 'aws.claude-opus-4.8' };
global.serverModel = 'aws.claude-opus-4.8';
global.activeStreams = new Map();
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._editingMsgIdx = null;
global.showStreamingUIForConv = () => {};
global.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.clearTimeout = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // main/main_init_tasks.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof initActiveTasks !== 'function') { console.log('FAIL fn_exposed initActiveTasks missing'); return; }
  check('fn_exposed', true);

  await initActiveTasks();
  // Case B recovery runs in a non-awaited background task; drain microtasks.
  for (let i = 0; i < 100; i++) { await Promise.resolve(); }

  const done = conversations.find(c => c.id === 'conv-done');
  const interr = conversations.find(c => c.id === 'conv-interr');
  const dAm = done.messages[done.messages.length - 1];
  const iAm = interr.messages[interr.messages.length - 1];

  // SETTLED (done): server tail wins VERBATIM even though local was longer.
  check('done_adopts_server_content',  dAm.content === SERVER_DONE);
  check('done_adopts_server_thinking', dAm.thinking === SERVER_DONE);

  // NOT settled (interrupted): longer local buffer is kept as offline rescue.
  check('interr_keeps_local_content',  iAm.content === LOCAL_LONG);
  check('interr_keeps_local_thinking', iAm.thinking === LOCAL_LONG);

  console.log('DONE_CONTENT=' + JSON.stringify(dAm.content));
  console.log('INTERR_CONTENT=' + JSON.stringify(iAm.content));
  console.log(out.join('\n'));
})();
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_caseb_merge_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_source_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_caseb_merge_is_server_status_authoritative():
    src_js = MAIN_INIT_TASKS
    proc = _run_harness(src_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Case-B server-authoritative merge regressions:\n' + output
    for inv in ('done_adopts_server_content', 'done_adopts_server_thinking',
                'interr_keeps_local_content', 'interr_keeps_local_thinking'):
        assert f'PASS {inv}' in output, f'expected {inv} to PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_caseb_merge_neuter(tmp_path):
    """NEUTER: revert the merge to the OLD content-length referee in a COPY of
    main_init_tasks.js and prove conv-done now WRONGLY keeps the stale-longer
    local buffer — i.e. the test genuinely discriminates the fix. Real file
    untouched."""
    src_js = MAIN_INIT_TASKS
    with open(src_js, encoding='utf-8') as f:
        src = f.read()

    # Revert to the length-referee: drop the status gate so a longer local wins
    # regardless of td.status (the old, buggy behaviour).
    anchor = "                const _serverSettled = td.status === 'done';"
    assert anchor in src, 'status-gate anchor not found — update the neuter target'
    neutered_src = src.replace(anchor, "                const _serverSettled = false;", 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'main_init_tasks_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # With the length-referee restored, the stale-longer local WINS on the
    # settled conv → the server-adopt invariant FAILS.
    assert lines.get('done_adopts_server_content') is False, (
        'NEUTER did not bite: reverting to the length-referee did NOT cause the '
        'settled conv to wrongly keep the stale-longer local buffer — the test '
        'does not discriminate the fix.\n' + output)

    # Restore-safety: the shipped file must be byte-identical after the neuter.
    with open(src_js, encoding='utf-8') as f:
        assert f.read() == src, 'shipped main_init_tasks.js was mutated by the neuter test'
