"""Terminal diagnostics retain independent failure domains and bounded labels."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.orchestrator import _completion_observers as observers

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('fault', ['experiment', 'metric', 'summary'])
def test_observer_failure_does_not_skip_later_observers_or_change_outcome(monkeypatch, fault):
    import lib.cost_experiments.cost_experiments as experiments
    import lib.observability as metrics

    calls = []
    outcome = {'experiment_id': 'experiment', 'arm': 'candidate'}
    task = {'id': 'observation-task', 'status': 'running', 'content': 'paid answer'}
    event = {'type': 'done', 'finishReason': 'stop', 'apiRounds': [{'round': 1}]}

    def stage(name, result=None):
        def observe(*args, **kwargs):
            calls.append(name)
            if fault == name:
                raise OSError('observer unavailable')
            return result
        return observe

    monkeypatch.setattr(experiments, 'build_cost_experiment_outcome', stage('experiment', outcome))
    monkeypatch.setattr(metrics, 'record_llm_task', stage('metric'))
    monkeypatch.setattr(observers.logger, 'info', stage('summary'))
    observers.observe_completion(task, event, model='model', finish_reason='stop')

    assert calls == ['experiment', 'metric', 'summary']
    assert task['status'] == 'running' and task['content'] == 'paid answer'
    assert event['type'] == 'done' and event['finishReason'] == 'stop'
    if fault == 'experiment':
        assert 'costExperiment' not in task and 'costExperiment' not in event
    else:
        assert task['costExperiment'] == event['costExperiment'] == outcome
        assert event['apiRounds'][0]['costExperiment'] == outcome


@pytest.mark.parametrize(('state', 'expected'), [
    ({}, 'success'),
    ({'error': {'code': 'provider_error'}}, 'error'),
    ({'aborted': True}, 'aborted'),
    ({'aborted': True, 'error': {'code': 'execution_budget'}}, 'budget'),
])
def test_terminal_metric_uses_settled_outcome_and_cost(monkeypatch, state, expected):
    import lib.cost_experiments.cost_experiments as experiments
    import lib.observability as metrics

    monkeypatch.setattr(experiments, 'build_cost_experiment_outcome', lambda *args, **kwargs: None)
    observed = []
    monkeypatch.setattr(metrics, 'record_llm_task', lambda *args: observed.append(args))
    task = {'id': 'metric-task', 'model': 'initial', 'provider_id': 'provider', **state}
    event = {'model': 'effective', 'usage': {'prompt_tokens': 3},
             'cost': {'costUsd': 0.25}, 'apiRounds': [{'round': 1}, {'round': 2}]}
    before = deepcopy((task, event))
    observers.observe_completion(task, event, model='initial', finish_reason='stop')

    assert len(observed) == 1
    model, provider, _elapsed, rounds, usage, cost, outcome = observed[0]
    assert (model, provider, rounds, usage, cost, outcome) == (
        'effective', 'provider', 2, {'prompt_tokens': 3}, 0.25, expected,
    )
    assert (task, event) == before
