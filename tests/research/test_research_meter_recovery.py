"""Acknowledged Research accounting restoration without budget or replay credit."""
from copy import deepcopy

import pytest

from lib.paper.agent_usage import (
    PaperAgentBudgetExceeded,
    PaperAgentUsageMeter,
)
from lib.storage.errors import StorageError
from tests.research.test_research_model_accounting import repository
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def meter():
    result = PaperAgentUsageMeter('research-action', token_budget=100,
                                 dispatch_budget=3)
    result._compute_cost = lambda *args, **kwargs: {
        'costUsd': .01, 'costCny': .07, 'pricingSource': 'default_estimate'}
    return result


def account(repo, result, ordinal):
    result.begin_dispatch()
    intent = repo.admit_model_call(ordinal=ordinal, round_index=ordinal,
        requested_model='controlled-model', request_digest='d' * 64,
        max_output=10, usage_before=result.snapshot())
    result.observe_agent_round({'prompt_tokens': 10, 'completion_tokens': 5,
                               'model': 'controlled-model'}, {})
    repo.record_model_usage(intent, result.snapshot(), usage_mapping_received=True)
    return result.agent_dispatches


def test_restoration_preserves_accounting_and_dispatch_count():
    repo = repository(MemoryRecords()); original = meter()
    account(repo, original, 1); account(repo, original, 2)
    restored = repo.restore_usage_meter(2)
    assert restored.snapshot() == original.snapshot()
    restored._compute_cost = original._compute_cost
    assert account(repo, restored, 3) == 3
    assert restored.agent_dispatches == 3 and restored.total_tokens == 45
    assert restored.cost_usd == .03 and restored.cost_estimated
    assert repo.load_model_usage(3)['usage_snapshot']['calls'] == 3
    with pytest.raises(StorageError, match='successor'): repo.restore_usage_meter(2)


def test_unresolved_successor_cannot_restore_earlier_budget():
    repo = repository(MemoryRecords()); result = meter(); account(repo, result, 1)
    result.begin_dispatch()
    repo.admit_model_call(ordinal=2, round_index=2, requested_model='controlled-model',
        request_digest='e' * 64, max_output=10, usage_before=result.snapshot())
    with pytest.raises(StorageError, match='successor'): repo.restore_usage_meter(1)
    with pytest.raises(StorageError, match='unresolved'): repo.restore_usage_meter(2)


def test_exhausted_declared_envelope_fails_after_restore():
    result = meter()
    result.begin_dispatch()
    result.observe_agent_round({'prompt_tokens': 100, 'completion_tokens': 5}, {})
    restored = PaperAgentUsageMeter.from_snapshot(result.snapshot())
    assert restored.snapshot() == result.snapshot()
    # The declared envelope is spent; the next paid request is refused rather
    # than dispatched without tools.
    with pytest.raises(PaperAgentBudgetExceeded) as raised:
        restored.begin_dispatch()
    assert raised.value.reason == 'token_budget'


@pytest.mark.parametrize('field,value', [
    ('calls', True), ('total_tokens', -1), ('cost_usd', float('nan')),
    ('cost_cny', float('inf')), ('version', 'unknown'), ('forced_final', True),
    ('models', ['same', 'same']), ('models', [1]), ('agent_tokens', 999),
    ('cost_estimated', 1), ('agent_token_budget', -1), ('stage', '')])
def test_malformed_accounting_is_refused(field, value):
    saved = meter().snapshot(); saved[field] = value
    with pytest.raises(ValueError): PaperAgentUsageMeter.from_snapshot(saved)


def test_minimal_legacy_snapshot_cannot_invent_missing_accounting():
    saved = meter().snapshot(); saved.pop('cost_usd')
    with pytest.raises(ValueError, match='Incomplete'): PaperAgentUsageMeter.from_snapshot(saved)


def test_restored_model_history_is_detached():
    saved = meter().snapshot(); saved['models'] = ['old']
    restored = PaperAgentUsageMeter.from_snapshot(saved)
    saved['models'].append('changed')
    assert restored.snapshot()['models'] == ['old']


def test_meter_restore_survives_selected_storage_restart(storage):
    supervisor, _ = storage
    repo = repository(supervisor.client); original = meter(); account(repo, original, 1)
    expected = deepcopy(original.snapshot())
    supervisor.stop(); supervisor.start()
    reopened = type(repo).open(supervisor.client, user_id=1, task_id='research_action_observed')
    restored = reopened.restore_usage_meter(1)
    assert restored.snapshot() == expected
    restored._compute_cost = original._compute_cost
    assert account(reopened, restored, 2) == 2
    assert reopened.load_model_usage(2)['usage_snapshot']['total_tokens'] == 30
