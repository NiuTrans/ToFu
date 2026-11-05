"""Recovery follows submission evidence, exact/stable target and caller role.

Use the real tool entry and portable message/identity CAS. Lost replies and Stop
arrive at durable boundaries; no worker, provider, live storage or replay runs.
"""
import json
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.swarm import message_service
from lib.swarm.integration import _tools
from lib.swarm.run_messages import RunMessageError
from lib.tools.result_envelope import tool_result_error
from tests.swarm import test_retained_messages as retained_tests
from tests.swarm import test_run_messages as mailbox_tests
from tests.swarm.test_team_messages import worker_context

context = mailbox_tests.context
stable_context = retained_tests.context
pytestmark = pytest.mark.unit


@pytest.fixture
def dispatch(monkeypatch):
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)

    def call(name, request, task, **kwargs):
        return json.loads(_tools.execute_swarm_tool(name, request, task, **kwargs))

    return call


def exact_caller(context, monkeypatch, actor):
    if actor == 'worker':
        worker_context(context)
    monkeypatch.setattr(_tools, '_get_session', lambda key: context.session)
    task = context.child if actor == 'worker' else context.task
    target = 'child-b' if actor == 'worker' else 'child-a'
    return task, mailbox_tests.args(agent_id=target)


def read_target(request, message_id=None):
    target = {key: request[key] for key in ('agent_id', 'swarm_run_id')}
    if message_id is not None:
        target['message_id'] = message_id
    return target


@pytest.mark.parametrize('actor', ['root', 'worker'])
@pytest.mark.parametrize('reason', ['target_settled', 'run_ended', 'caller_stopped', 'scope_revoked'])
def test_rejected_exact_send_needs_no_receipt_or_replacement(context, monkeypatch, dispatch, actor, reason):
    caller, request = exact_caller(context, monkeypatch, actor)
    if reason == 'target_settled':
        context.session._results_by_id[request['agent_id']] = object()
    elif reason == 'run_ended':
        context.session.is_terminated = True
    elif reason == 'caller_stopped':
        caller['aborted'] = True
    else:
        monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get',
            lambda *a, **kw: SimpleNamespace(metadata={'settings': {'archived': True}}))
    result = dispatch('send_agent_message', request, caller)
    assert result['submission_state'] == 'rejected'
    assert result['retryable'] is False
    action = result['next_action']
    assert 'get_agent_messages' not in action
    assert 'retained identity may start a new turn' not in action
    assert 'unchanged' in action
    if actor == 'worker':
        assert 'final result' in action.lower()
    assert not context.client.rows


def test_worker_reporting_to_finished_coordinator_returns_findings_not_a_readback_loop(context, monkeypatch, dispatch):
    worker_context(context)
    context.task['status'] = 'done'
    monkeypatch.setattr(_tools, '_get_session', lambda key: context.session)
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get', lambda key: context.task)
    result = dispatch('send_agent_message', mailbox_tests.args(agent_id='launch'), context.child)
    assert result['code'] == 'message_target_not_active'
    assert result['submission_state'] == 'rejected' and result['retryable'] is False
    assert 'get_agent_messages' not in result['next_action']
    assert 'final result' in result['next_action'].lower()
    assert not context.client.rows


@pytest.mark.parametrize('controls', [(), ('poll',), ('poll', 'kill')])
def test_idle_root_rejection_tells_the_worker_what_to_do_instead(
        context, monkeypatch, dispatch, controls):
    """The idle original root is never woken; the worker finishes with its
    blocker or keeps working, and manages only its own background commands."""
    from lib.tools.poll import KILL_TOOL, POLL_TOOL
    worker_context(context)
    context.task['status'] = 'done'
    monkeypatch.setattr(_tools, '_get_session', lambda key: context.session)
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get', lambda key: context.task)
    schemas = {'poll': POLL_TOOL, 'kill': KILL_TOOL}
    context.child['_executable_tool_catalog'] = [schemas[name] for name in controls]
    result = dispatch('send_agent_message', mailbox_tests.args(agent_id='launch'), context.child)
    assert result['code'] == 'message_target_not_active'
    assert result['submission_state'] == 'rejected' and result['retryable'] is False
    action = result['next_action']
    assert 'sending cannot wake it' in action
    assert 'finish now with a final result that states the blocker' in action
    assert 'delivered to the coordinator automatically' in action
    assert ('poll their bg_* ids' in action) == ('poll' in controls)
    assert ('kill one that runs away' in action) == ('kill' in controls)
    assert 'get_agent_messages' not in action
    assert not context.client.rows


@pytest.mark.parametrize('actor', ['root', 'worker'])
@pytest.mark.parametrize('state', ['unknown', 'saved'])
@pytest.mark.parametrize('code', ['message_target_not_active', 'message_not_found',
                                  'message_invalid_identity', 'message_forbidden', 'message_scope_unavailable'])
def test_postcommit_evidence_takes_precedence_over_error_code(context, monkeypatch, dispatch, actor, state, code):
    caller, request = exact_caller(context, monkeypatch, actor)
    original = message_service.send
    saved = []

    def fail_projection(*args, **kwargs):
        saved.append(original(*args, **kwargs))
        context.session.is_terminated = True
        raise RunMessageError(code, 'synthetic failure after saved submission', submission_state=state)

    monkeypatch.setattr(message_service, 'send', fail_projection)
    result = dispatch('send_agent_message', request, caller)
    assert result['submission_state'] == state and result['retryable'] is False
    action = result['next_action']
    assert 'get_agent_messages' in action
    assert 'agent_id' in action and 'swarm_run_id' in action and 'idempotency_key' in action
    assert 'Correct the arguments' not in action
    assert 'retained identity may start a new turn' not in action
    history = dispatch('get_agent_messages', read_target(request, saved[0]['message_id']), caller)
    assert history['messages'][0]['id'] == saved[0]['message_id']
    assert history['messages'][0]['content'] == request['content']
    assert len(context.client.rows) == 1


@pytest.mark.parametrize('actor', ['root', 'worker'])
def test_lost_commit_ack_and_failed_reconciliation_do_not_offer_write_retry(context, monkeypatch, dispatch, actor):
    caller, request = exact_caller(context, monkeypatch, actor)
    original_command, original_query = context.client.command, context.client.query
    commits = []

    def unavailable(*args, **kwargs):
        raise StorageError('database_timeout', 'synthetic reconciliation unavailable', retryable=True)

    def lose_ack(*args, **kwargs):
        commits.append(original_command(*args, **kwargs))
        caller['aborted'] = True
        monkeypatch.setattr(context.client, 'query', unavailable)
        raise StorageError('database_timeout', 'synthetic lost commit ACK', retryable=True)

    monkeypatch.setattr(context.client, 'command', lose_ack)
    result = dispatch('send_agent_message', request, caller)
    assert result['submission_state'] == 'unknown'
    assert result['retryable'] is False  # Storage retryability is not a write replay grant.
    action = result['next_action']
    assert 'get_agent_messages' in action and 'swarm_run_id' in action
    assert 'idempotency_key' in action and 'replay' in action.lower()
    assert len(commits) == 1
    monkeypatch.setattr(context.client, 'query', original_query)
    history = dispatch('get_agent_messages', read_target(request), caller)
    assert len(history['messages']) == 1
    assert history['messages'][0]['content'] == request['content']
    assert len(commits) == 1


@pytest.mark.parametrize('actor', ['root', 'worker'])
def test_stop_after_saved_commit_is_a_receipt_not_a_rejected_recovery(context, monkeypatch, dispatch, actor):
    caller, request = exact_caller(context, monkeypatch, actor)
    original = context.client.command
    commits = []

    def stop_after_commit(*args, **kwargs):
        commits.append(original(*args, **kwargs))
        caller['aborted'] = True
        return commits[-1]

    monkeypatch.setattr(context.client, 'command', stop_after_commit)
    result = dispatch('send_agent_message', request, caller)
    assert result['state'] == 'submitted' and result['execution_started'] is False
    assert 'next_action' not in result and result.get('status') != 'error'
    history = dispatch('get_agent_messages', read_target(request, result['message_id']), caller)
    assert history['messages'][0]['content'] == request['content']
    assert len(commits) == 1


@pytest.mark.parametrize('reason', ['cancelled', 'archived', 'caller_stopped', 'scope_revoked'])
def test_root_stable_rejection_is_not_an_uncertain_submission(stable_context, dispatch, reason):
    c = stable_context
    if reason == 'cancelled':
        retained_tests.settle(c, retained_tests.send(c), status='cancelled')
    elif reason == 'archived':
        c.repository.change(c.scope, c.agent['id'], lambda value: {**value, 'state': 'archived'})
    elif reason == 'caller_stopped':
        c.task['aborted'] = True
    else:
        c.conversations['conv-a']['settings']['readOnly'] = True
    before = len(c.launches)
    result = dispatch('send_agent_message', retained_tests.request(c, idempotency_key='new-message'), c.task,
        cfg=c.cfg, all_tools=c.tools, project_path=retained_tests.PROJECT)
    assert result['submission_state'] == 'rejected' and result['retryable'] is False
    assert 'get_agent_messages' not in result['next_action']
    assert 'unchanged' in result['next_action']
    if reason == 'cancelled':
        assert 'intent=resume' in result['next_action']
        assert 'explicit' in result['next_action'].lower()
    assert len(c.launches) == before


def test_root_stable_lost_launch_ack_reconciles_original_identity_key_without_launch(stable_context, dispatch):
    c = stable_context

    def lose_ack(name, args, parent, **kwargs):
        c.publish(args, parent, kwargs)
        c.task['aborted'] = True
        raise RuntimeError('synthetic lost launch ACK after child checkpoint')

    c.launch_behavior = lose_ack
    request = retained_tests.request(c)
    result = dispatch('send_agent_message', request, c.task,
        cfg=c.cfg, all_tools=c.tools, project_path=retained_tests.PROJECT)
    assert result['code'] == 'message_activation_unknown'
    assert result['submission_state'] == 'unknown' and result['retryable'] is False
    assert 'retained_agent_id' in result['next_action'] and 'idempotency_key' in result['next_action']
    assert 'replay' in result['next_action'].lower()
    query = {key: request[key] for key in ('retained_agent_id', 'idempotency_key')}
    history = dispatch('get_agent_messages', query, c.task, project_path=retained_tests.PROJECT)
    assert history['state'] == 'turn_started'
    assert history['retained_agent_id'] == c.agent['id'] and history['content'] == request['content']
    assert len(c.launches) == 1


def test_stable_read_of_unresolved_dispatch_keeps_typed_original_key_recovery(stable_context, dispatch):
    c = stable_context

    def no_receipt(*args, **kwargs):
        raise RuntimeError('synthetic launch ACK unavailable, admission unknown')

    c.launch_behavior = no_receipt
    request = retained_tests.request(c)
    dispatch('send_agent_message', request, c.task,
        cfg=c.cfg, all_tools=c.tools, project_path=retained_tests.PROJECT)
    query = {key: request[key] for key in ('retained_agent_id', 'idempotency_key')}
    result = dispatch('get_agent_messages', query, c.task, project_path=retained_tests.PROJECT)
    assert result['submission_state'] == 'unknown'
    assert result['retryable'] is False
    assert 'get_agent_messages' in result['next_action']
    assert 'retained_agent_id' in result['next_action'] and 'idempotency_key' in result['next_action']
    assert result['assignment']['agent_id']
    typed = tool_result_error(json.dumps(result))
    assert typed is not None and typed.code == result['code'] and typed.retryable is False
    assert result['retained_agent_id'] == c.agent['id'] and result['content'] == request['content']
    assert len(c.launches) == 1


@pytest.mark.parametrize('state', ['rejected', 'unknown'])
def test_worker_stable_recovery_never_grants_root_only_reads(context, monkeypatch, dispatch, state):
    from lib.swarm import retained_messages
    worker_context(context)

    if state == 'unknown':
        def uncertain(*args, **kwargs):
            raise RunMessageError('message_submission_unknown', 'synthetic missing reply', submission_state='unknown')
        monkeypatch.setattr(retained_messages, 'send', uncertain)
    result = dispatch('send_agent_message', {
        'retained_agent_id': 'colleague', 'content': 'Evidence', 'idempotency_key': 'logical-1',
    }, context.child)
    assert result['submission_state'] == state and result['retryable'] is False
    assert 'get_agent_messages' not in result['next_action']
    assert 'final result' in result['next_action'].lower()
    assert not context.client.rows


def test_worker_team_guidance_separates_original_live_root_from_message_recovery(context):
    from lib.swarm.team_messages import team_guidance
    guidance = team_guidance(context.binding)
    assert 'original running root' in guidance
    assert 'rejected send has no receipt' in guidance
    assert 'uncertain' in guidance and 'original target' in guidance and 'idempotency_key' in guidance
    assert 'final result' in guidance.lower()


@pytest.mark.parametrize('actor', ['root', 'worker'])
def test_untyped_exception_after_commit_still_reconciles_exact_message(context, monkeypatch, dispatch, actor):
    caller, request = exact_caller(context, monkeypatch, actor)
    original = message_service.send
    saved = []

    def fail_after_submission(*args, **kwargs):
        saved.append(original(*args, **kwargs))
        raise RuntimeError('synthetic receipt serialization failure')

    monkeypatch.setattr(message_service, 'send', fail_after_submission)
    result = dispatch('send_agent_message', request, caller)
    assert result['code'] == 'swarm_tool_failed' and result['submission_state'] == 'unknown'
    assert result['retryable'] is False and result['exception_type'] == 'RuntimeError'
    assert 'get_agent_messages' in result['next_action'] and 'swarm_run_id' in result['next_action']
    assert 'spawn' not in result['next_action']
    history = dispatch('get_agent_messages', read_target(request, saved[0]['message_id']), caller)
    assert history['messages'][0]['content'] == request['content']
    assert len(context.client.rows) == 1


@pytest.mark.parametrize('actor', ['root', 'worker'])
def test_retryable_receipt_read_keeps_original_target_without_write_advice(context, monkeypatch, dispatch, actor):
    caller, request = exact_caller(context, monkeypatch, actor)
    receipt = message_service.send(caller, request, context.session)

    def unavailable(*args, **kwargs):
        raise StorageError('database_timeout', 'synthetic receipt read unavailable', retryable=True)

    monkeypatch.setattr(context.client, 'query', unavailable)
    result = dispatch('get_agent_messages', read_target(request, receipt['message_id']), caller)
    assert result['code'] == 'database_timeout' and result['retryable'] is True
    assert result['submission_state'] == 'unknown'
    assert 'get_agent_messages' in result['next_action'] and 'swarm_run_id' in result['next_action']
    assert 'idempotency_key' in result['next_action'] and 'replay' in result['next_action'].lower()
    assert 'new message to a retained' not in result['next_action']
    assert len(context.client.rows) == 1


def test_saved_stable_rejection_read_preserves_failure_and_authored_evidence(stable_context, dispatch):
    c = stable_context
    c.launch_behavior = lambda *args, **kwargs: json.dumps({
        'status': 'error', 'code': 'invalid_agents', 'retryable': False,
        'message': 'synthetic pre-admission rejection', 'submission_state': 'rejected',
    })
    request = retained_tests.request(c)
    sent = dispatch('send_agent_message', request, c.task,
        cfg=c.cfg, all_tools=c.tools, project_path=retained_tests.PROJECT)
    assert sent['code'] == 'invalid_agents' and sent['submission_state'] == 'rejected'
    query = {key: request[key] for key in ('retained_agent_id', 'idempotency_key')}
    result = dispatch('get_agent_messages', query, c.task, project_path=retained_tests.PROJECT)
    assert result['code'] == 'invalid_agents' and result['submission_state'] == 'rejected'
    assert result['retryable'] is False and 'get_agent_messages' not in result['next_action']
    assert result['retained_agent_id'] == c.agent['id']
    assert result['content'] == request['content'] and result['message_id']
    assert result['intent'] == 'message' and len(c.launches) == 1


@pytest.mark.parametrize('mutation', ['missing_binding', 'forged_owner', 'cross_run'])
def test_invalid_worker_binding_does_not_escape_to_root_recovery(context, monkeypatch, dispatch, mutation):
    caller, request = exact_caller(context, monkeypatch, 'worker')
    if mutation == 'missing_binding':
        caller.pop('_swarmMessageBinding')
    elif mutation == 'forged_owner':
        caller['_userId'] = 8
    else:
        request['swarm_run_id'] = 'other'
    result = dispatch('send_agent_message', request, caller)
    assert result['code'] == 'message_forbidden'
    assert result['submission_state'] == 'rejected' and result['retryable'] is False
    assert 'get_agent_messages' not in result['next_action']
    assert 'final result' in result['next_action'].lower()
    assert 'manage_agents' not in result['next_action']
    assert not context.client.rows


@pytest.mark.parametrize('actor', ['root', 'worker'])
def test_invalid_content_rejection_can_correct_body_without_receipt_loop(context, monkeypatch, dispatch, actor):
    caller, request = exact_caller(context, monkeypatch, actor)
    request['content'] = '   '
    result = dispatch('send_agent_message', request, caller)
    assert result['code'] == 'message_invalid_content' and result['submission_state'] == 'rejected'
    assert 'Correct the content' in result['next_action']
    assert 'get_agent_messages' not in result['next_action']
    assert 'Preserve the authored content' not in result['next_action']
    assert not context.client.rows
