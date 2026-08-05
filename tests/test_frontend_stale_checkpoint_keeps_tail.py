"""node regression: a 409 `blocked_stale_checkpoint` sync rejection must REBASE
(server base + identity-filtered local-only tail), not wholesale-replace —
otherwise a genuinely un-synced local tail (poor-network pending-sync message,
failed-rescue rows) is silently dropped with no beacon.

WHY (2026-08-05 live-view audit, writers census top-1)
------------------------------------------------------
`syncConversationToServer`'s two 409 handlers were asymmetric:
`blocked_rev_conflict` rebased via `_rebaseUnackedTail`, but
`blocked_stale_checkpoint` did `conv.messages = freshMsgs` — dropping any
local-only tail rows. The fix routes the stale-checkpoint recovery through the
same tested rebase primitive.

HARNESS — drives the REAL shipped conv family (`syncConversationToServer`)
under bare node with a flippable API stub:
  • server holds [M0, M1]; local holds [M0, M1, L2] where L2 carries a _msgId
    and exists only locally (un-acked);
  • PUT answers 409 blocked_stale_checkpoint; GET answers the full server list;
  • asserts L2 SURVIVES the recovery (rebased onto the server base).
NC: restore the wholesale replace → L2 dropped → red.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

sys.path.insert(0, HERE)
from _jsdom import frontend_module_guard  # noqa: E402
from _conv_bundle_sources import conv_family_sources  # noqa: E402

frontend_module_guard(need_jsdom=False)

_HARNESS = r"""
const fs = require('fs');
global.window = global;

global.activeConvId = null;
global.activeStreams = new Map();
global.debugLog = () => {};
global.config = { defaultThinkingDepth: 'medium' };
global.renderConversationList = () => {};
global.AbortSignal = { timeout: () => undefined };
global.apiUrl = (p) => p;
global.ConvCache = { put: () => Promise.resolve(), get: async () => null, remove: async () => {} };
global.ConvView = { replaceAll: () => {} };

const M0 = { role: 'user', content: 'q1', _msgId: 'm0', timestamp: 1000 };
const M1 = { role: 'assistant', content: 'a1', _msgId: 'm1', timestamp: 2000, finishReason: 'stop' };
const L2 = { role: 'user', content: 'poor-network follow-up', _msgId: 'l2', timestamp: 3000 };

global.Api = {
  conversations: {
    put: async () => ({
      ok: false, status: 409,
      json: async () => ({ error: 'blocked_stale_checkpoint' }),
    }),
    get: async () => ({ messages: [M0, M1], title: 't', rev: 7 }),
  },
  health: { check: async () => ({ ok: true }) },
};

global.conversations = [];
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof syncConversationToServer !== 'function') {
    console.log('FAIL fn_exposed syncConversationToServer missing'); process.exit(0);
  }
  check('fn_exposed', true);

  const conv = {
    id: 'c-stale', title: 't',
    messages: [M0, M1, L2],
    _serverMsgCount: 2,
    createdAt: 1000, updatedAt: 3000, activeTaskId: null,
  };
  conversations.push(conv);

  const ok = await syncConversationToServer(conv);
  check('sync_reports_not_synced', ok === false);
  check('server_base_adopted',
    conv.messages.length >= 2 && conv.messages[0]._msgId === 'm0' && conv.messages[1]._msgId === 'm1');
  check('unsynced_local_tail_SURVIVES',
    conv.messages.length === 3 && conv.messages[2]._msgId === 'l2');
  check('server_msg_count_is_server_base', conv._serverMsgCount === 2);

  console.log(out.join('\n'));
  console.log('__JSDOM_RESULT__ ' + JSON.stringify({
    pass: out.filter(l => l.startsWith('PASS')).length,
    fail: out.filter(l => l.startsWith('FAIL')).length,
  }));
  process.exit(0);
})();
"""


def _sources(*, neuter=False):
    override = None
    if neuter:
        target = os.path.join(JS_DIR, 'core', 'conversations.js')
        src = open(target, encoding='utf-8').read()
        needle = "              conv.messages = _rebaseUnackedTail(freshMsgs, conv.messages);"
        assert src.count(needle) == 1, 'stale-checkpoint rebase line drifted — update the neuter target'
        src = src.replace(needle,
                          "              conv.messages = freshMsgs;  // NEUTERED-wholesale", 1)
        copy = os.path.join(HERE, '_conversations_neutered_stale409.js')
        with open(copy, 'w') as f:
            f.write(src)
        override = {'core/conversations.js': copy}
    return conv_family_sources(override=override)


def _run(srcs):
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE, delete=False) as fh:
        hp = fh.name
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', hp, *srcs], capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': os.path.join(HERE, '_jsdom_harness.js')})
    finally:
        os.remove(hp)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def test_stale_checkpoint_recovery_preserves_unsynced_tail():
    output = _run(_sources())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'stale-checkpoint recovery failures:\n' + output
    assert 'PASS unsynced_local_tail_SURVIVES' in output, output


def test_NC_wholesale_replace_drops_the_tail():
    """NEUTER: restore the wholesale replace → the un-synced local tail is
    dropped → check fails. Cleans up the neutered copy afterwards."""
    srcs = _sources(neuter=True)
    try:
        output = _run(srcs)
    finally:
        neu = os.path.join(HERE, '_conversations_neutered_stale409.js')
        if os.path.exists(neu):
            os.remove(neu)
    assert 'FAIL unsynced_local_tail_SURVIVES' in output, (
        'NEUTER did not bite: local tail survived the wholesale replace.\n' + output)
