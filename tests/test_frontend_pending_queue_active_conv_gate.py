#!/usr/bin/env python3
"""``renderPendingQueueUI`` must gate every DOM mutation on ``activeConvId``.

WHY
---
There is exactly ONE input-bar queue element in the DOM: ``#pendingQueueBar``
inside ``#pendingQueueContainer``. The retained ``renderPendingQueueUI``
(``frontend/src/runtime/sections/main/main_send_pipeline.js``) paints that
shared node from the AUTOPILOT sentinel rows of
``runtimeScope.ConversationTurnStore`` queue state. (Regular queued messages
now render inline in the transcript via ``conversation-queue-item``; the
input-bar queue only carries the legacy autopilot marker, which the transcript
view-model deliberately excludes.)

The TurnStore owns one queue per conversation, but the DOM write is not scoped
to the currently-visible conversation. ``_refreshServerQueue(convId)`` is async
and fires from many places (``finishStream``, autopilot arm/disarm, the
``_checkForQueuedTask`` retry loop, TurnStore revalidation, toolbar autopilot
handlers). Any one of those can be in flight when the user switches
conversations:

    User in conv A (armed autopilot) → switches to conv B → an in-flight
    ``_refreshServerQueue('A')`` resolves → it calls
    ``renderPendingQueueUI('A')`` → because the paint is un-gated, it paints
    A's autopilot marker into the shared bar which is currently displaying
    conv B.

FIX
---
``renderPendingQueueUI(convId)`` must no-op for DOM mutations when
``convId !== activeConvId``. The TurnStore still updates authoritatively;
switching back to A repaints correctly through ``loadConversation``'s explicit
``renderPendingQueueUI(id)`` call.

This test extracts the real shipped ``renderPendingQueueUI`` (+ its icon
consts) from the retained section and drives it in node with a minimal DOM shim
and a ``ConversationTurnStore`` stub, asserting the cross-conv paint does NOT
happen. The old per-source / collapse queue bar (``_queueSourceOf``,
``togglePendingQueueCollapsed``, ``QUEUE_AUTO_COLLAPSE_MIN``) was removed in
the Vite + storage runtime migration (commit ``026042a2``); see the deleted
``tests/test_frontend_queue_bar_sources_collapse.py``.
"""

from __future__ import annotations

import json
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


def _node() -> str:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    return node


def _extract_queue_bar_block(src: str) -> str:
    """The autopilot sentinel renderer + its two icon consts as ONE block.

    ``renderPendingQueueUI`` references ``_QUEUE_ICON_AUTOPILOT`` /
    ``_QUEUE_ICON_X`` directly, so extracting the bare function would
    ReferenceError under eval. The block spans from the icon consts through
    the end of the render function."""
    start = src.index('const _QUEUE_ICON_AUTOPILOT = ')
    m = re.search(r'function\s+renderPendingQueueUI\s*\(', src)
    assert m and m.start() > start
    i = src.find('{', m.end())
    return src[start:_brace_match(src, i)]


_HARNESS_PREAMBLE = r'''
var runtimeScope = (typeof window !== 'undefined') ? window : globalThis;
const _i18n = {
  'autopilot.pendingTakeover': 'Autopilot will take over',
  'autopilot.cancelTakeover': 'Cancel autopilot',
  'autopilot.armedShort': 'Autopilot armed',
};
function t(k) { return _i18n[k] != null ? _i18n[k] : k; }
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; });
}

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
global.setTimeout = (fn, _ms) => { /* no-op — the 200ms removal is not under test */ };

let activeConvId = null;

// ConversationTurnStore stub: renderPendingQueueUI reads queueItems through
// _queueItemsForConversation → runtimeScope.ConversationTurnStore (the same
// immutable store that projects the transcript).
const _queues = new Map();
runtimeScope.ConversationTurnStore = {
  ensureRuntimeStore: (convId) => ({
    getState: () => ({ queueItems: _queues.get(convId) || [] }),
  }),
};
function _queueItemsForConversation(convId) {
  if (!convId || !runtimeScope.ConversationTurnStore) return [];
  const state = runtimeScope.ConversationTurnStore
    .ensureRuntimeStore(convId).getState();
  return Array.isArray(state.queueItems) ? state.queueItems : [];
}

function _mkAutopilot(queueId) {
  return { queueId: queueId || 'ap-1', kind: 'autopilot', text: '',
           images: [], pdfTexts: [], convRefs: [], replyQuotes: [] };
}
'''


def _run(driver: str) -> str:
    """Extract the real renderPendingQueueUI + run the driver under node."""
    node = _node()
    block = _extract_queue_bar_block(_read(SEND_JS))
    src = _HARNESS_PREAMBLE + '\n' + block + '\n' + driver
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
      • User is viewing conv B (``activeConvId='conv-B'``) — B has no marker.
      • An in-flight ``_refreshServerQueue('conv-A')`` resolves after the
        switch, so conv A's TurnStore now carries an armed autopilot sentinel.
      • That handler unconditionally calls ``renderPendingQueueUI('conv-A')``.

    The shared ``#pendingQueueBar`` must NOT be created / populated with A's
    marker while B is the active conversation.
    """
    driver = r'''
activeConvId = 'conv-B';
_queues.set('conv-A', [ _mkAutopilot('ap-a') ]);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(JSON.stringify({
  barExists: !!bar,
  html: bar ? bar.innerHTML : '',
}));
'''
    data = json.loads(_run(driver))
    assert data['barExists'] is False, (
        "cross-conv bleed: conv A's autopilot marker painted into the shared "
        "bar while conv B is active — this is the reported bug"
    )
    assert 'Autopilot will take over' not in data['html']


def test_render_for_active_conv_paints_bar_normally():
    """Baseline: when convId matches activeConvId, the bar paints as before."""
    driver = r'''
activeConvId = 'conv-A';
_queues.set('conv-A', [ _mkAutopilot('ap-a') ]);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(bar ? bar.innerHTML : '<no-bar>');
'''
    html = _run(driver)
    assert 'Autopilot armed' in html, (
        'baseline broken: the active-conv paint should still fire'
    )
    assert 'Autopilot will take over' in html
    assert "cancelAutopilotMarker('conv-A')" in html


def test_render_for_inactive_conv_leaves_active_bar_alone():
    """Bar already displays B's marker → a stale render for A must not clobber
    it."""
    driver = r'''
activeConvId = 'conv-B';
// First: paint B's marker authoritatively (the visible state).
_queues.set('conv-B', [ _mkAutopilot('ap-b') ]);
renderPendingQueueUI('conv-B');
// Now: a stale in-flight refresh for A arrives.
_queues.set('conv-A', [ _mkAutopilot('ap-a') ]);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(bar ? bar.innerHTML : '<no-bar>');
'''
    html = _run(driver)
    assert "cancelAutopilotMarker('conv-B')" in html, (
        "active conv B's paint got clobbered by stale A refresh"
    )
    assert "cancelAutopilotMarker('conv-A')" not in html, (
        "cross-conv bleed: A's stale refresh overwrote the visible bar for B"
    )


def test_switching_to_null_conv_from_inactive_render_does_not_remove_bar():
    """The "empty → schedule removal" branch must ALSO be gated: an inactive
    render with an empty queue should NOT touch a bar that belongs to the
    active conv."""
    driver = r'''
activeConvId = 'conv-B';
// B has a real visible marker.
_queues.set('conv-B', [ _mkAutopilot('ap-b') ]);
renderPendingQueueUI('conv-B');
// A stale render for conv A whose queue was cleared server-side.
_queues.set('conv-A', []);
renderPendingQueueUI('conv-A');
const bar = document.getElementById('pendingQueueBar');
process.stdout.write(JSON.stringify({
  html: bar ? bar.innerHTML : '',
  removing: bar ? bar.classList.contains('queue-removing') : null,
}));
'''
    data = json.loads(_run(driver))
    assert "cancelAutopilotMarker('conv-B')" in data['html'], (
        "stale empty-queue render tore down B's visible bar"
    )
    assert data['removing'] is False, (
        "stale render for conv A tagged the active bar with queue-removing"
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
