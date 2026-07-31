#!/usr/bin/env python3
"""pt_conv_state_ssot — P1: server-authoritative conv-state channel.

This suite is the failing-first spec for extending ``notify_conv_changed``'s
payload with the CANONICAL busy-state signal, so client sidebar/composer/
loadConv-reconnect all read one source instead of guessing from settings.

Payload contract added by P1 (existing keys unchanged — pure addition):

    {
      type: 'conv_changed' | 'conv_deleted',
      convId, userId,
      rev?,                          # existing — body rev, bumps on messages change only
      runningTaskIds: [str, ...],    # NEW — SNAPSHOT of task registry, EMPTY when idle
      runningTaskIdsRev: [ns_int, replica_id_str],   # NEW — (monotonic_ns, replica_id)
    }

Design invariants this suite pins (per owner sign-off, 2026-07-24):

  * runningTaskIds READS THE TASK REGISTRY (SSOT), never settings.activeTaskId.
    A conv with a stale settings.activeTaskId but no live task must broadcast
    ``[]`` — that is the whole point of moving off settings.
  * runningTaskIdsRev is a tuple ``(monotonic_ns, replica_id)``, NOT a plain
    int. Owner mandate: "activeTaskIdsRev is not a plain counter; it must be
    (conv_id, monotonic_ns) server clock … use nanotime + replica_id tiebreak".
    We use ``(monotonic_ns, replica_id_str)`` — replica_id already exists in
    lib.agent_core.push (``TOFU_REPLICA_ID`` or pid) so we don't invent a
    second convention. Two frames from THE SAME PROCESS have strictly-
    increasing monotonic_ns (single monotonic clock); two frames from
    DIFFERENT replicas are ordered by (ns, replica_id) lex.
  * Deleted frames DO NOT carry runningTaskIds — the conv is gone, no busy
    concept applies. They DO carry runningTaskIdsRev so the client's
    idempotent gate has a uniform key. Absence of the field is the "no
    running set" marker; the client must treat it as ``[]``.
  * Carrier tasks (autopilot VU sub-task / inline reporter) MUST be excluded
    — same filter list_running_tasks / /api/chat/active use. Otherwise a
    convId='' VU carrier lights up sidebar dots the user cannot dismiss.
  * user_id scoping is preserved — payload['userId'] must equal the passed
    ``user_id`` even for the new fields.

Test naming: matches test_conv_changed_notify.py style so the failing-first
run is greppable next to the existing suite.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


@pytest.fixture
def captured(monkeypatch):
    """Same fixture shape as test_conv_changed_notify.captured — the two
    suites should read the same to any future maintainer."""
    frames = []

    def _fake_push_event(channel, task_id, payload):
        frames.append({'channel': channel, 'taskId': task_id, 'payload': payload})

    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake_push_event)
    return frames


@pytest.fixture
def empty_registry(monkeypatch):
    """Force the registry snapshot helper to return an empty projection.

    P1 introduces ``snapshot_running_by_conv`` in ``lib.tasks_pkg.manager._registry``
    — a pure read that groups the live task table by ``convId``. Patching it at
    the module level lets us test the notify seam without spinning up a whole
    orchestrator round.
    """
    import lib.tasks_pkg.manager._registry as reg_mod
    monkeypatch.setattr(reg_mod, 'snapshot_running_by_conv',
                        lambda user_id='': {})
    return reg_mod


@pytest.fixture
def one_running_task(monkeypatch):
    """Registry has ONE running task for conv-A → notify frame for conv-A
    should carry that taskId; frames for OTHER convs still see ``[]``."""
    import lib.tasks_pkg.manager._registry as reg_mod
    monkeypatch.setattr(reg_mod, 'snapshot_running_by_conv',
                        lambda user_id='': {'conv-A': ['task-alpha']})
    return reg_mod


# ─────────────────────────────────────────────────────────────────────
#  1. Content-change frame carries the new fields (idle conv → empty list)
# ─────────────────────────────────────────────────────────────────────
def test_content_change_carries_running_task_ids_field(captured, empty_registry):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=7)
    assert len(captured) == 1
    p = captured[0]['payload']
    # Old contract preserved:
    assert p['type'] == 'conv_changed'
    assert p['convId'] == 'conv-A'
    assert p['rev'] == 7
    # New contract:
    assert 'runningTaskIds' in p, 'P1 must add runningTaskIds to conv_changed'
    assert p['runningTaskIds'] == [], 'idle conv → empty list, not missing'
    assert 'runningTaskIdsRev' in p, 'P1 must add runningTaskIdsRev tuple'


def test_running_task_ids_reads_registry_not_settings(captured, one_running_task):
    """SSOT invariant: the list comes from the task registry.

    This is the whole point of P1 — settings.activeTaskId is the reason the
    phone/PC disagreement existed. The seam must NEVER look at settings.
    """
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=9)
    p = captured[0]['payload']
    assert p['runningTaskIds'] == ['task-alpha']


def test_frames_for_other_conv_get_own_running_ids(captured, one_running_task):
    """The registry has conv-A running; a notify for conv-B must NOT leak
    conv-A's ids into conv-B's frame."""
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-B', rev=11)
    p = captured[0]['payload']
    assert p['convId'] == 'conv-B'
    assert p['runningTaskIds'] == [], (
        'runningTaskIds must be the projection for THIS conv only — not the '
        'whole registry')


# ─────────────────────────────────────────────────────────────────────
#  2. Rev tuple is (monotonic_ns, replica_id) and monotonic per-process
# ─────────────────────────────────────────────────────────────────────
def test_running_task_ids_rev_is_ns_and_replica_tuple(captured, empty_registry):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=1)
    rev = captured[0]['payload']['runningTaskIdsRev']
    assert isinstance(rev, list) and len(rev) == 2, (
        'runningTaskIdsRev must be a 2-array so JSON-native lex compare works — '
        'got %r' % (rev,))
    ns, replica = rev
    assert isinstance(ns, int) and ns > 0, 'first component must be monotonic_ns int'
    assert isinstance(replica, str) and replica, 'second component must be replica_id str'


def test_running_task_ids_rev_is_monotonic_within_process(captured, empty_registry):
    """Two consecutive frames from THE SAME process must have strictly-
    increasing monotonic_ns. This is the guarantee that lets the client
    reduce ``rev_new > rev_old`` as a plain lex compare and never accept a
    reordered stale frame."""
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=1)
    notify_conv_changed('conv-A', rev=2)
    r1 = captured[0]['payload']['runningTaskIdsRev']
    r2 = captured[1]['payload']['runningTaskIdsRev']
    # Same process → same replica_id
    assert r1[1] == r2[1]
    # Strict monotonic on ns
    assert r2[0] > r1[0], (
        'monotonic_ns must strictly increase for consecutive frames on the '
        'same replica; got %r then %r' % (r1, r2))


def test_replica_id_matches_push_hub_convention(captured, empty_registry, monkeypatch):
    """Owner mandate: don't invent a second replica_id convention. Reuse the
    same TOFU_REPLICA_ID resolution already in lib.agent_core.push.PushHub."""
    monkeypatch.setenv('TOFU_REPLICA_ID', 'replica-xyz')
    # Force the hub to re-read env (its _rid is memoized per-instance, but the
    # meta_cache seam calls the SAME resolver — this test asserts they agree,
    # not that the hub's own cache is bypassed).
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=1)
    p = captured[0]['payload']
    assert p['runningTaskIdsRev'][1] == 'replica-xyz'


# ─────────────────────────────────────────────────────────────────────
#  3. Deleted frames don't carry runningTaskIds (but keep the rev key)
# ─────────────────────────────────────────────────────────────────────
def test_deleted_frame_omits_running_task_ids(captured, empty_registry):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-C', deleted=True)
    p = captured[0]['payload']
    assert p['type'] == 'conv_deleted'
    assert 'runningTaskIds' not in p, (
        'a deleted conv has no busy concept — client treats absence as []')
    # But still carries the rev tuple so client's idempotent gate has one key
    assert 'runningTaskIdsRev' in p, (
        'even deleted frames must carry runningTaskIdsRev so the client can '
        'reject an out-of-order stale conv_changed for the same conv')


# ─────────────────────────────────────────────────────────────────────
#  4. user_id scoping propagates
# ─────────────────────────────────────────────────────────────────────
def test_user_id_scoping_survives_new_fields(captured, one_running_task):
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-A', rev=3, user_id=42)
    p = captured[0]['payload']
    assert p['userId'] == 42
    # New fields still present even under non-default user
    assert 'runningTaskIds' in p
    assert 'runningTaskIdsRev' in p


# ─────────────────────────────────────────────────────────────────────
#  5. Registry-projection contract: carrier filter + full snapshot semantics
# ─────────────────────────────────────────────────────────────────────
def test_snapshot_running_by_conv_excludes_carriers():
    """The projection helper must NOT include convId='' VU carriers or inline
    reporter carriers — same filter list_running_tasks uses. Otherwise a
    background carrier lights up sidebar dots forever."""
    from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
    from lib.tasks_pkg.manager._state import tasks as _tasks_registry, tasks_lock as _tl
    import time as _time

    # Set up two tasks: one real (conv-real), one carrier (_vu_subtask=True).
    fake_real = {
        'id': 'tid-real', 'convId': 'conv-real', 'status': 'running',
        'aborted': False, 'created_at': _time.time(),
        '_t_last_event': _time.time(), '_dispatch_heartbeat': _time.time(),
    }
    fake_carrier = {
        'id': 'tid-carrier', 'convId': '', 'status': 'running',
        'aborted': False, 'created_at': _time.time(),
        '_vu_subtask': True,
        '_t_last_event': _time.time(), '_dispatch_heartbeat': _time.time(),
    }
    with _tl:
        _tasks_registry['tid-real'] = fake_real
        _tasks_registry['tid-carrier'] = fake_carrier
    try:
        snap = snapshot_running_by_conv()
        assert snap.get('conv-real') == ['tid-real']
        # Carrier's convId is '' — it must NOT appear under any key,
        # certainly not under '' either (empty convId means "unassigned").
        assert '' not in snap, 'carriers must be filtered out entirely'
    finally:
        with _tl:
            _tasks_registry.pop('tid-real', None)
            _tasks_registry.pop('tid-carrier', None)


def test_snapshot_running_by_conv_excludes_aborted_and_non_running():
    """A task with ``aborted=True`` or non-'running' status must be excluded
    — the sidebar dot should extinguish the instant supersede fires."""
    from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
    from lib.tasks_pkg.manager._state import tasks as _tasks_registry, tasks_lock as _tl
    import time as _time

    t_aborted = {
        'id': 'tid-abt', 'convId': 'conv-Z', 'status': 'running',
        'aborted': True, 'created_at': _time.time(),
    }
    t_done = {
        'id': 'tid-done', 'convId': 'conv-Z', 'status': 'done',
        'aborted': False, 'created_at': _time.time(),
    }
    with _tl:
        _tasks_registry['tid-abt'] = t_aborted
        _tasks_registry['tid-done'] = t_done
    try:
        snap = snapshot_running_by_conv()
        assert 'conv-Z' not in snap, (
            'aborted-or-done tasks must not surface as running')
    finally:
        with _tl:
            _tasks_registry.pop('tid-abt', None)
            _tasks_registry.pop('tid-done', None)


# ─────────────────────────────────────────────────────────────────────
#  6. Fail-open — the extended payload must not break the mutation path
# ─────────────────────────────────────────────────────────────────────
def test_registry_lookup_failure_does_not_raise(monkeypatch, captured):
    """If ``snapshot_running_by_conv`` explodes (registry corruption /
    partial init), the notify seam still emits a frame — just with an empty
    runningTaskIds list — and never raises. Best-effort matches the pre-P1
    contract for push failures."""
    import lib.tasks_pkg.manager._registry as reg_mod

    def _boom(user_id=''):
        raise RuntimeError('registry snapshot exploded')

    monkeypatch.setattr(reg_mod, 'snapshot_running_by_conv', _boom)
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-F', rev=1)  # must not raise
    assert len(captured) == 1
    # Best-effort default: empty list, rev tuple still present
    assert captured[0]['payload'].get('runningTaskIds', 'sentinel') == []


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
