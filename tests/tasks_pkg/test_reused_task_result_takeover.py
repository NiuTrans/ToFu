"""A resumed Worker takes over the task result its interrupted predecessor left.

Project-dispatch Workers resume in place under one deterministic workspace task
id (``project-worker:<assignmentId>``). Boot recovery settles the predecessor's
task-result record as ``interrupted``, and that record also fenced every write
of the resumed attempt: its terminal result was refused as a "stale terminal
finalizer", so its workspace delivery record, completion listeners and git
checkpoint never ran (2026-11-06 01:25 restart). The authority admits a write
naming another Attempt than the record's writer only when that Attempt is its
Turn's current Attempt bound to this exact task id -- the evidence worker entry
itself requires -- so the resumed attempt takes the record over through the
ordinary CAS while every late write of the predecessor stays fenced.
"""
from __future__ import annotations

from types import SimpleNamespace
import time
import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

_OWNER = 1


def _bind(attempt_id, task_id, *, start=True):
    from lib.turn_lifecycle import bind_task, claim_attempt_start, mark_task_started
    assert claim_attempt_start(attempt_id, user_id=_OWNER)
    assert bind_task(attempt_id, task_id, user_id=_OWNER)
    if start:
        assert mark_task_started(attempt_id, task_id, user_id=_OWNER)['status'] == 'running'


def _executor(worker, attempt, *, content):
    return {
        'id': worker.task_id, 'convId': worker.conversation_id, '_userId': _OWNER,
        '_turnId': attempt['turnId'], '_attemptId': attempt['attemptId'],
        'status': 'running', 'content': content, 'thinking': '', 'error': None,
        'model': 'worker-model', 'config': {'model': 'worker-model'},
        'created_at': time.time(), 'toolRounds': [], 'segments': [],
    }


def _finished(task, content):
    task.update(status='done', content=content, finishReason='stop')
    return task


def _result(task_id):
    from lib.storage import get_storage_client
    return get_storage_client().query('task_results.replay_get', {
        'key': task_id, 'user_id': _OWNER, 'include_metadata': True})


def _successor(worker, *, bind=True):
    from lib import turn_lifecycle
    turn = turn_lifecycle.get_turn(worker.conversation_id, worker.turn_id, user_id=_OWNER)
    attempt = turn_lifecycle.create_attempt(
        worker.conversation_id, worker.turn_id,
        command_id=f'{worker.conversation_id}-resume-{uuid.uuid4().hex[:8]}',
        operation='regenerate', expected_projection_revision=turn['projectionRevision'],
        config={'model': 'worker-model'}, user_id=_OWNER)['attempt']
    if bind:
        _bind(attempt['attemptId'], worker.task_id)
    return attempt


@pytest.fixture
def worker():
    """A Worker whose running predecessor attempt a server restart interrupted."""
    from lib import turn_lifecycle
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
    from lib.tasks_pkg.manager._sync import checkpoint_task_partial
    conversation_id = 'worker-' + uuid.uuid4().hex[:12]
    created = turn_lifecycle.create_turn_pair(
        conversation_id, command_id=conversation_id + '-request',
        input_projection={'content': 'implement the assignment'},
        config={'model': 'worker-model'}, user_id=_OWNER,
        conversation_defaults={'allowCreate': True, 'title': 'Worker', 'settings': {}})
    worker = SimpleNamespace(
        conversation_id=conversation_id, turn_id=created['turn']['turnId'],
        task_id='project-worker:' + str(uuid.uuid4()))
    _bind(created['attempt']['attemptId'], worker.task_id)
    worker.predecessor = _executor(worker, created['attempt'], content='partial work')
    assert checkpoint_task_partial(worker.predecessor, force=True) is True

    recover_stale_tasks_on_startup()
    assert _result(worker.task_id)['status'] == 'interrupted'
    return worker


@pytest.fixture
def completions(monkeypatch):
    """Record the workspace delivery intent and terminal listeners per attempt."""
    from lib.tasks_pkg import workspace_delivery
    from lib.tasks_pkg.manager import _terminal
    delivered, observed = [], []
    record_intent = workspace_delivery.persist_terminal_workspace_intent

    def intent(task):
        delivered.append(task['_attemptId'])
        return record_intent(task)

    monkeypatch.setattr(workspace_delivery, 'persist_terminal_workspace_intent', intent)
    monkeypatch.setattr(_terminal, '_TERMINAL_LISTENERS', [])
    _terminal.register_terminal_listener(lambda task: observed.append(task['_attemptId']))
    return SimpleNamespace(delivered=delivered, observed=observed)


def test_interrupted_attempt_stays_fenced_before_any_successor(worker, completions):
    from lib.tasks_pkg.manager._persist import persist_task_result

    assert persist_task_result(_finished(dict(worker.predecessor), 'late')) is False

    assert _result(worker.task_id)['status'] == 'interrupted'
    assert completions.delivered == completions.observed == []


def test_resumed_worker_terminal_result_takes_over_and_completes(worker, completions):
    """The incident: the resumed attempt's first write is its terminal result."""
    from lib.tasks_pkg.manager._persist import persist_task_result
    resumed = _successor(worker)
    current = _executor(worker, resumed, content='')

    assert persist_task_result(_finished(current, 'resumed result')) is True

    row = _result(worker.task_id)
    assert row['status'] == 'done'
    assert row['metadata']['attemptId'] == resumed['attemptId']
    assert completions.delivered == completions.observed == [resumed['attemptId']]

    # The predecessor's late terminal write no longer names the current attempt.
    assert persist_task_result(_finished(dict(worker.predecessor), 'late')) is False
    row = _result(worker.task_id)
    assert row['status'] == 'done'
    assert row['metadata']['attemptId'] == resumed['attemptId']
    assert completions.delivered == completions.observed == [resumed['attemptId']]


def test_only_the_bound_current_attempt_takes_over_running_checkpoints(worker):
    from lib.tasks_pkg.manager._persist import _upsert_task_row
    from lib.tasks_pkg.manager._sync import checkpoint_task_partial
    unbound = _successor(worker, bind=False)

    # Accepted but not yet bound to the workspace task: no takeover.
    assert checkpoint_task_partial(
        _executor(worker, unbound, content='unbound'), force=True) is False
    assert _result(worker.task_id)['status'] == 'interrupted'

    _bind(unbound['attemptId'], worker.task_id)
    current = _executor(worker, unbound, content='resumed work')
    assert checkpoint_task_partial(current, force=True) is True
    row = _result(worker.task_id)
    assert row['status'] == 'running'
    assert row['metadata']['attemptId'] == unbound['attemptId']

    # A late running checkpoint of the predecessor is refused by the authority
    # even with the current version witness.
    late = dict(worker.predecessor, content='late partial')
    late['_taskResultCheckpointGuard'] = current['_taskResultCheckpointGuard']
    late['_taskResultVersion'] = current['_taskResultVersion']
    assert _upsert_task_row(
        late, worker.conversation_id, content='late partial', thinking='',
        status='running', error_json=None, tr_json=None,
        meta_json='{"turnId": "%s", "attemptId": "%s"}' % (
            worker.predecessor['_turnId'], worker.predecessor['_attemptId'])) is False
    assert _result(worker.task_id)['metadata']['attemptId'] == unbound['attemptId']
