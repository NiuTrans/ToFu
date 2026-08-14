"""Responses WebSocket framing, state reuse, and incremental-input tests."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


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
