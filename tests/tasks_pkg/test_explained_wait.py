"""A silent task whose silence belongs to an external owner is waiting, not wedged.

Regression coverage for the 2026-10-20 incident: a turn's ``run_command`` waited
for command writer admission behind another task's 31-minute command. Writer
admission publishes no process and no output, so both reaper clocks looked
stale and the task was force-failed at 30 minutes — the model never answered and
the conversation was abandoned with ``content=0``.

The wait class is owned in one place (``explained_wait``) because it previously
lived in each consumer separately, and enumerating it per consumer is how a
legitimate admission wait was mistaken for a wedge.
"""

import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _silent_task(**fields):
    """A running task whose BOTH liveness clocks are an hour stale."""
    stale = time.time() - 3600
    task = {'id': 'conv-waiting', 'status': 'running', 'aborted': False,
            'config': {}, 'abort_event': threading.Event(), 'created_at': stale,
            '_t_last_event': stale, '_dispatch_heartbeat': stale}
    task.update(fields)
    return task


# ── The predicate ────────────────────────────────────────────────────────
def test_no_marker_means_silence_is_unexplained():
    from lib.tasks_pkg.explained_wait import explained_wait

    assert explained_wait({}) == ''
    assert explained_wait({'status': 'running'}) == ''
    assert explained_wait(None) == ''
    # An empty mapping is a released wait, not an active one.
    assert explained_wait({'_storageWaits': {}}) == ''


def test_published_markers_name_their_owner():
    from lib.tasks_pkg.explained_wait import explained_wait

    assert explained_wait({'_storageWaits': {'op': 1}}) == 'storage'
    assert explained_wait(
        {'_workspaceAdmission': {'since': 1.0, 'depth': 1}}) == 'workspace-admission'


def test_child_invocation_wait_is_explained_only_while_the_child_is_live():
    from lib.tasks_pkg.explained_wait import (
        CHILD_INVOCATION_WAIT_KEY, child_invocation_wait, explained_wait,
    )

    parent = {'id': 'flow-parent'}
    child = {'id': 'flow-child', 'status': 'running', 'aborted': False}
    with child_invocation_wait(parent, child):
        assert explained_wait(parent) == 'child-invocation'
        child['aborted'] = True  # reaped/stopped child no longer owns it
        assert explained_wait(parent) == ''
        child.update(aborted=False, status='done')
        assert explained_wait(parent) == ''
        child['status'] = 'running'
    assert parent[CHILD_INVOCATION_WAIT_KEY] == {}
    assert explained_wait(parent) == ''


def test_admission_publisher_and_predicate_share_one_key():
    """Binding test: the lease wait owner publishes the key the predicate reads."""
    from lib.project_mod.tool_admission import (
        WORKSPACE_ADMISSION_KEY, Cancellation, tool_admission_scope,
    )
    from lib.project_mod.workspace_lease import _publish_admission_wait
    from lib.tasks_pkg.explained_wait import explained_wait

    task = {'id': 'conv-waiting'}
    with tool_admission_scope(task=task):
        with _publish_admission_wait(Cancellation(task)):
            assert WORKSPACE_ADMISSION_KEY in task
            assert explained_wait(task) == 'workspace-admission'
        # The marker covers the WAIT only; execution owns the process clock.
        assert WORKSPACE_ADMISSION_KEY not in task
        assert explained_wait(task) == ''


def test_stale_filesystem_mount_explains_every_silence(monkeypatch):
    """2026-11-02: a 27-minute FUSE freeze silenced every agent at once.

    Blocked I/O cannot be interrupted, so the silence belongs to the mount.
    """
    import lib.fs_keepalive as fs_keepalive
    from lib.tasks_pkg.explained_wait import explained_wait

    monkeypatch.setattr(fs_keepalive, 'filesystem_unresponsive', lambda: True)
    assert explained_wait({'id': 'any-agent'}) == 'filesystem'
    # A task-owned wait still names its own, more specific owner.
    assert explained_wait({'_storageWaits': {'op': 1}}) == 'storage'
    monkeypatch.setattr(fs_keepalive, 'filesystem_unresponsive', lambda: False)
    assert explained_wait({'id': 'any-agent'}) == ''


# ── The one judge ────────────────────────────────────────────────────────
def test_unexplained_silence_is_wedged_for_the_shared_judge():
    from lib.tasks_pkg.liveness import judge

    verdict = judge(_silent_task(), time.time(), 1800)
    assert verdict.state == 'wedged'
    assert verdict.silent_seconds >= 3600


@pytest.mark.parametrize('marker', [
    {'_workspaceAdmission': {'since': time.monotonic(), 'depth': 1}},
    {'_storageWaits': {'op': 1}},
])
def test_explained_wait_is_never_wedged(marker):
    from lib.tasks_pkg.liveness import judge

    verdict = judge(_silent_task(**marker), time.time(), 1800)
    assert verdict.state == 'explained' and verdict.reason


def test_invocation_monitor_does_not_judge_silence_itself():
    """A child is judged like its parent, never by a private sub-agent rule.

    The per-invocation monitor once stopped a child after 900s of beacon
    silence while the root rule waited 1800s and interrupted a silent command
    instead. Only the shared judge (``lib.tasks_pkg.liveness``) decides.
    """
    from lib.swarm.liveness import ProgressBeacon
    from lib.tasks_pkg.invocation import invocation_abort_requested

    beacon = ProgressBeacon(stall_timeout=0)
    beacon.touch('conv-waiting')
    task = _silent_task(_invocationProgressBeacon=beacon,
                        _invocationProgressKey='conv-waiting')
    assert invocation_abort_requested(task) is False
    assert task['aborted'] is False and not task['abort_event'].is_set()
    assert '_invocationStopReason' not in task
