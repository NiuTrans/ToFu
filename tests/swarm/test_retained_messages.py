"""Stable colleague dispatch: durable routing, authority and activation fences.

Use real retained/mailbox repositories and completion, replacing only portable
storage, owned conversation lookup and the ordinary spawn/session authorities.
No worker, model, network or background polling is needed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from lib.swarm import retained_messages
from lib.swarm.message_contract import NAMESPACE as MESSAGE_NAMESPACE
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.retained import complete_assignment, execute_manage_agents
from lib.swarm.retained_repository import RetainedAgentRepository, scope_for
from lib.swarm.run_messages import RunMessageError, RunMessageRepository, binding_for
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit

PROJECT = '/workspace/retained-message-tests'
CONTENT = 'Fresh 09:31 UTC\n  selected file\truntime — 雪🙂'


class ConcurrentRecordClient(RecordClient):
    """Serialize each entire read/CAS operation, not an entire service call."""

    def __init__(self):
        super().__init__()
        self.lock = RLock()

    def query(self, operation, payload):
        with self.lock:
            return super().query(operation, payload)

    def command(self, operation, payload, command_id):
        with self.lock:
            return super().command(operation, payload, command_id)


@pytest.fixture
def context(monkeypatch):
    client = ConcurrentRecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    task = {'id': 'turn-a', 'convId': 'conv-a', '_userId': 7,
            'config': {'projectPath': PROJECT}}

    from lib.identity import PrincipalContext
    task['_principalContext'] = PrincipalContext.user(subject_id='test-user', owner_user_id=7).to_payload()
    repository = RetainedAgentRepository(7)
    scope = scope_for(project_path=PROJECT)
    saved_messages = [{'role': 'user', 'content': 'Yesterday'},
                      {'role': 'assistant', 'content': 'Verified source A'}]
    agent = repository.create(scope, role='coder', name='Ada', messages=saved_messages)
    context = SimpleNamespace(
        client=client, task=task, repository=repository, scope=scope, agent=agent,
        cfg={'model': 'current-model', 'codeExecEnabled': False, 'max_parallel': 1},
        tools=[{'type': 'function', 'function': {'name': 'read_file'}}],
        launches=[], sessions={}, checkpoints={}, conversations={
            'conv-a': {'owner': 7, 'settings': {'projectPath': PROJECT}},
            'conv-b': {'owner': 7, 'settings': {'projectPath': PROJECT}},
        }, launch_behavior=None, saved_messages=saved_messages,
    )

    def get_conversation(_self, conv_id, *, user_id, include_messages=False):
        del include_messages
        row = context.conversations.get(conv_id)
        if row is None or row['owner'] != user_id:
            return None
        return SimpleNamespace(metadata={'settings': deepcopy(row['settings'])})

    def load_session(key, **_kwargs):
        return deepcopy(context.checkpoints.get(key))

    def get_session(key):
        return context.sessions.get(key)

    def launch(name, args, parent, **kwargs):
        assert name == 'spawn_agents'
        assert len(args['agents']) == 1
        spec_args = args['agents'][0]
        binding = kwargs['retained_binding']
        found = repository.get(scope, agent['id'])[0]
        # The stable identity CAS precedes physical launch, even under overlap.
        assert found['state'] == 'busy'
        assert found['run_id'] == spec_args['id'] == binding['run_id']
        assert binding['agent_id'] == agent['id'] and binding['scope'] == scope
        call = (deepcopy(args), deepcopy(parent), kwargs)
        with client.lock:
            context.launches.append(call)
        if context.launch_behavior is not None:
            return context.launch_behavior(name, args, parent, **kwargs)
        return publish(args, parent, kwargs)

    def publish(args, parent, kwargs):
        spec_args = args['agents'][0]
        binding = kwargs['retained_binding']
        spec = SubTaskSpec(
            id=spec_args['id'], role=spec_args['role'], objective=spec_args['objective'],
            retained_agent_id=binding['agent_id'], retained_scope=binding['scope'],
            retained_run_id=binding['run_id'],
        )

        from lib.swarm.master import bind_retained_assignment_context
        bind_retained_assignment_context(spec, parent_task=parent,
            parent_config=kwargs['cfg'], all_tools=kwargs['all_tools'],
            model=kwargs['cfg']['model'], project_path=PROJECT)
        key = parent['convId'] or parent['id']
        context.sessions[key] = SimpleNamespace(
            task_id=parent['id'], conv_id=parent['convId'], user_id=parent['_userId'],
            inbox_key=key, _parent_task_proxy=deepcopy(parent), specs=[spec],
            _lock=RLock(), _results_by_id={}, _agents={}, _aborted=False,
            _restart_suspended=False, is_terminated=False, abort_check=lambda: False,
        )
        context.checkpoints[key] = {
            'task_id': parent['id'], 'conv_id': parent['convId'], 'status': 'running',

            'specs': [spec.to_dict()],
            'config': {'user_id': parent['_userId'], 'parent_cfg': deepcopy(parent['config']),
                       'project_path': PROJECT},
            'agents': [{'agent_id': spec.id, 'role': spec.role, 'status': 'running',
                        'retained_agent_id': binding['agent_id'],
                        'retained_scope': binding['scope'], 'retained_run_id': binding['run_id']}],
        }
        return json.dumps({'status': 'async_launched', 'swarm_key': key,
                           'swarm_run_id': parent['id'],
                           'agents': [{'id': spec.id, 'role': spec.role}]})

    context.publish = publish
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', get_conversation)
    monkeypatch.setattr('lib.swarm.persistence.load_session', load_session)
    monkeypatch.setattr('lib.swarm.integration.get_active_session', get_session)
    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool', launch)
    return context


def request(context, **changes):
    return {'retained_agent_id': context.agent['id'], 'content': CONTENT,
            'idempotency_key': 'logical-message-1', **changes}


def test_gateway_stable_message_keeps_service_owned_intent_default(context):
    from lib.swarm.message_contract import SEND_AGENT_MESSAGE_TOOL
    from lib.tools.contracts import (
        compile_execution_contract_documents, validate_tool_arguments_from_documents,
    )
    documents = compile_execution_contract_documents([SEND_AGENT_MESSAGE_TOOL])
    authored = request(context)
    prepared = validate_tool_arguments_from_documents(documents, 'send_agent_message', authored)
    assert 'intent' not in prepared
    receipt = retained_messages.send(context.task, prepared, cfg=context.cfg,
        all_tools=context.tools, project_path=PROJECT)
    assert receipt['state'] == 'turn_started'
    assert receipt['execution_started'] is True
    assert len(context.launches) == 1
    assert send(context, intent='message') == receipt


def send(context, *, task=None, project=PROJECT, **changes):
    return retained_messages.send(task or context.task, request(context, **changes),
        cfg=context.cfg, all_tools=context.tools, project_path=project)


def read(context, *, task=None, project=PROJECT, **changes):
    return retained_messages.read(task or context.task,
        {'retained_agent_id': context.agent['id'], 'idempotency_key': 'logical-message-1', **changes},
        project_path=project)


def assert_receipt(context, receipt, *, state, started, content=CONTENT):
    assert receipt['retained_agent_id'] == context.agent['id']
    assert receipt['scope'] == 'project'
    assert receipt['message_id']
    assert receipt['state'] == state
    assert receipt['kind'] == 'reference'
    assert receipt['contentChars'] == len(content)
    assert receipt['execution_started'] is started
    assignment = receipt['assignment']
    assert assignment['agent_id']
    assert assignment['swarm_run_id'] == context.task['id']
    assert assignment['conversation_id'] == context.task['convId']
    return assignment


def settle(context, receipt, *, status='completed'):
    assignment = receipt['assignment']
    key = assignment['conversation_id'] or assignment['swarm_run_id']
    session = context.sessions.get(key)
    if session:
        spec = next(spec for spec in session.specs if spec.id == assignment['agent_id'])
        RunMessageRepository(binding_for(session._parent_task_proxy, spec, key)).close()
        with session._lock:
            session._results_by_id[spec.id] = SimpleNamespace(status=status)
    if key in context.checkpoints:
        row = context.checkpoints[key]['agents'][0]
        row.update(status=status, messages=[{'role': 'assistant', 'content': 'Saved result'}],
                   result={'status': status, 'final_answer': 'Saved result'})
    assert complete_assignment(user_id=7, tenant_id='', scope=context.scope,
        agent_id=context.agent['id'], run_id=assignment['agent_id'],
        messages=[{'role': 'assistant', 'content': 'Saved result'}],
        result={'status': status, 'final_answer': 'Saved result'})


def mailbox(context, receipt):
    assignment = receipt['assignment']
    session = context.sessions[assignment['conversation_id']]
    spec = next(spec for spec in session.specs if spec.id == assignment['agent_id'])
    return RunMessageRepository(binding_for(session._parent_task_proxy, spec, session.inbox_key))


def test_idle_message_launches_with_saved_history_and_current_authority(context):
    from lib.swarm.master import MasterOrchestrator

    first = send(context)
    assignment = assert_receipt(context, first, state='turn_started', started=True)
    args, parent, kwargs = context.launches[0]
    assert args['agents'][0]['objective'] == CONTENT
    assert args['agents'][0]['max_retries'] == 0
    assert kwargs['cfg'] == context.cfg and kwargs['all_tools'] == context.tools
    assert (parent['id'], parent['convId'], parent['_userId']) == (
        context.task['id'], context.task['convId'], context.task['_userId'])
    assert kwargs['project_path'] == PROJECT
    assert first['assignment']['launch']['status'] == 'async_launched'
    session = context.sessions['conv-a']
    master = MasterOrchestrator(task_id=context.task['id'], conv_id='conv-a', user_id=7,
        specs=session.specs, model=context.cfg['model'], project_path=PROJECT,
        parent_task=context.task, parent_config=context.cfg)
    child = master._make_agent(session.specs[0])
    assert [row['content'] for row in child.messages if row.get('role') == 'user'][:2] == [
        'Yesterday', CONTENT]
    assert any(row.get('content') == 'Verified source A' for row in child.messages)
    assert context.repository.get(context.scope, context.agent['id'])[0]['run_id'] == assignment['agent_id']
    assert read(context)['message_id'] == first['message_id']


def test_duplicate_after_terminal_and_reset_keeps_original_receipt_without_launch(context):
    first = send(context)
    settle(context, first)
    later = {**context.task, 'id': 'turn-b', 'convId': 'conv-b'}
    assert send(context, task=later) == first
    reset = json.loads(execute_manage_agents(
        {'action': 'reset', 'agent_id': context.agent['id']}, task=later,
        cfg=context.cfg, all_tools=context.tools, project_path=PROJECT))
    assert reset['agent']['generation'] == 2
    assert send(context, task=later) == first
    assert read(context, task=later)['assignment'] == first['assignment']
    assert len(context.launches) == 1
    assert context.repository.get(context.scope, context.agent['id'])[0]['state'] == 'idle'


def test_same_key_changed_content_kind_or_intent_is_conflict_even_after_reset(context):
    first = send(context)
    settle(context, first)
    json.loads(execute_manage_agents({'action': 'reset', 'agent_id': context.agent['id']},
        task=context.task, cfg=context.cfg, all_tools=context.tools, project_path=PROJECT))
    for changed in ({'content': 'Changed'}, {'kind': 'steer'}, {'intent': 'resume'}):
        with pytest.raises(RunMessageError) as error:
            send(context, **changed)
        assert error.value.code == 'message_idempotency_conflict'
    assert len(context.launches) == 1
    assert read(context)['message_id'] == first['message_id']


def test_owner_tenant_scope_and_root_only_admission_do_not_modify_identity(context):
    before = context.repository.get(context.scope, context.agent['id'])
    cases = [
        ({**context.task, '_userId': 8}, PROJECT, {}),
        ({**context.task, '_tenant_id': 'other'}, PROJECT, {}),
        (context.task, '/workspace/other', {}),
        (context.task, PROJECT, {'scope': 'conversation'}),
        ({**context.task, '_parentTaskId': 'parent'}, PROJECT, {}),
        ({**context.task, 'convId': 'missing'}, PROJECT, {}),
        ({**context.task, '_transientRuntime': True}, PROJECT, {}),
    ]
    for task, project, changes in cases:
        with pytest.raises(RunMessageError):
            send(context, task=task, project=project, **changes)
        with pytest.raises(RunMessageError):
            read(context, task=task, project=project, **changes)
    for setting in ({'projectPath': '/workspace/rebound'},
                    {'projectPath': PROJECT, 'archived': True},
                    {'projectPath': PROJECT, 'readOnly': True}):
        context.conversations['conv-a']['settings'] = setting
        with pytest.raises(RunMessageError):
            send(context)
    assert context.repository.get(context.scope, context.agent['id']) == before
    assert not context.launches


def test_archived_cancelled_and_stopped_caller_require_explicit_safe_authority(context):
    context.repository.change(context.scope, context.agent['id'],
        lambda value: {**value, 'state': 'archived'})
    with pytest.raises(RunMessageError):
        send(context, intent='resume')
    context.repository.change(context.scope, context.agent['id'],
        lambda value: {**value, 'state': 'idle', 'last_status': 'cancelled'})
    with pytest.raises(RunMessageError):
        send(context)
    for stopped in ({**context.task, 'aborted': True},
                    {**context.task, 'abort_event': Event()}):
        if stopped.get('abort_event') is not None:
            stopped['abort_event'].set()
        with pytest.raises(RunMessageError):
            send(context, task=stopped, intent='resume')
    with pytest.raises(RunMessageError):
        retained_messages.send(context.task, request(context, intent='resume'),
            cfg=context.cfg, all_tools=context.tools, project_path=PROJECT,
            abort_check=lambda: True)
    assert not context.launches
    resumed = send(context, intent='resume')
    assert_receipt(context, resumed, state='turn_started', started=True)
    assert len(context.launches) == 1


def test_busy_message_uses_exact_owned_current_assignment_from_other_project_turn(context):
    first = send(context)
    later = {**context.task, 'id': 'turn-b', 'convId': 'conv-b'}
    receipt = send(context, task=later, idempotency_key='busy-message')
    assert_receipt(context, receipt, state='submitted', started=False)
    assert {key: receipt['assignment'][key] for key in ('agent_id', 'swarm_run_id', 'conversation_id')} == {
        key: first['assignment'][key] for key in ('agent_id', 'swarm_run_id', 'conversation_id')}
    items = mailbox(context, first).load()[0]['messages']
    assert len(items) == 1 and items[0]['content'] == CONTENT
    assert items[0]['senderTaskId'] == 'turn-b'
    assert send(context, task=later, idempotency_key='busy-message') == receipt
    assert len(context.launches) == 1


def test_busy_foreign_superseded_or_suspended_session_never_spawns_replacement(context):
    first = send(context)
    session = context.sessions['conv-a']
    for attribute, bad in [('user_id', 8), ('task_id', 'replacement'),
                           ('_restart_suspended', True), ('_aborted', True)]:
        original = getattr(session, attribute)
        setattr(session, attribute, bad)
        with pytest.raises(RunMessageError):
            send(context, idempotency_key='blocked-' + attribute)
        setattr(session, attribute, original)
    original = context.checkpoints['conv-a']['agents'][0]['status']
    context.checkpoints['conv-a']['agents'][0]['status'] = 'interrupted'
    session._restart_suspended = True
    with pytest.raises(RunMessageError):
        send(context, intent='resume', idempotency_key='interrupted')
    context.checkpoints['conv-a']['agents'][0]['status'] = original
    assert len(context.launches) == 1
    assert mailbox(context, first).load()[0]['messages'] == []


def test_completion_before_mailbox_commit_never_reroutes_to_fresh_turn(context, monkeypatch):
    first = send(context)
    original = RunMessageRepository.submit

    def finish_before_submit(repository, **kwargs):
        settle(context, first)
        return original(repository, **kwargs)

    monkeypatch.setattr(RunMessageRepository, 'submit', finish_before_submit)
    with pytest.raises(RunMessageError):
        send(context, idempotency_key='raced-before')
    assert len(context.launches) == 1
    assert mailbox(context, first).load()[0]['messages'] == []
    with pytest.raises(RunMessageError):
        send(context, idempotency_key='raced-before')
    assert len(context.launches) == 1


def test_completion_after_mailbox_cas_returns_saved_unconfirmed_not_false_rejection(context, monkeypatch):
    first = send(context)
    original = context.client.command
    triggered = False

    def finish_after_commit(operation, payload, command_id):
        nonlocal triggered
        result = original(operation, payload, command_id)
        if (not triggered and operation == 'record.put'
                and payload['namespace'] == MESSAGE_NAMESPACE
                and payload['value']['messages']):
            triggered = True
            settle(context, first)
        return result

    monkeypatch.setattr(context.client, 'command', finish_after_commit)
    raced = send(context, idempotency_key='raced-after')
    assert triggered
    # The sender can report its committed snapshot; fresh read observes the
    # exact run's independently closed mailbox without false rejection.
    assert_receipt(context, raced, state='submitted', started=False)
    assert {key: raced['assignment'][key] for key in ('agent_id', 'swarm_run_id', 'conversation_id')} == {
        key: first['assignment'][key] for key in ('agent_id', 'swarm_run_id', 'conversation_id')}
    assert read(context, idempotency_key='raced-after')['state'] == 'unconfirmed'
    retried = send(context, idempotency_key='raced-after')
    assert retried['state'] == 'unconfirmed'
    assert retried['assignment'] == raced['assignment']
    items = mailbox(context, first).load()[0]['messages']
    assert len(items) == 1 and items[0]['id'] == raced['assignment']['message_id']
    assert len(context.launches) == 1


def test_launch_rejection_releases_identity_but_dedup_preserves_rejected_outcome(context):
    context.launch_behavior = lambda *a, **kw: json.dumps(
        {'status': 'error', 'code': 'execution_budget_exhausted', 'message': 'No capacity'})
    with pytest.raises(RunMessageError) as first:
        send(context)
    assert first.value.code == 'execution_budget_exhausted'
    assert first.value.submission_state == 'rejected'
    saved = context.repository.get(context.scope, context.agent['id'])[0]
    assert saved['state'] == 'idle' and not saved['run_id']
    for _ in range(2):
        with pytest.raises(RunMessageError) as retry:
            send(context)
        assert retry.value.code == first.value.code
        assert retry.value.submission_state == 'rejected'
    rejected = read(context)
    assert rejected['submission_state'] == 'rejected'
    assert rejected['code'] == 'execution_budget_exhausted'
    assert len(context.launches) == 1


def test_lost_launch_ack_reconciles_exact_durable_child_without_second_spawn(context):
    def lose_ack(name, args, parent, **kwargs):
        del name
        context.publish(args, parent, kwargs)
        raise RuntimeError('ordinary launch ACK lost after acceptance')

    context.launch_behavior = lose_ack
    try:
        first = send(context)
    except RunMessageError as error:
        assert error.code == 'message_activation_unknown'
        assert error.submission_state == 'unknown'
        first = None
    # Only the durable exact child witness is available on this retry.
    context.sessions.clear()
    recovered = send(context)
    assert_receipt(context, recovered, state='turn_started', started=True)
    if first is not None:
        assert recovered == first
    assert read(context)['assignment'] == recovered['assignment']
    assert len(context.launches) == 1


def test_process_boundary_claim_without_witness_is_unknown_not_permission_to_spawn(context):
    class ProcessLost(BaseException):
        pass

    def disappear(*_args, **_kwargs):
        raise ProcessLost('process vanished after durable claim')

    context.launch_behavior = disappear
    with pytest.raises(ProcessLost):
        send(context)
    saved = context.repository.get(context.scope, context.agent['id'])[0]
    assert saved['state'] == 'busy' and saved['run_id']
    # Drop all service-local state while preserving only portable records.
    importlib.reload(retained_messages)
    context.launch_behavior = None
    with pytest.raises(RunMessageError) as retry:
        send(context)
    assert retry.value.code == 'message_activation_unknown'
    assert retry.value.submission_state == 'unknown'
    unknown = read(context)
    assert unknown['submission_state'] == 'unknown'

    # Unrelated execution evidence is not a witness for this claimed child.
    checkpoint = {
        'task_id': context.task['id'], 'conv_id': 'conv-a', 'status': 'running',
        'config': {'user_id': 7, 'parent_cfg': context.task['config']},
        'agents': [{'agent_id': saved['run_id'], 'status': 'running'}],
    }
    for mutation in ('owner', 'run', 'child'):
        wrong = deepcopy(checkpoint)
        if mutation == 'owner':
            wrong['config']['user_id'] = 8
        elif mutation == 'run':
            wrong['task_id'] = 'replacement-turn'
        else:
            wrong['agents'][0]['agent_id'] = 'unrelated-child'
        context.checkpoints['conv-a'] = wrong
        with pytest.raises(RunMessageError) as foreign:
            send(context)
        assert foreign.value.code == 'message_activation_unknown'
        assert foreign.value.submission_state == 'unknown'
    assert len(context.launches) == 1


def test_concurrent_same_message_claims_only_one_launch(context):
    entered, release = Event(), Event()

    def blocked(name, args, parent, **kwargs):
        del name
        entered.set()
        assert release.wait(5), 'test did not release spawn'
        return context.publish(args, parent, kwargs)

    context.launch_behavior = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, context)
        try:
            assert entered.wait(5), 'first dispatch never reached spawn'
            second = pool.submit(send, context)
            try:
                overlapping = second.result(timeout=5)
            except RunMessageError as error:
                assert error.code == 'message_activation_unknown'
                assert error.submission_state == 'unknown'
                overlapping = None
        finally:
            release.set()
        receipt = first.result(timeout=5)
    if overlapping is not None:
        assert overlapping['message_id'] == receipt['message_id']
        assert overlapping['assignment'] == receipt['assignment']
    assert send(context) == receipt
    assert len(context.launches) == 1


def test_competing_idle_messages_cannot_double_reserve_or_launch(context):
    entered, release = Event(), Event()

    def blocked(name, args, parent, **kwargs):
        del name
        entered.set()
        assert release.wait(5), 'test did not release spawn'
        return context.publish(args, parent, kwargs)

    context.launch_behavior = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, context)
        try:
            assert entered.wait(5), 'first dispatch never reserved identity'
            competing = pool.submit(send, context, idempotency_key='competing-message')
            with pytest.raises(RunMessageError):
                competing.result(timeout=5)
        finally:
            release.set()
        receipt = first.result(timeout=5)
    assert len(context.launches) == 1
    saved = context.repository.get(context.scope, context.agent['id'])[0]
    assert saved['run_id'] == receipt['assignment']['agent_id']
    assert mailbox(context, receipt).load()[0]['messages'] == []


def test_mixed_target_forms_and_authority_fields_are_rejected_before_dispatch(context):
    for changes in ({'agent_id': 'temporary-child', 'swarm_run_id': 'turn-a'},
                    {'agent_id': 'temporary-child'}, {'owner': 999},
                    {'scope': 'p:caller-supplied'}, {'intent': 'continue'}):
        with pytest.raises(RunMessageError):
            send(context, **changes)
    with pytest.raises(RunMessageError):
        retained_messages.read(context.task, {'retained_agent_id': context.agent['id']},
                               project_path=PROJECT)
    assert not context.launches
    assert context.repository.get(context.scope, context.agent['id'])[0]['state'] == 'idle'


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_cross_conversation_caller_stop_is_checked_at_mailbox_admission(context, monkeypatch, phase):
    first = send(context)
    later = {**context.task, 'id': 'turn-b', 'convId': 'conv-b', 'aborted': False}
    if phase == 'before':
        original = RunMessageRepository.submit
        def stop_before(repository, **kwargs):
            later['aborted'] = True
            return original(repository, **kwargs)
        monkeypatch.setattr(RunMessageRepository, 'submit', stop_before)
        with pytest.raises(RunMessageError) as error:
            send(context, task=later, idempotency_key='stopped-b')
        assert error.value.submission_state == 'rejected'
        assert not mailbox(context, first).load()[0]['messages']
    else:
        original = context.client.command
        def stop_after(operation, payload, command_id):
            result = original(operation, payload, command_id)
            if payload.get('namespace') == MESSAGE_NAMESPACE and payload['value']['messages']:
                later['aborted'] = True
            return result
        monkeypatch.setattr(context.client, 'command', stop_after)
        receipt = send(context, task=later, idempotency_key='stopped-b')
        assert receipt['state'] == 'submitted'
        assert len(mailbox(context, first).load()[0]['messages']) == 1
    assert len(context.launches) == 1


@pytest.mark.parametrize('revoked', [{'archived': True}, {'projectPath': '/workspace/rebound'}])
def test_committed_busy_message_scope_revocation_keeps_original_stable_receipt(context, monkeypatch, revoked):
    first = send(context)
    later = {**context.task, 'id': 'turn-b', 'convId': 'conv-b'}
    original = context.client.command
    def revoke_after(operation, payload, command_id):
        result = original(operation, payload, command_id)
        if payload.get('namespace') == MESSAGE_NAMESPACE and payload['value']['messages']:
            context.conversations['conv-a']['settings'].update(revoked)
        return result
    monkeypatch.setattr(context.client, 'command', revoke_after)
    receipt = send(context, task=later, idempotency_key='revoked-scope')
    assert receipt['state'] == 'submitted'
    assert send(context, task=later, idempotency_key='revoked-scope') == receipt
    assert read(context, task=later, idempotency_key='revoked-scope')['assignment'] == receipt['assignment']
    assert mailbox(context, first).load()[0]['messages'][0]['content'] == CONTENT
    assert len(context.launches) == 1
