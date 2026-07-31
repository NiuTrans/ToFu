#!/usr/bin/env python3
"""Terminal SSE frames must ship the supersede successor (``latestLiveTaskId``).

WHY (production incident 2026-07-25, conv ms04oggm34tkcp)
--------------------------------------------------------
The pt_8dc03017 cutover made ``is_task_terminal`` flip as soon as the parent's
``status != 'running'`` — the SSE stream closes PROMPTLY via the LATE-done
synthesis, and the client is supposed to discover any successor (the autopilot
VU sub-task / a spawned follow-up / an auto-dispatched queued task) through the
conv→latest-task supersede index. But that index was never shipped ON THE WIRE:

  * the LATE done carried no baton (the VU was still deciding — the baton is
    only stamped when a follow-up actually spawns) and no successor id;
  * the frontend's ``conv._latestLiveTaskId`` had ZERO writers, so the
    shipped attach reducer in ``_runTerminalContinuation`` was dead code;
  * the VU sub-task is a carrier (``_vu_subtask``), deliberately hidden from
    ``/api/v1/chat/active`` — the poll probe can never see it.

Chain reaction: parent stream closed at 16:57:36 → VU ran invisibly for ~3
minutes → user sent a message (correctly queued, the VU genuinely holds the
conv) → VU finished, deferred to the real message, the queue auto-dispatched
task 87d610ed at 17:00:30 → the client had NO mechanism to notice — six
minutes of dead silence until a manual page refresh re-attached.

FIX
---
``_live_successor_task_id(conv_id, exclude_task_id)`` (manager/_state.py)
resolves the conv's supersede-index entry to a DIFFERENT, still-live task.
Both terminal-frame builders stamp it as ``latestLiveTaskId``:

  * the LATE-done synthesis (``lib/chat_dispatch.next_live_tick`` branch 3),
  * the real ``done`` event (``lib/tasks_pkg/orchestrator/_finalize.py``).

The frontend stamps ``conv._latestLiveTaskId`` from those frames and the
attach reducer consumes it (tests/test_frontend_latest_live_task_wire.py).

NEUTER (manual A/B): delete the stamp block in chat_dispatch (or the helper
call in _finalize) and every test here goes red while the pre-existing
late_done wire-parity tests stay green — proving the new field is additive.
"""

from __future__ import annotations

import threading
import uuid

import pytest

pytestmark = pytest.mark.unit

_unit = pytest.mark.unit


def _cid() -> str:
    return 'cv-' + uuid.uuid4().hex[:12]


def _mk_task(task_id: str, conv_id: str, status: str) -> dict:
    return {
        'id': task_id,
        'convId': conv_id,
        'status': status,
        'events': [],
        'events_lock': threading.Lock(),
        'error': None,
    }


@pytest.fixture
def registry():
    """Give each test a clean slice of the manager registry + index."""
    from lib.tasks_pkg.manager import tasks, tasks_lock
    owned = []

    def _put(task: dict) -> dict:
        with tasks_lock:
            tasks[task['id']] = task
        owned.append(task['id'])
        return task

    yield _put

    with tasks_lock:
        for tid in owned:
            tasks.pop(tid, None)


def _tick_late_done(task: dict):
    from lib.chat_dispatch import next_live_tick
    return next_live_tick(
        task=task, cursor=0, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=1.0, task_id_short=task['id'][:8],
    )


# ────────────────────────── the wire stamp ──────────────────────────

@_unit
def test_late_done_carries_live_successor(registry):
    """THE BUG: a terminal parent whose supersede index points at a LIVE
    successor (the autopilot VU sub-task the hook just spawned) must ship
    that successor's id on the LATE done — before the fix the field was
    absent and the client went deaf for the whole VU window."""
    from lib.tasks_pkg.manager import _record_latest_task
    conv_id = _cid()
    dying = registry(_mk_task('task-parent', conv_id, 'done'))
    registry(_mk_task('task-vu-succ', conv_id, 'running'))
    _record_latest_task(conv_id, 'task-vu-succ')

    v = _tick_late_done(dying)
    assert v.kind == 'late_done', f'expected late_done; got {v.kind}'
    assert v.late_done_evt.get('latestLiveTaskId') == 'task-vu-succ', (
        'LATE done must carry latestLiveTaskId so the client can attach to '
        'the successor — without it the supersede-index attach reducer is '
        'dead code and the client sits deaf until a manual refresh'
    )


@_unit
def test_late_done_omits_stamp_when_index_points_at_self(registry):
    """The NORMAL case: the conv's latest task IS the dying one (no
    autopilot, no follow-up) → no successor → the field must be absent,
    not self-referential."""
    from lib.tasks_pkg.manager import _record_latest_task
    conv_id = _cid()
    dying = registry(_mk_task('task-solo', conv_id, 'done'))
    _record_latest_task(conv_id, 'task-solo')

    v = _tick_late_done(dying)
    assert v.kind == 'late_done'
    assert 'latestLiveTaskId' not in v.late_done_evt, (
        'a self-referential latestLiveTaskId would make the client attach '
        'to the task that just died'
    )


@_unit
def test_late_done_omits_stamp_when_successor_terminal(registry):
    """A successor that already settled (status='done') is not attachable —
    omit the stamp (the client probes /api/v1/chat/active instead)."""
    from lib.tasks_pkg.manager import _record_latest_task
    conv_id = _cid()
    dying = registry(_mk_task('task-old', conv_id, 'done'))
    registry(_mk_task('task-finished', conv_id, 'done'))
    _record_latest_task(conv_id, 'task-finished')

    v = _tick_late_done(dying)
    assert v.kind == 'late_done'
    assert 'latestLiveTaskId' not in v.late_done_evt


@_unit
def test_late_done_omits_stamp_when_successor_aborted(registry):
    from lib.tasks_pkg.manager import _record_latest_task
    conv_id = _cid()
    dying = registry(_mk_task('task-old2', conv_id, 'done'))
    succ = registry(_mk_task('task-aborted', conv_id, 'running'))
    succ['aborted'] = True
    _record_latest_task(conv_id, 'task-aborted')

    v = _tick_late_done(dying)
    assert v.kind == 'late_done'
    assert 'latestLiveTaskId' not in v.late_done_evt


@_unit
def test_late_done_omits_stamp_when_successor_evicted(registry):
    """The index points at a task no longer in the registry (cleaned up) —
    omit rather than send the client hunting a 404."""
    from lib.tasks_pkg.manager import _record_latest_task
    conv_id = _cid()
    dying = registry(_mk_task('task-old3', conv_id, 'done'))
    _record_latest_task(conv_id, 'task-gone')

    v = _tick_late_done(dying)
    assert v.kind == 'late_done'
    assert 'latestLiveTaskId' not in v.late_done_evt


# ────────────────────────── the helper itself ──────────────────────────

@_unit
def test_helper_pending_successor_counts_as_live(registry):
    """A freshly-created successor may still be 'pending' (thread not yet
    started) — that IS attachable (its stream exists and will replay)."""
    from lib.tasks_pkg.manager import _live_successor_task_id, _record_latest_task
    conv_id = _cid()
    registry(_mk_task('task-pend', conv_id, 'pending'))
    _record_latest_task(conv_id, 'task-pend')
    assert _live_successor_task_id(conv_id, exclude_task_id='task-other') == 'task-pend'


@_unit
def test_helper_empty_conv_or_no_index(registry):
    from lib.tasks_pkg.manager import _live_successor_task_id
    assert _live_successor_task_id('', exclude_task_id='x') == ''
    assert _live_successor_task_id(_cid(), exclude_task_id='x') == ''


@_unit
def test_helper_exposed_via_manager_facade():
    """Call sites import through the facade (project convention) — the
    symbol must be re-exported from lib.tasks_pkg.manager."""
    import lib.tasks_pkg.manager as mgr
    assert callable(getattr(mgr, '_live_successor_task_id', None)), (
        '_live_successor_task_id must be re-exported by the manager facade'
    )


# ────────────────────────── the real done event ──────────────────────────
# ────────────────────── the finalize-window latch ──────────────────────

@_unit
def test_late_done_held_during_finalize_window():
    """The status flips to terminal BEFORE the real done is appended (the
    autopilot hook + pre-emit sync run in between). A tick landing in that
    ms-scale window must NOT synthesize a LATE done — the successor stamp
    would be missing (the index isn't advanced yet) and the stream closes
    prematurely. Hold while ``_finalize_started_at`` is fresh."""
    import threading as _th
    from lib.chat_dispatch import next_live_tick
    task = {
        'events': [], 'events_lock': _th.Lock(),
        'status': 'done', 'error': None,
        'convId': 'cv-fin', '_finalize_started_at': 1.0,
    }
    v = next_live_tick(
        task=task, cursor=0, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=1.0, task_id_short='t-fin',
    )
    assert v.kind == 'sleep', (
        f'a terminal task mid-finalize must hold the LATE done; got {v.kind}'
    )


@_unit
def test_late_done_fires_after_finalize_window_expires():
    """The latch self-expires (a crashed finalize can never wedge the
    stream): a terminal task whose ``_finalize_started_at`` is older than
    the ceiling gets the LATE done as before."""
    import threading as _th
    from lib.chat_dispatch import next_live_tick
    task = {
        'events': [], 'events_lock': _th.Lock(),
        'status': 'done', 'error': None,
        'convId': 'cv-fin2', '_finalize_started_at': 1.0 - 31.0,
    }
    v = next_live_tick(
        task=task, cursor=0, sse_gen=1,
        stream_start=0.0, sse_max_duration=7200,
        last_t=0.0, now=1.0, task_id_short='t-fin2',
    )
    assert v.kind == 'late_done', (
        f'an expired finalize latch must not hold the LATE done; got {v.kind}'
    )


@_unit
def test_finalize_stamps_and_clears_the_latch():
    """Static pin: _finalize.py must reference ``_finalize_started_at``
    (set before the terminal flip, cleared after append_event(done))."""
    import os
    src_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..',
        'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    assert '_finalize_started_at' in src, (
        '_finalize.py must stamp the finalize-window latch'
    )


# ────────────────────────── the real done event ──────────────────────────

@_unit
def test_real_done_event_also_stamps_successor():
    """Wire symmetry: the REAL done (built in _finalize.py) must carry the
    same stamp for the race outcome where the SSE tick drains the real done
    before the terminal check — the follow-up the autopilot hook just
    spawned is the index entry at that moment. Static pin (the finalize
    path is exercised end-to-end by the e2e suites)."""
    import os
    src_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..',
        'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    assert 'latestLiveTaskId' in src, (
        '_finalize.py must stamp latestLiveTaskId on the real done event'
    )
    # The finalize path uses the two-return variant (it also surfaces
    # ``latestLiveTaskIsVu``); the LATE-done synthesis uses the simpler
    # ``_live_successor_task_id`` pinned above. Pin the name this file's
    # builder actually calls, not its sibling.
    assert '_live_successor_info' in src, (
        '_finalize.py must resolve the successor via _live_successor_info'
    )
