"""tests/test_frontend_autopilot_stop_vu_splice.py — clicking Stop WHILE the
autopilot Virtual-User bubble is streaming removes the placeholder LOCALLY, and
does NOT lose the follow-up baton.

WHY (the reported bug)
----------------------
During VU streaming the tail of ``conv.messages`` is a ``role:'user'`` msg with
``_isVirtualUser:true, _streamingVu:true`` (the "Autopilot · composing…"
placeholder). When the user clicks Stop, ``send_button.js`` calls
``controller.abort()`` — which tears down the SSE reader BEFORE the backend's
``autopilot_vu_cancel`` frame can be read. That frame is the ONLY code that
splices the placeholder (``_handleAutopilotVuEvent`` cancel branch), so it never
runs: a dangling ``_streamingVu`` ghost bubble is left rendering the frozen
"Autopilot…" pulse until a full page reload (only the idb-cache ``_streamingVu``
filter cleared it).

THE FIX
-------
A LOCAL splice helper ``_removeStreamingVuBubbleIfTail(conv, convId)`` (shipped
in ``static/js/ui/streaming_render.js``) that the Stop handler + ``finishStream``
call directly — it removes the tail placeholder without waiting for an event
that can't arrive. It is a no-op unless the tail is a streaming VU bubble.

★ BATON PRESERVED: ``conv._apPendingBaton`` is a conv FIELD, not a message, so
the message splice cannot touch it and the helper never clears it. This keeps
the ``test_frontend_autopilot_baton_survives_splice.py`` contract intact — a
follow-up the backend already spawned is still resolvable after the ghost is
gone.

This harness slices the REAL shipped ``_removeStreamingVuBubbleIfTail`` out of
``streaming_render.js`` and evals it (bites the actual logic, not a copy).

NEGATIVE CONTROLS (each proven to bite, applied to a COPY):
  • NC-1 — make the helper a no-op (early ``return false``): the ghost bubble
    SURVIVES the stop → the "placeholder removed" assertion FAILS.
  • NC-2 — clear the baton inside the helper (``delete conv._apPendingBaton``):
    the "baton preserved" assertion FAILS — proving the helper must NOT touch it.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SRC = os.path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js')


def _node_available() -> bool:
    """node binary AND node_modules/jsdom — the harness below does
    ``require(path.join(ROOT, 'node_modules', 'jsdom'))``, so a lane with a
    bare node but no npm ci (public CI test-unit job) must skip, not fail
    with 'Cannot find module .../node_modules/jsdom'."""
    try:
        from tests._jsdom import node_deps_available
    except ImportError:
        from _jsdom import node_deps_available
    return node_deps_available()


def _extract_helper(src_text: str) -> str:
    """Slice the real _removeStreamingVuBubbleIfTail out of the shipped file."""
    m = re.search(
        r'function _removeStreamingVuBubbleIfTail\(conv, convId\) \{.*?\n\}\n',
        src_text, re.DOTALL,
    )
    if not m:
        raise AssertionError('could not locate _removeStreamingVuBubbleIfTail')
    return m.group(0)


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"><div id="streaming-msg"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Streaming-substrate globals the helper touches.
win.activeConvId = global.activeConvId = 'C1';
let _twStopCalls = 0, _buildNavCalls = 0, _saveCalls = 0;
win.twStop = global.twStop = () => { _twStopCalls++; };
win.buildTurnNav = global.buildTurnNav = () => { _buildNavCalls++; };
win.saveConversations = global.saveConversations = () => { _saveCalls++; };
win.ConvCache = global.ConvCache = { put: () => {} };

eval(fs.readFileSync(process.argv[2], 'utf8'));   // _removeStreamingVuBubbleIfTail (real or neutered)
check('helper_fn_exposed', typeof _removeStreamingVuBubbleIfTail === 'function');

const BATON = { nextTaskId: 'task-next-123', vuMessage: { content: 'go on' } };

// ── Case A: Stop during VU streaming → placeholder spliced, baton preserved. ──
(function () {
  const vuMsg = { role: 'user', _isVirtualUser: true, _streamingVu: true,
                  _msgId: 'vu-1', content: '' };
  const conv = { id: 'C1', messages: [{ role: 'assistant', content: 'work' }, vuMsg] };
  conv._apPendingBaton = BATON;                       // backend already spawned a follow-up

  const removed = _removeStreamingVuBubbleIfTail(conv, 'C1');
  check('A_returned_true', removed === true);
  check('A_placeholder_spliced', conv.messages.length === 1
        && conv.messages[0].role === 'assistant');
  check('A_no_streaming_vu_left', !conv.messages.some(m => m._streamingVu));
  // BATON MUST SURVIVE — it's a conv field, not a message.
  check('A_baton_preserved', conv._apPendingBaton
        && conv._apPendingBaton.nextTaskId === 'task-next-123');
  // DOM teardown happened (active conv).
  check('A_streaming_msg_removed', !win.document.getElementById('streaming-msg'));
  check('A_twStop_called', _twStopCalls >= 1);
})();

// ── Case B: tail is a SETTLED VU turn (no _streamingVu) → no-op. ──
(function () {
  const settled = { role: 'user', _isVirtualUser: true, _msgId: 'vu-2', content: 'done' };
  const conv = { id: 'C1', messages: [{ role: 'assistant' }, settled] };
  const removed = _removeStreamingVuBubbleIfTail(conv, 'C1');
  check('B_settled_vu_not_removed', removed === false && conv.messages.length === 2);
})();

// ── Case C: tail is a normal assistant msg → no-op (never touch a real turn). ──
(function () {
  const conv = { id: 'C1', messages: [{ role: 'user' }, { role: 'assistant', content: 'reply' }] };
  const removed = _removeStreamingVuBubbleIfTail(conv, 'C1');
  check('C_assistant_tail_not_removed', removed === false && conv.messages.length === 2);
})();

// ── Case D: empty conv → no-op, no throw. ──
(function () {
  const conv = { id: 'C1', messages: [] };
  const removed = _removeStreamingVuBubbleIfTail(conv, 'C1');
  check('D_empty_conv_noop', removed === false);
})();

console.log(out.join('\n'));
"""


def _run(helper_js_text: str) -> str:
    helper_js = os.path.join(HERE, '_ap_stop_splice_helper.js')
    harness = os.path.join(HERE, '_ap_stop_splice_harness.js')
    with open(helper_js, 'w', encoding='utf-8') as f:
        f.write(helper_js_text)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, helper_js, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (helper_js, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node + jsdom not installed')
def test_stop_during_vu_splices_placeholder_and_keeps_baton():
    with open(SRC, encoding='utf-8') as f:
        helper = _extract_helper(f.read())
    output = _run(helper)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'stop-vu-splice failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node + jsdom not installed')
def test_stop_during_vu_neuters_bite():
    """Prove the helper is load-bearing: a no-op helper leaves the ghost bubble,
    and a baton-clearing helper loses the follow-up."""
    with open(SRC, encoding='utf-8') as f:
        real = _extract_helper(f.read())

    # NC-1: make the helper a no-op (return false before the splice).
    nc1 = real.replace(
        'if (!last || !last._isVirtualUser || !last._streamingVu) return false;',
        'if (!last || !last._isVirtualUser || !last._streamingVu) return false;\n  return false; // NC-1 neuter',
    )
    assert nc1 != real, 'NC-1 did not modify the source'
    out1 = _run(nc1)
    assert 'FAIL A_placeholder_spliced' in out1, \
        'NC-1 (no-op helper) should FAIL the splice assertion:\n' + out1

    # NC-2: clear the baton inside the helper (must NOT happen in the real one).
    nc2 = real.replace(
        '  conv.messages.pop();',
        '  conv.messages.pop();\n  delete conv._apPendingBaton; // NC-2 neuter',
    )
    assert nc2 != real, 'NC-2 did not modify the source'
    out2 = _run(nc2)
    assert 'FAIL A_baton_preserved' in out2, \
        'NC-2 (baton cleared) should FAIL the baton-preserved assertion:\n' + out2

    # Sanity: the REAL helper passes both.
    out_real = _run(real)
    for case in ('A_placeholder_spliced', 'A_baton_preserved'):
        assert f'PASS {case}' in out_real, f'real helper should PASS {case}:\n{out_real}'


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        test_stop_during_vu_splices_placeholder_and_keeps_baton()
        test_stop_during_vu_neuters_bite()
        print('PASS test_frontend_autopilot_stop_vu_splice')
