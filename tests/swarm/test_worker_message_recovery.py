"""Workers can correct message errors without losing their authorized task.

Exercise the scoped catalog, provider wire, real dispatch and durable mailbox;
the recorded failure used both exact-run handles and retained_agent_id='x'.
"""
from copy import deepcopy
import json

import pytest
from jsonschema import Draft202012Validator

from lib.swarm.message_contract import CONTRACT
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.registry import scope_tools_for_spec
from lib.tools.contracts import compile_execution_contract_documents, validate_tool_arguments_from_documents
from tests.swarm import test_run_messages as mailbox_tests
from tests.swarm.test_run_messages import args
from tests.swarm.test_team_messages import worker_context

pytestmark = pytest.mark.unit
context = mailbox_tests.context


@pytest.mark.parametrize('role', ['coder', 'general', 'researcher'])
@pytest.mark.parametrize('model', ['gpt-6-astra', 'kimi-k2.5', 'claude-sonnet-4-5'])
def test_workers_only_see_callable_team_targets_on_wire_and_gateway(role, model):
    from lib.tools.gateway import sanitize_wire_tools
    original = deepcopy(CONTRACT['tools'])
    scoped = scope_tools_for_spec(SubTaskSpec(id='child', role=role, objective='Inspect'), original)
    documents = compile_execution_contract_documents(scoped)
    for tool in sanitize_wire_tools(scoped, model=model):
        fn = tool['function']
        schema = fn['parameters']
        assert not {'retained_agent_id', 'scope', 'intent'} & schema['properties'].keys()
        assert {'agent_id', 'swarm_run_id'} <= set(schema['required'])
        request = args() if fn['name'] == 'send_agent_message' else {
            'agent_id': 'child-a', 'swarm_run_id': 'launch'}
        Draft202012Validator(schema).validate(request)
        validate_tool_arguments_from_documents(documents, fn['name'], request)
        assert not Draft202012Validator(schema).is_valid({**request, 'retained_agent_id': 'x'})
    assert original == CONTRACT['tools']  # Another root/worker keeps its own catalog.


@pytest.mark.parametrize('name', ['send_agent_message', 'get_agent_messages'])
def test_worker_mixed_target_is_rejected_with_correction_before_any_effect(context, monkeypatch, name):
    from lib.swarm.integration import _tools
    c = worker_context(context)
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(_tools, '_get_session', lambda key: c.session)
    c.task['status'] = 'running'
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get', lambda key: c.task)
    request = args(agent_id='launch') if name == 'send_agent_message' else {
        'agent_id': 'launch', 'swarm_run_id': 'launch'}
    rejected = json.loads(_tools.execute_swarm_tool(name, {
        **request, 'retained_agent_id': 'x', 'scope': 'project', 'intent': 'message',
    }, c.child))
    assert rejected['code'] == 'message_invalid_arguments'
    assert rejected['submission_state'] == 'rejected'
    assert 'remove retained_agent_id, scope and intent' in rejected['next_action']
    assert 'Inspect get_agent_messages' not in rejected['next_action']
    assert not c.client.rows
    if name == 'get_agent_messages':
        # Receipt lookup only has meaning after an actual accepted submission.
        sent = json.loads(_tools.execute_swarm_tool('send_agent_message', args(agent_id='launch'), c.child))
        request['message_id'] = sent['message_id']
    corrected = json.loads(_tools.execute_swarm_tool(name, request, c.child))
    assert corrected.get('status') != 'error'
    if name == 'send_agent_message':
        assert corrected['state'] == 'submitted'
        assert corrected['agent_id'] == 'launch'
        assert corrected['execution_started'] is False


def test_worker_forbidden_stable_message_does_not_prescribe_forbidden_receipt_read(context, monkeypatch):
    from lib.swarm.integration import _tools
    c = worker_context(context)
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    result = json.loads(_tools.execute_swarm_tool('send_agent_message', {
        'retained_agent_id': 'x', 'content': 'Starting diagnosis', 'idempotency_key': 'start',
    }, c.child))
    assert result['code'] == 'message_forbidden'
    assert result['retryable'] is False
    assert result['submission_state'] == 'rejected'
    assert 'Inspect get_agent_messages' not in result['next_action']
    assert 'continue' in result['next_action'].lower()
    assert not c.client.rows


def test_inherited_execution_contracts_are_narrowed_without_losing_authority():
    from lib.swarm.message_contract import worker_message_contracts
    original = compile_execution_contract_documents(CONTRACT['tools'])
    before = deepcopy(original)
    scoped = worker_message_contracts(original)
    for name, document in scoped.items():
        assert not {'retained_agent_id', 'scope', 'intent'} & document['arguments_schema']['properties'].keys()
        for key in original[name].keys() - {'arguments_schema', 'description'}:
            assert document[key] == original[name][key]
    assert original == before


def test_worker_projection_keeps_inherited_argument_limits_and_refuses_incompatible_requirements():
    from lib.swarm.message_contract import worker_message_tool
    tool = deepcopy(CONTRACT['tools'][0])
    tool['function']['parameters']['properties']['content']['maxLength'] = 10
    narrowed = worker_message_tool(tool)
    assert narrowed['function']['parameters']['properties']['content']['maxLength'] == 10
    tool['function']['parameters']['required'].append('retained_agent_id')
    with pytest.raises(ValueError, match='requires fields unavailable to workers'):
        worker_message_tool(tool)
