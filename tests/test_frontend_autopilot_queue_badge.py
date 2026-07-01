"""tests/test_frontend_autopilot_queue_badge.py — regression for the
"autopilot armed but the queue badge shows a stuck 1" report.

WHY
---
The autopilot armed-marker is a persistent turn-source sentinel (kind=
'autopilot', priority 90) that the backend deliberately NEVER dispatches as a
task — ``dequeue_next`` / ``_get_queue_depth`` in lib/message_queue.py both
exclude ``KIND_AUTOPILOT``. It is a flag consumed by the end-of-turn autopilot
hook, not a queued message awaiting dispatch.

But several FRONTEND gates used the raw ``pendingMessageQueue.get(convId).length``
as "there is pending work the backend will start next":
  • the Stop-button badge (static/js/ui/send_button.js) showed ``1`` for an
    armed-but-idle autopilot → looks like a message is permanently stuck;
  • ``finishStream`` (ui/stream_lifecycle.js) inserted a "Dispatching queued
    message…" ghost bubble + fired ``_checkForQueuedTask`` hunting a dispatch
    that never comes (a doomed ~15s retry loop) — the "排出不顺畅".

THE FIX
-------
A shared ``_dispatchableQueueCount(convId)`` helper (main_send_pipeline.js)
mirrors the backend's ``_get_queue_depth`` by excluding the autopilot
sentinel; every dispatch gate + the badge use it.

This harness loads the REAL shipped ``send_button.js`` plus the REAL
``_dispatchableQueueCount`` helper (sliced verbatim out of the shipped
``main_send_pipeline.js`` — so the test bites the actual logic, not a copy),
stubs the window globals ``updateSendButton`` reads, and drives three queue
states through it.

SOURCE-LEVEL NEGATIVE CONTROL (proven by hand; restored byte-identical):
  • Drop the ``.filter((it) => it && it.kind !== 'autopilot')`` from
    ``_dispatchableQueueCount`` (i.e. return the raw length) → the
    autopilot-only queue renders a ``queue-badge`` with count 1 and the
    ``badge_hidden_for_sentinel_only`` assertion FAILS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_helper() -> str:
    """Slice the real _dispatchableQueueCount function out of the shipped file.

    Faithful to the deployed code: any drift in the helper is picked up by the
    test because we eval the ACTUAL source, not a hand-copied duplicate.
    """
    src_path = os.path.join(JS_DIR, 'main', 'main_send_pipeline.js')
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    m = re.search(
        r'function _dispatchableQueueCount\(convId\) \{.*?\n\}\n'
        r'if \(typeof window !== .undefined.\) window\._dispatchableQueueCount = _dispatchableQueueCount;',
        src, re.DOTALL,
    )
    if not m:
        raise AssertionError('could not locate _dispatchableQueueCount in main_send_pipeline.js')
    return m.group(0)


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Fake DOM: a single #sendBtn whose innerHTML/className we can inspect ──
const btn = { innerHTML: '', className: '', onclick: null };
global.document = { getElementById: (id) => (id === 'sendBtn' ? btn : null) };

// ── Window-scope globals updateSendButton reads ──
global.pendingMessageQueue = new Map();
global.activeStreams = new Map();
global._branchStreams = new Map();
global._activeBranch = null;
global.activeConvId = 'conv-ap';
global.conversations = [];
let _activeConv = null;
global.getActiveConv = () => _activeConv;
global.sendMessage = () => {};
global.Api = { chat: { abortTask: () => {} } };
global.twStop = () => {};
global.finishStream = () => {};
global.renderConversationList = () => {};
global._branchKey = (c, m, b) => c + ':' + m + ':' + b;
global._finishBranchStream = () => {};

// argv[2] = real helper slice, argv[3] = real send_button.js
eval(fs.readFileSync(process.argv[2], 'utf8'));   // _dispatchableQueueCount (real)
eval(fs.readFileSync(process.argv[3], 'utf8'));   // ui/send_button.js (real)

check('helper_exposed', typeof _dispatchableQueueCount === 'function');
check('updateSendButton_exposed', typeof updateSendButton === 'function');

const conv = { id: 'conv-ap', activeTaskId: 'task-1', messages: [{ role: 'assistant' }] };
_activeConv = conv;
conversations.push(conv);

function badgeCount() {
  const m = /queue-badge">(\d+)</.exec(btn.innerHTML);
  return m ? parseInt(m[1], 10) : null;   // null = no badge rendered
}

// ── Case 1: ONLY the autopilot sentinel is queued (armed-but-idle). ──
pendingMessageQueue.set('conv-ap', [{ kind: 'autopilot', text: '' }]);
updateSendButton();
check('dispatchable_zero_for_sentinel_only', _dispatchableQueueCount('conv-ap') === 0);
check('badge_hidden_for_sentinel_only', badgeCount() === null);

// ── Case 2: one real queued message + the sentinel → badge counts ONLY the real. ──
pendingMessageQueue.set('conv-ap', [
  { kind: 'real', text: 'hello' },
  { kind: 'autopilot', text: '' },
]);
updateSendButton();
check('dispatchable_one_with_sentinel', _dispatchableQueueCount('conv-ap') === 1);
check('badge_one_with_sentinel', badgeCount() === 1);

// ── Case 3: two real messages, no sentinel → badge 2 (regression guard). ──
pendingMessageQueue.set('conv-ap', [
  { kind: 'real', text: 'a' },
  { kind: 'real', text: 'b' },
]);
updateSendButton();
check('dispatchable_two_reals', _dispatchableQueueCount('conv-ap') === 2);
check('badge_two_reals', badgeCount() === 2);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_autopilot_sentinel_excluded_from_queue_badge():
    helper_js = os.path.join(HERE, '_ap_queue_badge_helper.js')
    harness = os.path.join(HERE, '_ap_queue_badge_harness.js')
    with open(helper_js, 'w', encoding='utf-8') as f:
        f.write(_extract_helper())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             helper_js,
             os.path.join(JS_DIR, 'ui', 'send_button.js'),
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (helper_js, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot queue-badge failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'
