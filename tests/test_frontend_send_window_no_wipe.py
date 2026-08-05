"""jsdom regression: a Phase-2 loadConversationMessages fetch resolving while a
send is in flight (`conv._sendInFlight`) must NOT wipe the optimistic user
message — the send-window race behind the mse9r2ir7ql0v4 live-view corruption
(2026-08-05: the live tab showed only the first user turn + the streaming
bubble; the just-sent middle turns vanished until a manual refresh).

WHY
---
The Phase-2 reconcile decides KEEP_LOCAL vs OVERWRITE from three signals:
`_localGrewDuringFetch` (list grew during the fetch), `_localTsMovedDuringFetch`
(newest local timestamp moved), `_activeTaskIdAppearedDuringFetch`. A fetch that
STARTED *after* the optimistic push (the VLM-parse / POST window — seconds for
an image send) is blind on all three: preFetch already counts the push
(grew=false), the newest local ts IS the preFetch newest (tsMoved=false), and
activeTaskId lands only after the POST (taskAppeared=false). The OVERWRITE
branch then adopted the pre-send server list and wiped the user's just-sent
message; it only reappeared when a later refetch pulled the persisted copy.

THE FIX (static/js/core/conversations.js): `conv._sendInFlight` — held from the
optimistic push through connectToTask (cleared only in the send pipeline's
finally) — counts as un-synced local work, so KEEP_LOCAL covers the blind
window. The ghost-adopt branch (`MERGE_ACTIVE_TASK` adopt-shorter) is gated on
`!conv._sendInFlight` for the post-POST / pre-stream half of the same window.

HARNESS — drives the REAL shipped conv family under bare node:
  • seeds conv=[m0(user), m1(assistant settled)], pushes optimistic m2 and sets
    `conv._sendInFlight = true` (exactly what the send pipeline does);
  • the server GET answers the pre-send list [m0, m1] (chat_send has not
    persisted yet — the POST is still in flight);
  • asserts m2 SURVIVES the reconcile (and survives still when the placeholder
    + activeTaskId are also present — the ghost-adopt half).
DOUBLE-NEUTER:
  • strip the `_sendInFlight` disjunct from localHasUnsynced → m2 is wiped;
  • strip the `!conv._sendInFlight` gate from the ghost branch → the
    placeholder pair is adopted away in the post-POST window.
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
from _jsdom import run_harness, frontend_module_guard  # noqa: E402
from _conv_bundle_sources import conv_family_sources  # noqa: E402

frontend_module_guard(need_jsdom=False)

_HARNESS = r"""
const fs = require('fs');
global.window = global;

global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global.apiUrl = (p) => p;
global.ConvCache = {
  isAvailable: () => true,
  get: async () => null,
  getMeta: async () => null,
  getAllMeta: async () => [],
  put: async () => {},
  remove: async () => {},
};
global.getActiveConv = () => conversations.find((c) => c.id === activeConvId) || null;
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.ConvView = { replaceAll: () => {} };
global.saveConversations = () => {};
global.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };

/* The server answers the PRE-SEND list — chat_send has not persisted m2 yet. */
const M0 = { role: 'user', content: 'first question', _msgId: 'm0', timestamp: 1000 };
const M1 = { role: 'assistant', content: 'first answer', _msgId: 'm1', timestamp: 2000, finishReason: 'stop', _taskId: 'tOLD' };
global.Api = {
  conversations: {
    getResponse: async () => ({
      ok: true, status: 200,
      headers: { get: () => null },
      json: async () => ({ messages: [M0, M1], rev: 9, updatedAt: 3000 }),
    }),
    get: async () => ({ messages: [M0, M1], rev: 9, updatedAt: 3000 }),
  },
};

global.conversations = [];
const _files = [process.argv[2], ...process.argv.slice(4)];  // argv[3] is ROOT
for (const f of _files) eval(fs.readFileSync(f, 'utf8'));
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function seed() {
  conversations.length = 0;
  conversations.push({
    id: 'c1', title: 'c1', messages: [M0, M1],
    _serverMsgCount: 2, _needsLoad: true,
    createdAt: 1000, updatedAt: 2000, activeTaskId: null,
  });
}

(async () => {
  if (typeof loadConversationMessages !== 'function') {
    console.log('FAIL fn_exposed loadConversationMessages missing'); process.exit(0);
  }
  check('fn_exposed', true);

  /* ── A. PRE-POST window (VLM wait): fetch started AFTER the push resolves
   *      with the pre-send list — the optimistic m2 must survive. ── */
  {
    seed();
    const conv = conversations[0];
    const m2 = { role: 'user', content: 'second question (image attached)',
                 _msgId: 'm2', timestamp: 4000, images: [{ preview: '/api/images/x.jpg' }] };
    conv.messages.push(m2);             // the optimistic push (send pipeline)
    conv._sendInFlight = true;          // …and its in-flight marker
    await loadConversationMessages('c1');
    check('A_send_window_user_msg_survives',
      conv.messages.length === 3 && conv.messages[2]._msgId === 'm2');
  }

  /* ── B. POST-POST / pre-stream window: placeholder pushed + activeTaskId
   *      set, stream not yet registered, server still pre-send. The ghost-
   *      adopt branch must not sweep the fresh pair. ── */
  {
    seed();
    const conv = conversations[0];
    conv.messages.push({ role: 'user', content: 'q2', _msgId: 'm2', timestamp: 4000 });
    conv.messages.push({ role: 'assistant', content: '', thinking: '',
                         toolRounds: [], _msgId: 'ph1', timestamp: 4001 });
    conv.activeTaskId = 'tNEW-task';
    conv._sendInFlight = true;
    await loadConversationMessages('c1');
    check('B_send_window_pair_survives',
      conv.messages.length === 4 && conv.messages[3]._msgId === 'ph1');
  }

  /* ── C. CONTROL: no send in flight, idle settled conv — the normal
   *      reconcile still adopts the server body (no freeze). ── */
  {
    seed();
    const conv = conversations[0];
    await loadConversationMessages('c1');
    check('C_idle_reconcile_unaffected',
      conv.messages.length === 2 && conv.messages[1]._msgId === 'm1');
  }

  console.log(out.join('\n'));
  console.log('__JSDOM_RESULT__ ' + JSON.stringify({
    pass: out.filter(l => l.startsWith('PASS')).length,
    fail: out.filter(l => l.startsWith('FAIL')).length,
  }));
  process.exit(0);
})();
"""


def _sources(*, neuter=None):
    override = None
    if neuter:
        target = os.path.join(JS_DIR, 'core', 'conversations.js')
        src = open(target, encoding='utf-8').read()
        if neuter == 'both_gates':
            needle1 = "      !!conv._sendInFlight ||"
            assert src.count(needle1) == 1, 'send-window disjunct drifted — update the neuter target'
            src = src.replace(needle1, "      false ||  // NEUTERED-w2", 1)
            needle2 = ("    } else if (conv.activeTaskId && hasLocalData\n"
                       "               && !conv._sendInFlight\n"
                       "               && !activeStreams.has(convId) && serverMsgs.length < conv.messages.length")
            assert src.count(needle2) == 1, 'ghost-branch gate drifted — update the neuter target'
            src = src.replace(needle2,
                              "    } else if (conv.activeTaskId && hasLocalData\n"
                              "               && !activeStreams.has(convId) && serverMsgs.length < conv.messages.length",
                              1)
        else:  # pragma: no cover
            raise ValueError(neuter)
        copy = os.path.join(HERE, f'_conversations_neutered_{neuter}.js')
        with open(copy, 'w') as f:
            f.write(src)
        override = {'core/conversations.js': copy}
    return conv_family_sources(override=override)


def test_send_window_never_wipes_optimistic_pair():
    run_harness(
        target_js=_sources()[0],
        extra_targets=_sources()[1:],
        body_js=_HARNESS,
        expect_pass=4,
        label='send-window-no-wipe',
    )


def test_NC_send_window_gates_are_load_bearing(tmp_path):
    """NEUTER (both gates removed): the ghost-adopt branch fires in the
    post-POST / pre-stream window and wipes the fresh pair → check B FAILS.
    Check A still passes — the OVERWRITE branch's own `_rescuableLocalTail`
    rescue covers the pre-POST window even with both gates removed, which is
    exactly the two-layer story this suite documents."""
    srcs = _sources(neuter='both_gates')
    # run_harness asserts zero FAILs; for the neuter we EXPECT a fail, so run raw.
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
        _neu = os.path.join(HERE, '_conversations_neutered_both_gates.js')
        if os.path.exists(_neu):
            os.remove(_neu)
    out = proc.stdout
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    assert 'FAIL B_send_window_pair_survives' in out, (
        'NEUTER did not bite: fresh pair survived without either send-window gate.\n' + out)
    assert 'PASS A_send_window_user_msg_survives' in out, (
        'the OVERWRITE rescue should still cover the pre-POST window.\n' + out)



