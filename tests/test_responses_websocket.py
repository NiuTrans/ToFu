"""Responses WebSocket framing, state reuse, and incremental-input tests."""

from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip('websockets', reason='optional websockets dependency not installed')


class _FakeConnection:
    def __init__(self):
        self.sent = []
        self.events = []
        self.closed = False

    def send(self, payload):
        envelope = json.loads(payload)
        self.sent.append(envelope)
        if len(self.sent) == 1:
            self.events = [
                {'type': 'response.output_item.added', 'item': {
                    'type': 'function_call', 'id': 'fc_1',
                    'call_id': 'call_1', 'name': 'read_files',
                    'arguments': ''}},
                {'type': 'response.function_call_arguments.delta',
                 'item_id': 'fc_1', 'delta': '{"paths":["README.md"]}'},
                {'type': 'response.completed', 'response': {
                    'id': 'resp_1', 'status': 'completed',
                    'output': [{'type': 'function_call', 'id': 'fc_1',
                                'call_id': 'call_1', 'name': 'read_files',
                                'arguments': '{"paths":["README.md"]}'}],
                    'usage': {'input_tokens': 10, 'output_tokens': 2}}},
            ]
        else:
            self.events = [
                {'type': 'response.output_text.delta', 'delta': 'done'},
                {'type': 'response.completed', 'response': {
                    'id': 'resp_2', 'status': 'completed',
                    'output': [{'type': 'message', 'role': 'assistant',
                                'content': [{'type': 'output_text',
                                             'text': 'done'}]}],
                    'usage': {'input_tokens': 4, 'output_tokens': 1}}},
            ]

    def recv(self, timeout=None):
        if self.events:
            return json.dumps(self.events.pop(0))
        raise TimeoutError

    def close(self):
        self.closed = True


def _canonical_body(*, with_tool_output: bool):
    messages = [{'role': 'user', 'content': 'read the file'}]
    if with_tool_output:
        messages.extend([
            {'role': 'assistant', 'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'read_files',
                             'arguments': '{"paths":["README.md"]}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'call_1',
             'content': 'README contents'},
        ])
    return {
        'model': 'gpt-5.6-sol', '_task_id': 'task-websocket',
        '_responses_feature_profile': 'openai',
        '_responses_transport': 'websocket',
        'messages': messages,
        'tools': [{'type': 'function', 'function': {
            'name': 'read_files', 'description': 'read files',
            'parameters': {'type': 'object'},
        }}],
    }


def test_websocket_reuses_response_id_and_sends_only_new_tool_output(
        monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws

    ws._sessions().clear()
    connection = _FakeConnection()
    monkeypatch.setattr(
        'websockets.sync.client.connect', lambda *args, **kwargs: connection)

    first_plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    first_msg, first_finish, _ = ws.stream_responses_websocket(first_plan)
    assert first_finish == 'tool_calls'
    assert first_msg['tool_calls'][0]['function']['name'] == 'read_files'
    assert connection.closed is False

    second_plan = prepare_request(
        _canonical_body(with_tool_output=True), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    second_msg, second_finish, _ = ws.stream_responses_websocket(second_plan)
    assert second_finish == 'stop'
    assert second_msg['content'] == 'done'

    second_request = connection.sent[1]['response']
    assert second_request['previous_response_id'] == 'resp_1'
    assert [item['type'] for item in second_request['input']] == [
        'function_call_output']
    assert second_request['input'][0]['call_id'] == 'call_1'
    assert connection.closed is True


def test_incremental_input_preserves_excess_occurrences_not_set_membership():
    from lib.llm import responses_websocket as ws

    item = {
        'type': 'function_call_output', 'call_id': 'recycled-call',
        'output': 'same receipt',
    }
    session = ws._Session(connection=None)
    session.seen_external[ws._external_key(item)] = 1

    delta = ws._incremental_input(session, [dict(item), dict(item)])

    assert delta == [item]


def test_websocket_protocol_activity_outlives_stream_idle_window(
        monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class ProtocolActivityConnection:
        def __init__(self):
            self.events = []
            self.closed = False

        def send(self, _payload):
            self.events = [
                {'type': 'response.in_progress'}
                for _ in range(21)
            ] + [
                {'type': 'response.output_text.delta', 'delta': 'done'},
                {'type': 'response.completed', 'response': {
                    'id': 'resp-long', 'status': 'completed', 'output': [],
                    'usage': {'input_tokens': 4, 'output_tokens': 21}}},
            ]

        def recv(self, timeout=None):
            time.sleep(0.02)
            if self.events:
                return json.dumps(self.events.pop(0))
            raise TimeoutError

        def close(self):
            self.closed = True

    ws._sessions().clear()
    connection = ProtocolActivityConnection()
    monkeypatch.setattr(
        'websockets.sync.client.connect', lambda *args, **kwargs: connection)
    monkeypatch.setattr(
        'lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.15)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    result = ws.stream_responses_websocket(plan)

    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'done'
    assert result.message.get('reasoning_content', '') == ''
    assert '_no_actionable_timeout' not in result.usage
    assert '_semantic_progress_timeout' not in result.usage


def test_websocket_transport_silence_finalizes_as_premature_close(monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class SilentConnection:
        def __init__(self):
            self.closed = False

        def send(self, _payload):
            return None

        def recv(self, timeout=None):
            time.sleep(float(timeout or 0))
            raise TimeoutError

        def close(self):
            self.closed = True

    ws._sessions().clear()
    connection = SilentConnection()
    monkeypatch.setattr(
        'websockets.sync.client.connect', lambda *args, **kwargs: connection)
    monkeypatch.setattr(
        'lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.08)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    started = time.monotonic()
    result = ws.stream_responses_websocket(plan)

    assert time.monotonic() - started < 1.0
    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.usage['_failure_stage'] == 'midstream_close'
    assert result.usage['_missing_done'] is True
    assert connection.closed is True


def test_unverified_partial_tool_response_never_advances_socket_state(
        monkeypatch):
    from dataclasses import replace

    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class PartialToolConnection:
        def __init__(self):
            self.events = []
            self.closed = False

        def send(self, _payload):
            self.events = [{
                'type': 'response.output_item.added', 'output_index': 0,
                'item': {
                    'type': 'function_call', 'id': 'partial-item',
                    'call_id': 'partial-call', 'name': 'read_files',
                    'arguments': '{}',
                },
            }]

        def recv(self, timeout=None):
            if self.events:
                return json.dumps(self.events.pop(0))
            time.sleep(float(timeout or 0))
            raise TimeoutError

        def close(self):
            self.closed = True

    ws._sessions().clear()
    connection = PartialToolConnection()
    monkeypatch.setattr(
        'websockets.sync.client.connect', lambda *args, **kwargs: connection)
    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.08)
    original_finalize = ws.SSEAccumulator.finalize

    def compatibility_tool_finish(accumulator, *args, **kwargs):
        result = original_finalize(accumulator, *args, **kwargs)
        return replace(result, compatibility_finish_reason='tool_calls')

    monkeypatch.setattr(ws.SSEAccumulator, 'finalize', compatibility_tool_finish)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    # Exercise the dangerous compatibility shape directly: partial tools plus
    # a stale/non-terminal response token must still never keep the session.
    plan.wire_translator.response_id = 'resp-partial'

    result = ws.stream_responses_websocket(plan)

    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.finish_reason == 'tool_calls'
    assert connection.closed is True
    assert ws._sessions() == {}


def test_non_object_websocket_event_is_typed_malformed_stream(monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class InvalidShapeConnection:
        def __init__(self):
            self.closed = False
            self.sent = False

        def send(self, _payload):
            self.sent = True

        def recv(self, timeout=None):
            if self.sent:
                self.sent = False
                return json.dumps(['not', 'an', 'event'])
            raise TimeoutError

        def close(self):
            self.closed = True

    ws._sessions().clear()
    connection = InvalidShapeConnection()
    monkeypatch.setattr(
        'websockets.sync.client.connect', lambda *args, **kwargs: connection)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    result = ws.stream_responses_websocket(plan)

    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.evidence.malformed_frame_count == 1
    assert connection.closed is True
