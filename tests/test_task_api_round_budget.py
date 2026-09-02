"""Finite model-round defaults and the finalization-reserve contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('configured', [None, 0, 'invalid', False])
def test_unset_or_invalid_round_limit_inherits_deployment_profile(
    monkeypatch,
    configured,
):
    import lib.task_budget as task_budget

    observed = []

    def resolve(name, environment, *, maximum):
        observed.append((name, environment, maximum))
        return 192

    monkeypatch.setattr(task_budget, 'resolve_resource_budget', resolve)
    source = {} if configured is None else {'maxApiRounds': configured}
    original = dict(source)
    environment = {'TOFU_DEPLOYMENT_MODE': 'personal'}

    resolved = task_budget.resolve_task_budget_config(source, environment)

    assert source == original
    assert resolved['maxApiRounds'] == 192
    assert observed == [(
        'TOFU_TASK_MAX_API_ROUNDS',
        environment,
        task_budget.MAX_TASK_API_ROUNDS,
    )]


def test_explicit_round_limit_can_be_lower_but_never_exceed_server_cap(
    monkeypatch,
):
    import lib.task_budget as task_budget

    monkeypatch.setattr(
        task_budget,
        'resolve_resource_budget',
        lambda *_args, **_kwargs: pytest.fail(
            'an explicit positive task limit must not probe defaults'),
    )

    assert task_budget.resolve_task_budget_config(
        {'maxApiRounds': 64})['maxApiRounds'] == 64
    assert task_budget.resolve_task_budget_config(
        {'maxApiRounds': 999_999})['maxApiRounds'] == 1024


def test_finalization_reserve_is_bounded_for_small_and_large_tasks():
    from lib.task_budget import api_round_finalization_reserve

    assert api_round_finalization_reserve(4) == 1
    assert api_round_finalization_reserve(192) == 64
    assert api_round_finalization_reserve(512) == 64


def _round_state(completed_rounds: int) -> SimpleNamespace:
    return SimpleNamespace(
        accumulated_usage={},
        api_rounds=[{} for _ in range(completed_rounds)],
        last_finish_reason='tool_calls',
        exit_reason='running',
        model='model-under-test',
    )


def test_finalization_reserve_injects_one_model_visible_reminder(monkeypatch):
    import lib.tasks_pkg.orchestrator._round_gates as round_gates

    events = []
    monkeypatch.setattr(
        round_gates,
        'append_event',
        lambda _task, event: events.append(event),
    )
    task = {'id': 'task-finalization-reserve'}
    state = _round_state(127)
    messages = []
    cfg = {'maxApiRounds': 192, 'taskBudgetSoftRatio': 0.8}

    assert round_gates.check_task_resource_budget(
        task,
        state,
        round_num=127,
        cfg=cfg,
        messages=messages,
    ) is False
    assert messages == []
    assert events == []

    state.api_rounds.append({})
    assert round_gates.check_task_resource_budget(
        task,
        state,
        round_num=128,
        cfg=cfg,
        messages=messages,
    ) is False
    assert len(messages) == 1
    assert messages[0]['role'] == 'user'
    assert messages[0]['_isMeta'] is True
    assert '128/192 used; 64 remain' in messages[0]['content']
    assert task['_apiRoundFinalizationReminder'] == {
        'used': 128,
        'hardLimit': 192,
        'remaining': 64,
        'round': 128,
    }
    assert len(events) == 1
    assert events[0]['type'] == 'budget_warning'
    assert events[0]['limit'] == 'apiRounds'
    assert events[0]['remaining'] == 64.0

    assert round_gates.check_task_resource_budget(
        task,
        state,
        round_num=129,
        cfg=cfg,
        messages=messages,
    ) is False
    assert len(messages) == 1
    assert len(events) == 1


def test_hard_round_limit_stops_before_another_model_call(monkeypatch):
    import lib.tasks_pkg.orchestrator._round_gates as round_gates

    events = []
    monkeypatch.setattr(
        round_gates,
        'append_event',
        lambda _task, event: events.append(event),
    )
    task = {'id': 'task-hard-round-cap'}
    state = _round_state(192)
    messages = []

    assert round_gates.check_task_resource_budget(
        task,
        state,
        round_num=192,
        cfg={'maxApiRounds': 192},
        messages=messages,
    ) is True
    assert messages == []
    assert events == []
    assert state.last_finish_reason == 'budget_exceeded'
    assert state.exit_reason == 'budget_exceeded_apiRounds_round_192'
    assert task['error']['code'] == 'task_budget_exceeded'
    assert task['error']['budget'] == {
        'limit': 'apiRounds',
        'used': 192,
        'hardLimit': 192.0,
        'remaining': 0.0,
        'unit': 'rounds',
        'remainingBudget': {'apiRounds': 0.0},
    }
