"""DeepSeek's Messages dialect replays unsigned thinking through real I/O seams.

Claude signatures are a model requirement, not a requirement of every provider
implementing the Messages envelope. Fixtures contain only synthetic reasoning.
"""
from copy import deepcopy
import asyncio
import importlib
import json
import time

import pytest

from lib.llm._sse_core import SSEAccumulator, prepare_request
from lib.llm._sse_framer import SSEFramer
from lib.llm.anthropic_outbound import (
    anthropic_response_to_openai,
    openai_body_to_anthropic_checked,
)
from lib.llm.body import build_body
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator
from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
    build_assistant_tool_call_message,
)
from tests.llm.test_anthropic_signed_thinking_replay import _Dumper
from tests.sse.test_sse_core_parity import _FakeRequestsResp, _FakeAsyncStreamCtx

pytestmark = pytest.mark.unit

MODEL = 'deepseek-v4.1-flash'
THINKING = '  Inspect the file.\n保留原始空白。  '
TOOLS = [{'type': 'function', 'function': {
    'name': 'read_files', 'parameters': {'type': 'object', 'properties': {}}}}]
BLOCKS = [
    {'type': 'thinking', 'thinking': THINKING},
    {'type': 'tool_use', 'id': 'call-read', 'name': 'read_files', 'input': {}},
]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')


def _events():
    return [
        {'type': 'message_start', 'message': {
            'id': 'msg-synthetic', 'type': 'message', 'role': 'assistant',
            'model': MODEL, 'content': [],
            'usage': {'input_tokens': 10, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'thinking', 'thinking': ''}},
        *[{'type': 'content_block_delta', 'index': 0,
           'delta': {'type': 'thinking_delta', 'thinking': piece}}
          for piece in (THINKING[:8], THINKING[8:])],
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1,
         'content_block': deepcopy(BLOCKS[1])},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
         'usage': {'output_tokens': 20}},
        {'type': 'message_stop'},
    ]


def _stream_bytes():
    return ''.join(f'event: {e["type"]}\r\ndata: {json.dumps(e)}\r\n\r\n'
                   for e in _events()).encode()


def _message(ingress):
    if ingress == 'nonstream':
        return anthropic_response_to_openai({
            'id': 'msg-synthetic', 'type': 'message', 'role': 'assistant',
            'model': MODEL, 'content': deepcopy(BLOCKS), 'stop_reason': 'tool_use',
            'usage': {'input_tokens': 10, 'output_tokens': 20},
        })['choices'][0]['message']
    accumulator = SSEAccumulator(
        {'model': MODEL, 'tools': TOOLS}, 'synthetic', _Dumper(),
        AnthropicSSETranslator(), time.monotonic())
    framer = SSEFramer()
    for event in [*framer.feed(_stream_bytes()), *framer.finalize()]:
        accumulator.feed_event(event)
    return dict(accumulator.finalize().message)


def _history(ingress, replay):
    message = _message(ingress)
    assert message['reasoning_content'] == THINKING
    assert not message.get('thinking_signature')
    if replay == 'continue':
        from lib.tasks_pkg.message_builder import inject_tool_history
        tail = []
        inject_tool_history(tail, {'toolHistory': [{
            'toolCalls': [{'id': 'call-read', 'name': 'read_files', 'arguments': '{}'}],
            'toolResults': [{'tool_call_id': 'call-read', 'content': 'file contents'}],
            'thinking': message['reasoning_content'],
        }]}, {'id': 'synthetic', 'convId': 'synthetic'}, MODEL)
    elif replay == 'cold':
        tail = _reconstruct_tool_call_messages([{
            'toolCallId': 'call-read', 'toolName': 'read_files', 'toolArgs': '{}',
            'toolContent': 'file contents', 'status': 'done',
            'llmRound': 40, 'roundNum': 50, 'thinking': message['reasoning_content'],
        }])
    else:
        tail = [build_assistant_tool_call_message(
            tool_calls=message['tool_calls'],
            reasoning_content=message['reasoning_content']),
            {'role': 'tool', 'tool_call_id': 'call-read', 'content': 'file contents'}]
    return [{'role': 'user', 'content': 'Inspect the file'}, *tail]


@pytest.mark.parametrize('ingress', ['stream', 'nonstream'])
@pytest.mark.parametrize('replay', ['live', 'cold', 'continue'])
@pytest.mark.parametrize('wire_model', [MODEL, 'opaque-deployment'])
def test_unsigned_thinking_survives_to_sealed_messages_wire(ingress, replay, wire_model):
    history = _history(ingress, replay)
    original = deepcopy(history)
    body = build_body(wire_model, history, knowledge_model=MODEL, tools=TOOLS,
                      thinking_enabled=True, max_tokens=1024)
    converted = openai_body_to_anthropic_checked(body)
    assert converted.require_payload()['messages'][1]['content'] == BLOCKS
    assert 'optional_reasoning_omitted' not in converted.permitted_conversion_losses
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    wire = json.loads(plan.body.payload)
    assert wire['messages'][1]['content'] == BLOCKS
    assert wire['model'] == wire_model
    assert not any(key.startswith('_') for key in wire)
    assert history == original


@pytest.mark.parametrize('wire_model', [MODEL, 'opaque-deployment'])
def test_nonstream_http_replays_thinking_on_every_tool_round(monkeypatch, wire_model):
    chat_module = importlib.import_module('lib.llm.chat')
    wires = []

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {'id': 'msg-next', 'type': 'message', 'role': 'assistant',
                    'model': MODEL, 'content': deepcopy(BLOCKS),
                    'stop_reason': 'tool_use',
                    'usage': {'input_tokens': 10, 'output_tokens': 20}}

    def post(url, **kwargs):
        wire = json.loads(kwargs['data'])
        wires.append(wire)
        for msg in wire['messages']:
            if msg['role'] == 'assistant':
                assert msg['content'][0] == BLOCKS[0]
        return Response()

    monkeypatch.setattr(chat_module, 'http_post', post)
    history = _history('stream', 'live')
    original = deepcopy(history)
    for _ in range(2):
        _, usage = chat_module.chat(
            history, wire_model, knowledge_model=MODEL, api_protocol='anthropic',
            api_key='synthetic', base_url='https://example.invalid/v1',
            thinking_enabled=True, max_tokens=1024, max_retries=0,
            extra={'tools': TOOLS})
        history.extend([
            build_assistant_tool_call_message(
                tool_calls=usage['_tool_calls'],
                reasoning_content=usage['_reasoning_content']),
            {'role': 'tool', 'tool_call_id': 'call-read', 'content': 'next result'},
        ])
    assert len(wires) == 2
    assert history[:len(original)] == original


@pytest.mark.parametrize('asynchronous', [False, True])
def test_stream_transports_send_and_receive_unsigned_thinking(monkeypatch, asynchronous):
    wires = []
    raw = _stream_bytes()
    # Include framing and JSON splits, so ingress goes through the actual
    # byte-stream shell as well as the final outbound adapter.
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]

    def capture(kwargs):
        wire = json.loads(kwargs.get('content', kwargs.get('data')))
        wires.append(wire)
        assert wire['messages'][1]['content'] == BLOCKS

    class SyncSession:
        def post(self, url, **kwargs):
            capture(kwargs)
            return _FakeRequestsResp([], raw_chunks=chunks)

    class AsyncClient:
        def stream(self, method, url, **kwargs):
            capture(kwargs)
            return _FakeAsyncStreamCtx([], raw_chunks=chunks)

    body = build_body('opaque-deployment', _history('nonstream', 'live'),
                      knowledge_model=MODEL, tools=TOOLS, max_tokens=1024,
                      thinking_enabled=True)
    kwargs = dict(api_protocol='anthropic', api_key='synthetic',
                  base_url='https://example.invalid/v1')
    if asynchronous:
        module = importlib.import_module('lib.llm.astream')
        monkeypatch.setattr(module, 'get_async_client', lambda *a, **kw: AsyncClient())
        result = asyncio.run(module._async_stream_chat_once(body, **kwargs))
    else:
        module = importlib.import_module('lib.llm.stream')
        monkeypatch.setattr(module, 'get_sync_session', lambda *a, **kw: SyncSession())
        result = module._stream_chat_once(body, **kwargs)
    assert result.message['reasoning_content'] == THINKING
    assert result.message['tool_calls'][0]['function']['name'] == 'read_files'
    assert len(wires) == 1


@pytest.mark.parametrize('model', [MODEL, 'deepseek-v4-pro', 'deepseek-v3.2'])
def test_plain_assistant_reasoning_is_also_replayed(model):
    body = {'model': model, 'tools': TOOLS, 'messages': [
        {'role': 'user', 'content': 'first question'},
        {'role': 'assistant', 'content': 'answer', 'reasoning_content': THINKING},
        {'role': 'user', 'content': 'next question'},
    ]}
    original = deepcopy(body)
    converted = openai_body_to_anthropic_checked(body)
    assert converted.require_payload()['messages'][1]['content'] == [
        BLOCKS[0], {'type': 'text', 'text': 'answer'}]
    assert not converted.permitted_conversion_losses
    assert body == original


@pytest.mark.parametrize('has_tools', [False, True])
@pytest.mark.parametrize('source_model', [MODEL, 'source-model'])
def test_cold_completed_reply_keeps_terminal_reasoning(has_tools, source_model):
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    row = {'role': 'assistant', 'model': source_model,
           'content': 'Finished answer', 'thinking': 'Exact terminal reasoning'}
    if has_tools:
        row['toolRounds'] = [{
            'toolCallId': 'call-read', 'toolName': 'read_files', 'toolArgs': '{}',
            'toolContent': 'file contents', 'status': 'done',
            'llmRound': 0, 'roundNum': 0, 'thinking': THINKING,
        }]
    original = deepcopy(row)
    history = _transform_messages([
        {'role': 'user', 'content': 'first question'}, row,
        {'role': 'user', 'content': 'next question'},
    ], {'model': MODEL}, user_id=7)
    replies = [m for m in history if m.get('role') == 'assistant']
    assert replies[-1]['reasoning_content'] == 'Exact terminal reasoning'
    if has_tools:
        assert replies[0]['reasoning_content'] == THINKING
    converted = openai_body_to_anthropic_checked({
        'model': MODEL, 'tools': TOOLS, 'messages': history})
    thinking = [b['thinking'] for m in converted.require_payload()['messages']
                for b in m['content'] if b.get('type') == 'thinking']
    assert thinking == ([THINKING] if has_tools else []) + ['Exact terminal reasoning']
    assert row == original


@pytest.mark.parametrize(('wire_model', 'logical_model'), [
    ('claude-opus-5.5', 'claude-opus-5.5'),
    (MODEL, 'claude-opus-5.5'),
    ('compatible-messages-model', 'compatible-messages-model'),
])
def test_unsigned_claude_and_unknown_dialects_keep_existing_omission(wire_model, logical_model):
    body = {'model': wire_model, '_admission_logical_model': logical_model,
            'messages': _history('nonstream', 'live')}
    original = deepcopy(body)
    converted = openai_body_to_anthropic_checked(body)
    assert converted.require_payload()['messages'][1]['content'] == BLOCKS[1:]
    assert converted.permitted_conversion_losses == {'optional_reasoning_omitted': 1}
    assert body == original


@pytest.mark.parametrize('invalid', [42, True, 0, False, [], {}, ['text'], {'thinking': 'text'}])
def test_malformed_required_reasoning_cannot_reach_provider(invalid):
    result = openai_body_to_anthropic_checked({'model': MODEL, 'messages': [
        {'role': 'assistant', 'content': 'answer', 'reasoning_content': invalid}]})
    assert result.payload is None
    assert result.validation_issues == ('native_content_invalid',)
