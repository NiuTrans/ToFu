"""Detailed Coordinator briefs survive tool admission, persistence and Worker input."""
from copy import deepcopy
import json

import pytest

from lib.project_dispatch.contract import schema
from lib.tool_input_repair import ingest_tool_call
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.coordinator import COORDINATOR_TOOLS
from tests.project_mod.test_coordinator_workers import (
    base_runtime as base_runtime,
    context as context,
    runtime as runtime,
)

pytestmark = pytest.mark.unit


def brief_call(arguments):
    documents = compile_execution_contract_documents(COORDINATOR_TOOLS)
    return ingest_tool_call(
        {'id': 'detailed-delegation', 'function': {
            'name': 'delegate_project_work', 'arguments': json.dumps(arguments)}},
        known_tools=set(documents), contract_documents_by_name=documents,
        emit_audit=False)


@pytest.mark.parametrize('length', [4220, 65536])
def test_detailed_brief_reaches_worker_unchanged_and_replays_once(runtime, length, monkeypatch):
    r = runtime
    # Include non-ASCII constraints and a final acceptance criterion so byte
    # limits and accidental truncation cannot masquerade as successful delivery.
    brief = 'Objective:\n' + '证据与边界🧪' * (length // 6)
    brief = brief[:length - len('\nVERIFY THE FINAL CHECKPOINT')] + '\nVERIFY THE FINAL CHECKPOINT'
    arguments = {**r.args, 'brief': brief}
    original = deepcopy(arguments)
    call = brief_call(arguments)
    assert call.parse_error is None, call.parse_error
    assert call.fn_args == arguments == original
    assert len(brief) == length

    inputs = []
    create_turn = r.service.commands.create_turn

    def capture_input(conversation, owner, request, **kwargs):
        inputs.append(request['inputTurn']['content'])
        return create_turn(conversation, owner, request, **kwargs)

    monkeypatch.setattr(r.service.commands, 'create_turn', capture_input)
    result = r.call('delegate_project_work', **call.fn_args)
    assert result['dispatch'] == 'accepted', result
    row = r.c.repo.read(result['assignment_id'])
    assert row['request']['brief'] == brief
    assert row['request']['authorizationSource'] == r.c.request['authorizationSource']
    assert inputs[0].endswith('Assigned objective:\n' + brief)
    assert r.call('delegate_project_work', **arguments) == result
    assert len(r.calls) == len(r.created) == 1
    assert all(content.endswith('Assigned objective:\n' + brief) for content in inputs)
    replay_inputs = len(inputs)
    changed = {**arguments, 'brief': brief[:-1] + '!'}
    assert 'error' in r.call('delegate_project_work', **changed)
    assert len(r.calls) == len(r.created) == 1 and len(inputs) == replay_inputs


@pytest.mark.parametrize('brief', ['', '界' * 65537])
def test_invalid_brief_refused_without_repair_or_worker_creation(runtime, brief):
    r = runtime
    arguments = {**r.args, 'brief': brief}
    call = brief_call(arguments)
    assert call.contract_error['path'] == '$.brief'
    assert call.contract_error['code'] == 'invalid_argument_length'
    assert call.fn_args == arguments
    assert 'error' in r.call('delegate_project_work', **arguments)
    assert not r.created and not r.calls and not r.c.repo.list()


def test_tool_and_durable_request_share_brief_contract():
    tool = schema('delegateRequest')['properties']['brief']
    request = schema('request')['properties']['brief']
    assert tool == request
    assert tool['minLength'] == 1 and tool['maxLength'] == 65536
