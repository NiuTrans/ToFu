"""Accounting survives timeout/export loss without inventing free requests."""
import json

import pytest

from evaluations.swebench.usage_statistics import (
    observe_trial_usage, paired_statistics, summarize_usage, validate_reference_prices,
)
from evaluations.codex_kimi_proxy.codex_contract import TRIAL_HEADER

pytestmark = pytest.mark.unit
RATES = dict(inputUsdPerMillion=10, outputUsdPerMillion=20,
             cacheReadUsdPerMillion=1, cacheWriteUsdPerMillion=10)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _observe(root, trial):
    evidence = []
    out = observe_trial_usage(
        root, trial, read_json=lambda p: json.loads(p.read_text()),
        add_evidence=evidence.append, rates=RATES)
    return out, evidence


def test_timeout_uses_host_proxy_receipts_and_preserves_unknown(tmp_path):
    trial = tmp_path / 'jobs/run/task'
    token = 'a' * 64
    _write(trial / 'agent/opencode-evidence/opencode.json', {
        'provider': {'benchmark': {'options': {'headers': {TRIAL_HEADER: token}}}}})
    path = tmp_path / 'opencode-proxy/trials' / (token + '.jsonl')
    path.parent.mkdir(parents=True)
    rows = [
        dict(event='chatCompletion', trialToken=token, traceId='one', status='completed',
             usage=dict(prompt_tokens=100, completion_tokens=20, effectiveCachedTokens=80)),
        dict(event='chatCompletion', trialToken=token, traceId='two', status='cancelled',
             clientDisconnected=True, usage={}),
    ]
    path.write_text('\n'.join(json.dumps(r) for r in rows))
    value, evidence = _observe(tmp_path, trial)
    assert value['observedCalls'] == 2
    assert value['usageReceiptedCalls'] == 1
    assert value['unknownUsageCalls'] == 1
    assert value['uncachedInputTokens'] == 20
    assert value['knownReferenceCostUsd'] == pytest.approx(.00068)
    assert value['referenceCostUsd'] is None
    assert value['usageComplete'] is False
    assert path in evidence


def test_compaction_is_counted_once_and_cache_aliases_reconcile(tmp_path):
    trial = tmp_path / 'task'
    first = dict(prompt_tokens=100, completion_tokens=20,
                 cached_tokens=0, effectiveCachedTokens=80)
    second = dict(prompt_tokens=10, completion_tokens=5)
    _write(trial / 'agent/tofu-kimi-evidence/runtime-evidence.json', {
        'apiRounds': [{'round': 1, 'usage': first}, {'round': 'compaction-1', 'usage': second}],
        'usage': dict(prompt_tokens=110, completion_tokens=25, effectiveCachedTokens=80),
        'compactionUsage': {'calls': [{'usage': second}]},
        'finishReason': 'stop',
    })
    value, _ = _observe(tmp_path, trial)
    assert value['observedCalls'] == 2
    assert value['compactionCalls'] == 1
    assert value['outputTokens'] == 25
    assert value['usageComplete']
    assert value['referenceCostUsd'] == pytest.approx(.00088)


def test_missing_usage_is_unknown_not_zero_cost(tmp_path):
    value, _ = _observe(tmp_path, tmp_path / 'missing')
    assert value['referenceCostUsd'] is None
    assert not value['usageComplete']


@pytest.mark.parametrize('value', [dict(RATES, inputUsdPerMillion=True),
                                  dict(RATES, outputUsdPerMillion=float('nan')),
                                  dict(RATES, cacheReadUsdPerMillion=-1), {}])
def test_invalid_rates_cannot_be_registered(value):
    with pytest.raises(ValueError):
        validate_reference_prices(value)


def _row(task, cost, seconds=10, complete=True):
    usage = {k: 0 for k in ('inputTokens', 'uncachedInputTokens', 'cacheReadTokens',
                            'cacheWriteTokens', 'outputTokens', 'reasoningTokens')}
    usage.update(observedCalls=1, usageReceiptedCalls=int(complete), unknownUsageCalls=int(not complete),
                 knownReferenceCostUsd=cost, usageComplete=complete)
    return dict(task=task, agentSeconds=seconds, usageAccounting=usage)


def _arm(rows, archives=()):
    return dict(trials=rows, infrastructureTrials=list(archives),
                fullDomain={'allTrialsScored': True},
                allAttemptUsage=summarize_usage(rows + list(archives)),
                taskMeans={r['task']: 1 for r in rows})


def test_cost_bootstrap_keeps_repeats_and_archived_spend_in_task_cluster():
    baseline = _arm([_row('a', 1), _row('a', 3), _row('b', 2), _row('b', 2)])
    candidate = _arm([_row('a', 2), _row('a', 4), _row('b', 4), _row('b', 4)],
                     [_row('a', 2)])
    result = paired_statistics(baseline, candidate, task_names=['a', 'b'],
                               seed=1729, rates=RATES, samples=1000)
    assert result['knownCostRatio'] == 2
    assert result['costRatio95CI'] == [2, 2]
    assert result['meanAgentSecondsDelta95CI'] == [0, 5]  # archived agent time also counts
    candidate['infrastructureTrials'][0]['usageAccounting']['usageComplete'] = False
    candidate['allAttemptUsage']['usageComplete'] = False
    unknown = paired_statistics(baseline, candidate, task_names=['a', 'b'],
                                seed=1729, rates=RATES, samples=1000)
    assert unknown['knownCostRatio'] == 2
    assert unknown['costRatio95CI'] is None


def test_empty_runtime_config_freezes_production_output_budget():
    from evaluations.swebench.tofu_kimi_runtime import TofuKimiCandidateSettings
    from lib.tasks_pkg.model_config import resolve_max_output_tokens
    settings = TofuKimiCandidateSettings(provider_face='zai', provider_slot_id='fixture',
        agent_version='0.17.0', experiment_arm='default', runtime_config={}, model='glm-5.3-m17')
    record = settings.manifest_record()
    assert record['runtimeConfig'] == {}
    assert record['requestedMaxOutputTokens'] == resolve_max_output_tokens({})
    assert record['outputBudgetSource'] == 'production_default'
