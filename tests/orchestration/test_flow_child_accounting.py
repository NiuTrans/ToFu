"""Accounting boundaries without live providers or durable storage mutations."""
from copy import deepcopy
import json

import pytest

from lib.cost import accumulate_usage_into
from lib.orchestration.agent_runner import (
    OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner,
)
from lib.orchestration.chat_flow_adapter import FlowEventAdapter
from lib.orchestration.graph import FlowEventDeliveryError
from lib.orchestration.usage_accounting import FlowUsageLedger, project_child_accounting

pytestmark = pytest.mark.unit


def _receipt(identity='child-a', *, usage=None, cost=None):
    usage = {'prompt_tokens': 10, 'completion_tokens': 2} if usage is None else usage
    row = {'round': 1, 'model': 'fixture-model', 'usage': usage}
    if cost is not None:
        row['cost'] = cost
    return project_child_accounting({'usage': usage, 'apiRounds': [row]},
                                    invocation_id=identity)


def _event(projection, *, role='worker', status='completed'):
    return {'type': 'step_complete', 'role': role, 'status': status,
            'outputProjection': {'content': 'answer', 'finishReason': 'stop',
                                 **deepcopy(projection)}}


@pytest.mark.parametrize('role', ['worker', 'virtual_user'])
@pytest.mark.parametrize('outcome', ['completed', 'cancelled', 'failed', 'empty'])
def test_finalizer_runner_adapter_and_public_projection(monkeypatch, role, outcome):
    """Real child lifetime + finalizer + both old whitelist boundaries."""
    import lib.tasks_pkg.child_invocation as children
    import lib.cost as costs
    from lib.tasks_pkg.orchestrator import _finalize
    from lib.tasks_pkg.compaction._compaction_usage import record_compaction_usage
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.turn_projection_patch import normalize_projection_document

    parent = {'id': 'accounting-parent', 'convId': 'accounting-conv', '_userId': 71,
              '_transientRuntime': True, 'config': {'model': 'fixture-model'},
              'usage': {'prompt_tokens': 999}, 'apiRounds': [{'round': 'parent'}]}
    before = deepcopy(parent)
    observed = []
    priced = []
    raw = 'PRIVATE REQUEST PAYLOAD ' * 10000

    def price(usage, model_id='', provider_id=None, **kwargs):
        priced.append((model_id, provider_id))
        return {'costUsd': 0.5, 'costCny': 3.5, 'pricingSource': 'fixture_snapshot',
                'pricingSnapshot': {'model': model_id, 'provider': provider_id,
                                    'currency': 'USD', 'rates': {'input': 1}}}

    monkeypatch.setattr(costs, 'compute_cost', price)
    monkeypatch.setattr(_finalize, 'append_event', lambda *a, **kw: None)
    monkeypatch.setattr(_finalize, 'stamp_chat_task_terminal', lambda *a, **kw: None)

    def execute(child, *, policy):
        observed.append(child)
        assert policy.is_child
        first = {'prompt_tokens': 100, 'completion_tokens': 20}
        second = {'input_tokens': 5, 'output_tokens': 6, 'cache_read_input_tokens': 40}
        accumulated = {}
        accumulate_usage_into(accumulated, first)
        accumulate_usage_into(accumulated, second)
        rounds = [
            {'round': 1, 'model': 'model-a', 'provider_id': 'provider-a',
             'requestId': 'request-a', 'requestPayload': raw,
             'usage': {**first, '_dispatch': {'requestId': 'request-a', 'raw': raw},
                       'timing': {'prompt': raw}}},
            {'round': 1, 'model': 'model-b', 'provider_id': 'provider-b',
             'requestId': 'request-b', 'tag': 'DISCARDED-RETRY', 'usage': second},
        ]
        record_compaction_usage(compaction_owner_key(child),
                                {'prompt_tokens': 3, 'completion_tokens': 1,
                                 '_dispatch': {'model': 'compact-model',
                                               'provider_id': 'provider-c'}})
        child.update(content='' if outcome == 'empty' else 'answer',
                     aborted=outcome == 'cancelled',
                     error='provider unavailable' if outcome == 'failed' else None)
        finish = 'error' if outcome == 'failed' else 'stop'
        _finalize._finalize_and_emit_done(
            child, model='fixture-model', preset='', thinking_depth=None, cfg={},
            last_finish_reason=finish, last_usage=second, last_stream_result=None,
            accumulated_usage=accumulated, api_rounds=rounds, tool_call_happened=False,
            messages=[], original_messages=[], all_search_results_text=[], max_tokens=10,
            thinking_enabled=False, temperature=None, _loop_exit_reason=finish,
            _abort_detected_phase=None, project_path='', project_enabled=False,
            round_num=1, assistant_msg={'role': 'assistant', 'content': child['content']})

    monkeypatch.setattr(children, 'execute_invocation', execute)
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=parent, model='fixture-model',
                                       model_routing_policy='selected'),
        emit=lambda event: None, abort_check=lambda: False,
    )({'id': role, 'type': 'role', 'role': role, 'params': {'objective': 'fixture'}}, '', 1)
    # Principal hydration is allowed; parent/phase-zero billing is immutable.
    assert parent['usage'] == before['usage']
    assert parent['apiRounds'] == before['apiRounds']
    assert chat_task_runtime.get(observed[0]['id']) is None  # Ephemeral child released.
    assert priced == [('model-a', 'provider-a'), ('model-b', 'provider-b'),
                      ('compact-model', 'provider-c')]
    projection = result.output_projection
    assert projection['usageAccounting']['invocationId'] == observed[0]['id']
    assert projection['cost']['costUsd'] == 1.5
    assert projection['compactionUsage']['n_calls'] == 1
    assert projection['usage']['prompt_tokens'] == 103
    assert [row['ledgerIndex'] for row in projection['apiRounds']] == [0, 1, 2]
    assert [row['requestId'] for row in projection['apiRounds'][:2]] == ['request-a', 'request-b']
    assert len(json.dumps(projection)) < 6000
    assert 'PRIVATE REQUEST' not in json.dumps(projection)

    adapter = FlowEventAdapter(projection='autopilot')
    event = {'type': 'step_complete', 'role': role, 'status': result.status,
             'outputProjection': projection}
    adapter.on_event(event)
    adapter.on_event(event)
    assert len(adapter.messages) == 1
    phase = adapter.messages[0]
    assert phase['apiRounds'] == projection['apiRounds']
    assert phase['usage'] == projection['usage']
    assert phase['cost'] == projection['cost']
    public = normalize_projection_document(phase)
    assert public['apiRounds'] == phase['apiRounds']
    assert public['cost'] == phase['cost']
    # These local receipt fields need a NEW durable contract, not an unknown
    # nested property slipped into the closed TurnOrchestration schema.
    assert 'usageAccounting' not in public and 'compactionUsage' not in public
    summary = adapter.usage_summary
    assert summary['invocations'] == 1
    assert summary['recordedApiRoundCount'] == 3
    assert summary['cost']['costUsd'] == 1.5  # Not cost + per-call cost + compaction.
    assert summary['costStatus'] == summary['tokenTotalsStatus'] == 'complete'
    assert summary['recordedTokenTotals']['totalInputTokens'] == 148
    assert summary['recordedTokenTotals']['outputTokens'] == 27
    assert not any(key in summary for key in ('usage', 'apiRounds', 'compactionUsage'))


@pytest.mark.parametrize('usage,expected', [
    (None, 'missing'), ({}, 'missing'), ({'_dispatch': {'a': 1}}, 'missing'),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, 'reported'),
    ({'prompt_tokens': False, 'total_tokens': float('nan')}, 'missing'),
    ({'prompt_tokens': -1, 'total_tokens': float('inf')}, 'missing'),
])
def test_missing_is_not_zero_and_unpriced_is_not_free(usage, expected):
    projection = project_child_accounting({'usage': usage}, invocation_id='child')
    assert projection['usageAccounting']['usageStatus'] == expected
    assert ('usage' in projection) == (expected == 'reported')
    ledger = FlowUsageLedger()
    ledger.record(projection)
    assert ledger.summary()['costStatus'] == 'missing'
    assert 'cost' not in ledger.summary()
    assert 'recordedTokenTotals' not in ledger.summary()


def test_empty_ledger_does_not_fabricate_billing():
    summary = FlowUsageLedger().summary()
    assert summary['usageStatus'] == summary['costStatus'] == 'empty'
    assert summary['invocations'] == 0 and 'cost' not in summary
    assert 'usage' not in summary and 'recordedTokenTotals' not in summary


def test_zero_snapshot_distinct_from_missing_cost():
    ledger = FlowUsageLedger()
    ledger.record(_receipt(cost={'costUsd': 0, 'costCny': 0}))
    ledger.record(project_child_accounting({}, invocation_id='cancelled-before-dispatch'))
    assert ledger.summary()['cost']['costUsd'] == 0
    assert ledger.summary()['costStatus'] == 'partial'
    assert ledger.summary()['usageStatus'] == 'partial'
    assert ledger.summary()['invocationsMissingUsage'] == 1


def test_compaction_truncation_and_unrepresented_usage_never_become_complete_cost():
    source = {'usage': {'prompt_tokens': 40},
              'apiRounds': [{'kind': 'compaction', 'usage': {'prompt_tokens': 10},
                             'cost': {'costUsd': 1}}],
              'compactionUsage': {'prompt_tokens': 40, 'n_calls': 4,
                                  'calls': [{'rawPrompt': 'secret'}]}}
    projection = project_child_accounting(source, invocation_id='compact')
    assert projection['usageAccounting']['compactionRoundDetailTruncated']
    assert projection['usageAccounting']['costStatus'] == 'partial'
    assert 'cost' not in projection  # No misleading top-level footer total.
    assert 'calls' not in projection['compactionUsage']
    assert project_child_accounting(projection) == projection
    ledger = FlowUsageLedger()
    ledger.record(projection)
    assert ledger.summary()['cost']['costUsd'] == 1
    assert ledger.summary()['costStatus'] == 'partial'
    assert ledger.summary()['recordedTokenTotals']['totalInputTokens'] == 10
    assert ledger.summary()['tokenTotalsStatus'] == 'partial'


def test_identical_round_numbers_are_distinct_calls_and_reprojection_is_stable():
    projection = _receipt(cost={'costUsd': 1})
    source = {'usage': {'prompt_tokens': 20, 'completion_tokens': 4},
              'apiRounds': projection['apiRounds'] * 2}
    projected = project_child_accounting(source, invocation_id='canonical-id')
    assert [row['ledgerIndex'] for row in projected['apiRounds']] == [0, 1]
    assert projected['cost']['costUsd'] == 2
    assert project_child_accounting(projected) == projected
    ledger = FlowUsageLedger()
    ledger.record(projected)
    ledger.record(deepcopy(projected))
    assert ledger.summary()['recordedApiRoundCount'] == 2
    assert ledger.summary()['cost']['costUsd'] == 2
    changed = deepcopy(projected)
    changed['usage']['prompt_tokens'] = 21
    with pytest.raises(ValueError, match='Conflicting'):
        ledger.record(changed)
    assert ledger.summary()['cost']['costUsd'] == 2


def test_replay_after_failed_persistence_and_drain_retains_phase_identity():
    deliveries = []

    def persist(message):
        deliveries.append(message)
        if len(deliveries) == 1:
            raise RuntimeError('lost ACK')

    adapter = FlowEventAdapter(emit=persist)
    event = _event(_receipt(cost={'costUsd': 1}))
    with pytest.raises(FlowEventDeliveryError):
        adapter.on_event(event)
    first = adapter.drain()[0]
    adapter.on_event(event)
    assert deliveries[1] is first
    assert adapter.messages == []
    assert adapter.usage_summary['invocations'] == 1
    assert adapter.usage_summary['cost']['costUsd'] == 1
    changed = deepcopy(event)
    changed['outputProjection']['content'] = 'different terminal content'
    with pytest.raises(FlowEventDeliveryError, match='Conflicting'):
        adapter.on_event(changed)
    assert len(deliveries) == 2


def test_compact_receipt_size_independent_of_prompt_and_timing_payload():
    source = {'usage': {'prompt_tokens': 1, 'latency_ms': 999}, 'apiRounds': [
        {'round': 1, 'usage': {'prompt_tokens': 1, 'latency_ms': 999,
                              '_wire_messages': ['x' * 1000000],
                              '_dispatch': {'provider_id': 'p', 'model': 'm',
                                            'requestTag': 'canonical-request',
                                            'payload': 'x' * 1000000}},
         'timing': {'raw': 'x' * 1000000}}]}
    projection = project_child_accounting(source, invocation_id='canonical-child')
    assert len(json.dumps(projection)) < 1600
    assert projection['apiRounds'][0]['requestTag'] == 'canonical-request'
    assert 'latency_ms' not in projection['usage']
    assert 'latency_ms' not in projection['apiRounds'][0]['usage']
    assert 'timing' not in json.dumps(projection)


def test_finalizer_preserves_snapshots_and_price_failures_do_not_lose_usage(monkeypatch):
    from lib.tasks_pkg.orchestrator._finalize import _stamp_child_round_costs
    import lib.cost as costs
    frozen = {'costUsd': 9, 'pricingSource': 'historical-snapshot'}
    rows = [{'usage': {'prompt_tokens': 1}, 'cost': frozen},
            {'usage': {'prompt_tokens': 2}}, {'usage': {}}, {},
            {'usage': {'_dispatch': {'provider_id': 'p'}}},
            {'usage': {'prompt_tokens': False}},
            {'usage': {'prompt_tokens': float('nan')}}]
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('price unavailable')

    monkeypatch.setattr(costs, 'compute_cost', unavailable)
    _stamp_child_round_costs({}, rows, model='m')
    assert rows[0]['cost'] is frozen
    assert len(calls) == 1 and rows[1]['usage'] == {'prompt_tokens': 2}
    assert 'cost' not in rows[1]


def test_no_implicit_currency_conversion_or_missing_amount_zero():
    ledger = FlowUsageLedger()
    ledger.record(_receipt('usd', cost={'costUsd': 1}))
    ledger.record(_receipt('cny', cost={'costCny': 7}))
    summary = ledger.summary()
    assert summary['costStatus'] == 'partial'
    assert 'cost' not in summary


def test_finalizer_uses_request_provider_not_terminal_task_provider(monkeypatch):
    from lib.tasks_pkg.orchestrator._finalize import _stamp_child_round_costs
    import lib.cost as costs
    observed = []

    def price(usage, *, model_id, provider_id):
        observed.append((model_id, provider_id))
        return {'costUsd': 0}

    monkeypatch.setattr(costs, 'compute_cost', price)
    rows = [{'usage': {'prompt_tokens': 0, 'completion_tokens': 0,
                       '_dispatch': {'model': 'request-model', 'provider_id': 'request-provider'}}},
            {'model': 'unscoped', 'usage': {'prompt_tokens': 1}}]
    _stamp_child_round_costs({'provider_id': 'terminal-provider'}, rows, model='terminal-model')
    assert observed == [('request-model', 'request-provider'), ('unscoped', None)]
    assert rows[0]['cost']['costUsd'] == 0


def test_verifier_replay_uses_canonical_not_rendered_content():
    adapter = FlowEventAdapter(projection='autopilot')
    event = _event(_receipt(), role='virtual_user')
    event['outputProjection']['content'] = 'Verified. [VERDICT: STOP]'
    adapter.on_event(event)
    adapter.on_event(event)
    assert len(adapter.messages) == 1
    assert '[VERDICT:' not in adapter.messages[0]['content']
    assert adapter.usage_summary['invocations'] == 1
    changed = deepcopy(event)
    changed['status'] = 'failed'
    with pytest.raises(FlowEventDeliveryError, match='Conflicting'):
        adapter.on_event(changed)
