"""Retained failed calls use production evidence and the same price rules as success."""
import json

import pytest

from evaluations.long_agent_release.run_store import (
    ReleaseRunError, audit_release_attempts, claim_release_task_attempts,
    fail_release_task_attempt, initialize_release_run, record_release_task,
    release_task_retry_evidence, store_run_artifact,
)
from lib.agent_core.execution_budget import ExecutionBudget, model_budget_admission
from lib.agent_core.run_contract import project_agent_evidence
from lib.benchmark_contract import public_price_cost_from_usage
from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
from tests.benchmark_contract.test_long_agent_release_run_store import (
    _manifest, _store_artifacts, _task,
)

pytestmark = pytest.mark.unit
_TASK_ID = 'pilot:task-1'


def _start(tmp_path, role='candidate'):
    root = tmp_path / 'run'
    manifest = _manifest(role)
    initialize_release_run(root, manifest)
    claim = claim_release_task_attempts(
        root, task_ids=[_TASK_ID], execution_id='execution-1', runner_kind='fixture-runner')
    return root, manifest, claim['claims'][0]['occurredAt']


def _production_compaction_task():
    main = {'prompt_tokens': 21, 'completion_tokens': 4, 'cache_read_tokens': 7}
    compact = {'prompt_tokens': 9, 'completion_tokens': 2, 'cache_read_tokens': 0}
    task = {'id': 'failed-task', '_userId': 7, 'model': 'kimi-k3', 'status': 'error',
            'config': {}, 'usage': dict(main), 'apiRounds': [{'round': 1, 'usage': main}],
            '_pendingCompactionUsage': {**compact, 'n_calls': 1, 'calls': [
                {'kind': 'summary', 'model': 'kimi-k3', 'usage': compact}]}}
    settle_compaction_usage(task)
    return task


def _close_candidate(tmp_path, root, started, evidence):
    raw = tmp_path / 'failed.jsonl'
    raw.write_text(json.dumps({'type': 'error', 'usage': evidence['usage']}) + '\n')
    runtime = tmp_path / 'runtime.json'
    runtime.write_text(json.dumps(evidence))
    artifacts = [store_run_artifact(root, task_id=_TASK_ID, kind=kind, source=path)
                 for kind, path in [('failed_attempt_trajectory', raw),
                                    ('failed_attempt_runtime_evidence', runtime)]]
    return fail_release_task_attempt(
        root, task_id=_TASK_ID, execution_id='execution-1', code='stream_truncated',
        model_usages=[row['usage'] for row in evidence['apiRounds']], paid_tool_cost_usd=0,
        artifacts=artifacts, no_paid_calls=False, task_started_at_unix_ms=started)


def test_production_failed_compaction_is_retained_and_charged_once(tmp_path):
    root, manifest, started = _start(tmp_path)
    task = _production_compaction_task()
    evidence = project_agent_evidence(task, model='kimi-k3')
    _close_candidate(tmp_path, root, started, evidence)
    retries = release_task_retry_evidence(root, task_id=_TASK_ID)
    usages = retries[0]['modelUsages']
    assert len(usages) == 2
    assert sum(u['prompt_tokens'] for u in usages) == 30
    assert sum(u['completion_tokens'] for u in usages) == 6
    assert sum(public_price_cost_from_usage(u, manifest['priceCard'])['costUsd']
               for u in usages) == pytest.approx(0.000148272)
    assert audit_release_attempts(root)['openAttempts'] == 0


@pytest.mark.parametrize('drift', ['double_aggregate', 'missing_round', 'summary', 'call_detail', 'model', 'physical_model', 'detail_model', 'main_model', 'main_physical_model'])
def test_failed_compaction_rejects_missing_or_double_counted_evidence(tmp_path, drift):
    root, _, started = _start(tmp_path)
    evidence = project_agent_evidence(_production_compaction_task(), model='kimi-k3')
    if drift == 'double_aggregate':
        evidence['usage']['prompt_tokens'] += 9
        evidence['usage']['completion_tokens'] += 2
    elif drift == 'missing_round':
        evidence['apiRounds'].pop()
    elif drift == 'summary':
        evidence['compactionUsage']['prompt_tokens'] += 1
    elif drift == 'call_detail':
        evidence['compactionUsage']['calls'][0]['usage']['prompt_tokens'] += 1
    elif drift == 'main_model':
        evidence['apiRounds'][0]['model'] = 'another-model'
    elif drift == 'main_physical_model':
        evidence['apiRounds'][0]['usage']['_dispatch'] = {'model': 'another-model'}
    elif drift == 'detail_model':
        evidence['compactionUsage']['calls'][0]['model'] = 'another-model'
    elif drift == 'model':
        evidence['apiRounds'][-1]['model'] = 'another-model'
    else:
        evidence['apiRounds'][-1]['usage']['_dispatch'] = {'model': 'another-model'}
    with pytest.raises(ReleaseRunError):
        _close_candidate(tmp_path, root, started, evidence)
    assert audit_release_attempts(root)['openAttempts'] == 1


@pytest.mark.parametrize('condition', ['known_unknown', 'pending', 'lost_paid_round'])
def test_failed_usage_cannot_hide_incomplete_execution_account(tmp_path, condition):
    root, _, started = _start(tmp_path)
    task = _production_compaction_task()
    lease = None
    if condition == 'known_unknown':
        task['_executionActualsComplete'] = False
    else:
        task['config']['executionLimits'] = {
            'max_tokens': 1000, 'max_tool_calls': None, 'deadline': None}
        if condition == 'pending':
            task['status'] = 'running'
            lease = model_budget_admission(task)(input_tokens=10, max_output_tokens=10)
            lease.mark_dispatched()
            task['status'] = 'error'
        else:
            task['_executionBudget'] = ExecutionBudget(task)
            task['apiRounds'].pop()
            task['usage'] = dict(task['apiRounds'][0]['usage'])
            task['compactionUsage'] = {}
    try:
        evidence = project_agent_evidence(task, model='kimi-k3')
        with pytest.raises(ReleaseRunError, match='account'):
            _close_candidate(tmp_path, root, started, evidence)
        assert audit_release_attempts(root)['openAttempts'] == 1
    finally:
        if lease is not None:
            lease.settle({})


def test_failed_responses_cache_is_repriced_before_immutable_task_commit(tmp_path):
    root, manifest, started = _start(tmp_path, 'baseline')
    usage = {'input_tokens': 1000, 'output_tokens': 100,
             'input_tokens_details': {'cached_tokens': 600},
             'output_tokens_details': {'reasoning_tokens': 20}}
    raw = tmp_path / 'failed.jsonl'
    raw.write_text('{"type":"error"}\n')
    metrics = tmp_path / 'failed-metrics.jsonl'
    metrics.write_text(json.dumps({
        'event': 'responsesTranslation', 'trialToken': 'fixture-trial', 'status': 'completed',
        'upstreamCalls': 1, 'invalidTrial': False, 'clientDisconnected': False,
        'translationCpuNs': 0, 'proxyCpuNs': 0, 'rawWallNs': 10, 'usage': usage,
    }) + '\n')
    artifacts = [store_run_artifact(root, task_id=_TASK_ID, kind=kind, source=path)
                 for kind, path in [('failed_attempt_trajectory', raw),
                                    ('failed_attempt_proxy_metrics', metrics)]]
    fail_release_task_attempt(
        root, task_id=_TASK_ID, execution_id='execution-1', code='stream_truncated',
        model_usages=[usage], paid_tool_cost_usd=0, artifacts=artifacts,
        no_paid_calls=False, task_started_at_unix_ms=started)
    retry = claim_release_task_attempts(
        root, task_ids=[_TASK_ID], execution_id='execution-2', runner_kind='fixture-runner')
    record = _task(manifest, _store_artifacts(tmp_path, root, baseline=True) + artifacts)
    record['retries'] = release_task_retry_evidence(root, task_id=_TASK_ID)
    second_started = retry['claims'][0]['occurredAt']
    record['completedAt'] = second_started + 100
    wall = record['completedAt'] - started
    record['latency'].update(rawWallMs=wall, oracleReadyMs=wall, codexFavoredCorrectedWallMs=wall - 10)
    record['environment'] = {'releaseAttempt': {'taskStartedAtUnixMs': second_started}}
    correct_cost = record['cost']['modelCostUsd'] + 0.0026506
    record['cost'].update(modelCostUsd=correct_cost, agentCostUsd=correct_cost)
    wrong = {**record, 'cost': {**record['cost'], 'modelCostUsd': correct_cost + 0.001,
                              'agentCostUsd': correct_cost + 0.001}}
    with pytest.raises(ReleaseRunError, match='priced evidence'):
        record_release_task(root, wrong)
    record_release_task(root, record)
    assert audit_release_attempts(root, require_complete=True)['complete'] is True
