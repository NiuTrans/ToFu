"""Team communication reuses exact mailboxes without granting worker execution."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.swarm import message_service
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.run_messages import RunMessageError, RunMessageRepository, binding_for, inject_messages
from lib.swarm.team_messages import coordinator_binding, prepare_coordinator_mailbox
from tests.swarm.test_run_messages import context as context, args  # noqa: F401

pytestmark = pytest.mark.unit


def worker_context(context):
    c = context
    c.child.update(_parentTaskId='launch', _transientRuntime=True, _invocationStorageFree=False)
    c.session._agents = {'child-a': SimpleNamespace(_child=c.child)}
    sibling = SubTaskSpec(id='child-b', role='coder', objective='Review')
    c.session.specs.append(sibling)
    return c


def test_worker_to_worker_preserves_provenance_and_namespaces_keys(context):
    c = worker_context(context)
    request = args(agent_id='child-b')
    root_receipt = message_service.send(c.task, request, c.session)
    receipt = message_service.send(c.child, request, c.session)
    assert receipt['message_id'] != root_receipt['message_id']
    assert receipt['execution_started'] is False
    assert message_service.send(c.child, request, c.session) == receipt
    binding = binding_for(c.task, c.session.specs[1], 'conv-a')
    sibling = {'id': 'runtime-child-2', '_swarmMessageBinding': binding, 'config': {}}
    messages = [{'role': 'user', 'content': 'Review'}]
    inject_messages(sibling, messages, 0)
    assert 'sender-task="runtime-child-1"' in messages[-1]['content']
    assert 'not a human instruction' in messages[-1]['content']
    assert len(RunMessageRepository(binding).load()[0]['messages']) == 2


def test_worker_reports_to_root_and_root_uses_same_input_pipeline(context, monkeypatch):
    c = worker_context(context); c.task['status'] = 'running'
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get', lambda tid: c.task if tid == 'launch' else None)
    monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: c.session)
    receipt = message_service.send(c.child, args(agent_id='launch', content='Need a product decision'), c.session)
    prepare_coordinator_mailbox(c.task)
    assert c.task['_swarmMessageBinding'] == coordinator_binding(c.session)
    # Children can settle before the root resumes its existing authorized turn.
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {'task_id': 'launch', 'status': 'terminated', 'agents': []})
    messages = [{'role': 'user', 'content': 'Coordinate'}]
    inject_messages(c.task, messages, 0)
    assert 'Need a product decision' in messages[-1]['content']
    assert receipt['message_id'] in messages[-1]['_swarmMessageIds']
    c.task['status'] = 'done'
    newer = [{'role': 'user', 'content': 'Later'}]
    inject_messages(c.task, newer, 1)
    assert len(newer) == 1


@pytest.mark.parametrize('mutation', ['foreign_run', 'forged_owner', 'stale_child', 'cancelled', 'storage_free', 'stable'])
def test_worker_cannot_escape_team_or_activate_work(context, mutation):
    c = worker_context(context); request = args(agent_id='child-b')
    if mutation == 'foreign_run': request['swarm_run_id'] = 'other'
    if mutation == 'forged_owner': c.child['_userId'] = 8
    if mutation == 'stale_child': c.session._agents['child-a']._child = deepcopy(c.child)
    if mutation == 'cancelled': c.child['aborted'] = True
    if mutation == 'storage_free': c.child['_invocationStorageFree'] = True
    with pytest.raises((RunMessageError, PermissionError)):
        if mutation == 'stable':
            from lib.swarm.retained_messages import send
            send(c.child, {'retained_agent_id': 'persistent', 'content': 'wake', 'idempotency_key': 'key'},
                 cfg={}, all_tools=[], project_path='/p')
        else:
            message_service.send(c.child, request, c.session)
    assert not c.client.rows


def test_worker_team_tools_respect_explicit_narrowing():
    from lib.swarm.message_contract import CONTRACT
    from lib.swarm.registry import scope_tools_for_spec
    tools = CONTRACT['tools']
    names = lambda spec: {tool['function']['name'] for tool in scope_tools_for_spec(spec, tools)}
    assert names(SubTaskSpec(id='a', role='coder', objective='Work')) == {'send_agent_message', 'get_agent_messages'}
    assert names(SubTaskSpec(id='a', role='coder', objective='Work', tools=['get_agent_messages'])) == {'get_agent_messages'}


def test_real_tool_entry_routes_worker_by_stamped_team_identity(context, monkeypatch):
    import json
    from lib.swarm.integration import _tools
    c = worker_context(context)
    keys = []
    def session(key):
        keys.append(key)
        return c.session if key == 'conv-a' else None
    monkeypatch.setattr(_tools, '_get_session', session)
    reply = json.loads(_tools.execute_swarm_tool('send_agent_message', args(agent_id='child-b'), c.child))
    assert reply['state'] == 'submitted'
    assert keys == ['conv-a']
    assert not reply['execution_started']


def test_root_receipt_stays_pending_until_original_root_ends(context, monkeypatch):
    c = worker_context(context); c.task['status'] = 'running'
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get', lambda tid: c.task)
    receipt = message_service.send(c.child, args(agent_id='launch'), c.session)
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {'task_id':'launch','status':'terminated','agents':[]})
    query = {'agent_id':'launch','swarm_run_id':'launch','message_id':receipt['message_id']}
    assert message_service.read(c.child, query)['messages'][0]['state'] == 'submitted'
    c.task['status'] = 'done'
    assert message_service.read(c.child, query)['messages'][0]['state'] == 'unconfirmed'
