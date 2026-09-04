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
    load_cost_experiment_config,
    normalize_cost_experiment_config,
    task_outcome_report_rows,
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
        assignment = assign_cost_experiment(config, conv_id, owner_id=1)
        if assignment.get('arm') == wanted:
            return conv_id
    raise AssertionError(f'no deterministic bucket found for {wanted}')


def test_default_config_is_inert_and_declares_both_policies():
    cfg = normalize_cost_experiment_config({})
    assert cfg['enabled'] is False
    assert cfg['lifecycle'] == 'draft'
    assert cfg['started_at_ms'] == 0
    assert cfg['sealed_at_ms'] == 0
    assert cfg['traffic_percent'] == 10
    assert cfg['arms']['control'] == {
        'mcpToolExposure': 'inline',
        'workingSetTokens': 0,
    }
    assert cfg['arms']['optimized'] == {
        'mcpToolExposure': 'auto',
        'workingSetTokens': 128_000,
    }
    assert cfg['contract_version'] == 'tofu.experiment/v1'
    assert len(cfg['spec_digest']) == 64


def test_capability_catalog_exposes_plugin_metadata_not_callbacks(flask_client):
    response = flask_client.get('/api/v1/experiments/capabilities')
    assert response.status_code == 200
    body = response.get_json()
    assert body['contractVersion'] == 'tofu.experiment-plugin-catalog/v1'
    plugin = next(row for row in body['plugins']
                  if row['pluginId'] == 'tofu.context-cost')
    assert {row['strategyId'] for row in plugin['strategies']} == {
        'control', 'optimized'}
    assert 'apply' not in str(plugin)


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


def test_persisted_spec_drift_disables_reads_and_rejects_writes():
    drifted = {
        'enabled': True,
        'experiment_id': 'context-cost-v1',
        'spec_digest': '0' * 64,
    }
    safe = normalize_cost_experiment_config(drifted)
    assert safe['enabled'] is False
    assert safe['invalid_reason'] == 'strategy_spec_changed'
    with pytest.raises(ValueError, match='choose a new experiment_id'):
        normalize_cost_experiment_config(drifted, strict=True)


def test_saved_experiment_requires_new_id_for_routing_changes():
    previous = _enabled_config(traffic_percent=10, treatment_percent=50)
    with pytest.raises(CostExperimentTransitionError):
        validate_cost_experiment_transition(
            previous, _enabled_config(traffic_percent=20))
    with pytest.raises(CostExperimentTransitionError):
        validate_cost_experiment_transition(
            previous, _enabled_config(treatment_percent=60))

    with pytest.raises(CostExperimentTransitionError):
        validate_cost_experiment_transition(
            previous, _enabled_config(traffic_percent=10, min_sample_size=100))
    validate_cost_experiment_transition(
        previous, _enabled_config(experiment_id='context-cost-v2',
                                  traffic_percent=20,
                                  treatment_percent=60))


def test_running_experiment_seals_once_and_cannot_restart_same_id():
    running = _enabled_config()
    sealed = validate_cost_experiment_transition(
        running, {**running, 'enabled': False, 'lifecycle': 'running'},
        now_ms=123_456)
    assert sealed['enabled'] is False
    assert sealed['lifecycle'] == 'sealed'
    assert sealed['sealed_at_ms'] == 123_456

    with pytest.raises(CostExperimentTransitionError, match='cannot be restarted'):
        validate_cost_experiment_transition(
            sealed, {**sealed, 'enabled': True, 'lifecycle': 'draft'})
    restarted = validate_cost_experiment_transition(
        sealed, {
            **sealed, 'enabled': True, 'lifecycle': 'draft',
            'experiment_id': 'context-cost-v2',
        })
    assert restarted['lifecycle'] == 'running'


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
    assert read_json(str(path))['cost_experiment']['started_at_ms'] > 0

    running_v2 = read_json(str(path))['cost_experiment']
    forged_clock = flask_client.post('/api/v1/server-config', json={
        'cost_experiment': {
            **running_v2, 'started_at_ms': 1, 'sealed_at_ms': 999,
        },
    })
    assert forged_clock.status_code == 200
    assert read_json(str(path))['cost_experiment']['started_at_ms'] == (
        running_v2['started_at_ms'])
    assert read_json(str(path))['cost_experiment']['sealed_at_ms'] == 0

    stopped = flask_client.post('/api/v1/server-config', json={
        'cost_experiment': {**running_v2, 'enabled': False},
    })
    assert stopped.status_code == 200
    sealed_v2 = read_json(str(path))['cost_experiment']
    assert sealed_v2['lifecycle'] == 'sealed'
    assert sealed_v2['sealed_at_ms'] >= sealed_v2['started_at_ms']

    rejected_restart = flask_client.post('/api/v1/server-config', json={
        'cost_experiment': {**sealed_v2, 'enabled': True, 'lifecycle': 'draft'},
    })
    assert rejected_restart.status_code == 400
    assert read_json(str(path))['cost_experiment'] == sealed_v2


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

    assert assign_cost_experiment(
        exp, control_id, owner_id=1
    ) == assign_cost_experiment(exp, control_id, owner_id=1)

    control_task = {'convId': control_id, '_userId': 1}
    control_cfg = apply_cost_experiment(
        control_task, {'model': 'kimi-k3'}, experiment_config=exp)
    assert control_cfg['mcpToolExposure'] == 'inline'
    assert control_cfg['compaction']['workingSetTokens'] == 0
    assert control_task['_costExperiment']['arm'] == 'control'

    optimized_task = {'convId': optimized_id, '_userId': 1}
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
    task = {'convId': 'manual-policy', '_userId': 1}
    result = apply_cost_experiment(
        task, request_cfg, experiment_config=exp)
    assert result is request_cfg
    assert task['_costExperiment']['status'] == 'excluded'
    assert task['_costExperiment']['reason'] == 'request_override'


def test_enabled_experiment_requires_explicit_owner_identity():
    request_cfg = {'model': 'kimi-k3'}
    task = {'convId': 'ownerless'}
    result = apply_cost_experiment(
        task, request_cfg, experiment_config=_enabled_config())
    assert result is request_cfg
    assert task['_costExperiment']['status'] == 'excluded'
    assert task['_costExperiment']['reason'] == 'missing_owner_identity'


def test_turn_hot_path_compiles_the_pinned_application_plan_once(monkeypatch):
    import lib.cost_experiments as experiment_module

    exp = _enabled_config()
    original = experiment_module.compile_experiment_application
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        experiment_module, 'compile_experiment_application', counted)
    with experiment_module._APPLICATION_CACHE_LOCK:
        experiment_module._APPLICATION_CACHE.update({'key': None, 'apply': None})
    for index in range(2):
        task = {'convId': f'compiled-plan-{index}', '_userId': 1}
        apply_cost_experiment(task, {}, experiment_config=exp)
    assert calls == 1


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


def test_task_outcome_report_rows_mirror_the_conversation_scan_shape():
    now_ms = int(time.time() * 1000)
    outcome = {
        'experiment_id': 'context-cost-v1',
        'arm': 'optimized',
        'status': 'assigned',
        'completedAt': now_ms,
        'metrics': {'costUsd': 0.10, 'promptTokens': 1000},
        'quality': {'terminalWithoutError': True},
    }
    records = [
        {'task_id': 't1', 'conv_id': 'c1', 'completed_at': now_ms,
         'outcome': outcome},
        # The legacy SQL path hands the outcome over as a JSON string.
        {'task_id': 't2', 'conv_id': 'c2', 'completed_at': now_ms,
         'outcome': json.dumps({**outcome, 'arm': 'control',
                                'metrics': {'costUsd': 0.20}})},
        # Malformed / empty records are counted, never fatal.
        {'task_id': 't3', 'conv_id': 'c3', 'completed_at': now_ms,
         'outcome': '{broken json'},
        {'task_id': 't4', 'conv_id': 'c4', 'completed_at': now_ms,
         'outcome': None},
        'not-a-record',
    ]
    rows, invalid = task_outcome_report_rows(records)
    assert invalid == 3
    assert len(rows) == 2
    assert rows[0]['id'] == 'c1'
    assert rows[0]['updated_at'] == now_ms
    assert rows[0]['messages'][0]['role'] == 'assistant'
    assert rows[0]['messages'][0]['costExperiment'] is outcome

    # End-to-end: the aggregator consumes the projected rows unchanged.
    report = aggregate_cost_experiment_rows(
        rows, experiment_id='context-cost-v1', days=14,
        now_ms=now_ms, min_sample_size=20)
    assert report['arms']['optimized']['turns'] == 1
    assert report['arms']['optimized']['conversations'] == 1
    assert report['arms']['control']['turns'] == 1
    assert report['arms']['control']['costPerPricedTurnUsd'] == 0.20


def test_load_cost_experiment_config_reparse_only_on_mtime_change(
        monkeypatch, tmp_path):
    import lib.cost_experiments as experiment_module
    from lib.json_store import write_json_atomic

    path = tmp_path / 'server_config.json'
    write_json_atomic(str(path), {'cost_experiment': {'enabled': False}})
    monkeypatch.setattr(
        experiment_module, 'config_path', lambda _name: str(path))
    monkeypatch.setattr(
        experiment_module, '_CONFIG_CACHE',
        {'mtime_ns': None, 'config': None})

    first = load_cost_experiment_config()
    assert first['enabled'] is False
    first['arms']['control']['mcpToolExposure'] = 'mutated-by-caller'

    # Same mtime → cached value even if the bytes underneath changed
    # (the atomic writer never does this; it always bumps the mtime).
    stat = path.stat()
    path.write_text(json.dumps(
        {'cost_experiment': {'enabled': True, 'traffic_percent': 55}}))
    import os
    os.utime(str(path), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    cached = load_cost_experiment_config()
    assert cached['enabled'] is False
    assert cached['arms']['control']['mcpToolExposure'] == 'inline'

    # New mtime → re-parse.
    os.utime(str(path), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    refreshed = load_cost_experiment_config()
    assert refreshed['enabled'] is True
    assert refreshed['traffic_percent'] == 55

    # A vanished file fails safe to the inert defaults.
    path.unlink()
    assert load_cost_experiment_config()['enabled'] is False


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
    assert report['comparison']['pointEstimateOptimizedCheaper'] is True
    assert report['comparison']['optimizedIsCheaper'] is False
    assert 'incomplete_pricing' in report['decision']['blockers']
    assert report['ready'] is False


def test_versioned_end_to_end_report_promotes_only_complete_valid_evidence():
    now_ms = int(time.time() * 1000)
    exp = _enabled_config(min_sample_size=10)
    rows = []
    arm_counts = {'control': 0, 'optimized': 0}
    index = 0
    per_arm_horizon = exp['spec']['analysis']['maximumAssignmentUnits'] // 2
    while min(arm_counts.values()) < per_arm_horizon:
        conv_id = f'promotion-conv-{index}'
        index += 1
        task = {'convId': conv_id, '_userId': 1}
        apply_cost_experiment(task, {}, experiment_config=exp)
        assignment = task.get('_costExperiment') or {}
        arm = assignment.get('arm')
        if arm not in arm_counts or arm_counts[arm] >= per_arm_horizon:
            continue
        assignment['exposedAt'] = now_ms - 10_000 + len(rows)
        arm_counts[arm] += 1
        cost = 0.20 if arm == 'control' else 0.10
        latency = 100 if arm == 'control' else 105
        outcome = build_cost_experiment_outcome(
            assignment,
            usage={'prompt_tokens': 1000, 'completion_tokens': 100},
            cost={'costUsd': cost, 'costCny': cost * 7,
                  'pricingSource': 'model_table'},
            api_rounds=[{'round': 1}], finish_reason='stop', error=None,
            elapsed_ms=latency, completed_at_ms=now_ms,
            oracle_passed=True, model='gpt-4o', provider_id='openai',
        )
        rows.append({
            'id': conv_id, 'updated_at': now_ms,
            'messages': [{'role': 'assistant', 'costExperiment': outcome}],
        })

    live = aggregate_cost_experiment_rows(
        rows, experiment_id=exp['experiment_id'], days=14, now_ms=now_ms,
        min_sample_size=10, experiment_spec=exp['spec'],
        analysis_start_ms=now_ms - 20_000)
    assert live['promotionEligible'] is False
    assert 'experiment_still_enrolling' in live['decision']['blockers']

    report = aggregate_cost_experiment_rows(
        rows, experiment_id=exp['experiment_id'], days=14, now_ms=now_ms,
        min_sample_size=10, experiment_spec=exp['spec'], analysis_closed=True,
        analysis_start_ms=now_ms - 20_000, analysis_sealed_ms=now_ms)
    assert report['decision']['status'] == 'promote'
    assert report['ready'] is True
    assert report['promotionEligible'] is True
    assert report['comparison']['optimizedIsCheaper'] is True

    # Later observations can remain descriptive, but can never replace the
    # precommitted first cohort or turn a fixed-horizon decision into peeking.
    for wanted_arm in ('control', 'optimized'):
        while True:
            conv_id = f'late-conv-{index}'
            index += 1
            task = {'convId': conv_id, '_userId': 1}
            apply_cost_experiment(task, {}, experiment_config=exp)
            assignment = task.get('_costExperiment') or {}
            if assignment.get('arm') == wanted_arm:
                break
        assignment['exposedAt'] = now_ms - 1_000 + len(rows)
        reversed_cost = 0.01 if wanted_arm == 'control' else 10.0
        late_outcome = build_cost_experiment_outcome(
            assignment,
            usage={'prompt_tokens': 1000, 'completion_tokens': 100},
            cost={'costUsd': reversed_cost, 'costCny': reversed_cost * 7,
                  'pricingSource': 'model_table'},
            api_rounds=[{'round': 1}], finish_reason='stop', error=None,
            elapsed_ms=100, completed_at_ms=now_ms, oracle_passed=True,
            model='gpt-4o', provider_id='openai',
        )
        rows.append({
            'id': conv_id, 'updated_at': now_ms,
            'messages': [{'role': 'assistant', 'costExperiment': late_outcome}],
        })
    first_outcome = rows[0]['messages'][0]['costExperiment']
    repeated_assignment = {
        key: first_outcome[key] for key in (
            'contractVersion', 'experimentId', 'experiment_id', 'specDigest',
            'assignmentUnit', 'assignmentAlgorithm', 'subjectDigest', 'status',
            'exposureStatus', 'arm', 'strategy', 'policy',
        ) if key in first_outcome
    }
    repeated_assignment['exposedAt'] = now_ms - 500
    repeated_outcome = build_cost_experiment_outcome(
        repeated_assignment,
        usage={'prompt_tokens': 1000, 'completion_tokens': 100},
        cost={'costUsd': 50.0, 'costCny': 350.0,
              'pricingSource': 'model_table'},
        api_rounds=[{'round': 1}], finish_reason='stop', error=None,
        elapsed_ms=500, completed_at_ms=now_ms, oracle_passed=False,
        model='gpt-4o', provider_id='openai',
    )
    rows[0]['messages'].append({
        'role': 'assistant', 'costExperiment': repeated_outcome,
    })
    stable = aggregate_cost_experiment_rows(
        rows, experiment_id=exp['experiment_id'], days=14, now_ms=now_ms,
        min_sample_size=10, experiment_spec=exp['spec'], analysis_closed=True,
        analysis_start_ms=now_ms - 20_000, analysis_sealed_ms=now_ms)
    assert stable['observedAssignmentUnits'] == (
        stable['maximumAssignmentUnits'] + 2)
    assert stable['analyzedAssignmentUnits'] == stable['maximumAssignmentUnits']
    assert stable['promotionEligible'] is True
    assert stable['comparison']['pointEstimateOptimizedCheaper'] is True
    assert stable['comparison'][
        'allObservedCostPerConversationDeltaPct'] > 0

    pending_task = {'convId': f'pending-conv-{index}', '_userId': 1}
    apply_cost_experiment(pending_task, {}, experiment_config=exp)
    pending_assignment = dict(pending_task['_costExperiment'])
    pending_assignment['exposedAt'] = now_ms - 19_999
    with_pending = aggregate_cost_experiment_rows(
        [*rows, {
            'id': pending_task['convId'], 'updated_at': now_ms,
            'messages': [{
                'role': 'assistant', 'costExperiment': pending_assignment,
            }],
        }],
        experiment_id=exp['experiment_id'], days=14, now_ms=now_ms,
        min_sample_size=10, experiment_spec=exp['spec'], analysis_closed=True,
        analysis_start_ms=now_ms - 20_000, analysis_sealed_ms=now_ms,
    )
    assert with_pending['promotionEligible'] is False
    assert with_pending['funnel']['pendingAnalysisExposures'] == 1
    assert 'pending_exposures' in with_pending['decision']['blockers']

    partial = aggregate_cost_experiment_rows(
        rows, experiment_id=exp['experiment_id'], days=14, now_ms=now_ms,
        min_sample_size=10, experiment_spec=exp['spec'], analysis_closed=True,
        analysis_start_ms=now_ms - 20_000, analysis_sealed_ms=now_ms,
        truncated=True)
    assert partial['promotionEligible'] is False
    assert partial['comparison']['pointEstimateOptimizedCheaper'] is True
    assert partial['comparison']['optimizedIsCheaper'] is False
    assert 'truncated_source' in partial['decision']['blockers']


def test_report_endpoint_reads_task_results_projection(flask_client,
                                                       monkeypatch):
    """The HTTP adapter uses the owner-scoped semantic storage operation."""
    import lib.storage as storage_module
    import routes.config as config_routes

    now_ms = int(time.time() * 1000)
    experiment_id = 'context-cost-e2e'

    def outcome(arm, cost):
        return {
            'experiment_id': experiment_id, 'arm': arm, 'status': 'assigned',
            'completedAt': now_ms, 'latencyMs': 900,
            'metrics': {'costUsd': cost, 'costCny': cost * 7,
                        'promptTokens': 1000, 'uncachedInputTokens': 500,
                        'outputTokens': 100, 'cacheReadTokens': 500,
                        'cacheWriteTokens': 0, 'rounds': 1},
            'quality': {'terminalWithoutError': True, 'compactions': 0},
        }

    records = [
        {'task_id': 'e2e-task-1', 'conv_id': 'e2e-control',
         'completed_at': now_ms, 'outcome': outcome('control', 0.20)},
        {'task_id': 'e2e-task-2', 'conv_id': 'e2e-optimized',
         'completed_at': now_ms, 'outcome': outcome('optimized', 0.10)},
    ]
    calls = []

    class _Storage:
        def query(self, operation, payload):
            calls.append((operation, payload))
            return {'records': records, 'invalid': 0, 'capped': False}

    monkeypatch.setattr(storage_module, 'get_storage_client', lambda: _Storage())

    monkeypatch.setattr(config_routes, '_read_server_config', lambda: {
        'cost_experiment': {
            'enabled': True, 'lifecycle': 'running',
            'experiment_id': experiment_id,
            'started_at_ms': now_ms - 20 * 86_400_000,
        },
    })
    resp = flask_client.get('/api/v1/cost-experiments/report?days=14')
    assert resp.status_code == 200
    report = resp.get_json()
    assert report['source'] == 'task_results'
    assert report['arms']['control']['turns'] == 1
    assert report['arms']['optimized']['turns'] == 1
    assert report['arms']['control']['costPerPricedTurnUsd'] == 0.20
    assert report['comparison']['costPerPricedTurnDeltaPct'] == -50.0
    assert calls[0][0] == 'task_results.cost_experiment_scan'
    assert calls[0][1]['user_id'] == 1
    assert calls[0][1]['experiment_id'] == experiment_id
    assert calls[0][1]['completed_at_gte'] == now_ms - 20 * 86_400_000


def test_cost_experiment_repository_resumes_bounded_storage_pages():
    """A cold legacy scan resumes pages instead of restarting one giant RPC."""
    from lib.cost_experiment_repository import scan_cost_experiment_outcomes

    calls = []

    class _Storage:
        def query(self, operation, payload):
            calls.append((operation, dict(payload)))
            if len(calls) == 1:
                return {
                    'records': [{
                        'task_id': 'older', 'conv_id': 'conv-a',
                        'completed_at': 100, 'outcome': {'arm': 'control'},
                    }],
                    'invalid': 1,
                    'scanned': 256,
                    'capped': False,
                    'exhausted': False,
                    'next_cursor': 'task-page-1',
                }
            return {
                'records': [{
                    'task_id': 'newer', 'conv_id': 'conv-b',
                    'completed_at': 200, 'outcome': {'arm': 'optimized'},
                }],
                'invalid': 2,
                'scanned': 11,
                'capped': False,
                'exhausted': True,
                'next_cursor': '',
            }

    result = scan_cost_experiment_outcomes(
        user_id=7,
        completed_at_gte=50,
        experiment_id='exp-v1',
        limit=5_000,
        storage_client=_Storage(),
    )

    assert [row['task_id'] for row in result['records']] == [
        'newer', 'older']
    assert result['invalid'] == 3
    assert result['scanned'] == 267
    assert result['capped'] is False
    assert len(calls) == 2
    assert calls[0][0] == 'task_results.cost_experiment_scan'
    assert calls[0][1]['user_id'] == 7
    assert calls[0][1]['after_key'] == ''
    assert calls[0][1]['scan_limit'] == 256
    assert calls[1][1]['after_key'] == 'task-page-1'


@pytest.mark.parametrize('field,value', [
    ('traffic_percent', -1),
    ('traffic_percent', 101),
    ('treatment_percent', 101),
    ('min_sample_size', 0),
    ('min_sample_size', 1),
    ('traffic_percent', 10.5),
])
def test_invalid_low_code_controls_are_rejected(field, value):
    with pytest.raises(ValueError):
        normalize_cost_experiment_config({field: value}, strict=True)


@pytest.mark.parametrize('treatment', [0, 100])
def test_enabled_two_arm_experiment_rejects_an_empty_arm(treatment):
    with pytest.raises(ValueError, match='between 1 and 99'):
        normalize_cost_experiment_config({
            'enabled': True, 'treatment_percent': treatment,
        }, strict=True)
    safe = normalize_cost_experiment_config({
        'enabled': True, 'treatment_percent': treatment,
    })
    assert safe['enabled'] is False
    assert safe['invalid_reason'] == 'empty_experiment_arm'
