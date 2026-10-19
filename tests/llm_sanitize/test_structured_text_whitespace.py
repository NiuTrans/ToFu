"""Synthetic structured text fidelity: common sanitizer and Anthropic wire."""

from copy import deepcopy
from itertools import product

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.body import build_body
from lib.llm_sanitize import _fix_empty_user_messages, _strip_empty_text_blocks
from lib.llm_sanitize.text_blocks import normalize_text_blocks

pytestmark = pytest.mark.unit


def text(value, **metadata):
    return {'type': 'text', 'text': value, **metadata}


def texts(blocks):
    return [b['text'] for b in blocks if b.get('type') == 'text']


def assert_valid(blocks):
    assert all(b['text'].strip() for b in blocks if b.get('type') == 'text')


SEPARATORS = [' ', '   ', '\n  ', '\t', '\r\n\t  ', '\u00a0', '\u2003\n\t']


@pytest.mark.parametrize('separator', SEPARATORS)
def test_exact_internal_separator_at_common_and_direct_boundaries(separator):
    parts = [text('A'), text(separator), text('B')]
    original = deepcopy(parts)
    expected = [text('A' + separator), text('B')]
    assert normalize_text_blocks(parts) == expected
    messages = [{'role': 'user', 'content': parts}]
    _strip_empty_text_blocks(messages)
    assert messages[0]['content'] == expected
    assert parts == original  # in-place message contract, not in-place blocks
    body = {'model': 'm', 'messages': [{'role': 'user', 'content': parts}]}
    wire = openai_body_to_anthropic(body)
    actual = wire['messages'][0]['content']
    assert actual == expected
    assert ''.join(texts(actual)).encode('utf-8') == ('A' + separator + 'B').encode('utf-8')
    assert_valid(actual)
    assert body['messages'][0]['content'] == original


@pytest.mark.parametrize('chunks', [
    ['```python', '\n', 'def f():', '\n', '  ', '\t', 'return "雪🙂"', '\n', '```'],
    ['\t', 'α', '', ' ', '\n  ', 'β🙂', '\r\n'],
    ['A', '', 'B'],
    ['A', 'B'],
    ['\u200b'],  # zero-width space is not whitespace under Python's predicate
])
def test_chunked_text_has_identical_bytes_and_is_idempotent(chunks):
    parts = [text(chunk) for chunk in chunks]
    original = deepcopy(parts)
    normalized = normalize_text_blocks(parts)
    assert ''.join(texts(normalized)).encode('utf-8') == ''.join(chunks).encode('utf-8')
    assert_valid(normalized)
    assert normalize_text_blocks(normalized) == normalized
    assert parts == original
    messages = [{'role': 'user', 'content': parts}]
    _strip_empty_text_blocks(messages)
    once = deepcopy(messages)
    _strip_empty_text_blocks(messages)
    assert messages == once
    built = build_body('kimi-k3', messages)
    direct = openai_body_to_anthropic({'model': 'm', 'messages': once})
    integrated = openai_body_to_anthropic(built)
    assert direct['messages'] == integrated['messages']
    blocks = integrated['messages'][0]['content']
    assert ''.join(texts(blocks)).encode('utf-8') == ''.join(chunks).encode('utf-8')
    assert_valid(blocks)


def test_exhaustive_small_plain_runs_preserve_bytes_or_drop_wholly_blank():
    for size in range(6):
        for chunks in product(('', ' ', '\n\t', '字'), repeat=size):
            normalized = normalize_text_blocks([text(c) for c in chunks])
            expected = ''.join(chunks)
            assert ''.join(texts(normalized)) == (expected if expected.strip() else '')
            assert normalize_text_blocks(normalized) == normalized
            assert_valid(normalized)


@pytest.mark.parametrize('metadata', [
    {'cache_control': {'type': 'ephemeral', 'ttl': '1h'}},
    {'signature': 'opaque-signature'},
    {'unknown': {'nested': ['opaque']}},
])
def test_metadata_is_a_hard_boundary_even_when_equal(metadata):
    parts = [text('A', **metadata), text(' '), text('B', **metadata)]
    original = deepcopy(parts)
    expected = [original[0], original[2]]
    assert normalize_text_blocks(parts) == expected
    messages = [{'role': 'user', 'content': parts}]
    _strip_empty_text_blocks(messages)
    assert messages[0]['content'] == expected
    assert parts == original
    assert normalize_text_blocks(expected) == expected


@pytest.mark.parametrize('boundary', [
    text('', cache_control={'type': 'ephemeral'}),
    text(' \n', signature='opaque'),
    text('\t', unknown={'opaque': 1}),
    {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image.png'}},
    {'type': 'future_block', 'data': {'opaque': ['value']}},
    'opaque-nondict',
])
def test_no_bytes_move_across_blank_metadata_or_multimodal_boundary(boundary):
    parts = [text('A'), text(' '), boundary, text('\n\t'), text('B')]
    original = deepcopy(parts)
    middle = [] if isinstance(boundary, dict) and boundary.get('type') == 'text' else [boundary]
    expected = [text('A '), *middle, text('\n\tB')]
    normalized = normalize_text_blocks(parts)
    assert normalized == expected
    assert normalize_text_blocks(normalized) == normalized
    assert parts == original


def test_direct_wire_cache_boundaries_and_image_order_are_unchanged():
    cc = {'type': 'ephemeral', 'ttl': '1h'}
    image = {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image.png'}}
    parts = [text('cached', cache_control=cc), text(' '), text('next'),
             text('\n'), image, text('\t'), text('tail', unknown={'opaque': 1})]
    body = {'model': 'm', 'messages': [{'role': 'user', 'content': parts}]}
    original = deepcopy(body)
    blocks = openai_body_to_anthropic(body)['messages'][0]['content']
    assert blocks == [text('cached', cache_control=cc), text(' next\n'),
                      {'type': 'image', 'source': {'type': 'url', 'url': image['image_url']['url']}},
                      text('tail')]
    # Existing adapter metadata projection stays unchanged; no bytes fold into it.
    assert body == original
    assert_valid(blocks)


@pytest.mark.parametrize('role', ['system', 'user', 'assistant', 'tool'])
@pytest.mark.parametrize('separator', SEPARATORS)
def test_build_body_then_converter_matches_direct_wire(role, separator):
    parts = [text('行🙂'), text(separator), text('end')]
    calls = [{'id': 'tc1', 'type': 'function',
              'function': {'name': 'f', 'arguments': '{}'}}]
    messages = [{'role': 'system', 'content': parts if role == 'system' else 'sys'},
                {'role': 'user', 'content': parts if role == 'user' else 'q'},
                {'role': 'assistant', 'tool_calls': calls,
                 'content': parts if role == 'assistant' else ''},
                {'role': 'tool', 'tool_call_id': 'tc1',
                 'content': parts if role == 'tool' else 'result'},
                {'role': 'user', 'content': 'next'}]
    original = deepcopy(messages)
    body = build_body('kimi-k3', messages)
    assert messages == original
    canonical = next(m for m in body['messages'] if m['role'] == role)
    assert texts(canonical['content']) == ['行🙂' + separator, 'end']
    wire = openai_body_to_anthropic(body)
    assert wire == openai_body_to_anthropic({'model': 'kimi-k3', 'messages': original,
                                          **{k: v for k, v in body.items() if k != 'messages'}})
    if role == 'system':
        assert texts(wire['system']) == ['行🙂' + separator, 'end']
        assert_valid(wire['system'])
    else:
        index = {'user': 0, 'assistant': 1, 'tool': 2}[role]
        blocks = wire['messages'][index]['content']
        if role == 'tool':
            blocks = blocks[0]['content']
        assert texts(blocks) == ['行🙂' + separator, 'end']
        assert_valid(blocks)


@pytest.mark.parametrize('chunks', [[''], [' ', '\n\t', '\u00a0'], ['', '  ', '']])
def test_all_blank_tool_call_content_omits_key_and_wire_text(chunks):
    calls = [{'id': 'tc1', 'type': 'function', 'function': {'name': 'f', 'arguments': '{}'}}]
    messages = [{'role': 'user', 'content': 'q'},
                {'role': 'assistant', 'content': [text(c) for c in chunks], 'tool_calls': calls},
                {'role': 'tool', 'tool_call_id': 'tc1', 'content': 'result'},
                {'role': 'user', 'content': 'next'}]
    original = deepcopy(messages)
    body = build_body('kimi-k3', messages)
    assert 'content' not in body['messages'][1]
    assert [b['type'] for b in openai_body_to_anthropic(body)['messages'][1]['content']] == ['tool_use']

    direct = openai_body_to_anthropic({'model': 'm', 'messages': messages})
    assert [b['type'] for b in direct['messages'][1]['content']] == ['tool_use']
    assert messages == original


@pytest.mark.parametrize('value', ['  unchanged\n\t雪🙂  ', '```\n  code\n```'])
def test_nonblank_single_strings_unchanged(value):
    messages = [{'role': 'user', 'content': value}]
    _strip_empty_text_blocks(messages)
    assert messages[0]['content'] == value
    assert build_body('kimi-k3', messages)['messages'][0]['content'] == value
    assert openai_body_to_anthropic({'model': 'm', 'messages': messages})['messages'][0]['content'] == [text(value)]


@pytest.mark.parametrize('role', ['user', 'tool'])
def test_wholly_blank_content_still_heals_with_strict_placeholder(role):
    messages = [{'role': role, 'content': [text(' '), text('\n\t')]}]
    _strip_empty_text_blocks(messages)
    assert messages[0]['content'] == ''
    _fix_empty_user_messages(messages)
    wire = openai_body_to_anthropic({'model': 'm', 'messages': messages})
    blocks = wire['messages'][0]['content']
    if role == 'tool':
        assert blocks[0]['content'] == '[empty tool result]'
    else:
        assert_valid(blocks)
        assert texts(blocks) == ['[empty message]']
