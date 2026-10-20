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


class _StalledBeacon:
    """Minimal progress beacon stuck past its stall threshold."""

    stall_timeout = 1800

    def is_making_progress(self, _key):
        return False

    def seconds_since_activity(self, _key):
        return 4000.0

    def touch(self, _key, _kind):
        raise AssertionError('a stalled beacon must not be touched here')


def _invocation_task(**fields):
    task = {'id': 'conv-waiting', 'aborted': False, 'config': {},
            'abort_event': threading.Event(),
            '_invocationProgressBeacon': _StalledBeacon()}
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


def test_admission_publisher_and_predicate_share_one_key():
    """Binding test: the lease wait owner publishes the key the predicate reads."""
    from lib.project_mod.tool_admission import Cancellation, tool_admission_scope
    from lib.project_mod.workspace_lease import _publish_admission_wait
    from lib.tasks_pkg.explained_wait import (
        WORKSPACE_ADMISSION_KEY, explained_wait,
    )

    task = {'id': 'conv-waiting'}
    with tool_admission_scope(task=task):
        with _publish_admission_wait(Cancellation(task)):
            assert WORKSPACE_ADMISSION_KEY in task
            assert explained_wait(task) == 'workspace-admission'
        # The marker covers the WAIT only; execution owns the process clock.
        assert WORKSPACE_ADMISSION_KEY not in task
        assert explained_wait(task) == ''


# ── Invocation stall guard ───────────────────────────────────────────────
def test_stalled_beacon_still_stops_an_unexplained_silence():
    from lib.tasks_pkg.invocation import invocation_abort_requested

    task = _invocation_task()
    assert invocation_abort_requested(task) is True
    assert task['aborted'] is True
    assert task['_invocationStopReason'] == 'stalled'


@pytest.mark.parametrize('marker', [
    {'_workspaceAdmission': {'since': time.monotonic(), 'depth': 1}},
    {'_storageWaits': {'op': 1}},
])
def test_explained_wait_does_not_stall_stop_the_invocation(marker):
    from lib.tasks_pkg.invocation import invocation_abort_requested

    task = _invocation_task(**marker)
    assert invocation_abort_requested(task) is False
    assert task['aborted'] is False
    assert task['abort_event'].is_set() is False
