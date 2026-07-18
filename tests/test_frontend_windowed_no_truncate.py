"""tests/test_frontend_windowed_no_truncate.py — data-loss guard for the
windowed-read cutover.

WHY (the "worse than the wait" bug)
-----------------------------------
When the backend serves a WINDOWED first-open (only the tail N messages of a
long conversation), `loadConversationMessages` OVERWRITEs `conv.messages` with
that N-message tail. If it then stamped the sync-truncation baseline
`conv._serverMsgCount` from the WINDOW length (N), the guard in
`syncConversationToServer`::

    if (!allowTruncate && conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) return;

would read `N < N` = false and NOT fire — so a later PUT of the N-message tail
would TRUNCATE the full (e.g. 205-message) server conversation down to N.
Permanent data loss, silently, on every windowed open of a long conv.

THE FIX (static/js/core/conversations.js, Phase-2 OVERWRITE)
------------------------------------------------------------
When the response is windowed, stamp `conv._serverMsgCount` from the
authoritative FULL count `data.totalCount`, not the window length. Then the
guard reads `N < totalCount` = true and correctly BLOCKS the truncating PUT.

This test drives the REAL shipped `_loadConversationMessagesImpl` with a
windowed server response (tail of 3, totalCount 205) and asserts
`conv._serverMsgCount === 205`. NEUTER: force the stamp back to the window
length → the value becomes 3 (the data-loss regression), and the guard would
no longer protect the head.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# Drive _loadConversationMessagesImpl for a NON-cached conv (first load) whose
# server GET returns a WINDOWED envelope: only the tail 3 messages but
# totalCount=205, windowed=true, firstLoadedSeq=202, hasMore=true.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.window.TOFU_CONV_WINDOW = 60;

global.activeConvId = 'long';
global.activeStreams = new Map();
global.streamBufs = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.renderChat = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global.attachCompactionMarkersToConversation = undefined;
global._bgRefreshChat = undefined;
global.Icon = () => '';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.syncConversationToServer = () => {};
global._retriggerHgTranslations = () => {};
global.apiUrl = (p) => p;
global._convSorter = (a, b) => 0;

// The real windowed client half — provides recordWindowState + convWindowParam.
eval(fs.readFileSync(process.argv[3], 'utf8'));  // conv_window.js

// No cache → Phase 1 is skipped, Phase 2 fetch is authoritative.
global.ConvCache = {
  isAvailable: () => true,
  get: () => Promise.resolve(null),
  getMeta: () => Promise.resolve(null),
  getAllMeta: () => Promise.resolve([]),
  put: () => {},
  remove: () => {},
};

const TAIL = [
  { role: 'user', content: 'q202', timestamp: 202 },
  { role: 'assistant', content: 'a203', timestamp: 203 },
  { role: 'user', content: 'q204', timestamp: 204 },
];
const WINDOWED = {
  id: 'long', title: 'long', updatedAt: 999, rev: 3,
  windowed: true, totalCount: 205, firstLoadedSeq: 202, lastLoadedSeq: 204,
  hasMore: true, messages: TAIL,
};
global.Api = {
  conversations: {
    getResponse: async (id, opts) => ({
      status: 200, ok: true,
      _opts: opts,
      headers: { get: () => null },
      json: async () => WINDOWED,
    }),
    get: async () => WINDOWED,
  },
};

// A conv SHELL that still needs its body (first load, no cache).
global.conversations = [{
  id: 'long', title: 'long', messages: [],
  _serverMsgCount: 205, _needsLoad: true,
  createdAt: 1, updatedAt: 1, activeTaskId: null,
}];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof loadConversationMessages !== 'function') {
    console.log('FAIL fn_exposed'); process.exit(0);
  }
  await loadConversationMessages('long');
  for (let i = 0; i < 50; i++) { await Promise.resolve(); }

  const conv = conversations.find(c => c.id === 'long');

  // The window tail was adopted (3 msgs).
  check('windowed_tail_adopted', conv.messages.length === 3);
  // recordWindowState stamped the pagination cursors.
  check('windowed_flag_set', conv._windowed === true);
  // ★ THE GUARD FIX: _serverMsgCount must be the FULL count (205), NOT the
  //   window length (3), so syncConversationToServer's truncation guard
  //   (messages.length < _serverMsgCount → 3 < 205 → true → block) protects
  //   the 202 un-loaded head messages.
  check('serverMsgCount_is_totalCount_not_window', conv._serverMsgCount === 205);

  // Prove the ?window= param was actually sent on the first-open GET.
  console.log('SERVER_MSG_COUNT=' + conv._serverMsgCount);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(conv_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_windowed_no_truncate_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    conv_window_js = os.path.join(JS_DIR, 'conv_window.js')
    try:
        return subprocess.run(
            ['node', harness, conv_js, conv_window_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_windowed_serverMsgCount_is_full_total():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'windowed truncation-guard failures:\n' + output
    assert 'PASS serverMsgCount_is_totalCount_not_window' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_windowed_totalCount_stamp_is_load_bearing(tmp_path):
    """NEUTER: stamp _serverMsgCount from the window length instead of
    totalCount on a COPY → the guard baseline collapses to 3, reintroducing the
    truncation data-loss. Proves the totalCount stamp is what protects the
    un-loaded head. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = ("      conv._serverMsgCount = _isWindowed && typeof data.totalCount === 'number'\n"
              "        ? data.totalCount\n"
              "        : Math.max(serverMsgs.length, conv.messages.length);")
    assert src.count(needle) == 1, 'windowed _serverMsgCount stamp drifted — update the neuter target'
    neutered = src.replace(
        needle,
        '      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_window.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL serverMsgCount_is_totalCount_not_window' in output, (
        'NEUTER did not bite: _serverMsgCount was still the full total without '
        'the totalCount stamp.\n' + output)
    assert 'SERVER_MSG_COUNT=3' in output, (
        'expected the neutered stamp to collapse to the window length (3)\n' + output)

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'
