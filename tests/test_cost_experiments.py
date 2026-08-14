"""Safety, accounting, and aggregation contracts for the cost A/B channel."""

from __future__ import annotations

import json
import time

import pytest

from lib.cost_experiments import (
    CostExperimentTransitionError,
    aggregate_cost_experiment_rows,
    apply_cost_experiment,
    assign_cost_experiment,
    build_cost_experiment_outcome,
    build_task_cost_experiment_outcome,
    normalize_cost_experiment_config,
    validate_cost_experiment_transition,
)

pytestmark = pytest.mark.unit


def _enabled_config(**overrides):
    raw = {
        'enabled': True,
        'experiment_id': 'context-cost-v1',
        'traffic_percent': 100,
        'treatment_percent': 50,
        'min_sample_size': 20,
    }
    raw.update(overrides)
    return normalize_cost_experiment_config(raw)


def _conv_for_arm(config, wanted):
    for index in range(10_000):
        conv_id = f'conv-{index}'
        assignment = assign_cost_experiment(config, conv_id)
        if assignment.get('arm') == wanted:
            return conv_id
    raise AssertionError(f'no deterministic bucket found for {wanted}')


def test_default_config_is_inert_and_declares_both_policies():
    cfg = normalize_cost_experiment_config({})
    assert cfg['enabled'] is False
    assert cfg['traffic_percent'] == 10
    assert cfg['arms']['control'] == {
        'mcpToolExposure': 'inline',
        'workingSetTokens': 0,
    }
    assert cfg['arms']['optimized'] == {
        'mcpToolExposure': 'auto',
        'workingSetTokens': 128_000,
    }


def test_low_code_input_cannot_inject_arbitrary_arm_settings():
    cfg = normalize_cost_experiment_config({
        'enabled': True,
        'arms': {
            'optimized': {'model': 'expensive-surprise',
                          'workingSetTokens': 1},
        },
    }, strict=True)
    assert cfg['arms']['optimized'] == {
        'mcpToolExposure': 'auto',
        'workingSetTokens': 128_000,
    }
    assert 'model' not in cfg['arms']['optimized']


def test_saved_experiment_requires_new_id_for_routing_changes():
    previous = _enabled_config(traffic_percent=10, treatment_percent=50)
    with pytest.raises(CostExperimentTransitionError):
        validate_cost_experiment_transition(
            previous, _enabled_config(traffic_percent=20))
    with pytest.raises(CostExperimentTransitionError):
        validate_cost_experiment_transition(
            previous, _enabled_config(treatment_percent=60))

    validate_cost_experiment_transition(
        previous, _enabled_config(traffic_percent=10, min_sample_size=100))
    validate_cost_experiment_transition(
        previous, _enabled_config(experiment_id='context-cost-v2',
                                  traffic_percent=20,
                                  treatment_percent=60))


def test_server_config_rejects_rebucket_and_keeps_file_unchanged(
        flask_client, monkeypatch, tmp_path):
    import routes.config as config_routes
    from lib.json_store import read_json, write_json_atomic

    path = tmp_path / 'server_config.json'
    previous = _enabled_config(traffic_percent=10, treatment_percent=50)
    write_json_atomic(str(path), {'cost_experiment': previous})
    monkeypatch.setattr(config_routes, '_SERVER_CONFIG_PATH', str(path))

    rejected = flask_client.post('/api/v1/server-config', json={
        'cost_experiment': {
            **previous,
            'traffic_percent': 20,
        },
    })
    assert rejected.status_code == 400
    assert read_json(str(path))['cost_experiment'] == previous

    accepted = flask_client.post('/api/v1/server-config', json={
        'cost_experiment': {
            **previous,
            'experiment_id': 'context-cost-v2',
            'traffic_percent': 20,
        },
    })
    assert accepted.status_code == 200
    assert read_json(str(path))['cost_experiment']['experiment_id'] == (
        'context-cost-v2')


def test_disabled_experiment_is_a_byte_for_byte_config_noop():
    request_cfg = {'model': 'kimi-k3', 'compaction': {'method': 'summary'}}
    task = {'convId': 'conv-disabled'}
    result = apply_cost_experiment(
        task, request_cfg,
        experiment_config=normalize_cost_experiment_config({}),
    )
    assert result is request_cfg
    assert task.get('_costExperiment') is None


def test_assignment_is_sticky_and_applies_only_the_selected_arm():
    exp = _enabled_config()
    control_id = _conv_for_arm(exp, 'control')
    optimized_id = _conv_for_arm(exp, 'optimized')

    assert assign_cost_experiment(exp, control_id) == assign_cost_experiment(
        exp, control_id)

    control_task = {'convId': control_id}
    control_cfg = apply_cost_experiment(
        control_task, {'model': 'kimi-k3'}, experiment_config=exp)
    assert control_cfg['mcpToolExposure'] == 'inline'
    assert control_cfg['compaction']['workingSetTokens'] == 0
    assert control_task['_costExperiment']['arm'] == 'control'

    optimized_task = {'convId': optimized_id}
    optimized_cfg = apply_cost_experiment(
        optimized_task, {'model': 'kimi-k3'}, experiment_config=exp)
    assert optimized_cfg['mcpToolExposure'] == 'auto'
    assert optimized_cfg['compaction']['workingSetTokens'] == 128_000
    assert optimized_task['_costExperiment']['arm'] == 'optimized'


def test_manual_request_policy_is_excluded_instead_of_overwritten():
    exp = _enabled_config()
    request_cfg = {
        'mcpToolExposure': 'progressive',
        'compaction': {'workingSetTokens': 96_000},
    }
    task = {'convId': 'manual-policy'}
    result = apply_cost_experiment(
        task, request_cfg, experiment_config=exp)
    assert result is request_cfg
    assert task['_costExperiment']['status'] == 'excluded'
    assert task['_costExperiment']['reason'] == 'request_override'


def test_outcome_uses_persisted_provider_usage_and_price_snapshot():
    assignment = {
        'experiment_id': 'context-cost-v1',
        'arm': 'optimized',
        'status': 'assigned',
    }
    usage = {
        'prompt_tokens': 10_000,
        'completion_tokens': 500,
        'cache_read_tokens': 6_000,
        'cache_write_tokens': 1_000,
    }
    cost = {
        'costUsd': 0.12,
        'costCny': 0.87,
        'cacheSavingsUsd': 0.03,
        'totalInputTokens': 10_000,
        'inputTokens': 3_000,
        'outputTokens': 500,
        'cacheReadTokens': 6_000,
        'cacheWriteTokens': 1_000,
    }
    outcome = build_cost_experiment_outcome(
        assignment,
        usage=usage,
        cost=cost,
        api_rounds=[{'round': 1}, {'round': 2}],
        finish_reason='stop',
        error=None,
        elapsed_ms=1_234,
        compactions=1,
    )
    assert outcome['metrics']['costUsd'] == 0.12
    assert outcome['metrics']['promptTokens'] == 10_000
    assert outcome['metrics']['cacheReadTokens'] == 6_000
    assert outcome['metrics']['rounds'] == 2
    assert outcome['quality']['terminalWithoutError'] is True
    assert outcome['quality']['compactions'] == 1
    assert outcome['latencyMs'] == 1_234


def test_unknown_model_default_estimate_is_unpriced_not_zero_or_real_cost():
    from lib.cost import compute_cost

    snapshot = compute_cost(
        {'prompt_tokens': 10_000, 'completion_tokens': 100},
        model_id='definitely-unknown-cost-model',
    )
    assert snapshot['pricingSource'] == 'default_estimate'
    assert snapshot['costUsd'] > 0  # legacy display behavior is preserved
    outcome = build_cost_experiment_outcome(
        {'experiment_id': 'context-cost-v1', 'arm': 'control',
         'status': 'assigned'},
        usage={'prompt_tokens': 10_000, 'completion_tokens': 100},
        cost=snapshot,
        api_rounds=[], finish_reason='stop', error=None, elapsed_ms=10,
        model='definitely-unknown-cost-model',
    )
    assert outcome['metrics']['costUsd'] is None
    assert outcome['metrics']['pricingSource'] == 'default_estimate'


def test_outcome_uses_precise_components_instead_of_rounded_display_total():
    outcome = build_cost_experiment_outcome(
        {'experiment_id': 'context-cost-v1', 'arm': 'optimized',
         'status': 'assigned'},
        usage={'prompt_tokens': 10, 'completion_tokens': 1},
        cost={
            'costUsd': 0.0,
            'costCny': 0.0001,
            'pricingSource': 'model_table',
            'inputCostUsd': 0.000009,
            'outputCostUsd': 0.000002,
            'cacheWriteCostUsd': 0.0,
            'cacheReadCostUsd': 0.0,
            'inputCostCny': 0.000065,
            'outputCostCny': 0.000014,
            'cacheWriteCostCny': 0.0,
            'cacheReadCostCny': 0.0,
        },
        api_rounds=[], finish_reason='stop', error=None, elapsed_ms=10,
        model='gpt-4o',
    )
    assert outcome['metrics']['costUsd'] == pytest.approx(0.000011)
    assert outcome['metrics']['costCny'] == pytest.approx(0.000079)


def test_task_outcome_exists_before_task_result_persistence():
    task = {
        '_costExperiment': {
            'experiment_id': 'context-cost-v1', 'arm': 'control',
            'status': 'assigned',
        },
        'created_at': time.time() - 0.01,
        'usage': {'prompt_tokens': 100, 'completion_tokens': 5},
        'model': 'gpt-4o',
        'provider_id': 'openai',
        'finishReason': 'stop',
        'apiRounds': [{'round': 1}],
    }
    outcome = build_task_cost_experiment_outcome(task)
    assert outcome['metrics']['costUsd'] is not None
    assert outcome['metrics']['rounds'] == 1
    assert outcome['quality']['terminalWithoutError'] is True

    from lib.tasks_pkg.manager._persist import build_result_meta
    meta = build_result_meta(task)
    assert meta['costExperiment']['metrics']['costUsd'] is not None
    assert task['costExperiment'] == meta['costExperiment']


def test_turn_prelude_fails_open_if_experiment_observer_breaks(monkeypatch):
    import lib.cost_experiments as experiment_module
    from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude

    def broken_observer(_task, _cfg):
        raise RuntimeError('observer unavailable')

    monkeypatch.setattr(
        experiment_module, 'apply_cost_experiment', broken_observer)
    cfg = {'_swarmAutoContinue': True, 'model': 'gpt-4o'}
    task = {'config': cfg}
    result = run_turn_prelude(task, cfg, 'deadbeef')
    assert result is cfg
    assert task['config'] is cfg


def test_report_keeps_real_cost_coverage_and_conversation_sample_unit():
    now_ms = int(time.time() * 1000)

    def message(arm, cost, *, priced=True, latency=1000):
        metrics = {
            'costUsd': cost if priced else None,
            'costCny': cost * 7 if priced else None,
            'promptTokens': 1000,
            'uncachedInputTokens': 500,
            'outputTokens': 100,
            'cacheReadTokens': 500,
            'cacheWriteTokens': 0,
            'rounds': 2,
        }
        return {
            'role': 'assistant',
            'costExperiment': {
                'experiment_id': 'context-cost-v1',
                'arm': arm,
                'status': 'assigned',
                'completedAt': now_ms,
                'latencyMs': latency,
                'metrics': metrics,
                'quality': {
                    'terminalWithoutError': True,
                    'compactions': 0,
                },
            },
        }

    rows = [
        {'id': 'c1', 'updated_at': now_ms,
         'messages': json.dumps([message('control', 0.20)])},
        {'id': 'c2', 'updated_at': now_ms,
         'messages': json.dumps([message('optimized', 0.10)])},
        {'id': 'c3', 'updated_at': now_ms,
         'messages': json.dumps([message('optimized', 0.14, priced=False)])},
    ]
    report = aggregate_cost_experiment_rows(
        rows, experiment_id='context-cost-v1', days=14,
        now_ms=now_ms, min_sample_size=20)
    assert report['arms']['control']['conversations'] == 1
    assert report['arms']['optimized']['conversations'] == 2
    assert report['arms']['optimized']['fullyPricedConversations'] == 1
    assert report['arms']['optimized']['turns'] == 2
    assert report['arms']['optimized']['pricedTurns'] == 1
    assert report['arms']['optimized']['unpricedTurns'] == 1
    assert report['arms']['optimized']['costPerPricedTurnUsd'] == 0.10
    assert report['arms']['optimized'][
        'costPerFullyPricedConversationUsd'] == 0.10
    assert report['comparison']['costPerConversationDeltaPct'] == -50.0
    assert report['comparison']['costPerPricedTurnDeltaPct'] == -50.0
    assert report['ready'] is False


@pytest.mark.parametrize('field,value', [
    ('traffic_percent', -1),
    ('traffic_percent', 101),
    ('treatment_percent', 101),
    ('min_sample_size', 0),
])
def test_invalid_low_code_controls_are_rejected(field, value):
    with pytest.raises(ValueError):
        normalize_cost_experiment_config({field: value}, strict=True)
