"""Retained activation paths share admission evidence, never replay permission."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from lib.swarm.retained import execute_manage_agents
from lib.swarm.run_messages import RunMessageError
from tests.swarm.test_retained_messages import PROJECT, context, read, send

pytestmark = pytest.mark.unit


def manage(context, action='assign', **changes):
    return json.loads(execute_manage_agents(
        {'action': action, 'agent_id': context.agent['id'],
         **({'objective': 'Investigate'} if action == 'assign' else {}), **changes},
        task=context.task, cfg=context.cfg, all_tools=context.tools, project_path=PROJECT))


def saved(context):
    return context.repository.get(context.scope, context.agent['id'])[0]


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('reply', [
    'not JSON', 'null', '[]', 'true',
    json.dumps({'status': 'error', 'code': 'swarm_launch_unknown',
                'submission_state': 'unknown'}),
    json.dumps({'status': 'error', 'code': 'unrecognized_failure'}),
])
def test_unknown_launch_keeps_original_reservation_across_inspection_and_retry(context, path, reply):
    context.launch_behavior = lambda *args, **kwargs: reply
    if path == 'assign':
        outcome = manage(context)
        assert outcome['submission_state'] == 'unknown'
    else:
        with pytest.raises(RunMessageError) as error:
            send(context)
        assert error.value.submission_state == 'unknown'
    original = deepcopy(saved(context))
    assert original['state'] == 'busy'
    assert original['run_id']
    assert manage(context, 'inspect')['agent']['state'] == 'busy'
    assert manage(context)['code'] == 'retained_agent_error'
    with pytest.raises(RunMessageError):
        send(context)
    assert saved(context)['run_id'] == original['run_id']
    assert len(context.launches) == 1


def activate(context, path):
    if path == 'assign':
        return manage(context)
    return send(context)


def assert_unknown(context, path):
    if path == 'assign':
        result = manage(context)
        assert result['status'] == 'error'
        assert result['submission_state'] == 'unknown'
    else:
        with pytest.raises(RunMessageError) as error:
            send(context)
        assert error.value.submission_state == 'unknown'
    assert saved(context)['state'] == 'busy'


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('fault', [
    'no_session', 'bad_session', 'wrong_child', 'no_children', 'extra_child',
    'duplicate_child', 'bad_child', 'bad_children', 'wrong_key',
    'wrong_child_session', 'wrong_child_conversation', 'contradictory_submission', 'error_code',
])
def test_only_exact_expected_assignment_success_is_accepted(context, path, fault):
    def reply(_name, args, parent, **_kwargs):
        child = {'id': args['agents'][0]['id']}
        handle = {'status': 'async_launched', 'swarm_run_id': parent['id'],
                  'swarm_key': parent['convId'], 'agents': [child]}
        if fault == 'no_session':
            handle.pop('swarm_run_id')
        elif fault == 'bad_session':
            handle['swarm_run_id'] = 7
        elif fault == 'wrong_child':
            child['id'] = 'unrequested-child'
        elif fault == 'no_children':
            handle['agents'] = []
        elif fault == 'extra_child':
            handle['agents'].append({'id': 'unrequested-child'})
        elif fault == 'duplicate_child':
            handle['agents'].append(deepcopy(child))
        elif fault == 'bad_child':
            handle['agents'] = [None]
        elif fault == 'bad_children':
            handle['agents'] = {'id': child['id']}
        elif fault == 'wrong_key':
            handle['swarm_key'] = 'foreign-conversation'
        elif fault == 'wrong_child_session':
            child['swarm_run_id'] = 'foreign-session'
        elif fault == 'wrong_child_conversation':
            child['conversation_id'] = 'foreign-conversation'
        elif fault == 'error_code':
            handle['code'] = 'invalid_agent'
        else:
            handle['submission_state'] = 'rejected'
        return json.dumps(handle)
    context.launch_behavior = reply
    assert_unknown(context, path)
    assert saved(context)['run_session_id'] == ''
    assert manage(context)['code'] == 'retained_agent_error'
    assert len(context.launches) == 1


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('rejection', [
    {'code': 'execution_budget_exhausted'},
    {'code': 'invalid_agent'},
    {'code': 'agent_dependency_cycle', 'submission_state': 'rejected', 'phase': 'validation'},
    {'code': 'swarm_context_preparation_failed', 'submission_state': 'rejected', 'phase': 'preparation'},
    {'code': 'swarm_tool_failed', 'submission_state': 'rejected', 'phase': 'preparation'},
])
def test_proven_rejection_releases_both_paths_and_new_authorized_work_can_launch(context, path, rejection):
    context.launch_behavior = lambda *args, **kwargs: json.dumps({'status': 'error', **rejection})
    if path == 'assign':
        assert manage(context)['submission_state'] == 'rejected'
    else:
        with pytest.raises(RunMessageError) as error:
            send(context)
        assert error.value.submission_state == 'rejected'
        assert read(context)['code'] == rejection['code']
        with pytest.raises(RunMessageError):
            send(context)
    assert saved(context)['state'] == 'idle'
    assert saved(context)['run_session_id'] == ''
    context.launch_behavior = None
    assert send(context, idempotency_key='new-authorized-message')['state'] == 'turn_started'
    assert saved(context)['run_launch_state'] == 'accepted'
    assert len(context.launches) == 2


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('submission_state', ['unknown', 'accepted', True, {'bad': 'state'}])
def test_nonrejected_submission_overrides_even_a_known_rejection_code(context, path, submission_state):
    context.launch_behavior = lambda *args, **kwargs: json.dumps({
        'status': 'error', 'code': 'invalid_agent', 'submission_state': submission_state})
    assert_unknown(context, path)
    assert len(context.launches) == 1


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('actual_binding', [False, True])
def test_unknown_terminal_checkpoint_needs_actual_binding_not_a_predicted_lookup(context, path, actual_binding):
    def lose_ack(_name, args, parent, **kwargs):
        context.publish(args, parent, kwargs)
        run_id = args['agents'][0]['id']
        if actual_binding:
            # The real common child factory binds this before worker entry.
            context.repository.change(context.scope, context.agent['id'], lambda current: {
                **current, 'run_session_id': parent['id'],
                'run_conversation_id': parent['convId'],
            })
        context.checkpoints[parent['convId']]['agents'][0].update(
            status='completed', messages=[{'role': 'assistant', 'content': 'Exact completed work'}],
            result={'status': 'completed', 'final_answer': 'Exact completed work'})
        raise RuntimeError('ACK and completion callback both lost')
    context.launch_behavior = lose_ack
    assert_unknown(context, path)
    original = saved(context)['run_id']
    inspected = manage(context, 'inspect')['agent']
    if actual_binding:
        assert inspected['state'] == 'idle'
        assert inspected['last_result'] == 'Exact completed work'
        assert saved(context)['last_assignment']['agent_id'] == original
    else:
        assert inspected['state'] == 'busy'
        assert inspected['assignment']['agent_id'] == original
        assert inspected['assignment']['swarm_run_id'] == ''
        assert manage(context)['code'] == 'retained_agent_error'
    assert len(context.launches) == 1
    if path == 'message':
        assert send(context)['state'] == 'turn_started'
        assert len(context.launches) == 1


@pytest.mark.parametrize('path', ['assign', 'message'])
def test_unknown_reply_preserves_original_lookup_names_without_claiming_acceptance(context, path):
    def reply(_name, args, parent, **_kwargs):
        return json.dumps({'status': 'error', 'code': 'swarm_launch_unknown',
            'submission_state': 'unknown', 'swarm_key': parent['convId'],
            'swarm_run_id': 'original-older-session',
            'requested_agent_ids': [args['agents'][0]['id']]})
    context.launch_behavior = reply
    assert_unknown(context, path)
    original = saved(context)
    lookup = manage(context, 'inspect')['agent']['launch_lookup']
    assert lookup == {'swarm_key': 'conv-a', 'swarm_run_id': 'original-older-session',
                      'requested_agent_ids': [original['run_id']]}
    assert original['run_session_id'] == ''
    if path == 'message':
        assert read(context)['launch_lookup'] == lookup
    assert manage(context)['code'] == 'retained_agent_error'
    assert len(context.launches) == 1


@pytest.mark.parametrize('path', ['assign', 'message'])
def test_crash_after_durable_reservation_is_not_a_replay_lease(context, path):
    import importlib
    from lib.swarm import retained, retained_messages

    class ProcessLost(BaseException):
        pass
    def crash(*_args, **_kwargs):
        raise ProcessLost('process vanished at launch boundary')
    context.launch_behavior = crash
    with pytest.raises(ProcessLost):
        activate(context, path)
    original = saved(context)['run_id']
    importlib.reload(retained)
    importlib.reload(retained_messages)
    context.launch_behavior = None
    inspected = manage(context, 'inspect')['agent']
    assert inspected['state'] == 'busy'
    assert inspected['launch_lookup']['requested_agent_ids'] == [original]
    assert manage(context)['code'] == 'retained_agent_error'
    with pytest.raises(RunMessageError):
        send(context)
    assert saved(context)['run_id'] == original
    assert len(context.launches) == 1


@pytest.mark.parametrize('first_path', ['assign', 'message'])
def test_concurrent_activation_paths_cannot_double_launch(context, first_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    def blocked(_name, args, parent, **kwargs):
        entered.set()
        assert release.wait(5)
        return context.publish(args, parent, kwargs)
    context.launch_behavior = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(activate, context, first_path)
        try:
            assert entered.wait(5)
            if first_path == 'assign':
                with pytest.raises(RunMessageError):
                    pool.submit(send, context).result(timeout=5)
            else:
                assert pool.submit(manage, context).result(timeout=5)['code'] == 'retained_agent_error'
        finally:
            release.set()
        assert first.result(timeout=5)
    assert saved(context)['run_launch_state'] == 'accepted'
    assert len(context.launches) == 1


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('late_outcome', ['accepted', 'rejected', 'unknown'])
def test_late_launch_receipt_cannot_modify_new_generation_or_assignment(context, path, late_outcome):
    replacement = {}
    def replace_during_launch(_name, args, parent, **_kwargs):
        replacement['old_run'] = args['agents'][0]['id']
        assert manage(context, 'reset', abandon=True)['agent']['generation'] == 2
        context.launch_behavior = None
        replacement['receipt'] = send(context, idempotency_key='replacement-authorized-message')
        replacement['value'] = deepcopy(saved(context))
        if late_outcome == 'accepted':
            return json.dumps({'status': 'async_launched', 'swarm_run_id': parent['id'],
                               'agents': [{'id': replacement['old_run']}]})
        return json.dumps({'status': 'error', 'code': (
            'invalid_agent' if late_outcome == 'rejected' else 'swarm_launch_unknown')})
    context.launch_behavior = replace_during_launch
    if late_outcome == 'accepted':
        assert activate(context, path)
    elif path == 'assign':
        assert manage(context)['submission_state'] == late_outcome
    else:
        with pytest.raises(RunMessageError) as error:
            send(context)
        assert error.value.submission_state == late_outcome
    current = saved(context)
    assert current['state'] == 'busy'
    for field in ('generation', 'run_id', 'run_session_id', 'run_conversation_id',
                  'swarm_key', 'run_launch_state', 'run_launch_lookup'):
        assert current[field] == replacement['value'][field]
    assert current['run_id'] != replacement['old_run']
    assert len(context.launches) == 2


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('committed', [False, True])
@pytest.mark.parametrize('receipt', ['accepted', 'rejected'])
def test_outcome_write_failure_preserves_original_claim_and_never_replays_launch(
        context, monkeypatch, path, committed, receipt):
    from lib.storage.errors import StorageError
    from lib.swarm.retained_repository import NAMESPACE

    original_command = context.client.command
    injected = False
    def fail_outcome_write(operation, payload, command_id):
        nonlocal injected
        value = payload.get('value') or {}
        is_outcome = (value.get('run_launch_state') == 'accepted'
                      or (value.get('state') == 'idle' and value.get('message_dispatches'))
                      or (value.get('state') == 'idle' and value.get('run_launch_state') == ''))
        if (not injected and operation == 'record.put' and payload.get('namespace') == NAMESPACE
                and is_outcome):
            injected = True
            if committed:
                original_command(operation, payload, command_id)
            # Non-conflict failures are not a fresh-CAS retry permission.
            raise StorageError('database_unavailable', 'injected outcome persistence failure',
                               retryable=True, request_not_dispatched=not committed)
        return original_command(operation, payload, command_id)
    monkeypatch.setattr(context.client, 'command', fail_outcome_write)
    if receipt == 'rejected':
        context.launch_behavior = lambda *args, **kwargs: json.dumps({
            'status': 'error', 'code': 'invalid_agent'})
    if not committed:
        assert_unknown(context, path)
        assert injected
        assert saved(context)['run_session_id'] == ''
        assert manage(context)['code'] == 'retained_agent_error'
    else:
        # Lost ACK after the exact authority commit is recovered by repository
        # receipt reconciliation, not by another physical launch.
        if receipt == 'accepted':
            assert activate(context, path)
            assert saved(context)['state'] == 'busy'
            assert saved(context)['run_launch_state'] == 'accepted'
        elif path == 'assign':
            assert manage(context)['submission_state'] == 'rejected'
            assert saved(context)['state'] == 'idle'
        else:
            with pytest.raises(RunMessageError) as error:
                send(context)
            assert error.value.submission_state == 'rejected'
            assert saved(context)['state'] == 'idle'
        assert injected
    assert len(context.launches) == 1


@pytest.mark.parametrize('path', ['assign', 'message'])
@pytest.mark.parametrize('fault', [
    'owner', 'tenant', 'session', 'conversation', 'retained_agent', 'retained_scope',
    'retained_run', 'duplicate_spec', 'duplicate_child', 'malformed_config',
])
def test_unknown_launch_reconciliation_never_imports_foreign_or_ambiguous_child(context, path, fault):
    def lose_ack(_name, args, parent, **kwargs):
        context.publish(args, parent, kwargs)
        # This is actual child-factory binding, not a guessed launch hint.
        context.repository.change(context.scope, context.agent['id'], lambda current: {
            **current, 'run_session_id': parent['id'], 'run_conversation_id': parent['convId'],
        })
        row = context.checkpoints[parent['convId']]
        row['agents'][0].update(status='completed',
            messages=[{'role': 'assistant', 'content': 'Must not import foreign memory'}],
            result={'status': 'completed', 'final_answer': 'Must not disclose foreign result'})
        if fault == 'owner':
            row['config']['user_id'] = 8
        elif fault == 'tenant':
            row['config']['parent_cfg']['_tenant_id'] = 'other-tenant'
        elif fault == 'session':
            row['task_id'] = 'replacement-session'
        elif fault == 'conversation':
            row['conv_id'] = 'replacement-conversation'
        elif fault == 'retained_agent':
            row['specs'][0]['retained_agent_id'] = 'foreign-agent'
        elif fault == 'retained_scope':
            row['specs'][0]['retained_scope'] = 'foreign-scope'
        elif fault == 'retained_run':
            row['specs'][0]['retained_run_id'] = 'foreign-run'
        elif fault == 'duplicate_spec':
            row['specs'].append(deepcopy(row['specs'][0]))
        elif fault == 'duplicate_child':
            row['agents'].append(deepcopy(row['agents'][0]))
        else:
            row['config'] = ['not a config']
        raise RuntimeError('lost launch reply')
    context.launch_behavior = lose_ack
    assert_unknown(context, path)
    original = deepcopy(saved(context))
    assert manage(context, 'inspect')['agent']['state'] == 'busy'
    assert saved(context)['messages'] == context.saved_messages
    assert manage(context)['code'] == 'retained_agent_error'
    if path == 'message':
        assert read(context)['submission_state'] == 'unknown'
        with pytest.raises(RunMessageError) as error:
            send(context)
        assert error.value.submission_state == 'unknown'
    assert saved(context)['run_id'] == original['run_id']
    assert len(context.launches) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_stable_lost_reply_uses_actual_retained_binding_not_predicted_session(context, completed):
    from lib.swarm.retained import complete_assignment

    def lost_reply(_name, args, parent, **kwargs):
        actual_parent = {**parent, 'id': 'actual-older-session'}
        context.publish(args, actual_parent, kwargs)
        context.repository.change(context.scope, context.agent['id'], lambda current: {
            **current, 'run_session_id': actual_parent['id'],
            'run_conversation_id': parent['convId'],
        })
        if completed:
            assert complete_assignment(user_id=7, tenant_id='', scope=context.scope,
                agent_id=context.agent['id'], run_id=args['agents'][0]['id'],
                messages=[{'role': 'assistant', 'content': 'Completed exact work'}],
                result={'status': 'completed', 'final_answer': 'Completed exact work'})
            context.checkpoints.clear()  # Immutable retained completion is enough.
        raise RuntimeError('launch reply lost before a handle could be returned')

    context.launch_behavior = lost_reply
    with pytest.raises(RunMessageError) as error:
        send(context)
    assert error.value.submission_state == 'unknown'
    outcome = send(context)
    assert outcome['state'] == 'turn_started'
    assert outcome['assignment']['swarm_run_id'] == 'actual-older-session'
    assert len(context.launches) == 1


@pytest.mark.parametrize('fault', ['run', 'conversation', 'key', 'session', 'generation'])
def test_lost_stable_reply_cannot_rebind_from_a_different_current_assignment(context, fault):
    def lost_reply(_name, args, parent, **kwargs):
        actual_parent = {**parent, 'id': 'actual-older-session'}
        context.publish(args, actual_parent, kwargs)
        context.repository.change(context.scope, context.agent['id'], lambda current: {
            **current, 'run_session_id': actual_parent['id'],
            'run_conversation_id': parent['convId'],
        })
        raise RuntimeError('launch reply lost')
    context.launch_behavior = lost_reply
    with pytest.raises(RunMessageError):
        send(context)
    fields = {'run': ('run_id', 'another-assignment'),
              'conversation': ('run_conversation_id', 'another-conversation'),
              'key': ('swarm_key', 'another-key'), 'session': ('run_session_id', ''),
              'generation': ('generation', 2)}
    field, changed = fields[fault]
    context.repository.change(context.scope, context.agent['id'], lambda current: {
        **current, field: changed})
    with pytest.raises(RunMessageError) as error:
        send(context)
    assert error.value.submission_state == 'unknown'
    assert len(context.launches) == 1
