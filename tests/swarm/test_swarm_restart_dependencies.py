"""Unsafe restart checkpoints block their dependency closure, not unrelated work."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentResult, SubTaskSpec
from lib.swarm.scheduler import StreamingScheduler

pytestmark = pytest.mark.unit


@pytest.fixture
def recovery(monkeypatch):
    """Exercise scheduler admission without storage, providers, or driver writes."""
    persisted = Mock()
    inbox = Mock()
    monkeypatch.setattr('lib.swarm.persistence.save_agent', persisted)
    monkeypatch.setattr('lib.swarm.persistence._storage', Mock(
        side_effect=AssertionError('Restart dependency tests must not access storage')))
    monkeypatch.setattr('lib.swarm.master.inbox_enqueue', inbox)
    monkeypatch.setattr('lib.swarm.master.inbox_consume', Mock())
    monkeypatch.setattr('lib.swarm.master._build_sub_agent', Mock(
        side_effect=AssertionError('Restart dependency tests must not build model workers')))
    monkeypatch.setattr('lib.tasks_pkg.context_composer._render._count_tokens',
                        lambda text, model: 1)
    schedulers = []

    def restore(specs, rows):
        master = MasterOrchestrator(
            task_id='restart-dependencies', conv_id='restart-conversation',
            user_id=7, specs=specs, model='test-model', max_parallel=2,
            max_retries=0)
        workers = {}

        def factory(spec):
            worker = SimpleNamespace(
                messages=[{'role': 'user', 'content': spec.objective}],
                run=lambda: SubAgentResult(status='completed',
                                           final_answer=f'Resumed {spec.id}'))
            workers[spec.id] = worker
            return worker

        scheduler = StreamingScheduler(
            agent_factory=factory, max_parallel=2, default_retries=0,
            on_agent_complete=master._on_agent_complete_callback)
        schedulers.append(scheduler)
        driver = Mock()
        monkeypatch.setattr(master, '_build_scheduler', lambda: scheduler)
        monkeypatch.setattr(master, '_start_driver', driver)
        monkeypatch.setattr(master, '_persist_agent_snapshot', Mock())
        monkeypatch.setattr(master, '_resolve_spec_model', lambda spec: 'test-model')
        master.rehydrate_in_background(rows)
        scheduler.run_until_idle(timeout=2)
        assert scheduler.is_idle, 'Recovery left unschedulable children pending'
        return SimpleNamespace(master=master, scheduler=scheduler, workers=workers,
                               driver=driver, persisted=persisted, inbox=inbox)

    yield restore
    for scheduler in schedulers:
        scheduler.cancel_pending()
        scheduler.shutdown(require_quiescence=True)


@pytest.mark.parametrize('checkpoint, reason', [
    ({'result': {'checkpoint_phase': 'tools_in_flight'}},
     'restart_tool_outcome_unknown'),
    ({'messages': [{'role': 'assistant', 'content': 'Write queued',
                   'tool_calls': [{'id': 'external-write', 'type': 'function',
                                   'function': {'name': 'external_write',
                                                'arguments': '{}'}}]}]},
     'restart_tool_outcome_unknown'),
    ({'messages': [{'role': 'tool', 'tool_call_id': 'missing-intent',
                   'content': 'Unpaired receipt'}]},
     'restart_checkpoint_invalid'),
])
def test_unsafe_checkpoint_blocks_out_of_order_closure_only(recovery, checkpoint, reason):
    # Reverse dependency order prevents a single forward scan from sufficing.
    specs = [SubTaskSpec(id='leaf', objective='publish after review', depends_on=['review']),
             SubTaskSpec(id='review', objective='review external write', depends_on=['write']),
             SubTaskSpec(id='write', objective='external write'),
             SubTaskSpec(id='unrelated', objective='independent analysis')]
    rows = [
        {'agent_id': 'write', 'status': 'running', **checkpoint},
        {'agent_id': 'review', 'status': 'pending', 'rounds_used': 2,
         'messages': [{'role': 'user', 'content': 'Prior review context'}],
         'result': {'final_answer': 'Partial review, not a finished prerequisite',
                    'total_tokens': 17, 'rounds_used': 2,
                    'tool_log': [{'tool': 'read', 'result': 'prior evidence'}]}},
        {'agent_id': 'unrelated', 'status': 'running', 'rounds_used': 3,
         'messages': [{'role': 'user', 'content': 'Independent checkpoint'}]},
    ]
    original_rows = deepcopy(rows)
    restored = recovery(specs, rows)
    master = restored.master
    assert set(restored.workers) == {'unrelated'}
    assert set(master._resume_messages) == {'unrelated'}
    assert master._resume_rounds == {'unrelated': 3}
    assert rows == original_rows
    assert master._results_by_id['write'][1].recovery_reason == reason
    for child_id in ('review', 'leaf'):
        result = master._results_by_id[child_id][1]
        assert result.status == 'failed'
        assert result.recovery_reason == 'restart_dependency_reconciliation_required'
        assert 'write' in result.error_message and reason in result.error_message
        assert 'reconcil' in result.error_message.lower()
        assert restored.scheduler._completed[child_id][1] is result
    review = master._results_by_id['review'][1]
    assert review.final_answer == rows[1]['result']['final_answer']
    assert review.total_tokens == 17 and review.rounds_used == 2
    assert review.tool_log == rows[1]['result']['tool_log']
    assert not master._delivered_result_ids
    visible = [master.get_agent_result(child_id, delivery_ack=Mock())
               for child_id in ('write', 'review', 'leaf')]
    assert {row['agent_id']: row['status'] for row in visible} == {
        'write': 'failed', 'review': 'failed', 'leaf': 'failed'}
    assert all(row['error'] and not row['final_answer'] for row in visible)
    assert visible[1]['partial_output'] == rows[1]['result']['final_answer']
    saved = {call.args[1]: call.kwargs
             for call in restored.persisted.call_args_list}
    assert set(saved) == {'review', 'leaf'}
    assert saved['leaf']['result']['recovery_reason'] == 'restart_dependency_reconciliation_required'
    assert saved['review']['messages'] == rows[1]['messages']
    assert saved['review']['rounds_used'] == 2
    assert all(payload.get('delivered') is None for payload in saved.values())
    updates = {call.kwargs['agent_id']: call.args[1]
               for call in restored.inbox.call_args_list}
    assert 'restart_dependency_reconciliation_required' in updates['leaf']
    restored.driver.assert_called_once_with()


def test_finished_results_and_delivery_flags_survive_unsafe_ancestry(recovery):
    specs = [SubTaskSpec(id='leaf', objective='new publication', depends_on=['bridge']),
             SubTaskSpec(id='bridge', objective='finished review', depends_on=['write']),
             SubTaskSpec(id='failed', objective='failed old audit', depends_on=['write']),
             SubTaskSpec(id='cancelled', objective='cancelled old audit', depends_on=['write']),
             SubTaskSpec(id='write', objective='prior external mutation'),
             SubTaskSpec(id='unrelated', objective='new independent work')]
    rows = [
        {'agent_id': 'write', 'status': 'failed', 'delivered': True,
         'result': {'status': 'failed', 'recovery_reason': 'restart_tool_outcome_unknown',
                    'error_message': 'restart_tool_outcome_unknown: inspect external write',
                    'final_answer': 'Prior partial write report', 'total_tokens': 9}},
        {'agent_id': 'bridge', 'status': 'completed', 'delivered': True,
         'result': {'status': 'completed', 'final_answer': 'Durable finished review',
                    'total_tokens': 11}},
        {'agent_id': 'failed', 'status': 'failed', 'delivered': False,
         'result': {'status': 'failed', 'error_message': 'ordinary audit failure',
                    'final_answer': 'Prior failed audit evidence'}},
        {'agent_id': 'cancelled', 'status': 'cancelled', 'delivered': True,
         'result': {'status': 'cancelled', 'error_message': 'user stop'}},
    ]
    original_rows = deepcopy(rows)
    restored = recovery(specs, rows)
    master = restored.master
    assert set(restored.workers) == {'unrelated'}
    for row in rows:
        assert master._results_by_id[row['agent_id']][1].to_dict() == (
            SubAgentResult(**row['result']).to_dict())
    assert rows == original_rows
    assert master._delivered_result_ids == {'write', 'bridge', 'cancelled'}
    assert 'write' in master._results_by_id['leaf'][1].error_message
    assert {call.args[1] for call in restored.persisted.call_args_list} == {'leaf'}
    assert {call.kwargs['agent_id'] for call in restored.inbox.call_args_list} == {
        'failed', 'leaf', 'unrelated'}
    unread = master.await_agents(delivery_ack=Mock())
    assert {row['agent_id'] for row in unread['completed']} == {'failed', 'leaf', 'unrelated'}
    assert master._delivered_result_ids == {spec.id for spec in specs}


@pytest.mark.parametrize('reason', ['restart_tool_outcome_unknown',
                                  'restart_dependency_reconciliation_required'])
def test_persisted_restart_failure_blocks_new_descendant_without_replaying_it(recovery, reason):
    specs = [SubTaskSpec(id='descendant', objective='later dependent work', depends_on=['blocked']),
             SubTaskSpec(id='blocked', objective='previous reconciliation failure')]
    # Older results may have the marker only on their error channel.
    rows = [{'agent_id': 'blocked', 'status': 'failed', 'delivered': True,
             'result': {'status': 'failed', 'error_message': reason + ': reconcile prior outcomes'}}]
    restored = recovery(specs, rows)
    assert not restored.workers
    assert not restored.master._resume_results
    assert restored.master._delivered_result_ids == {'blocked'}
    outcome = restored.master.get_agent_result('descendant', delivery_ack=Mock())
    assert outcome['status'] == 'failed'
    assert 'blocked' in outcome['error'] and reason in outcome['error']
    assert restored.master._delivered_result_ids == {'blocked', 'descendant'}
    assert {call.kwargs['agent_id'] for call in restored.inbox.call_args_list} == {'descendant'}
    restored.driver.assert_called_once_with()


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'completed'])
def test_terminal_tool_intent_blocks_unfinished_descendants_without_rewriting_receipts(recovery, status):
    specs = [SubTaskSpec(id='dependent', objective='publish after write', depends_on=['write']),
             SubTaskSpec(id='write', objective='prior mutation'),
             SubTaskSpec(id='unrelated', objective='independent work')]
    row = {'agent_id': 'write', 'status': status, 'delivered': True,
           'result': {'status': status, 'checkpoint_phase': 'tools_in_flight',
                      'error_message': '' if status == 'completed' else 'ordinary dispatch exit',
                      'total_tokens': 23}}
    restored = recovery(specs, [row])
    assert set(restored.workers) == {'unrelated'}
    assert restored.master._results_by_id['write'][1].to_dict() == SubAgentResult(**row['result']).to_dict()
    assert restored.master._delivered_result_ids == {'write'}
    dependent = restored.master._results_by_id['dependent'][1]
    assert dependent.status == 'failed'
    assert dependent.recovery_reason == 'restart_dependency_reconciliation_required'
    assert 'restart_tool_outcome_unknown' in dependent.error_message
    assert {call.args[1] for call in restored.persisted.call_args_list} == {'dependent'}


@pytest.mark.parametrize('stored_status', ['failed', 'completed'])
def test_ordinary_failed_prerequisite_remains_schedulable_with_typed_evidence(recovery, stored_status):
    parent = SubTaskSpec(id='failed', objective='ordinary failed prerequisite')
    child = SubTaskSpec(id='child', objective='follow up safely', depends_on=['failed'],
                        context='Original delegated context')
    rows = [{'agent_id': 'failed', 'status': stored_status, 'delivered': True,
             'result': {'status': stored_status, 'final_answer': 'Partial evidence only',
                        'error_message': 'provider unavailable'}}]
    restored = recovery([child, parent], rows)
    assert set(restored.workers) == {'child'}
    assert child.context == 'Original delegated context'
    dependency_messages = [message for message in restored.workers['child'].messages
                           if message.get('_contextInjectionId') == 'worker_dependencies']
    assert len(dependency_messages) == 1
    evidence = dependency_messages[0]['content']
    assert 'Status: failed' in evidence
    assert 'Error: provider unavailable' in evidence
    assert 'Partial evidence only' in evidence
    assert restored.master.get_agent_result('child', delivery_ack=Mock())['status'] == 'completed'
