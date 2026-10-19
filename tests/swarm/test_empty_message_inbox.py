"""Lazy inbox absence is not execution absence; reads never create work."""
import json

import pytest

from lib.storage.errors import StorageError
from lib.swarm import message_service
from lib.swarm.run_messages import RunMessageError
from tests.swarm.test_run_messages import context as context  # noqa: F401
from tests.swarm.test_team_messages import worker_context

pytestmark = pytest.mark.unit


@pytest.fixture
def inbox(context, monkeypatch):
    c = worker_context(context)
    c.task['status'] = 'running'
    monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: c.session if key == 'conv-a' else None)
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get', lambda tid: c.task if tid == 'launch' else None)
    return c


@pytest.mark.parametrize('caller', ['root', 'worker'])
@pytest.mark.parametrize('recipient', ['child-a', 'child-b', 'launch'])
def test_first_inbox_read_is_verified_empty_without_writes(inbox, caller, recipient):
    c = inbox
    task = c.task if caller == 'root' else c.child
    result = message_service.read(task, {'agent_id': recipient, 'swarm_run_id': 'launch'})
    assert result == {'agent_id': recipient, 'swarm_run_id': 'launch', 'run_closed': False, 'messages': []}
    assert not c.client.rows


def durable(c, monkeypatch, **updates):
    row = {'task_id': 'launch', 'conv_id': 'conv-a', 'status': 'running', 'agents': [],
           'config': {'user_id': 7, 'parent_cfg': {'projectPath': '/p'}},
           'specs': [s.to_dict() for s in c.session.specs]}
    row.update(updates)
    monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: None)
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: row)
    return row


@pytest.mark.parametrize('status', ['running', 'terminated'])
def test_empty_inbox_after_memory_loss_uses_owned_durable_specs(inbox, monkeypatch, status):
    c = inbox
    durable(c, monkeypatch, status=status)
    result = message_service.read(c.child, {'agent_id': 'child-b', 'swarm_run_id': 'launch'})
    assert result['messages'] == []
    assert result['run_closed'] is (status == 'terminated')
    assert not c.client.rows


@pytest.mark.parametrize('mutation', ['run', 'owner', 'tenant', 'conversation', 'recipient', 'no_run'])
def test_unwitnessed_empty_inbox_is_not_success(inbox, monkeypatch, mutation):
    c = inbox
    row = durable(c, monkeypatch)
    request = {'agent_id': 'child-b', 'swarm_run_id': 'launch'}
    if mutation == 'run': row['task_id'] = 'replacement'
    if mutation == 'owner': row['config']['user_id'] = 8
    if mutation == 'tenant': row['config']['parent_cfg']['_tenant_id'] = 'foreign'
    if mutation == 'conversation': row['conv_id'] = 'foreign'
    if mutation == 'recipient': request['agent_id'] = 'unknown'
    if mutation == 'no_run': monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: None)
    with pytest.raises(RunMessageError) as error:
        message_service.read(c.task, request)
    assert error.value.code == 'message_run_not_found'
    assert not c.client.rows


def test_empty_inbox_missing_specific_receipt_is_message_not_found(inbox):
    with pytest.raises(RunMessageError) as error:
        message_service.read(inbox.task, {'agent_id': 'child-a', 'swarm_run_id': 'launch', 'message_id': 'missing'})
    assert error.value.code == 'message_not_found'
    assert not inbox.client.rows


def test_empty_inbox_still_revalidates_project_scope(inbox, monkeypatch):
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', lambda *a, **kw: None)
    with pytest.raises(RunMessageError) as error:
        message_service.read(inbox.task, {'agent_id': 'child-a', 'swarm_run_id': 'launch'})
    assert error.value.code == 'message_scope_unavailable'


@pytest.mark.parametrize('boundary', ['mailbox', 'session'])
def test_storage_failure_cannot_claim_empty_success(inbox, monkeypatch, boundary):
    c = inbox
    def fail(*a, **kw):
        raise StorageError('database_unavailable', 'offline')
    if boundary == 'mailbox':
        monkeypatch.setattr(c.client, 'query', fail)
    else:
        monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: None)
        monkeypatch.setattr('lib.swarm.persistence.load_session', fail)
    with pytest.raises(StorageError):
        message_service.read(c.task, {'agent_id': 'child-a', 'swarm_run_id': 'launch'})
    assert not c.client.rows


def test_dispatch_distinguishes_empty_inbox_from_unknown_run(inbox):
    from lib.swarm.integration._tools import execute_swarm_tool
    c = inbox
    result = json.loads(execute_swarm_tool('get_agent_messages', {'agent_id': 'child-a', 'swarm_run_id': 'launch'}, c.child))
    assert result['messages'] == [] and not result['run_closed']
    result = json.loads(execute_swarm_tool('get_agent_messages', {'agent_id': 'absent', 'swarm_run_id': 'launch'}, c.task))
    assert result['code'] == 'message_run_not_found'
    assert 'do not repeat' in result['next_action']
    assert 'Inspect get_agent_messages' not in result['next_action']
    assert not c.client.rows


@pytest.mark.parametrize('value', [None, [], {'closed': False, 'messages': []}])
def test_malformed_existing_mailbox_is_integrity_error(inbox, monkeypatch, value):
    monkeypatch.setattr(inbox.client, 'query', lambda *a, **kw: {'value': value, 'version': 1})
    with pytest.raises(StorageError) as error:
        message_service.read(inbox.task, {'agent_id': 'child-a', 'swarm_run_id': 'launch'})
    assert error.value.code == 'database_integrity'
    assert not inbox.client.rows


def test_conversationless_root_can_read_only_original_run(inbox, monkeypatch):
    c = inbox
    c.task['convId'] = ''
    c.session.conv_id = ''
    monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: c.session if key == 'launch' else None)
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {'task_id': 'launch', 'status': 'running'})
    assert message_service.read(c.task, {'agent_id': 'child-a', 'swarm_run_id': 'launch'})['messages'] == []
    with pytest.raises(RunMessageError) as error:
        message_service.read({**c.task, 'id': 'later'}, {'agent_id': 'child-a', 'swarm_run_id': 'launch'})
    assert error.value.code == 'message_forbidden'
    assert not c.client.rows
