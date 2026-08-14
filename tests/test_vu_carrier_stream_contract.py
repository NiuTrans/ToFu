"""tests/test_vu_carrier_stream_contract.py — the autopilot VU sub-task's OWN
SSE stream carries the full VU contract, not a raw agent turn.

WHY (production incident 2026-07-26, conv ms1rrjchpa5pqw)
---------------------------------------------------------
After the pt_8dc03017 cutover the client hops from the parent's closed stream
to the VU sub-task's stream (``latestLiveTaskId``).  The sub-task's event
list, however, carried the RAW inner events (plain ``delta`` / ``tool_start``
…), and the fresh-connect path synthesized an agent ``state`` snapshot from
``content``/``thinking``.  A client attached there rendered a second "Agent"
bubble (never "Autopilot"), the machine sentinels (``[VU: TASK_DONE]`` /
``[PROGRESS: …]``) stayed visible, and the stream never ended (the
endpoint-managed finalize neither flips ``status`` nor emits ``done``), so
the sidebar showed 回答中 forever.

The contract pinned here (owner-ratified):
  1. The carrier's own stream carries the SAME VU envelope the parent
     stream always did: ``autopilot_vu_start`` + wrapped
     ``autopilot_vu_event`` frames + ``autopilot_vu_done`` /
     ``autopilot_vu_cancel``.  Parent forwarding is preserved (the pre-hop
     window is still the client's only channel then).
  2. The fresh-connect snapshot for a carrier is an
     ``autopilot_vu_start`` frame carrying a ``replaySnapshot`` (current
     content/thinking/toolRounds) — never an agent ``state`` frame.
  3. A terminal carrier's tick synthesizes a MINIMAL ``done`` (no agent
     meta) that closes the stream and ships ``latestLiveTaskId`` (+
     ``latestLiveTaskIsVu`` when the successor is itself a VU carrier).
  4. Lifecycle frames are dual-emitted (parent + carrier); the carrier is
     flipped terminal after the lifecycle frame so the tick above fires.
  5. Non-contract frames (``done``, ``round_committed``, …) never reach
     the carrier's own stream / event log.

Guarded against NEUTER: dropping the transform seam, the carrier snapshot
branch, the terminal-tick branch, or the dual-emit flips specific tests red.
"""
from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ──────────────────────────────────────────────────────────────────────
#  Shared fakes / fixtures
# ──────────────────────────────────────────────────────────────────────

def _fake_task(tid: str, **over):
    """Minimal task dict satisfying append_event / tick / snapshot readers."""
    t = {
        'id': tid,
        'convId': over.pop('convId', f'conv-{tid}'),
        'status': 'running',
        'events': [],
        'events_lock': threading.Lock(),
        'content': '',
        'thinking': '',
        'toolRounds': [],
        'config': {},
        'messages': [],
        'error': None,
    }
    t.update(over)
    return t


@pytest.fixture()
def capture_wire(monkeypatch):
    """Intercept the two side-channels append_event writes (persist + push)
    so tests assert the WIRE shape without touching a DB or a push hub."""
    cap = {'persisted': [], 'pushed': []}
    monkeypatch.setattr(
        'lib.tasks_pkg.event_log.append_persistent_event',
        lambda task_id, seq, event: cap['persisted'].append((task_id, seq, event)),
        raising=False)
    monkeypatch.setattr(
        'lib.push.push_event',
        lambda channel, task_id, event: cap['pushed'].append((channel, task_id, event)),
        raising=False)
    return cap


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from lib.tasks_pkg import tasks, tasks_lock
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put
    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


# ──────────────────────────────────────────────────────────────────────
#  1. The transform — the carrier's own stream carries the VU envelope
# ──────────────────────────────────────────────────────────────────────

def test_transform_wraps_forward_types_on_own_stream_and_parent(capture_wire):
    """A forwardable inner event (delta) must land on BOTH streams as a
    wrapped autopilot_vu_event — own stream (post-hop client) AND parent
    stream (pre-hop window), each carrying vuMsgId + inner."""
    from lib.tasks_pkg.autopilot_event_forwarding import make_vu_event_transform
    from lib.tasks_pkg.manager import append_event

    parent = _fake_task('parent-t1')
    sub = _fake_task('vu-t1')
    sub['_vu_event_transform'] = make_vu_event_transform(parent, 'vu-msg-1')

    append_event(sub, {'type': 'delta', 'content': 'hello'})

    assert len(sub['events']) == 1, (
        f'carrier own stream must carry the wrapped frame, got {sub["events"]}')
    own = sub['events'][0]
    assert own.get('type') == 'autopilot_vu_event', (
        f'carrier own frame must be autopilot_vu_event, got {own.get("type")!r}')
    assert own.get('vuMsgId') == 'vu-msg-1'
    assert own.get('inner', {}).get('type') == 'delta'
    assert own.get('inner', {}).get('content') == 'hello'

    assert len(parent['events']) == 1, (
        'parent stream must receive the SAME wrapped forward (pre-hop window)')
    fwd = parent['events'][0]
    assert fwd.get('type') == 'autopilot_vu_event'
    assert fwd.get('vuMsgId') == 'vu-msg-1'
    assert fwd.get('inner', {}).get('type') == 'delta'

    # The persisted event log must hold the WRAPPED frame too — cold replay
    # reads it back, so a raw frame there would re-render as an agent turn.
    persisted_types = [e.get('type') for _, _, e in capture_wire['persisted']]
    assert 'delta' not in persisted_types, (
        f'raw inner events must not persist on the carrier log: {persisted_types}')
    assert 'autopilot_vu_event' in persisted_types


def test_transform_drops_non_contract_frames(capture_wire):
    """Frames that are NOT part of the VU contract (done / round_committed /
    state / arbitrary) must never reach the carrier's own stream, its event
    log, or the parent (parity with the old forwarder, which only ever
    forwarded _VU_FORWARD_TYPES)."""
    from lib.tasks_pkg.autopilot_event_forwarding import make_vu_event_transform
    from lib.tasks_pkg.manager import append_event

    parent = _fake_task('parent-t2')
    sub = _fake_task('vu-t2')
    sub['_vu_event_transform'] = make_vu_event_transform(parent, 'vu-msg-2')

    for raw in ({'type': 'done'},
                {'type': 'round_committed', 'snapshotId': 'x'},
                {'type': 'state', 'content': 'y'},
                {'type': 'mystery_new_frame'}):
        append_event(sub, raw)

    assert sub['events'] == [], (
        f'non-contract frames must be dropped from the carrier stream: {sub["events"]}')
    assert parent['events'] == [], (
        f'non-contract frames must not be forwarded to the parent: {parent["events"]}')
    assert capture_wire['persisted'] == []


def test_transform_passes_lifecycle_frames_verbatim(capture_wire):
    """Lifecycle frames (vu_start / vu_done / vu_cancel) are the carrier
    contract's own spine: they land on the carrier's own stream VERBATIM
    (not double-wrapped), and are NOT forwarded by the transform (the
    dual-emit helper owns the parent-side copy — a transform-side forward
    would double them)."""
    from lib.tasks_pkg.autopilot_event_forwarding import make_vu_event_transform
    from lib.tasks_pkg.manager import append_event

    parent = _fake_task('parent-t3')
    sub = _fake_task('vu-t3')
    sub['_vu_event_transform'] = make_vu_event_transform(parent, 'vu-msg-3')

    for evt in ({'type': 'autopilot_vu_start', 'vuMsgId': 'vu-msg-3'},
                {'type': 'autopilot_vu_done', 'vuMsgId': 'vu-msg-3'},
                {'type': 'autopilot_vu_cancel', 'vuMsgId': 'vu-msg-3'}):
        append_event(sub, evt)

    types = [e.get('type') for e in sub['events']]
    assert types == ['autopilot_vu_start', 'autopilot_vu_done', 'autopilot_vu_cancel'], (
        f'lifecycle frames must land verbatim on the carrier stream, got {types}')
    assert parent['events'] == [], (
        'transform must not forward lifecycle frames to the parent '
        '(the dual-emit helper owns that copy)')


def test_transform_bookkeeping_reads_raw_frame(capture_wire):
    """Facade bookkeeping (phase tracking for the poll fallback) must keep
    reading the RAW inner event even though the wire frame is wrapped."""
    from lib.tasks_pkg.autopilot_event_forwarding import make_vu_event_transform
    from lib.tasks_pkg.manager import append_event

    parent = _fake_task('parent-t4')
    sub = _fake_task('vu-t4')
    sub['_vu_event_transform'] = make_vu_event_transform(parent, 'vu-msg-4')

    append_event(sub, {'type': 'phase', 'phase': 'working', 'detail': 'checking…'})
    assert (sub.get('phase') or {}).get('phase') == 'working', (
        'phase tracking must read the RAW inner frame, not the wrapper')
    assert sub['events'] and sub['events'][0].get('type') == 'autopilot_vu_event'


# ──────────────────────────────────────────────────────────────────────
#  2. Carrier installation + lifecycle dual-emit + terminal flip
# ──────────────────────────────────────────────────────────────────────

def test_install_carrier_contract_seeds_stream_and_pins_ids(capture_wire):
    """run_virtual_user's install seam must: install the transform, pin
    _vu_msg_id (the snapshot builder reads it), expose the carrier on the
    parent (the dual-emit helper finds it), and seed autopilot_vu_start on
    the carrier's own stream (its event-log spine)."""
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')

    parent = _fake_task('parent-t5')
    sub = _fake_task('vu-t5')
    ap._install_vu_carrier_contract(parent, sub, 'vu-msg-5')

    assert sub.get('_vu_msg_id') == 'vu-msg-5'
    assert callable(sub.get('_vu_event_transform')), (
        'carrier must carry its per-task event transform')
    assert parent.get('_vu_carrier') is sub, (
        'the parent must expose the carrier for the lifecycle dual-emit')
    seeds = [e for e in sub['events'] if e.get('type') == 'autopilot_vu_start']
    assert len(seeds) == 1 and seeds[0].get('vuMsgId') == 'vu-msg-5', (
        f'the carrier stream must open with autopilot_vu_start, got {sub["events"]}')


def test_dual_emit_lifecycle_frame_reaches_both_streams(capture_wire):
    """vu_done must land on the parent stream AND the carrier's own stream
    (verbatim on the carrier via the transform's lifecycle passthrough)."""
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')

    parent = _fake_task('parent-t6')
    carrier = _fake_task('vu-t6')
    ap._install_vu_carrier_contract(parent, carrier, 'vu-msg-6')
    n_seed = len(carrier['events'])

    ap._emit_vu_lifecycle_frame(parent, {'type': 'autopilot_vu_done',
                                         'vuMsgId': 'vu-msg-6'})

    assert any(e.get('type') == 'autopilot_vu_done' for e in parent['events']), (
        'parent stream must carry the lifecycle frame (pre-hop path)')
    landed = carrier['events'][n_seed:]
    assert len(landed) == 1 and landed[0].get('type') == 'autopilot_vu_done', (
        f'carrier stream must carry the lifecycle frame verbatim, got {landed}')


def test_close_vu_carrier_stream_flips_terminal_and_pops_ref():
    """The terminal flip is carrier-scoped (owner's ruling: never inside
    _run_single_turn — endpoint shares it) and idempotent."""
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')

    parent = _fake_task('parent-t7')
    carrier = _fake_task('vu-t7')
    parent['_vu_carrier'] = carrier
    ap._close_vu_carrier_stream(parent)
    assert carrier.get('status') == 'done', (
        f'carrier must flip terminal so its SSE stream closes, got {carrier.get("status")!r}')
    assert parent.get('_vu_carrier') is None, 'carrier ref must be popped'
    # Idempotent + missing-carrier safe
    ap._close_vu_carrier_stream(parent)
    ap._close_vu_carrier_stream(_fake_task('parent-none'))


# ──────────────────────────────────────────────────────────────────────
#  3. Connect snapshot — carrier gets the VU contract, never an agent state
# ──────────────────────────────────────────────────────────────────────

def test_connect_snapshot_carrier_is_vu_start_replay_snapshot():
    """Fresh connect to a carrier must receive an autopilot_vu_start frame
    with a replaySnapshot (current content/thinking/toolRounds) and a cursor
    at the live tail — NOT an agent `state` frame (the Agent-mislabel fix)."""
    from lib.chat_dispatch import build_connect_snapshot

    carrier = _fake_task('vu-t8', _vu_subtask=True, _vu_msg_id='vu-msg-8',
                         content='partial reply', thinking='some reasoning',
                         toolRounds=[{'roundNum': 1, 'status': 'done'}])
    carrier['events'] = [{'type': 'autopilot_vu_start', 'vuMsgId': 'vu-msg-8'},
                         {'type': 'autopilot_vu_event', 'vuMsgId': 'vu-msg-8',
                          'inner': {'type': 'delta', 'content': 'partial reply'}}]

    state, meta, cursor = build_connect_snapshot(carrier)

    assert state.get('type') == 'autopilot_vu_start', (
        f'carrier snapshot must be autopilot_vu_start, got {state.get("type")!r}')
    assert state.get('vuMsgId') == 'vu-msg-8'
    snap = state.get('replaySnapshot') or {}
    assert snap.get('content') == 'partial reply'
    assert snap.get('thinking') == 'some reasoning'
    assert snap.get('toolRounds') == [{'roundNum': 1, 'status': 'done'}]
    assert cursor == 2, (
        'cursor must sit at the absolute live tail — replayed history would '
        'double-append')


def test_connect_snapshot_carrier_cursor_survives_rolling_buffer():
    from lib.chat_dispatch import build_connect_snapshot

    carrier = _fake_task('vu-t8-roll', _vu_subtask=True,
                         _vu_msg_id='vu-msg-8-roll')
    carrier['_eventBaseSeq'] = 50
    carrier['_eventNextSeq'] = 52
    carrier['events'] = [
        {'type': 'autopilot_vu_event', 'seq': 50},
        {'type': 'autopilot_vu_event', 'seq': 51},
    ]

    state, _meta, cursor = build_connect_snapshot(carrier)
    assert state['type'] == 'autopilot_vu_start'
    assert cursor == 52


def test_connect_snapshot_normal_task_unchanged():
    """Regression parity: a normal task still gets the agent `state` frame."""
    from lib.chat_dispatch import build_connect_snapshot

    task = _fake_task('worker-t9', content='real answer')
    state, meta, cursor = build_connect_snapshot(task)
    assert state.get('type') == 'state', (
        f'normal task snapshot must stay the agent state frame, got {state.get("type")!r}')
    assert state.get('content') == 'real answer'
    assert cursor == 0


# ──────────────────────────────────────────────────────────────────────
#  4. Warm resume — carrier replays missed VU frames, no agent state echo
# ──────────────────────────────────────────────────────────────────────

def test_warm_resume_carrier_replays_frames_without_agent_state():
    """Warm resume (Last-Event-ID, network drop — JS state survives, the VU
    bubble is intact) must replay ONLY the missed VU frames.  Echoing an
    agent `state` (content/thinking) would render a phantom Agent bubble;
    echoing a replaySnapshot would reset-then-reappend (duplication)."""
    from lib.chat_dispatch import plan_warm_resume

    e0 = {'type': 'autopilot_vu_start', 'vuMsgId': 'vu-msg-10'}
    e1 = {'type': 'autopilot_vu_event', 'vuMsgId': 'vu-msg-10',
          'inner': {'type': 'delta', 'content': 'a'}}
    e2 = {'type': 'autopilot_vu_event', 'vuMsgId': 'vu-msg-10',
          'inner': {'type': 'delta', 'content': 'b'}}
    carrier = _fake_task('vu-t10', _vu_subtask=True, _vu_msg_id='vu-msg-10',
                         content='ab')
    carrier['events'] = [e0, e1, e2]

    plan = plan_warm_resume(carrier, '0', 'vu-t10')
    assert plan is not None
    assert plan.resume_state is None, (
        f'carrier warm resume must NOT echo an agent/vu state frame, got {plan.resume_state!r}')
    assert plan.replay_events == [e1, e2], (
        'carrier warm resume must replay exactly the missed VU frames')


def test_warm_resume_carrier_uses_absolute_rolling_sequences():
    from lib.chat_dispatch import plan_warm_resume

    carrier = _fake_task('vu-t10-roll', _vu_subtask=True,
                         _vu_msg_id='vu-msg-10-roll', content='ab')
    carrier['_eventBaseSeq'] = 100
    carrier['_eventNextSeq'] = 103
    carrier['events'] = [
        {'type': 'autopilot_vu_event', 'seq': 100},
        {'type': 'autopilot_vu_event', 'seq': 101},
        {'type': 'autopilot_vu_event', 'seq': 102},
    ]

    plan = plan_warm_resume(carrier, '100', 'vu-roll')
    assert plan is not None
    assert plan.resume_state is None
    assert plan.replay_frames == [
        (101, carrier['events'][1]),
        (102, carrier['events'][2]),
    ]
    assert plan_warm_resume(carrier, '98', 'vu-roll') is None


def test_warm_resume_normal_task_unchanged():
    """Regression parity: a normal task's warm resume still leads with the
    agent state frame."""
    from lib.chat_dispatch import plan_warm_resume

    task = _fake_task('worker-t11', content='partial')
    task['events'] = [{'type': 'delta', 'content': 'p'},
                      {'type': 'delta', 'content': 'artial'}]
    plan = plan_warm_resume(task, '0', 'worker-t11')
    assert plan is not None
    assert plan.resume_state is not None
    assert plan.resume_state.get('type') == 'state'


# ──────────────────────────────────────────────────────────────────────
#  5. Terminal tick — carrier closes with a minimal done + successor stamp
# ──────────────────────────────────────────────────────────────────────

def _tick(task):
    from lib.chat_dispatch import next_live_tick
    from lib.task_replay import task_event_base_cursor
    now = time.time()
    cursor = task_event_base_cursor(task, task['events']) + len(task['events'])
    return next_live_tick(
        task=task, cursor=cursor, sse_gen=1,
        stream_start=now - 1, sse_max_duration=7200,
        last_t=now, now=now, task_id_short=task['id'][:8])


def test_tick_carrier_terminal_emits_minimal_done_with_successor(put_task):
    """A terminal carrier's tick closes the stream with a MINIMAL done (no
    agent meta — the VU bubble was settled by autopilot_vu_done, and the
    frontend binds a detached dummy on this stream) carrying the supersede
    successor so the client hops VU → follow-up worker without polling."""
    from lib.tasks_pkg.manager._state import _record_latest_task

    carrier = _fake_task('vu-t12', _vu_subtask=True, status='done',
                         finishReason='stop', usage={'input_tokens': 1},
                         model='kimi-k3')
    follower = _fake_task('fw-t12', convId=carrier['convId'])
    put_task(follower)
    _record_latest_task(carrier['convId'], follower['id'])

    tick = _tick(carrier)
    assert tick.kind == 'late_done', (
        f'terminal carrier must close via a done frame, got kind={tick.kind!r}')
    evt = tick.late_done_evt
    assert evt.get('type') == 'done'
    assert evt.get('latestLiveTaskId') == follower['id'], (
        'the carrier done must ship the follow-up successor for the next hop')
    assert not evt.get('latestLiveTaskIsVu'), (
        'a WORKER successor must not be flagged as a VU carrier')
    assert 'usage' not in evt and 'finishReason' not in evt, (
        f'carrier done must stay minimal (no agent meta), got keys={sorted(evt)}')


def test_tick_carrier_terminal_vu_successor_marks_is_vu(put_task):
    """The IsVu flag lets the frontend delegate the NEXT hop to the VU
    connector too (kick → VU → … chains)."""
    from lib.tasks_pkg.manager._state import _record_latest_task

    carrier = _fake_task('vu-t13', _vu_subtask=True, status='done')
    next_vu = _fake_task('vu-t13b', convId=carrier['convId'], _vu_subtask=True)
    put_task(next_vu)
    _record_latest_task(carrier['convId'], next_vu['id'])

    tick = _tick(carrier)
    evt = tick.late_done_evt
    assert evt.get('latestLiveTaskId') == next_vu['id']
    assert evt.get('latestLiveTaskIsVu') is True, (
        'a VU successor must be flagged so the frontend skips the Agent bubble')


def test_tick_carrier_terminal_no_successor_plain_done():
    """TASK_DONE end: no successor → a plain minimal done; the frontend just
    finishes (sidebar clears without refresh)."""
    carrier = _fake_task('vu-t14', _vu_subtask=True, status='done',
                         finishReason='stop', usage={'input_tokens': 1})
    tick = _tick(carrier)
    assert tick.kind == 'late_done'
    evt = tick.late_done_evt
    assert evt.get('type') == 'done'
    assert 'latestLiveTaskId' not in evt
    assert 'finishReason' not in evt and 'usage' not in evt, (
        f'carrier done must stay minimal even when the carrier carries agent '
        f'meta (the VU bubble was settled by autopilot_vu_done), got {sorted(evt)}')


def test_tick_normal_terminal_marks_vu_successor(put_task):
    """The parent's (LATE) done must flag a VU successor — this is the stamp
    that drives the parent → VU hop through the VU connector."""
    from lib.tasks_pkg.manager._state import _record_latest_task

    parent = _fake_task('parent-t15', status='done', finishReason='stop')
    carrier = _fake_task('vu-t15', convId=parent['convId'], _vu_subtask=True)
    put_task(carrier)
    _record_latest_task(parent['convId'], carrier['id'])

    tick = _tick(parent)
    assert tick.kind == 'late_done'
    evt = tick.late_done_evt
    assert evt.get('latestLiveTaskId') == carrier['id']
    assert evt.get('latestLiveTaskIsVu') is True, (
        'the parent done must flag the VU successor so the frontend attaches '
        'with the VU connector (detached dummy, no Agent placeholder)')


def test_tick_normal_terminal_no_successor_regression():
    """Regression parity: a normal terminal task without a successor emits
    the meta-filled late_done with no successor/IsVu keys."""
    task = _fake_task('worker-t16', status='done', finishReason='stop')
    tick = _tick(task)
    assert tick.kind == 'late_done'
    evt = tick.late_done_evt
    assert evt.get('type') == 'done'
    assert 'latestLiveTaskId' not in evt
    assert 'latestLiveTaskIsVu' not in evt
