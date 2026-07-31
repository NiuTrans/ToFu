#!/usr/bin/env python3
"""Queued-dispatch discovery: watcher + in-flight latch + notify hook.

WHY (production incident 2026-07-25, conv ms04oggm34tkcp)
--------------------------------------------------------
When ``/api/v1/chat/send`` returns ``{queued: true}`` the client painted the
queue bar and RETURNED — zero listeners. ``_checkForQueuedTask`` (the only
dispatch-discovery probe) ran ONLY from finishStream, which had fired three
minutes BEFORE the message was even queued (the running turn's stream closed
at turn end while the autopilot VU kept the conv busy invisibly). The backend
auto-dispatched the queued message at 17:00:30 and the client noticed
NOTHING — six minutes of silence until a manual page refresh.

FIX (three layers, all additive)
--------------------------------
  1. ``_watchQueuedDispatch(convId)`` — the queued-send branch now starts a
     bounded backoff watcher (~90s) that re-probes via the SAME
     ``_checkForQueuedTask`` seam until the dispatched task appears locally
     (attached), the queue drains, or the budget ends. This is the
     push-down fallback.
  2. The notify-push hook (cross_tab_sync.js ``_onConvNotifyPush``) — the
     event-driven primary: the dispatch path emits ``notify_conv_changed``,
     and a frame landing on a conv whose LOCAL queue mirror holds
     dispatchable items (and which has no local stream/task) now triggers
     ``_checkForQueuedTask`` immediately.
  3. An in-flight latch (``_queuedCheckInFlight``) — with THREE callers
     (finishStream, watcher, notify hook) overlapping probes would stack
     duplicate ``/api/v1/chat/active`` fetches; at most one check chain per
     conv runs at a time (retries are the SAME chain and pass through).

NEUTER (manual A/B): deleting the latch turns test_latch_blocks_concurrent_checks
red (double fetch); deleting the watcher call from the queued branch turns the
static pin red. Both prove the new seams are load-bearing.

Uses the splice-extract + node-eval pattern from
tests/test_frontend_pending_queue_active_conv_gate.py — the REAL shipped
functions are extracted from main_send_pipeline.js, never re-implemented.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SEND_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_send_pipeline.js')
SYNC_JS = os.path.join(ROOT, 'static', 'js', 'core', 'cross_tab_sync.js')


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


def _extract_watch_block(src: str) -> str:
    """_dispatchableQueueCount + latch const + _checkForQueuedTask +
    _QUEUED_WATCH_DELAYS + _watchQueuedDispatch as ONE eval-able block."""
    parts = [_extract_fn(src, '_dispatchableQueueCount')]
    m = re.search(r'const\s+_queuedCheckInFlight\s*=', src)
    assert m, 'in-flight latch const missing'
    parts.append(src[m.start():src.index('\n', m.start())])
    parts.append(_extract_fn(src, '_checkForQueuedTask'))
    m = re.search(r'const\s+_QUEUED_WATCH_DELAYS\s*=', src)
    assert m, 'watcher delay schedule missing'
    parts.append(src[m.start():src.index('\n', m.start())])
    parts.append(_extract_fn(src, '_watchQueuedDispatch'))
    return '\n'.join(parts)


_HARNESS_PREAMBLE = r'''
// ── Controllable timer: capture callbacks; the driver pumps them manually. ──
const _timers = [];
global.setTimeout = (fn, ms) => { _timers.push({ fn, ms }); return _timers.length; };

// ── DOM stub (ghost-placeholder cleanup is try/catch guarded). ──
global.document = { getElementById: () => null };

// ── Shared globals the extracted functions reference. ──
var pendingMessageQueue = new Map();
var activeConvId = 'C1';
const activeStreams = new Map();
global.activeStreams = activeStreams;
const conv = { id: 'C1', messages: [], activeTaskId: null, autopilotEnabled: false };
var conversations = [conv];

const calls = { fetch: 0, check: 0, refresh: 0, load: 0, connect: [], dbg: [] };
// Api.chat.active — MODE-controlled (set by the driver).
global.Api = { chat: { active: () => Promise.resolve([]) } };
global._refreshServerQueue = (cid) => { calls.refresh++; return Promise.resolve(); };
global.loadConversationMessages = (cid) => { calls.load++; return Promise.resolve(conv); };
global._ensureMsgId = (m) => {};
global.connectToTask = (cid, tid) => { calls.connect.push([cid, tid]); };
global.renderConversationList = () => {};
global.debugLog = () => {};
global.window = global;
global.window.ConvView = { replaceAll: () => {} };
'''


def _run(driver: str) -> str:
    node = _node()
    block = _extract_watch_block(_read(SEND_JS))
    src = _HARNESS_PREAMBLE + '\n' + block + '\n' + driver
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}\n---\n{src[-3000:]}'
        return out.stdout
    finally:
        os.unlink(tmp)


# ─────────────────────── dynamic behaviour (node) ───────────────────────

def test_watcher_discovers_dispatch_and_attaches():
    """THE BUG's fix, end to end at the seam: queue has a real item, no local
    stream; the server has now auto-dispatched (a running task appears in
    /api/v1/chat/active). The watcher must probe, attach, and STOP (no
    further ticks once conv.activeTaskId is bound by the attach)."""
    driver = r'''
(async () => {
  pendingMessageQueue.set('C1', [{ kind: 'real', text: 'hello' }]);
  Api.chat.active = () => {
    calls.fetch++;
    return Promise.resolve([{ id: 'task-new-1', convId: 'C1', status: 'running', aborted: false }]);
  };
  _watchQueuedDispatch('C1');
  // Let all pending promises resolve (probe → attach chain).
  await new Promise(r => { const p = () => Promise.resolve().then(() => {
    if (calls.connect.length) r(); else setImmediate(p);
  }); p(); });
  const out = [];
  const check = (n, c) => out.push((c ? 'PASS ' : 'FAIL ') + n);
  check('probed_active', calls.fetch === 1);
  check('attached_to_dispatched', calls.connect.length === 1
        && calls.connect[0][0] === 'C1' && calls.connect[0][1] === 'task-new-1');
  check('attach_bound_task', conv.activeTaskId === 'task-new-1');
  check('reloaded_messages', calls.load === 1);
  // Watcher scheduled at most the first follow-up tick before the attach
  // landed; after binding, any pumped timer must no-op.
  const before = calls.fetch;
  for (const t of _timers.splice(0)) { t.fn(); await Promise.resolve(); }
  await new Promise(r => setImmediate(r));
  check('no_extra_probes_after_attach', calls.fetch === before);
  console.log(out.join('\n'));
})();
'''
    output = _run(driver)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'watcher dispatch-discovery guard failed:\n' + output


def test_watcher_noops_when_stream_live():
    """A live local stream means THIS tab is already driving the conv — the
    watcher must not probe at all (finishStream owns that path)."""
    driver = r'''
(async () => {
  activeStreams.set('C1', {});
  pendingMessageQueue.set('C1', [{ kind: 'real', text: 'hello' }]);
  Api.chat.active = () => { calls.fetch++; return Promise.resolve([]); };
  _watchQueuedDispatch('C1');
  await new Promise(r => setImmediate(r));
  const out = [];
  const check = (n, c) => out.push((c ? 'PASS ' : 'FAIL ') + n);
  check('no_probe_with_live_stream', calls.fetch === 0);
  check('no_timer_scheduled', _timers.length === 0);
  console.log(out.join('\n'));
})();
'''
    output = _run(driver)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'watcher live-stream no-op guard failed:\n' + output


def test_latch_blocks_concurrent_checks():
    """The in-flight latch: a second _checkForQueuedTask for the same conv
    while the first chain is still awaiting /active must be a no-op —
    otherwise finishStream + watcher + notify-hook stack duplicate probes."""
    driver = r'''
(async () => {
  Api.chat.active = () => { calls.fetch++; return new Promise(() => {}); };  // never resolves
  const p1 = _checkForQueuedTask('C1');
  const p2 = _checkForQueuedTask('C1');   // must short-circuit on the latch
  await Promise.resolve(); await Promise.resolve();
  const out = [];
  const check = (n, c) => out.push((c ? 'PASS ' : 'FAIL ') + n);
  check('single_inflight_fetch', calls.fetch === 1);
  console.log(out.join('\n'));
})();
'''
    output = _run(driver)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'in-flight latch guard failed (NEUTER the latch to watch this go red):\n' + output


def test_latch_releases_after_chain_settles():
    """Once the chain resolves (no task found, no retry budget consumed
    because the queue is empty), the latch must RELEASE — the next caller
    probes again. A leaking latch would freeze discovery forever."""
    driver = r'''
(async () => {
  Api.chat.active = () => { calls.fetch++; return Promise.resolve([]); };
  await _checkForQueuedTask('C1');        // settles: no queue items → no retry
  await _checkForQueuedTask('C1');        // latch released → probes again
  const out = [];
  const check = (n, c) => out.push((c ? 'PASS ' : 'FAIL ') + n);
  check('chain_reruns_after_settle', calls.fetch === 2);
  console.log(out.join('\n'));
})();
'''
    output = _run(driver)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'latch release guard failed:\n' + output


# ────────────────────── static wire pins (no node needed) ──────────────────────

def test_queued_branch_starts_watcher():
    """The cross-function seam: the {queued:true} send branch MUST start the
    dispatch watcher — deleting the call silently returns the client to the
    pre-fix world (paint the bar, listen to nothing)."""
    src = _read(SEND_JS)
    branch = src.index('if (result.queued)')
    call = src.index('_watchQueuedDispatch(convId)')
    assert branch >= 0 and branch < call < branch + 2500, (
        'the result.queued branch must call _watchQueuedDispatch(convId)'
    )


def test_check_fn_has_inflight_latch():
    src = _read(SEND_JS)
    assert '_queuedCheckInFlight' in src, 'in-flight latch missing'


def test_notify_push_has_dispatch_hook():
    """The event-driven layer: _onConvNotifyPush must trigger the dispatch
    check for a conv with queued items + no local stream (the narrow gate)."""
    src = _read(SYNC_JS)
    assert '_checkForQueuedTask(frame.convId)' in src, (
        'cross_tab_sync.js notify handler must hook _checkForQueuedTask '
        'for convs with dispatchable queued items'
    )


def test_check_fn_window_published():
    """cross_tab_sync.js (core/) reaches _checkForQueuedTask through window
    scope at runtime — the publish line must exist."""
    src = _read(SEND_JS)
    assert 'window._checkForQueuedTask' in src
