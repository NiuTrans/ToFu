"""Durable GoalRun transaction, ownership and supersession contract."""

from __future__ import annotations

import pytest

from lib.goal_runs.contract import goal_run_policy
from lib.goal_runs.repository import (
    GoalRunRepositoryError,
    SidecarGoalRunRepository,
)
from lib.storage import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture
def goal_store(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        yield (
            SidecarGoalRunRepository(
                41, client=lambda **_kwargs: supervisor.client),
            supervisor.client,
        )
    finally:
        supervisor.stop()


def _start(repository, run_id, objective='ship the durable design'):
    return repository.start(
        run_id,
        conversation_id='conv-goal',
        objective=objective,
        definition={
            'schema': 'tofu.orchestration/v1', 'nodes': [], 'edges': [],
        },
        policy=goal_run_policy(),
    )


def test_start_supersede_and_terminal_transition_are_one_durable_history(
    goal_store,
):
    repository, client = goal_store

    first = _start(repository, 'goal-first')
    second = _start(repository, 'goal-second', 'replace with the better goal')

    assert first['run']['status'] == 'active'
    assert second['run']['status'] == 'active'
    assert second['supersededRunIds'] == ['goal-first']
    old = client.query(
        'orchestration.run.get', {'run_id': 'goal-first', 'user_id': 41})
    assert old['status'] == 'aborted'
    old_events = client.query(
        'orchestration.event.page',
        {'run_id': 'goal-first', 'user_id': 41, 'cursor': 0},
    )['events']
    assert [(event['type'], event['status'], event['reason'])
            for event in old_events] == [
        ('goal_run_started', 'active', 'started'),
        ('goal_run_transition', 'cancelled', 'superseded_by_new_goal'),
    ]

    completed = repository.transition(
        'goal-second',
        status='completed',
        reason='objective_verified',
        final='all checks passed',
        outcome={'category': 'success'},
    )
    replay = repository.transition(
        'goal-second',
        status='completed',
        reason='objective_verified',
        final='all checks passed',
        outcome={'category': 'success'},
    )

    assert completed['transitioned'] is True
    assert completed['run']['status'] == 'completed'
    assert replay['transitioned'] is False
    physical = client.query(
        'orchestration.run.get', {'run_id': 'goal-second', 'user_id': 41})
    assert physical['status'] == 'done'
    assert physical['final'] == 'all checks passed'
    events = client.query(
        'orchestration.event.page',
        {'run_id': 'goal-second', 'user_id': 41, 'cursor': 0},
    )['events']
    assert [event['type'] for event in events] == [
        'goal_run_started', 'goal_run_transition']


def test_terminal_meaning_is_immutable_and_owner_scoped(goal_store):
    repository, client = goal_store
    _start(repository, 'goal-private')
    repository.transition(
        'goal-private', status='blocked',
        reason='no_verified_progress', outcome={'category': 'incomplete'},
    )

    with pytest.raises(GoalRunRepositoryError, match='database_conflict'):
        repository.transition(
            'goal-private', status='completed',
            reason='objective_verified', outcome={'category': 'success'},
        )

    other = SidecarGoalRunRepository(
        42, client=lambda **_kwargs: client)
    assert other.latest_for_conversation('conv-goal') is None
    assert _start(other, 'goal-other-owner')['run']['status'] == 'active'


def test_terminal_reason_must_match_status_and_is_fully_immutable(goal_store):
    repository, _client = goal_store
    _start(repository, 'goal-terminal-contract')

    with pytest.raises(
        GoalRunRepositoryError, match='database_protocol_error',
    ):
        repository.transition(
            'goal-terminal-contract',
            status='completed',
            reason='runtime_failure',
            final='not verified',
        )

    repository.transition(
        'goal-terminal-contract',
        status='blocked',
        reason='no_verified_progress',
        final='evidence one',
        outcome={'category': 'incomplete'},
    )
    with pytest.raises(GoalRunRepositoryError, match='database_conflict'):
        repository.transition(
            'goal-terminal-contract',
            status='blocked',
            reason='iteration_budget_exhausted',
            final='evidence two',
            outcome={'category': 'incomplete'},
        )


def test_startup_recovery_records_typed_worker_lost_transition(goal_store):
    repository, client = goal_store
    _start(repository, 'goal-interrupted')

    assert client.maintenance(
        'orchestration.run.retire_interrupted_all',
        {'error': {'kind': 'worker_lost', 'message': 'server restarted'}},
    ) == {'retired': 1}

    recovered = repository.latest_for_conversation('conv-goal')
    assert recovered is not None
    assert recovered['runId'] == 'goal-interrupted'
    assert recovered['status'] == 'failed'
    assert recovered['reason'] == 'worker_lost'
    assert recovered['storageStatus'] == 'error'
    assert recovered['terminal'] is True
    events = client.query(
        'orchestration.event.page',
        {'run_id': 'goal-interrupted', 'user_id': 41, 'cursor': 0},
    )['events']
    assert [(event['status'], event['reason']) for event in events] == [
        ('active', 'started'),
        ('failed', 'worker_lost'),
    ]
