#!/usr/bin/env python3
"""``renderPendingQueueUI`` must gate every DOM mutation on ``activeConvId``.

WHY
---
There is exactly ONE input-bar queue element in the DOM: ``#pendingQueueBar``
inside ``#pendingQueueContainer`` (``index.html``). ``renderPendingQueueUI``
paints that shared node from ``pendingMessageQueue.get(convId)``. The
``pendingMessageQueue`` Map is per-conv (correct), but the DOM write is not
scoped to the currently-visible conversation.

``_refreshServerQueue(convId)`` is async and fires from many places
(``finishStream``, autopilot arm/disarm, ``_checkForQueuedTask`` retry loop,
TurnStore revalidation, toolbar autopilot handlers). Any one of
those can be in flight when the user switches conversations:

    User in conv A (has 4 queued items) → switches to conv B (empty queue) →
    an in-flight ``_refreshServerQueue('A')`` resolves → it calls
    ``renderPendingQueueUI('A')`` → because that function is un-gated, it
    paints A's 4 items into the shared bar which is currently displaying
    conv B.

Symptom matches the bug report verbatim: 4 Project-Brain autopilot dispatches
belonging to conv ``mrxinirv0t6n6v`` render in the input bar while the user is
viewing an unrelated conversation.

FIX
---
``renderPendingQueueUI(convId)`` must no-op for DOM mutations when
``convId !== activeConvId``. The Map still updates authoritatively; switching
back to A repaints correctly through ``loadConversation``'s explicit
``renderPendingQueueUI(id)`` call.

This test extracts the real shipped function and drives it in node with a
minimal DOM shim, asserting the cross-conv paint does NOT happen.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
SEND_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_send_pipeline.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


def _node() -> str:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    return node


def _extract_queue_block(src: str) -> str:
    """The queue source/collapse helpers + renderPendingQueueUI as ONE block.

    renderPendingQueueUI now delegates to the source/collapse helpers
    (``_queueSourceOf``, ``_queueCollapsedNow``, the ``_QUEUE_*`` icon
    consts, …), so extracting the bare function would ReferenceError under
    eval. The block spans from the sources marker through the end of the
    render function (the collapse toggle is defined in between)."""
    start = src.index('/* ── Queue item sources')
    m = re.search(r'function\s+renderPendingQueueUI\s*\(', src)
    assert m and m.start() > start
    i = src.find('{', m.end())
    return src[start:_brace_match(src, i)]


_HARNESS_PREAMBLE = r'''
const _i18n = {
  'queue.messagesQueued': '条消息排队中',
  'queue.clearAll': '全部清空',
  'queue.attachment': '(attachment)',
  'queue.imagesCount': 'imgs',
  'queue.fromConv': 'from',
  'queue.fromOperator': 'from operator',
  'queue.syncedToServer': 'synced',
  'queue.cancelMsg': 'cancel',
  'autopilot.pendingTakeover': 'Autopilot will take over',
  'autopilot.cancelTakeover': 'Cancel autopilot',
  'autopilot.armedShort': 'Autopilot armed',
};
function t(k, args) { return _i18n[k] != null ? _i18n[k] : k; }
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; });
}
function convTitleById(cid) { return 'Chat ' + String(cid).slice(0, 4); }

// Minimal DOM shim: a real element registry keyed by id.
const _els = {};
global.document = {
  getElementById: (id) => _els[id] || null,
  createElement: (tag) => {
    const el = {
      tagName: tag, _id: '', className: '', _html: '',
      classList: {
        _c: new Set(),
        add(x) { this._c.add(x); },
        remove(x) { this._c.delete(x); },
        contains(x) { return this._c.has(x); },
      },
      get id() { return this._id; },
      set id(v) { this._id = v; if (v) _els[v] = this; },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; },
      appendChild(child) { this._child = child; child.parentNode = this; },
      remove() { if (this.parentNode) this.parentNode._child = null; this.parentNode = null;
                 for (const k of Object.keys(_els)) if (_els[k] === this) delete _els[k]; },
      parentNode: null,
    };
    return el;
  },
};
_els['pendingQueueContainer'] = global.document.createElement('div');
global.setTimeout = (fn, _ms) => { /* no-op — we do not test the 200ms removal */ };

let activeConvId = null;
var pendingMessageQueue = new Map();
'''


def _run(driver: str) -> str:
    """Extract the real renderPendingQueueUI + run the driver under node."""
    node = _node()
    fn = _extract_queue_block(_read(SEND_JS))
    src = _HARNESS_PREAMBLE + '\n' + fn + '\n' + driver
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}\n---\n{src}'
        return out.stdout
    finally:
        os.unlink(tmp)


# ─────────────────────── the bug: cross-conv paint ───────────────────────

def test_render_for_inactive_conv_does_not_paint_bar():
    """Repro of the reported bug.

    Setup:
      • User is viewing conv B (``activeConvId='conv-B'``) — B has no queue.
      • An in-flight ``_refreshServerQueue('conv-A')`` resolves after the
        switch, so ``pendingMessageQueue`` now carries A's 4 items.
      • That handler unconditionally calls ``renderPendingQueueUI('conv-A')``.

    The shared ``#pendingQueueBar`` must NOT be created / populated with A's
    items while B is the active conversation. Any create-and-paint of A's
    queue into the visible bar is the bug.
    """
    driver = r'''
activeConvId = 'conv-B';
pendingMessageQueue.set('conv-A', [
  { queueId: 'q1', kind: 'real', text: 'A-item-1', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
  { queueId: 'q2', kind: 'real', text: 'A-item-2', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
  { queueId: 'q3', kind: 'real', text: 'A-item-3', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
  { queueId: 'q4', kind: 'real', text: 'A-item-4', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(JSON.stringify({
  barExists: !!bar,
  html: bar ? bar.innerHTML : '',
}));
'''
    result = _run(driver)
    import json
    data = json.loads(result)
    # The bug renders A's items into the DOM while B is visible.
    assert 'A-item-1' not in data['html'], (
        "cross-conv bleed: conv A's queued item painted into the bar while "
        "conv B is active — this is the reported bug"
    )
    assert '条消息排队中' not in data['html'], (
        "cross-conv bleed: header count for conv A rendered while conv B is active"
    )


def test_render_for_active_conv_paints_bar_normally():
    """Baseline: when convId matches activeConvId, the bar paints as before."""
    driver = r'''
activeConvId = 'conv-A';
pendingMessageQueue.set('conv-A', [
  { queueId: 'q1', kind: 'real', text: 'my message', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(bar ? bar.innerHTML : '<no-bar>');
'''
    html = _run(driver)
    assert 'my message' in html, (
        'baseline broken: the active-conv paint should still fire'
    )
    assert '条消息排队中' in html


def test_render_for_inactive_conv_leaves_active_bar_alone():
    """Bar already displays B's queue → a stale render for A must not clobber
    it."""
    driver = r'''
activeConvId = 'conv-B';
// First: paint B's queue authoritatively (the visible state).
pendingMessageQueue.set('conv-B', [
  { queueId: 'qb1', kind: 'real', text: 'B-message', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('conv-B');
// Now: a stale in-flight refresh for A arrives.
pendingMessageQueue.set('conv-A', [
  { queueId: 'qa1', kind: 'real', text: 'A-stale-message', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(bar ? bar.innerHTML : '<no-bar>');
'''
    html = _run(driver)
    assert 'B-message' in html, "active conv B's paint got clobbered by stale A refresh"
    assert 'A-stale-message' not in html, (
        "cross-conv bleed: A's stale refresh overwrote the visible bar for B"
    )


def test_switching_to_null_conv_from_inactive_render_does_not_remove_bar():
    """The "empty → schedule removal" branch must ALSO be gated: an inactive
    render with an empty queue should NOT touch a bar that belongs to the
    active conv."""
    driver = r'''
activeConvId = 'conv-B';
// B has a real visible queue.
pendingMessageQueue.set('conv-B', [
  { queueId: 'qb1', kind: 'real', text: 'B-visible', images: [], pdfTexts: [], convRefs: [], replyQuotes: [] },
]);
renderPendingQueueUI('conv-B');
// A stale render for conv A whose queue was cleared server-side.
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(JSON.stringify({
  html: bar ? bar.innerHTML : '',
  removing: bar ? bar.classList.contains('queue-removing') : null,
}));
'''
    import json
    data = json.loads(_run(driver))
    assert 'B-visible' in data['html'], "stale empty-queue render tore down B's visible bar"
    assert data['removing'] is False, (
        "stale render for conv A tagged the active bar with queue-removing"
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
