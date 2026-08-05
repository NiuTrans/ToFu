"""tests/test_autopilot_successor_gate.py — the autopilot follow-up spawn gate
must not mistake a DEAD latest-task pointer for a live successor.

Incident (conv msb6ohqifdz7yj, 2026-08-02 11:49:29, app.log): the parent task
a0fa289b finished, the VU carrier d5bf109a produced its reply, the carrier was
discarded — and the final supersede recheck in
``_maybe_run_autopilot_inner`` read the conv→latest-task index, saw a pointer
naming the dead carrier, concluded "Superseded (a newer task owns conv)", and
stood down. The delivered VU bubble sat as the last message with NO follow-up
task spawned: sidebar idle, send button active, autopilot armed but paused —
until an unrelated brain dispatch happened to revive the conv 6.5 min later.

Two structural facts the incident exposed:

  1. ``_successor_already_running`` treated "index names a task id ≠ parent"
     as "a newer task owns the conv". But the index is a raw POINTER: it can
     legitimately name the run's OWN discarded VU carrier (the HB-1 handoff
     window) or any terminal/discarded corpse. The conv-sync freshness guard
     has encoded "superseded by own VU carrier = by-design handoff" since
     HB-1 (manager/_sync.py), and ``_live_successor_task_id`` applies the
     same liveness judgement for SSE stamping — the spawn gate was the one
     place that didn't. Fix: only a task still LIVE in the registry counts.

  2. ``_record_latest_task`` dual-writes a store mirror (TTL 1h) that local
     deletes never invalidated, so every store-backed ``_latest_task_for_conv``
     read kept returning the corpse for up to an hour. Fix: all deletion
     sites go through ``_clear_latest_task`` (local + mirror together).
"""

import pytest


@pytest.fixture()
def clean_index():
    """Isolate the conv→latest-task index + store mirror around each test."""
    from lib.runtime_state_store import reset_for_test
    from lib.tasks_pkg import manager as m
    with m._conv_latest_task_lock:
        m._conv_latest_task.clear()
    reset_for_test()
    yield m
    with m._conv_latest_task_lock:
        m._conv_latest_task.clear()
    reset_for_test()


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the live registry; auto-cleanup."""
    from lib.tasks_pkg import tasks, tasks_lock
    added = []

    def _put(task_id, conv_id, status='running'):
        with tasks_lock:
            tasks[task_id] = {'id': task_id, 'convId': conv_id,
                              'status': status, 'config': {}}
        added.append(task_id)
        return task_id

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


@pytest.mark.unit
def test_gate_ignores_own_discarded_vu_carrier(clean_index, put_task):
    """THE incident: index points at the run's own VU carrier, carrier already
    discarded from the registry → NOT superseded (the follow-up must spawn)."""
    from lib.tasks_pkg.autopilot_baton import _successor_already_running
    m = clean_index
    conv = 'conv-gate-1'
    parent = {'id': 'parent-1', 'convId': conv, '_vu_carrier_id': 'vu-dead-1'}
    m._record_latest_task(conv, 'vu-dead-1')   # HB-1 claim; carrier then discarded
    assert _successor_already_running(parent, conv) is False


@pytest.mark.unit
def test_gate_ignores_terminal_successor_corpse(clean_index, put_task):
    """Index names a task still in the registry but terminal → NOT superseded.
    A finished task owns nothing; the follow-up must not stand down for it."""
    from lib.tasks_pkg.autopilot_baton import _successor_already_running
    m = clean_index
    conv = 'conv-gate-2'
    put_task('done-task-1', conv, status='done')
    m._record_latest_task(conv, 'done-task-1')
    parent = {'id': 'parent-2', 'convId': conv}
    assert _successor_already_running(parent, conv) is False


@pytest.mark.unit
def test_gate_true_for_live_newer_task(clean_index, put_task):
    """The REAL race the recheck guards against: a live newer task (user regen /
    queued dispatch) owns the conv → superseded, stand down. This MUST keep
    returning True or the follow-up would snipe the user's turn."""
    from lib.tasks_pkg.autopilot_baton import _successor_already_running
    m = clean_index
    conv = 'conv-gate-3'
    for live_status in ('pending', 'running'):
        put_task(f'live-{live_status}', conv, status=live_status)
        m._record_latest_task(conv, f'live-{live_status}')
        parent = {'id': 'parent-3', 'convId': conv}
        assert _successor_already_running(parent, conv) is True, live_status


@pytest.mark.unit
def test_gate_false_when_index_names_parent(clean_index):
    """Steady state: the index names the parent itself → not superseded."""
    from lib.tasks_pkg.autopilot_baton import _successor_already_running
    m = clean_index
    conv = 'conv-gate-4'
    m._record_latest_task(conv, 'parent-4')
    parent = {'id': 'parent-4', 'convId': conv}
    assert _successor_already_running(parent, conv) is False


@pytest.mark.unit
def test_discard_task_invalidates_store_mirror(clean_index, put_task):
    """F2: discard_task clears BOTH the local index entry and the store mirror.
    Before the fix the mirror (TTL 1h) kept naming the discarded carrier, so
    every store-backed _latest_task_for_conv read returned the corpse."""
    from lib.runtime_state_store import get_store
    m = clean_index
    conv = 'conv-gate-5'
    put_task('carrier-5', conv, status='running')
    m._record_latest_task(conv, 'carrier-5')
    # Mirror written by _record_latest_task — visible via the store-backed read.
    assert get_store().get_value(m._LATEST_KIND, conv) == 'carrier-5'
    m.discard_task('carrier-5', conv_id=conv)
    assert get_store().get_value(m._LATEST_KIND, conv) is None
    assert m._latest_task_for_conv(conv) is None


@pytest.mark.unit
def test_clear_latest_task_respects_expect_task_id(clean_index, put_task):
    """The compare-and-delete discipline: a stale discard must not evict a
    NEWER owner's pointer (local entry kept when it names someone else)."""
    m = clean_index
    conv = 'conv-gate-6'
    put_task('new-owner-6', conv, status='running')
    m._record_latest_task(conv, 'new-owner-6')
    removed = m._clear_latest_task(conv, expect_task_id='old-corpse-6')
    assert removed is False
    with m._conv_latest_task_lock:
        assert m._conv_latest_task.get(conv) == 'new-owner-6'
