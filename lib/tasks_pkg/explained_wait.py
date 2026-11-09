"""Reasons a silent task is not a wedged one.

Entry point: ``explained_wait(task) -> str``. Empty means the caller's own
liveness clocks are the only evidence available, i.e. silence that no other
component owns and that therefore may be a real wedge.

The one silence judge for root and child agents (``tasks_pkg.liveness``) asks
this owner instead of testing one marker itself. Enumerating the wait class
separately in each consumer is exactly how a 20-minute writer-admission wait
was force-failed as "no progress" while it was queued behind another writer.
The judge also stamps when it last saw a wait explained, so the silence that
follows restarts from the wait's end (``liveness.silence_origin``).

Markers are published for the DURATION of the wait only, by the module that
owns the wait:

- ``_storageWaits`` — persistence ACK backpressure
  (``persistence_wait.wait_for_persistence``). The write is unacknowledged;
  storage may take as long as it takes.
- ``_workspaceAdmission`` — a task-bound internal workspace lease acquisition
  (``project_mod.workspace_lease._publish_admission_wait``): dependency setup
  before a worker command, or a stable integration snapshot. Model tool calls
  take no lease and never publish it. The key itself is owned by
  ``project_mod.tool_admission``. No process exists yet, no output can
  arrive, and ``tool_admission.admission_timeout()`` defaults to waiting until
  admitted. The marker is dropped before execution, where the ordinary process
  clock applies again.
- ``_childInvocationWaits`` — a parent blocked synchronously on child
  invocation carriers (:func:`child_invocation_wait`, published by the Flow
  step runner ``orchestration.agent_runner``). The parent's clocks advance
  only when a child emits events, so a child inside one silent 30-minute
  command froze both parent clocks and the reaper force-failed the Flow turn
  on the very tick it correctly interrupted the child's command (2026-11-01).
  The wait is explained only while a published child is itself live: the
  child is a registered task judged by its own clocks and command watch, and
  once it is reaped or finished the parent's silence is unexplained again.
- ``_deviceWaits`` — a task blocked in ``desktop.bridge.send_desktop_command``
  while its queued command is claimed by a device that is still connected
  (the bridge's own ``_command_active_locked`` rule: same owner, a poll inside
  the connection window, the command in the device's active set). An unclaimed
  command, a settled one or a device that stopped polling explains nothing:
  the command's pickup deadline, a transport timeout, the stale-mount verdict
  or an explicit Stop still owns that silence.
- host filesystem stall — a keepalive ``stat`` unanswered past the stale-mount
  bound (``lib.fs_keepalive.filesystem_unresponsive``). Worktrees, logs and the
  storage authority share that mount, so blocked I/O silences every agent at
  once and no stop can interrupt it; a verdict could only take effect when the
  I/O returns, i.e. exactly when the agent could continue (2026-11-02).

A Coordinator waiting on live Workers publishes no marker here. That silence is
owned by the observation itself: ``conversations.coordinator_workers`` refreshes
the waiter's existing dispatch heartbeat after every COMPLETED status poll, and
only then, exactly like a ratified human wait. Delegation's worker-root
preparation (``project_mod.managed_repository``) refreshes it the same way, on
each observed advance and never while a step is blocked. A declared-wait marker whose
freshness is the same stall threshold would be redundant while polls complete
and would delay the reap of a genuinely blocked status read from one threshold
to two, because the reaper restarts silence from the last explained verdict.

An explained wait never grants cancellation immunity: explicit Stop, an
execution deadline, and the operator bound
``TOFU_WORKSPACE_TOOL_WAIT_SECONDS`` all remain owned by the invocation.
"""

from contextlib import contextmanager
import time

STORAGE_WAIT_KEY = '_storageWaits'
CHILD_INVOCATION_WAIT_KEY = '_childInvocationWaits'
DEVICE_WAIT_KEY = '_deviceWaits'


@contextmanager
def child_invocation_wait(parent, child):
    """Publish that ``parent`` is blocked on ``child`` for the wait's duration."""
    # Never removed: concurrent Flow branches share one mapping, and an empty
    # mapping already means no wait.
    waits = parent.setdefault(CHILD_INVOCATION_WAIT_KEY, {})
    waits[child['id']] = child
    try:
        yield
    finally:
        waits.pop(child['id'], None)


def _live_child(child):
    return (isinstance(child, dict) and not child.get('aborted')
            and child.get('status') in ('pending', 'running'))


@contextmanager
def device_wait(task, command):
    """Publish one task's queued desktop command for the duration of its wait.

    ``command`` is the bridge's own queued-command mapping, read under
    ``command_queue_lock`` by :func:`_live_device_claim`, so a claim follows the
    device's real presence instead of a second copy. Several commands of one task
    share the wait mapping, keyed by command id; an empty mapping means no device
    wait owns the silence.
    """
    waits = task.setdefault(DEVICE_WAIT_KEY, {})
    waits[command['id']] = command
    try:
        yield
    finally:
        waits.pop(command['id'], None)


def _live_device_claim(command, now):
    """The bridge's own activity rule: claimed by a device still connected."""
    if not isinstance(command, dict):
        return False
    from lib.desktop import bridge
    with bridge.command_queue_lock:
        queued = bridge.command_queue.get(command['id'])
        return bool(queued is not None and bridge._command_active_locked(queued, now))


def explained_wait(task, now=None):
    """Name the external owner of this task's silence, or '' when none does.

    ``now`` is the caller's wall clock (the liveness judge passes its own, so a
    test's controlled clock and the verdict never disagree).
    """
    if not isinstance(task, dict):
        return ''
    now = time.time() if now is None else now
    if task.get(STORAGE_WAIT_KEY):
        return 'storage'
    # Deferred: the task manager imports this module at server boot, and the
    # project tool package must stay unloaded until a task actually runs.
    from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY
    if isinstance(task.get(WORKSPACE_ADMISSION_KEY), dict):
        return 'workspace-admission'
    children = task.get(CHILD_INVOCATION_WAIT_KEY)
    if isinstance(children, dict) and any(
            _live_child(child) for child in list(children.values())):
        return 'child-invocation'
    devices = task.get(DEVICE_WAIT_KEY)
    if isinstance(devices, dict) and any(
            _live_device_claim(claim, now) for claim in list(devices.values())):
        return 'device'
    from lib.fs_keepalive import filesystem_unresponsive
    if filesystem_unresponsive():
        return 'filesystem'
    return ''
