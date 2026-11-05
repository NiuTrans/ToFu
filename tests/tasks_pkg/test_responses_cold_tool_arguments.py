"""Provider argument bytes survive real tool settlement and cold Responses replay.

Only the provider and conversation repository IO are controlled. Ingress,
stream announcements, argument admission, handler dispatch, tool settlement,
Turn projection, JSON persistence, history rebuilding and checked egress use
production owners. No round is hand-authored and no replay witness is repaired.
"""

from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm.body import build_body
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.responses_outbound import (
    ResponsesSSETranslator,
    openai_body_to_responses_checked,
    responses_response_to_openai,
)
from lib.llm.responses_replay import REPLAY_ORDER_TYPE
from lib.tasks_pkg.conv_message_builder.api import build_api_messages_from_db
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tasks_pkg.segments import assemble_segments, segments_to_json
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline, parse_tool_calls
from lib.tools.contracts import ToolContractV2
from lib.turn_lifecycle import capture_task_projection_source, _task_projection

pytestmark = pytest.mark.unit

_MODEL = 'gpt-6-astra'
_TOOL = 'custom__replay_probe'
_USER = {'role': 'user', 'content': 'Read the synthetic values.'}


def _task():
    calls = []

    def handler(task, call, name, call_id, arguments, number, row, *rest):
        calls.append((call_id, deepcopy(arguments)))
        return call_id, 'SETTLED:' + call_id, False

    contract = ToolContractV2(
        name=_TOOL,
        parameters={'type': 'object', 'properties': {
            'n': {'type': 'number'}, 'text': {'type': 'string'},
        }, 'additionalProperties': False},
        model_description='Read synthetic values without external effects.',
        permission='read', idempotency='read_only', effect_recovery='read_only',
    )
    schema = contract.provider_schema()
    return {
        'id': 'cold-arguments-task', 'convId': 'cold-arguments-conversation',
        '_attemptId': 'cold-arguments-attempt', '_userId': 1,
        'status': 'running', 'model': _MODEL, 'aborted': False,
        'content': '', 'thinking': '', 'toolRounds': [],
        'events': [], 'events_lock': threading.Lock(),
        # A focused task has no live durable event sink. Projection/persistence
        # below still runs through the normal Turn owner after tool settlement.
        '_suppressEvents': True,
        'config': {'autoApply': True, 'tools': {'resultEnvelope': 'v2'}},
        '_tool_schema': [schema], '_executable_tool_catalog': [schema],
        '_toolContractDocumentsByName': {_TOOL: contract.search_document()},
        '_tool_env': SimpleNamespace(
            schemas=[schema], resolve=lambda name: handler if name == _TOOL else None,
            write_names=frozenset(), idempotent_names=frozenset(),
            confirmation_names=frozenset(),
        ),
    }, calls


def _output(arguments):
    def reasoning(cipher):
        return {'type': 'reasoning', 'id': 'reused-reasoning-id',
                'encrypted_content': cipher, 'summary': []}

    def call(identifier, text):
        return {'type': 'function_call', 'id': 'fc-' + identifier,
                'call_id': identifier, 'name': _TOOL,
                'arguments': text, 'status': 'completed'}

    # Reasoning after a function call requires the actual ingress order witness;
    # a canonical opaque -> prose -> calls fixture would not expose the bug.
    return [reasoning('cipher-before'), call('first', '{}'),
            reasoning('cipher-between'),
            {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
             'content': [{'type': 'output_text', 'text': 'Reading values.'}]},
            call('second', arguments)]


def _receive(output, task, ingress, announcer):
    response = {'id': 'resp-cold-arguments', 'status': 'completed',
                'output': deepcopy(output),
                'usage': {'input_tokens': 3, 'output_tokens': 2}}
    if ingress == 'nonstream':
        result = responses_response_to_openai(response)
        return result['choices'][0]['message'], result['usage']
    body = {'model': _MODEL, 'messages': [], 'tools': task['_tool_schema']}
    accumulator = SSEAccumulator(
        body, 'cold-arguments', RawSSEDumper(_MODEL, 'cold-arguments', body),
        ResponsesSSETranslator(_MODEL), time.time(),
        on_tool_call_ready=announcer.on_tool_call_ready if announcer else None,
    )
    for index, item in enumerate(output):
        for event in ('response.output_item.added', 'response.output_item.done'):
            accumulator.feed_line('data: ' + json.dumps({
                'type': event, 'output_index': index, 'item': item}))
    accumulator.feed_line('data: ' + json.dumps({
        'type': 'response.completed', 'response': response}))
    accumulator.fire_final_tool_callback()
    message, _, usage = accumulator.finalize()
    return message, usage


def _settled_projection(arguments, ingress):
    task, calls = _task()
    output = _output(arguments)
    announcer = (StreamingToolAccumulator(task, project_path=None, round_num=0)
                 if ingress == 'announced' else None)
    try:
        message, usage = _receive(output, task, ingress, announcer)
        original_message, original_usage = deepcopy(message), deepcopy(usage)
        assert any(item.get('type') == REPLAY_ORDER_TYPE
                   for item in message['_responses_items'])
        early = None
        if announcer:
            announcer.reconcile_announced_rounds(message)
            early = announcer.announced_tc_map
            assert len(early) == 2
            assert announcer.submitted_count == 0
        parsed, count = parse_tool_calls(
            message, task, round_num=0,
            tool_round_num=announcer.tool_round_num if announcer else 0,
            project_enabled=False, early_announced=early,
        )
        assert count == 2 and len(parsed) == 2
        assert all(item[6] is None for item in parsed)
        messages = [deepcopy(_USER), message]
        execute_tool_pipeline(
            task, parsed, cfg=task['config'], project_path=None,
            project_enabled=False, tool_list=task['_tool_schema'],
            messages=messages, all_search_results_text=[], round_num=0, model=_MODEL,
        )
        assert sorted(calls) == [('first', {}), ('second', json.loads(arguments))]
        assert [row['status'] for row in task['toolRounds']] == ['done', 'done']
        assert [row['toolContent'] for row in task['toolRounds']] == [
            item['content'] for item in messages if item['role'] == 'tool']
        assert message == original_message and usage == original_usage
        # Round-end checkpointing reassembles after the tools have settled;
        # never persist the provisional, result-less parse-time segment mirror.
        task['segments'] = assemble_segments(task)
        # Freeze the same structural source and canonical Turn projection that
        # ordinary event persistence consumes, excluding locks and IO ports.
        source = capture_task_projection_source(task, {'type': 'round_end'})
        projection = _task_projection(source, {})
        assert [row['toolCallId'] for row in projection['toolRounds']] == ['first', 'second']
        return projection, output
    finally:
        if announcer:
            announcer.close(wait=True)


def _cold_body(projection, history, tmp_path, monkeypatch):
    stored = deepcopy(projection)
    if history == 'rounds':
        stored.pop('segments', None)  # Legacy direct toolRounds path.
    else:
        assert stored['segments']
        stored['segments'] = segments_to_json(stored['segments'])
    path = tmp_path / 'settled-turn.json'
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding='utf-8')
    reads = []

    def get_conversation(conversation_id, *, user_id):
        reads.append((conversation_id, user_id))
        cold = json.loads(path.read_text(encoding='utf-8'))
        return SimpleNamespace(messages=[deepcopy(_USER), {'role': 'assistant', **cold}])

    monkeypatch.setattr('lib.conversations.repository.get_conversation', get_conversation)
    messages = build_api_messages_from_db(
        'cold-arguments-conversation', {'model': _MODEL}, user_id=1)
    assert reads == [('cold-arguments-conversation', 1)]
    assert messages is not None
    body = build_body(_MODEL, messages)
    assert projection == json.loads(json.dumps(projection, ensure_ascii=False))
    return body


@pytest.mark.parametrize('ingress', ['nonstream', 'stream', 'announced'])
@pytest.mark.parametrize('history', ['rounds', 'segments'])
@pytest.mark.parametrize('arguments', [
    pytest.param('{"n":2}', id='compact'),
    pytest.param(' { "n" : 2, "text": "字" } \n', id='whitespace'),
    pytest.param(r'{"n":2e0,"text":"\u5b57"}', id='escapes-number'),
    pytest.param(' { } \n', id='empty-object'),
])
def test_normal_tool_settlement_survives_cold_checked_responses(
        arguments, ingress, history, tmp_path, monkeypatch):
    projection, output = _settled_projection(arguments, ingress)
    original = deepcopy(projection)
    body = _cold_body(projection, history, tmp_path, monkeypatch)
    result = openai_body_to_responses_checked(body, profile='codex')
    assert result.validation_issues == (), {
        'issues': result.validation_issues,
        'persisted_toolArgs': [row['toolArgs'] for row in projection['toolRounds']],
        'provider_arguments': arguments,
    }
    replayed = [item for item in result.require_payload()['input']
                if item.get('type') != 'function_call_output' and item.get('role') != 'user']
    assert [item['type'] for item in replayed] == [item['type'] for item in output]
    assert [item['encrypted_content'] for item in replayed if item['type'] == 'reasoning'] == [
        'cipher-before', 'cipher-between']
    replayed_calls = [item for item in replayed if item['type'] == 'function_call']
    assert [(item['call_id'], item['name'], item['arguments']) for item in replayed_calls] == [
        ('first', _TOOL, '{}'), ('second', _TOOL, arguments)]
    assert [row['toolArgs'] for row in projection['toolRounds']] == ['{}', arguments]
    assert projection == original


@pytest.mark.parametrize('history', ['rounds', 'segments'])
@pytest.mark.parametrize('change', ['arguments', 'removed'])
def test_cold_edited_calls_remain_authoritative_and_fail_the_old_witness(
        history, change, tmp_path, monkeypatch):
    projection, _ = _settled_projection('{"n":2}', 'nonstream')
    if change == 'arguments':
        projection['toolRounds'][1]['toolArgs'] = '{"n":3}'
        for segment in projection['segments']:
            if segment.get('type') == 'tool_use' and segment.get('id') == 'second':
                segment['input'] = '{"n":3}'
    else:
        projection['toolRounds'].pop()
        projection['segments'] = [segment for segment in projection['segments']
                                  if not (segment.get('type') == 'tool_use'
                                          and segment.get('id') == 'second')]
    original = deepcopy(projection)
    body = _cold_body(projection, history, tmp_path, monkeypatch)
    calls = [call for message in body['messages'] for call in message.get('tool_calls') or []]
    if change == 'arguments':
        assert calls[1]['function']['arguments'] == '{"n":3}'
    else:
        assert [call['id'] for call in calls] == ['first']
    result = openai_body_to_responses_checked(body, profile='codex')
    assert result.payload is None
    assert result.validation_issues == ('native_replay_projection_mismatch',)
    assert projection == original


def _parse_one(arguments, *, early=False, name=_TOOL, parameters=None, caller=None):
    task, _ = _task()
    if parameters is not None:
        contract = ToolContractV2(name=_TOOL, parameters=parameters,
                                  model_description='Exercise argument repair.')
        schema = contract.provider_schema()
        task['_toolContractDocumentsByName'] = {_TOOL: contract.search_document()}
        task['_tool_schema'] = task['_executable_tool_catalog'] = [schema]
    message = {'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'only', 'type': 'function',
        'function': {'name': name, 'arguments': arguments},
    }]}
    if caller is not None:
        message['tool_calls'][0]['caller'] = caller
    announcer = StreamingToolAccumulator(task, project_path=None) if early else None
    try:
        early_rows = None
        if announcer:
            announcer.on_tool_call_ready(message['tool_calls'][0])
            early_rows = announcer.announced_tc_map
        parsed, count = parse_tool_calls(
            message, task, round_num=0,
            tool_round_num=announcer.tool_round_num if announcer else 0,
            project_enabled=False, early_announced=early_rows,
        )
        assert count == 1 and len(parsed) == len(task['toolRounds']) == 1
        if early and isinstance(arguments, str):
            assert parsed[0][5] is early_rows['only'][1]
        elif early:
            assert early_rows == {}  # The streaming callback accepts text only.
        return parsed[0], message
    finally:
        if announcer:
            announcer.close(wait=True)


@pytest.mark.parametrize('early', [False, True])
@pytest.mark.parametrize('arguments', [
    pytest.param('{"n":2', id='json-salvage'),
    pytest.param('  ', id='blank'),
    pytest.param('[]', id='non-object'),
    pytest.param({'n': 2}, id='dictionary'),
])
def test_repaired_or_non_object_inputs_keep_canonical_serialization(early, arguments):
    parsed, message = _parse_one(arguments, early=early)
    _, _, _, decoded, _, row, _ = parsed
    assert json.loads(row['toolArgs']) == decoded
    assert row['toolArgs'] == json.dumps(decoded, ensure_ascii=False)
    assert message['tool_calls'][0]['function']['arguments'] == arguments
    if arguments == '{"n":2':
        assert row['_repaired']['label'] == 'auto-fixed'


@pytest.mark.parametrize('early', [False, True])
@pytest.mark.parametrize('repair', ['default', 'bounds'])
def test_actual_execution_argument_repairs_cannot_reuse_original_bytes(early, repair):
    parameters = {'type': 'object', 'properties': {'n': {
        'type': 'integer', 'default': 2, 'maximum': 5,
    }}, 'additionalProperties': False}
    raw = '{}' if repair == 'default' else '{"n":9}'
    parsed, message = _parse_one(raw, early=early, parameters=parameters)
    _, _, _, arguments, _, row, error = parsed
    assert error is None
    assert arguments == {'n': 2 if repair == 'default' else 5}
    assert json.loads(row['toolArgs']) == arguments
    assert row['toolArgs'] != raw
    assert row['_repaired']['label'] == 'auto-fixed'
    assert message['tool_calls'][0]['function']['arguments'] == raw


def test_tool_name_alias_does_not_rewrite_untouched_argument_bytes():
    raw = ' { "path": "fixture.txt" } '
    task, _ = _task()
    schema = {'type': 'function', 'function': {
        'name': 'read_files', 'parameters': {'type': 'object'}}}
    task['_tool_schema'] = task['_executable_tool_catalog'] = [schema]
    task['_toolContractDocumentsByName'] = None
    task['_tool_env'].schemas = []
    message = {'tool_calls': [{'id': 'alias', 'type': 'function',
        'function': {'name': 'read_file', 'arguments': raw}}]}
    parsed, _ = parse_tool_calls(message, task, 0, 0, False)
    assert parsed[0][1] == 'read_files' and parsed[0][6] is None
    assert parsed[0][5]['_repaired']['patterns'] == ['tool_name_alias']
    assert parsed[0][5]['toolArgs'] == raw


def test_contract_rejection_preserves_the_valid_raw_evidence_and_error():
    raw = ' { "unexpected": 2 } '
    parsed, _ = _parse_one(raw)
    assert parsed[6]
    assert parsed[5]['status'] == 'rejected'
    assert parsed[5]['_contractError']['code'] == 'unknown_arguments'
    assert parsed[5]['toolArgs'] == raw


@pytest.mark.parametrize('caller', [
    pytest.param({'type': 'multi_agent', 'agent_name': 'reader'}, id='valid'),
    pytest.param({'type': 'root'}, id='invalid'),
])
def test_argument_preservation_never_changes_caller_admission(caller):
    raw = ' { "n": 2 } '
    parsed, _ = _parse_one(raw, caller=caller)
    row = parsed[5]
    if caller['type'] == 'multi_agent':
        assert parsed[6] is None and row['caller'] == caller
        assert row['toolArgs'] == raw
    else:
        assert parsed[6] and row['status'] == 'rejected'
        assert row['rejection']['kind'] == 'invalid_tool_caller'
