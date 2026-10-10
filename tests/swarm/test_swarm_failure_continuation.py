"""Owner-scoped, restart-safe bounded swarm continuation execution specs."""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from lib import agent_inbox
from lib.swarm import integration
from lib.swarm.integration._autocontinue import (
    _durable_chain_allows, _start_autocontinue_turn,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def harness():
    key = 'failure-policy-' + uuid4().hex
    turns = [dict(turnId='human', actor='human', kind='chat_input',
                  status='completed', projection={}),
             dict(turnId='parent', actor='assistant', kind='chat_reply',
                  status='completed', projection={})]
    calls = []
    disposition = ['started']

    def dispatch(conv, projection, config, **kwargs):
        assert conv == key
        assert kwargs['user_id'] == 17
        assert kwargs['require_parent_not_stopped'] is True
        calls.append((projection, kwargs))
        if disposition[0] == 'started':
            input_id = f'input-{len(calls)}'
            turns.extend([
                dict(turnId=input_id, actor='virtual_user',
                     kind=kwargs['input_kind'], status='completed',
                     projection=dict(projection)),
                dict(turnId=f'output-{len(calls)}', parentTurnId=input_id,
                     actor='assistant', kind=kwargs['turn_kind'],
                     status='completed', projection={}),
            ])
        return SimpleNamespace(disposition=disposition[0], task_id='fake-task')

    def listing(conv, **kwargs):
        assert conv == key and kwargs['user_id'] == 17
        assert kwargs['light'] is False
        return {'turns': list(turns)}

    def enqueue(*statuses):
        agent_inbox.reset_for_test(key)
        for index, status in enumerate(statuses):
            agent_inbox.enqueue(key, 'evidence', priority='later',
                                mode='swarm-update', agent_id=str(index),
                                extra={'status': status})

    with patch('lib.turn_lifecycle.list_turns', side_effect=listing), \
         patch('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', side_effect=dispatch), \
         patch('lib.agent_core.push.push_event'), \
         patch.object(integration, '_key_is_live', return_value=False), \
         patch.object(integration, 'SWARM_AUTOCONTINUE_ENABLED', True), \
         patch.object(integration, 'SWARM_AUTOCONTINUE_MAX_CHAIN', 3):
        yield SimpleNamespace(key=key, turns=turns, calls=calls,
                              disposition=disposition, enqueue=enqueue,
                              run=lambda source='wave-1': integration._maybe_autocontinue(
                                  key, 17, source_id=source))
    agent_inbox.reset_for_test(key)
    integration.reset_autocontinue_chain(key)


@pytest.mark.parametrize('statuses', [('failed',), ('failed', 'completed', 'cancelled')])
def test_failure_bearing_turn_is_factual_once_and_restart_safe(harness, statuses):
    h = harness
    h.enqueue(*statuses)
    h.run()
    assert len(h.calls) == 1
    content = h.calls[0][0]['content']
    assert 'Never blindly repeat' in content
    assert f'{statuses.count("completed")} completed' in content
    assert h.calls[0][0]['_swarmFailureRecovery'] is True
    h.run()  # duplicate settlement
    integration.reset_autocontinue_chain(h.key)  # process registry lost on restart
    h.run('wave-2')
    assert len(h.calls) == 1
    # A genuinely new human authorization, not an automatic turn, resets it.
    h.turns.append(dict(turnId='new-human', actor='human', status='completed', projection={}))
    h.run('wave-3')
    assert len(h.calls) == 2


@pytest.mark.parametrize('statuses', [('cancelled',), (None,), ('aborted',)])
def test_cancelled_or_unstamped_never_authorizes(harness, statuses):
    harness.enqueue(*statuses)
    harness.run()
    assert not harness.calls


@pytest.mark.parametrize('status', ['cancelled', 'aborted', 'interrupted', 'running'])
def test_stop_or_live_authority_blocks_even_success(harness, status):
    harness.enqueue('completed', 'failed')
    harness.turns[-1]['status'] = status
    harness.run()
    assert not harness.calls


def test_disabled_and_live_registry_do_not_dispatch(harness):
    harness.enqueue('failed')
    with patch.object(integration, 'SWARM_AUTOCONTINUE_ENABLED', False):
        harness.run()
    with patch.object(integration, '_key_is_live', return_value=True):
        harness.run()
    assert not harness.calls


@pytest.mark.parametrize('outcome', ['busy', 'start_failed', 'target_missing'])
def test_failed_dispatch_does_not_consume_allowance(harness, outcome):
    harness.enqueue('failed')
    harness.disposition[0] = outcome
    harness.run()
    assert integration._autocontinue_chain.get(harness.key, 0) == 0
    harness.disposition[0] = 'started'
    harness.run()
    assert len(harness.calls) == 2


def test_dispatch_exception_does_not_consume_or_leave_latch(harness):
    harness.enqueue('failed')
    with patch.object(integration, '_start_autocontinue_turn', side_effect=RuntimeError):
        harness.run()
    assert integration._autocontinue_chain.get(harness.key, 0) == 0
    assert harness.key not in integration._autocontinue_inflight
    harness.run()
    assert len(harness.calls) == 1


def test_total_ceiling_survives_restart_and_failure_is_not_extra_credit(harness):
    harness.enqueue('completed')
    for index in range(3):
        harness.run(str(index))
    integration.reset_autocontinue_chain(harness.key)
    harness.enqueue('failed')
    harness.run('failure')
    assert len(harness.calls) == 3


def test_success_duplicates_do_not_charge_budget(harness):
    harness.enqueue('completed')
    harness.run()
    harness.run()
    assert len(harness.calls) == 1
    assert integration._autocontinue_chain[harness.key] == 1


def test_owner_query_failure_stands_down(harness):
    harness.enqueue('failed')
    with patch('lib.turn_lifecycle.list_turns', side_effect=PermissionError):
        assert not _start_autocontinue_turn(harness.key, 18, command_id='foreign')
    assert not harness.calls


def test_no_human_or_truncated_authority_fails_closed(harness):
    harness.enqueue('failed')
    harness.turns[0]['actor'] = 'virtual_user'
    harness.run()
    harness.turns[:] = [harness.turns[-1]] * 2000
    harness.run()
    assert not harness.calls


def test_typed_durable_admission_failure_does_not_spend_recovery(harness):
    harness.enqueue('failed')
    harness.run()
    harness.turns[-1]['projection']['error'] = {'code': 'task_start_failed'}
    harness.turns[-1]['status'] = 'failed'
    integration.reset_autocontinue_chain(harness.key)
    harness.run('another-source')
    assert len(harness.calls) == 2


@pytest.mark.parametrize('error', [
    {'kind': 'internal', 'retryable': False, 'context': 'request-prepare',
     'detail': 'protocol_conversion:mandatory_continuity_unrepresentable'},
    {'kind': 'internal', 'context': 'request-prepare',
     'detail': 'protocol_conversion:mandatory_continuity_unrepresentable'},
    {'kind': 'tool_call_rejected', 'retryable': True,
     'rejection': {'kind': 'git_guard', 'retryable': True}},
    {'kind': 'abnormal_stop', 'retryable': True, 'autoRetryExhausted': True},
    {'kind': 'tool_call_rejected', 'retryable': True,
     'retryPolicy': {'mode': 'after_condition_change', 'automatic': False}},
])
@pytest.mark.parametrize('child_status', ['completed', 'failed'])
def test_main_failure_blocks_wake_and_retains_child_results(
        harness, error, child_status):
    harness.turns[-1].update(
        status='failed', settlement={'error': error},
        projection={'content': 'Partial result'})
    harness.enqueue(child_status)
    harness.run()
    # Repeated notification and process restart do not clear durable evidence.
    integration.reset_autocontinue_chain(harness.key)
    harness.run('later-wave')
    assert harness.calls == []
    assert agent_inbox.peek(harness.key) == 1
    assert harness.turns[-1]['status'] == 'failed'


def test_legacy_git_guard_projection_blocks_but_later_recovery_supersedes_it():
    human = {'turnId': 'h', 'actor': 'human', 'status': 'completed'}
    blocked = {
        'turnId': 'm', 'actor': 'assistant', 'status': 'failed',
        'projection': {'toolRounds': [{
            'toolName': 'run_command', 'status': 'rejected',
            'rejection': {'kind': 'git_guard', 'retryable': True},
        }]},
    }
    assert not _durable_chain_allows([human, blocked], 'wake', failure=False, ceiling=8)
    not_executed = {
        'turnId': 'admission-failure', 'actor': 'assistant', 'status': 'failed',
        'projection': {'error': {'code': 'task_start_failed', 'retryable': False}},
    }
    assert not _durable_chain_allows(
        [human, blocked, not_executed], 'wake', failure=False, ceiling=8)
    recovered = {'turnId': 'recovered', 'actor': 'assistant', 'status': 'completed'}
    assert _durable_chain_allows([human, blocked, recovered], 'wake', failure=False, ceiling=8)


def test_retryable_main_failure_keeps_existing_single_recovery_opportunity():
    turns = [
        {'turnId': 'h', 'actor': 'human', 'status': 'completed'},
        {'turnId': 'm', 'actor': 'assistant', 'status': 'failed',
         'settlement': {'error': {'kind': 'overloaded', 'retryable': True}}},
    ]
    assert _durable_chain_allows(turns, 'wake', failure=True, ceiling=8)



def test_optional_delivery_frontier_deduplicates_across_restart_and_admits_new_required_work(harness, monkeypatch):
    from lib.swarm.types import SubTaskSpec
    h = harness
    session = SimpleNamespace(user_id=17, specs=[
        SubTaskSpec(id='needed'), SubTaskSpec(id='extra', required_for_completion=False)])
    monkeypatch.setattr(integration, 'get_active_session', lambda key: session if key == h.key else None)
    h.enqueue('completed')
    h.run()
    first = h.calls[0][1]['command_id']
    integration.reset_autocontinue_chain(h.key)
    h.enqueue('completed')
    h.run()
    assert len(h.calls) == 1  # same persisted required frontier after restart
    session.specs.append(SubTaskSpec(id='new-needed'))
    h.enqueue('completed')
    h.run()
    assert len(h.calls) == 2
    assert h.calls[1][1]['command_id'] != first


@pytest.mark.parametrize('internal', [True, False])
def test_guard_terminal_classification_preserves_bounded_wake_gate(harness, internal):
    from threading import Event
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.swarm.liveness import ProgressBeacon
    from lib.turn_verdict import derive_task_verdict
    task = {'id': 'child-guard', 'abort_event': Event(), 'config': {},
            '_invocationAbortCheck': lambda: not internal}
    beacon = ProgressBeacon(stall_timeout=0)
    beacon.touch(task['id'])
    task['_invocationProgressBeacon'] = beacon
    assert invocation_abort_requested(task)
    verdict = derive_task_verdict(task)
    status = 'failed' if verdict.status.value == 'failed' else 'cancelled'
    agent_inbox.enqueue(harness.key, str(task.get('error') or 'User stopped agent'),
        priority='later', mode='swarm-update', agent_id=task['id'],
        extra={'status': status})
    harness.run()
    harness.run()  # duplicate notification
    integration.reset_autocontinue_chain(harness.key)
    harness.run('cold-notification')
    assert len(harness.calls) == (1 if internal else 0)
    if internal:
        assert harness.calls[0][0]['_swarmFailureRecovery'] is True
