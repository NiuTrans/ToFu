"""Executable semantic contract for local protocol conversion."""
import copy
import json

import pytest

from lib.llm.anthropic_outbound._to_anthropic import openai_body_to_anthropic
from lib.llm.responses_outbound._to_responses import openai_body_to_responses
from lib.llm.conversion import ProtocolConversionError, tool_name_reverse
from lib.llm.request import seal_request

pytestmark = pytest.mark.unit


def responses(body):
    return openai_body_to_responses(body)[0]


def call(arguments='{}', name='lookup'):
    return {'id': 'c1', 'type': 'function', 'function': {
        'name': name, 'arguments': arguments}}


@pytest.mark.parametrize('convert', [responses, openai_body_to_anthropic])
@pytest.mark.parametrize('arguments', ['{secret', '[]', 'null', '', None, [], '{"x":NaN}'])
def test_invalid_arguments_are_local_and_secret_free(convert, arguments):
    body = {'messages': [{'role': 'assistant', 'tool_calls': [call(arguments)]}]}
    before = copy.deepcopy(body)
    with pytest.raises(ProtocolConversionError) as exc:
        convert(body)
    assert isinstance(exc.value, ValueError)
    assert 'secret' not in str(exc.value)
    assert body == before


@pytest.mark.parametrize('convert', [responses, openai_body_to_anthropic])
@pytest.mark.parametrize('payload', [
    {'messages': [{'role': 'user', 'content': [{'type': 'audio', 'data': 'secret'}]}]},
    {'tools': [{'type': 'unknown', 'secret': 'hidden'}]},
    {'messages': [{'role': 'user', 'content': [None]}]},
])
def test_unsupported_is_not_silently_dropped(convert, payload):
    with pytest.raises(ProtocolConversionError):
        convert(payload)


def test_names_are_bounded_reversible_and_reserve_short_aliases():
    long_a, long_b = 'x' * 80 + 'a', 'x' * 80 + 'b'
    initial = {'tools': [{'type': 'function', 'function': {'name': long_a}}]}
    short = next(iter(tool_name_reverse(initial)))
    names = [long_a, long_b, short, 'ordinary']
    body = {'tools': [{'type': 'function', 'function': {'name': n}} for n in names],
            'messages': [{'role': 'assistant', 'tool_calls': [call(name=n) for n in names]}],
            'tool_choice': {'type': 'function', 'function': {'name': long_a}}}
    before = copy.deepcopy(body)
    out, reverse = openai_body_to_responses(body)
    emitted = [t['name'] for t in out['tools']]
    assert len(set(emitted)) == len(names)
    assert all(len(n) <= 64 for n in emitted)
    assert [reverse.get(n, n) for n in emitted] == names
    assert [i['name'] for i in out['input']] == emitted
    assert out['tool_choice']['name'] == emitted[0]
    assert openai_body_to_responses(body)[1] == reverse
    assert body == before


@pytest.mark.parametrize('convert', [responses, openai_body_to_anthropic])
def test_optional_reasoning_report_is_bounded_and_sealed(convert):
    body = {'messages': [{'role': 'assistant', 'content': 'public',
                          'reasoning_content': 'secret-private-content'}]}
    before = copy.deepcopy(body)
    out = convert(body)
    assert out['_conversion_report'] == {'optional_reasoning_omitted': 1}
    assert 'secret' not in json.dumps(out['_conversion_report'])
    sealed = seal_request(out, model='m', input_tokens=1, semantic_id='test')
    assert '_conversion_report' not in sealed.wire_body()
    assert body == before


def test_native_signed_and_redacted_order_and_format_effort_survive():
    blocks = [{'type': 'thinking', 'thinking': 'opaque', 'signature': 'signed'},
              {'type': 'redacted_thinking', 'data': 'opaque-redacted'},
              {'type': 'tool_use', 'id': 'c', 'name': 'lookup', 'input': {}}]
    body = {'messages': [{'role': 'assistant', '_anthropic_content_blocks': blocks}],
            'effort': 'high', 'reasoning_effort': 'low',
            'output_config': {'effort': 'medium', 'format': {'type': 'json_schema'}}}
    before = copy.deepcopy(body)
    out = openai_body_to_anthropic(body)
    assert out['messages'][0]['content'] == blocks
    assert out['output_config'] == {'effort': 'high', 'format': {'type': 'json_schema'}}
    assert body == before


@pytest.mark.parametrize('blocks', [[None], [{'type': 'tool_use', 'input': None}],
                                   [{'type': 'thinking', 'thinking': 'secret'}]])
def test_native_invalid_structure_is_not_repaired(blocks):
    with pytest.raises(ProtocolConversionError):
        openai_body_to_anthropic({'messages': [
            {'role': 'assistant', '_anthropic_content_blocks': blocks}]})
