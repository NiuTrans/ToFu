"""Successful nonstream responses retain receipts without decoding prompt bytes."""
import importlib
import json

import pytest

from lib.llm.request import seal_request
from lib.llm.request_receipt import request_parameter_receipt

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_successful_nonstream_response_uses_sealed_mapping(monkeypatch, protocol):
    chat_module = importlib.import_module('lib.llm.chat')
    calls = []
    payloads = {
        'openai': {'choices': [{'message': {'content': 'Translated input'},
                               'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 10, 'completion_tokens': 3}},
        'anthropic': {'content': [{'type': 'text', 'text': 'Translated input'}],
                      'stop_reason': 'end_turn',
                      'usage': {'input_tokens': 10, 'output_tokens': 3}},
        'responses': {'output': [{'type': 'message', 'role': 'assistant',
                                  'content': [{'type': 'output_text',
                                               'text': 'Translated input'}]}],
                      'status': 'completed',
                      'usage': {'input_tokens': 10, 'output_tokens': 3}},
    }

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return payloads[protocol]

    def post(*args, **kwargs):
        assert isinstance(kwargs['data'], bytes)
        calls.append(json.loads(kwargs['data']))
        return Response()

    monkeypatch.setattr(chat_module, 'http_post', post)
    text, usage = chat_module.chat(
        [{'role': 'user', 'content': '需要翻译的输入'}],
        model='test-model', api_key='test-only',
        base_url='https://public.example/v1', api_protocol=protocol,
        max_tokens=128, temperature=0, max_retries=0,
    )
    assert text == 'Translated input'
    assert len(calls) == 1
    assert usage['prompt_tokens'] == 10
    assert usage['completion_tokens'] == 3
    receipt = usage['_request_parameters']
    token_key = 'max_output_tokens' if protocol == 'responses' else 'max_tokens'
    assert calls[0][token_key] == 128
    assert receipt['max_tokens'] == calls[0].get('max_tokens')
    assert 'messages' not in receipt
    assert usage['_request_identity']['semantic_request_id']


def test_receipt_reads_only_controls_without_decoding_large_prompt(monkeypatch):
    body = {'messages': [{'role': 'user', 'content': 'private' * 200000}],
            'max_tokens': 128, 'temperature': 0, 'top_p': 0.9}
    admitted = seal_request(body, model='test-model', input_tokens=1,
                            semantic_id='test')

    def forbid_decode(*args, **kwargs):
        pytest.fail('receipt decoded sealed prompt bytes')

    monkeypatch.setattr('lib.llm.request.json.loads', forbid_decode)
    assert request_parameter_receipt(admitted) == request_parameter_receipt(body)
    assert request_parameter_receipt(admitted) == {
        'max_tokens': 128, 'max_completion_tokens': None,
        'temperature': 0, 'top_p': 0.9, 'reasoning_effort': None,
        'thinking_type': None}
