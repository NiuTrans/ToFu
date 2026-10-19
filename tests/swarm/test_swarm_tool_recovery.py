"""A real Swarm invocation survives tool rejection and receives bounded guidance."""
import json
from types import SimpleNamespace

import pytest

from lib.tools.result_envelope import typed_tool_error
from tests.swarm import test_swarm_invocation as invocation_tests
from tests.swarm.test_swarm_invocation import (
    agent_for, reply, tool_call, scripted_provider,
)

pytestmark = pytest.mark.unit
runtime = invocation_tests.runtime


@pytest.mark.parametrize('recover', ['correct', 'independent', 'repeat'])
def test_worker_sees_correction_then_continues_or_stops_repeated_failure(runtime, monkeypatch, recover):
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from lib.swarm.protocol import SubTaskSpec
    failure = typed_tool_error(
        'message_invalid_arguments', retryable=False,
        message='Choose one message target.',
        next_action='Remove retained_agent_id, scope and intent.').to_envelope_text()
    executed = []

    def handler(task, tc, name, call_id, args, rn, row, *rest):
        executed.append(args)
        succeeds = recover != 'repeat' and len(executed) == 3
        content = 'Verified diagnosis' if succeeds else failure
        _finalize_tool_round(task, rn, row, [{'title': name, 'output': content}],
                             status='done' if succeeds else 'error')
        return call_id, content, False

    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: handler)
    tool = {'type': 'function', 'function': {
        'name': 'fixture_goal_probe', 'description': 'Inspect synthetic evidence',
        'parameters': {'type': 'object', 'properties': {'target': {'type': 'string'}},
                       'additionalProperties': False},
    }}
    alternative_tool = {'type': 'function', 'function': {
        **tool['function'], 'name': 'fixture_independent_probe',
    }}
    responses = [reply('', calls=[tool_call(f'call-{i}', name=(
        'fixture_independent_probe' if recover == 'independent' and i == 2 else None), arguments=json.dumps({
        'target': 'corrected' if recover == 'correct' and i == 2 else 'mixed',
    }))]) for i in range(3)]
    if recover != 'repeat':
        responses.append(reply('Completed diagnosis after correcting the tool call.'))
    scripted_provider(monkeypatch, runtime, responses)
    agent = agent_for(runtime, tools=[tool, alternative_tool], spec=SubTaskSpec(
        id='diagnostic-worker', role='general', objective='Inspect synthetic evidence'))
    result = agent.run()
    assert len(executed) == 3
    notices = [m for m in runtime.calls[2]['messages']
               if m.get('_contextInjectionId') == 'tool_progress']
    assert len(notices) == 1
    assert notices[0]['_isMeta'] is True
    assert 'continue independent work' in notices[0]['content']
    assert 'does not stop your whole task' in notices[0]['content']
    if recover != 'repeat':
        assert result.status == 'completed', result.error_message
        assert 'Completed diagnosis' in result.final_answer
        assert len(runtime.calls) == 4
    else:
        assert result.status == 'failed'
        assert 'three consecutive rounds' in result.error_message
        assert len(runtime.calls) == 3


def test_real_worker_keeps_parent_execution_contracts_narrowed_through_dispatch(runtime, monkeypatch):
    from copy import deepcopy
    import lib.tasks_pkg.manager._stream as stream
    from lib.swarm.message_contract import CONTRACT
    from lib.tools.contracts import compile_execution_contract_documents
    tools = deepcopy(CONTRACT['tools'])
    documents = compile_execution_contract_documents(tools)
    runtime.parent['_toolContractDocumentsByName'] = deepcopy(documents)
    scripted_provider(monkeypatch, runtime, [reply('Verified final diagnosis')])
    dispatch = stream.dispatch_stream

    def inspect(body, **kwargs):
        child = runtime.children[-1]
        for tool in body['tools']:
            schema = tool['function']['parameters']
            assert 'retained_agent_id' not in schema['properties']
        for document in child['_toolContractDocumentsByName'].values():
            assert 'retained_agent_id' not in document['arguments_schema']['properties']
        return dispatch(body, **kwargs)

    monkeypatch.setattr(stream, 'dispatch_stream', inspect)
    result = agent_for(runtime, tools=tools).run()
    assert result.status == 'completed', result.error_message
    assert runtime.parent['_toolContractDocumentsByName'] == documents
