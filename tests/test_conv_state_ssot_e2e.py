#!/usr/bin/env python3
"""pt_conv_state_ssot — E2E integration: end-to-end evidence that the
server-authoritative busy channel actually connects, replacing the
"claim victory from unit-tests alone" gap owner flagged.

The three unit suites in the SSOT set each cover ONE pipeline segment:

  * test_conv_state_ssot_payload.py    — notify frame carries the new
                                         fields (server payload contract).
  * test_conv_state_ssot_snapshot.py   — connect snapshot frame is well-formed
                                         (server route contract).
  * test_frontend_conv_state_reducer.py — reducer consumes the fields
                                         correctly (client contract).

None of them proves the SEGMENTS ARE CONNECTED. This suite does — it drives
the real seams together with test doubles ONLY at the transport layer:

  1. Seed a running task directly into the SHARED task registry.
  2. Invoke notify_conv_changed with a captured push_event outbound seam.
  3. Feed the captured payload into the JavaScript reducer via a Node
     subprocess (real static/js/core/conv_state_reducer.js loaded from
     disk — NOT a re-implementation).
  4. Assert conv._authoritativeActiveTaskIds contains the seeded tid
     AND computeConvBusy(conv) returns true.
  5. Flip the task to 'done', re-notify, feed to reducer, assert Set
     cleared + computeConvBusy returns false.

Two visible-bug scenarios end-to-end:

  * "phone starts generating, PC sidebar lights busy dot without F5"
    (send-side ignition, faces 1-3 below).
  * "phone finishes, PC sidebar extinguishes dot without waiting for
    poll" (completion-side extinction, faces 4-5 below). This depends
    on P3's supersede-abort broadcast + happy-path completion updating
    the registry BEFORE the notify frame's snapshot read.

Test doubles ONLY at:
  * push_event (captured, not sent to a real WebSocket — inspected)
  * DB rev query inside notify_conv_changed (not needed for busy signal;
    the busy signal carries the tuple rev, not the body rev)

Everything else runs the REAL code paths:
  * lib.tasks_pkg.manager._registry.snapshot_running_by_conv
  * lib.conversations.meta_cache.notify_conv_changed
  * lib.conversations.meta_cache._running_task_ids_rev
  * static/js/core/conv_state_reducer.js (loaded live into Node)

Fair criticism this suite still doesn't cover:
  * ASGI WebSocket send/receive layer — asserted by
    test_conv_state_ssot_snapshot's routes/push.py _handle_client_frame
    call, not here.
  * Multi-replica bus fan-out — the push hub bus path is inproc default;
    a redis multi-replica test would need a live redis and is out of
    scope for this integration layer.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
from tests._runtime_sections import runtime_section_path

REDUCER = runtime_section_path('core/conv_state_reducer.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


@pytest.fixture
def captured_frames(monkeypatch):
    frames = []

    def _fake(channel, task_id, payload):
        frames.append({'channel': channel, 'taskId': task_id, 'payload': payload})

    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake)
    return frames


@pytest.fixture
def clean_registry():
    """Wipe the task registry so a prior test cannot contaminate this one."""
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    with _tl:
        _tasks.clear()
    yield
    with _tl:
        _tasks.clear()


def _seed_registry_task(tid, conv_id, **extra):
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    t = {
        'id': tid, 'convId': conv_id, 'status': 'running',
        'aborted': False, 'created_at': time.time(),
        '_t_last_event': time.time(), '_dispatch_heartbeat': time.time(),
    }
    t.update(extra)
    with _tl:
        _tasks[tid] = t
    return t


def _drive_reducer_with_frame(conv_id, frame_payload, *, extra_convs=None):
    """Drive static/js/core/conv_state_reducer.js in a Node subprocess.

    Seeds a conversations array (one conv by ``conv_id`` plus any in
    ``extra_convs``), applies ``frame_payload`` through
    ``applyRunningTaskIdsFrame``, then reports the resulting
    ``_authoritativeActiveTaskIds`` and computeConvBusy verdict.
    """
    convs = [{'id': conv_id}]
    if extra_convs:
        convs.extend({'id': c} for c in extra_convs)
    script = r"""
const fs = require('fs');
global.window = global;
global.debugLog = () => {};
global.saveConversations = () => {};
global.activeStreams = new Map();
global._currentUserId = null;
const src = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(src);
const conversations = JSON.parse(process.argv[3]);
const frame = JSON.parse(process.argv[4]);
applyRunningTaskIdsFrame(conversations, frame);
const conv = conversations.find(c => c.id === frame.convId);
const set = conv && conv._authoritativeActiveTaskIds
  ? Array.from(conv._authoritativeActiveTaskIds) : [];
const rev = conv && conv._authoritativeActiveTaskIdsRev
  ? conv._authoritativeActiveTaskIdsRev : null;
const busy = computeConvBusy(conv, activeStreams);
console.log(JSON.stringify({
  authoritativeSet: set,
  authoritativeRev: rev,
  computeConvBusy: busy,
}));
"""
    script_path = os.path.join(HERE, '_e2e_reducer_driver.js')
    with open(script_path, 'w') as f:
        f.write(script)
    try:
        proc = subprocess.run(
            ['node', script_path,
             REDUCER,
             json.dumps(convs),
             json.dumps({
                 'convId': conv_id,
                 'runningTaskIds': frame_payload.get('runningTaskIds', []),
                 'runningTaskIdsRev': frame_payload.get('runningTaskIdsRev'),
                 'userId': frame_payload.get('userId', 1),
             })],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
    assert proc.returncode == 0, f'node reducer driver failed: {proc.stderr}'
    return json.loads(proc.stdout.strip())


# ═════════════════════════════════════════════════════════════════════
#  Scenario A: send-side ignition
#  Phone starts generating on conv-A. Server writes new task into
#  registry, chat_send calls notify_conv_changed. PC-side reducer must
#  see the new tid in _authoritativeActiveTaskIds and computeConvBusy
#  must return true.
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_e2e_send_ignites_dot_on_sibling_device(captured_frames, clean_registry):
    """SCENARIO: phone (device A) posts /api/chat/send for conv-X. On the
    server, chat_send seeds the task into the registry and calls
    notify_conv_changed(conv-X, rev=<new_rev>). PC (device B) subscribed
    to notify:* receives the frame; its reducer applies it; conv-X's
    _authoritativeActiveTaskIds now contains the tid and computeConvBusy
    returns true — SIDEBAR DOT LIGHTS without F5."""
    from lib.conversations.meta_cache import notify_conv_changed

    # Server: task enters the registry (what chat_send does).
    _seed_registry_task('tid-phone', 'conv-X')

    # Server: chat_send fires notify. rev=42 mimics a real body-rev bump.
    notify_conv_changed('conv-X', rev=42)

    # A frame was pushed.
    notify_frames = [f for f in captured_frames
                     if f['channel'] == 'notify' and f['taskId'] == 'conv-X']
    assert notify_frames, 'expected a notify frame for conv-X'
    payload = notify_frames[-1]['payload']

    # Payload carries the SSOT projection with our tid.
    assert payload['type'] == 'conv_changed'
    assert payload.get('rev') == 42
    assert 'tid-phone' in payload['runningTaskIds'], (
        'chain-of-truth broken between registry and payload; got %r' %
        payload)

    # Drive the REAL reducer with the REAL payload.
    result = _drive_reducer_with_frame('conv-X', payload,
                                       extra_convs=['conv-Y', 'conv-Z'])
    assert 'tid-phone' in result['authoritativeSet'], (
        'reducer did not apply the frame — Set=%r' % result['authoritativeSet'])
    assert result['computeConvBusy'] is True, (
        'busy predicate should be true when authoritative Set non-empty; '
        'got %r' % result)


# ═════════════════════════════════════════════════════════════════════
#  Scenario B: completion-side extinction — happy path
#  Phone finishes. On the server, task status flips to 'done'; the
#  persist_task_result → _sync_result_to_conversation seam calls
#  notify_conv_changed. Because the task is no longer running,
#  snapshot_running_by_conv filters it out. Reducer applied to the new
#  frame extinguishes _authoritativeActiveTaskIds → computeConvBusy=false.
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_e2e_completion_extinguishes_dot_via_happy_path(captured_frames,
                                                        clean_registry):
    """SCENARIO: phone's task finishes normally. Registry keeps the task
    dict momentarily (with status='done'). Server-side sync path fires
    notify_conv_changed. Payload's runningTaskIds must now EXCLUDE the
    finished tid (snapshot filter status!=running). Reducer applied →
    _authoritativeActiveTaskIds cleared → computeConvBusy false. SIDEBAR
    DOT EXTINGUISHES."""
    from lib.conversations.meta_cache import notify_conv_changed

    # First: frame that lit the dot (device B receives + applies).
    _seed_registry_task('tid-phone', 'conv-X')
    notify_conv_changed('conv-X', rev=42)
    lit_payload = captured_frames[-1]['payload']
    lit_result = _drive_reducer_with_frame('conv-X', lit_payload)
    assert 'tid-phone' in lit_result['authoritativeSet']

    # Server side: task completes.
    from lib.tasks_pkg.manager._state import tasks as _tasks
    _tasks['tid-phone']['status'] = 'done'

    # Second: completion frame — sync path fires notify with new rev.
    notify_conv_changed('conv-X', rev=43)
    completion_frames = [f for f in captured_frames
                         if f['channel'] == 'notify' and f['taskId'] == 'conv-X']
    assert len(completion_frames) >= 2, 'expected TWO notify frames (lit + done)'
    done_payload = completion_frames[-1]['payload']
    assert 'tid-phone' not in done_payload['runningTaskIds'], (
        'completed task must be filtered out of the running-projection; got %r' %
        done_payload['runningTaskIds'])
    assert done_payload['runningTaskIds'] == [], (
        'no other tasks running → projection must be empty; got %r' %
        done_payload['runningTaskIds'])

    # Drive reducer with the SEQUENTIAL frames (lit then done) so the
    # rev-gate is exercised. The Node script above only applies ONE
    # frame, so simulate the sequence by chaining: apply the lit
    # frame first, then the done frame, and inspect the terminal state.
    combined = _drive_reducer_sequential(
        'conv-X',
        [lit_payload, done_payload],
    )
    assert combined['authoritativeSet'] == [], (
        'authoritative Set must be empty after done frame; got %r' %
        combined['authoritativeSet'])
    assert combined['computeConvBusy'] is False, (
        'busy predicate must be false after done frame; got %r' % combined)


def _drive_reducer_sequential(conv_id, frames):
    """Apply multiple frames in order to the reducer, then report state."""
    script = r"""
const fs = require('fs');
global.window = global;
global.debugLog = () => {};
global.saveConversations = () => {};
global.activeStreams = new Map();
global._currentUserId = null;
const src = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(src);
const convs = [{ id: process.argv[3] }];
const frames = JSON.parse(process.argv[4]);
for (const f of frames) applyRunningTaskIdsFrame(convs, f);
const conv = convs[0];
const set = conv._authoritativeActiveTaskIds ? Array.from(conv._authoritativeActiveTaskIds) : [];
const busy = computeConvBusy(conv, activeStreams);
console.log(JSON.stringify({ authoritativeSet: set, computeConvBusy: busy }));
"""
    payloads = [{
        'convId': conv_id,
        'runningTaskIds': f.get('runningTaskIds', []),
        'runningTaskIdsRev': f.get('runningTaskIdsRev'),
        'userId': f.get('userId', 1),
    } for f in frames]
    script_path = os.path.join(HERE, '_e2e_reducer_seq_driver.js')
    with open(script_path, 'w') as f:
        f.write(script)
    try:
        proc = subprocess.run(
            ['node', script_path,
             REDUCER,
             conv_id, json.dumps(payloads)],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
    assert proc.returncode == 0, f'node driver failed: {proc.stderr}'
    return json.loads(proc.stdout.strip())


# ═════════════════════════════════════════════════════════════════════
#  Scenario C: completion-side extinction — SUPERSEDE ABORT PATH
#  This is the path P3 was explicitly added to close: when a new task
#  supersedes an old one, abort_running_tasks_for_conv is what fires
#  the notify frame (happy-path finalize doesn't fire because the task
#  never reached persist_task_result — it was aborted mid-flight).
#  Without P3, the frame never leaves the server and the sidebar dot
#  stays lit until the 25/90s poll fallback.
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_e2e_supersede_abort_extinguishes_dot(captured_frames, clean_registry,
                                               monkeypatch):
    """SCENARIO: user hits regenerate on conv-X. Server calls
    abort_running_tasks_for_conv(conv-X, exclude=tid-new). The old task
    (tid-stale) is aborted mid-flight. P3's broadcast fires the notify
    frame with the CURRENT projection: tid-new (still live) present,
    tid-stale (aborted) filtered. Reducer applied → Set has only tid-new;
    computeConvBusy still true (a new task is live). Then when tid-new
    itself completes, the SECOND notify fires and the dot extinguishes."""
    import lib.tasks_pkg.manager._registry as reg_mod
    monkeypatch.setattr(reg_mod, '_write_aborted_terminal_floor', lambda t: None)

    _seed_registry_task('tid-stale', 'conv-X')
    _seed_registry_task('tid-new', 'conv-X')

    # First: pre-supersede frame lit the dot with the stale task.
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-X', rev=42)
    lit_payload = captured_frames[-1]['payload']
    assert 'tid-stale' in lit_payload['runningTaskIds']
    assert 'tid-new' in lit_payload['runningTaskIds']

    # Fire supersede abort — P3 emits a notify frame.
    reg_mod.abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-new')

    # Grab the P3 broadcast frame.
    supersede_frames = [f for f in captured_frames
                        if f['channel'] == 'notify' and f['taskId'] == 'conv-X']
    assert len(supersede_frames) >= 2, (
        'P3 must have added a supersede-abort broadcast; got %d frames' %
        len(supersede_frames))
    supersede_payload = supersede_frames[-1]['payload']
    assert 'tid-stale' not in supersede_payload['runningTaskIds'], (
        'aborted task must be filtered out of P3 broadcast; got %r' %
        supersede_payload['runningTaskIds'])
    assert 'tid-new' in supersede_payload['runningTaskIds'], (
        'the surviving task must still appear; got %r' %
        supersede_payload['runningTaskIds'])

    # Drive reducer with both frames.
    combined = _drive_reducer_sequential(
        'conv-X', [lit_payload, supersede_payload],
    )
    assert combined['authoritativeSet'] == ['tid-new'], (
        'reducer must transition from {stale,new} to {new}; got %r' %
        combined['authoritativeSet'])
    assert combined['computeConvBusy'] is True

    # Now finish tid-new (registry status flips).
    from lib.tasks_pkg.manager._state import tasks as _tasks
    _tasks['tid-new']['status'] = 'done'
    notify_conv_changed('conv-X', rev=43)
    final_payload = [f for f in captured_frames
                     if f['channel'] == 'notify' and f['taskId'] == 'conv-X'][-1]['payload']
    assert final_payload['runningTaskIds'] == []

    final = _drive_reducer_sequential(
        'conv-X', [lit_payload, supersede_payload, final_payload],
    )
    assert final['authoritativeSet'] == [], (
        'final state must have empty Set; got %r' % final['authoritativeSet'])
    assert final['computeConvBusy'] is False, (
        'dot must extinguish; got busy=%r' % final)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
