"""GoalRun application lifecycle service behavior."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lib.goal_runs.service import (
    GoalRunLifecycleError,
    GoalRunService,
    GoalRunServicePorts,
)


pytestmark = pytest.mark.unit


class MemoryGoalRuns:
    def __init__(self):
        self.starts = []
        self.transitions = []

    def start(self, run_id, **kwargs):
        self.starts.append((run_id, kwargs))
        return {'run': {
            'format': 'tofu.goal-run/v1',
            'runId': run_id,
            'status': 'active',
        }}

    def transition(self, run_id, **kwargs):
        self.transitions.append((run_id, kwargs))
        return {'run': {
            'format': 'tofu.goal-run/v1',
            'runId': run_id,
            'status': kwargs['status'],
        }}

    def latest_for_conversation(self, _conversation_id):
        return None


def _service(repository, *, queued_human_waiting=lambda *_args: False):
    return GoalRunService(ports=GoalRunServicePorts(
        repository_for_owner=lambda owner, tenant: repository,
        queued_human_waiting=queued_human_waiting,
    ))


def test_service_starts_from_exact_objective_and_completes_after_verification():
    repository = MemoryGoalRuns()
    service = _service(repository)
    task = {
        'id': 'task-123',
        'convId': 'conv-123',
        '_userId': 7,
        'config': {'_goalObjective': 'fix the root cause'},
        'messages': [{'role': 'user', 'content': 'older wording'}],
        'content': 'verified result',
    }
    definition = {'schema': 'tofu.orchestration/v1', 'nodes': [], 'edges': []}

    started = service.start(task, definition)

    assert started['runId'] == 'goal_task-123'
    assert task['_goalRunId'] == 'goal_task-123'
    assert task['_goalRunStatus'] == 'active'
    run_id, start = repository.starts[0]
    assert run_id == 'goal_task-123'
    assert start['conversation_id'] == 'conv-123'
    assert start['objective'] == 'fix the root cause'
    assert start['definition'] is definition
    assert start['policy']['solutionHorizon'] == 'long_term'

    terminal = SimpleNamespace(
        category='success',
        stop_reason='verified_complete',
        as_dict=lambda: {'category': 'success'},
    )
    completed = service.complete(task, terminal)

    assert completed['status'] == 'completed'
    assert task['_goalRunStatus'] == 'completed'
    assert task['_goalRunReason'] == 'objective_verified'
    assert repository.transitions == [(
        'goal_task-123',
        {
            'status': 'completed',
            'reason': 'objective_verified',
            'final': 'verified result',
            'outcome': {'category': 'success'},
        },
    )]


def test_service_fails_closed_without_an_accepted_human_objective():
    service = _service(MemoryGoalRuns())
    task = {
        'id': 'task-empty', 'convId': 'conv-empty', '_userId': 7,
        'config': {},
        'messages': [{
            'role': 'user', 'content': 'synthetic', '_isVirtualUser': True,
        }],
    }

    with pytest.raises(GoalRunLifecycleError, match='accepted human turn'):
        service.start(task, {'nodes': [], 'edges': []})


def test_stale_continuation_is_cancelled_when_newer_human_intent_is_queued():
    service = _service(
        MemoryGoalRuns(), queued_human_waiting=lambda conv, owner: (
            conv == 'conv-race' and owner == 7
        ))
    abort_event = threading.Event()
    task = {
        'id': 'task-race',
        'convId': 'conv-race',
        '_userId': 7,
        'config': {
            '_goalObjective': 'old objective',
            '_goalContinuationCommand': True,
        },
        'abort_event': abort_event,
        'aborted': False,
    }

    service.start(task, {'nodes': [], 'edges': []})

    assert task['aborted'] is True
    assert task['_abort_reason'] == 'superseded_by_human'
    assert abort_event.is_set() is True


def test_incomplete_flow_is_blocked_not_completed_or_failed():
    repository = MemoryGoalRuns()
    service = _service(repository)
    task = {
        'id': 'task-budget', 'convId': 'conv-budget', '_userId': 7,
        'config': {'_goalObjective': 'finish durable migration'},
        'content': 'partial result',
    }
    service.start(task, {'nodes': [], 'edges': []})

    service.complete(task, SimpleNamespace(
        category='incomplete',
        stop_reason='max_iterations',
        as_dict=lambda: {'category': 'incomplete'},
    ))

    assert repository.transitions[-1][1]['status'] == 'blocked'
    assert repository.transitions[-1][1]['reason'] == (
        'iteration_budget_exhausted')
