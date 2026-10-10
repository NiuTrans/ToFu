"""Canonical usage-prefix digests, failures, mutation and concurrent isolation."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from lib.token_counter import usage_cache

pytestmark = pytest.mark.unit


def original_signature(messages, end=None):
    digest = hashlib.sha256()
    for item in messages[:len(messages) if end is None else end]:
        digest.update(json.dumps(item, sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()


@pytest.mark.parametrize('messages', [[], [{}], [{'content': '你好\0\n"\\'}],
    [{'role': 'assistant', 'tool_calls': [{'function': {'arguments': '{"x":1}'}}]},
     {'role': 'tool', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:fixture'}}]}],
    [{'numeric': [1, -0.0, 1e100, float('inf'), float('nan')], 'boolean': True}],
    [{1: 'integer-key', 2: 'other-key'}]])
def test_canonical_digest_bytes_match_original(messages):
    assert usage_cache._signature(messages) == original_signature(messages)
    for end in range(len(messages)+1):
        assert usage_cache._signature(messages, end=end) == original_signature(messages, end)


@pytest.mark.parametrize('kind', ['unsupported', 'mixed_keys', 'surrogate', 'cycle'])
def test_original_encoding_failures_are_preserved(kind):
    row = {'unsupported': {'value': object()}, 'mixed_keys': {1: 'a', '1': 'b'},
           'surrogate': {'content': '\ud800'}, 'cycle': {}}[kind]
    if kind == 'cycle':
        row['self'] = row
    with pytest.raises((TypeError, ValueError, UnicodeEncodeError)) as expected:
        original_signature([row])
    with pytest.raises(type(expected.value)):
        usage_cache._signature([row])


def test_nested_early_edit_cannot_reuse_provider_usage():
    conv = 'signature-encoder-edit'
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'original'}]},
                {'role': 'assistant', 'content': 'latest'}]
    usage_cache.record_usage(conv, prompt_tokens=500, model='owned-model',
                             message_count=2, messages=messages)
    counter = usage_cache.UsageCacheCounter()
    try:
        assert counter.count(messages, model='owned-model', conv_id=conv) == 500
        messages[0]['content'][0]['text'] = 'edited'
        assert counter.count(messages, model='owned-model', conv_id=conv) is None
    finally:
        usage_cache.invalidate(conv)


def test_concurrent_digests_have_no_encoder_cross_call_state():
    fixtures = [[{'content': '你好 '*i, 'nested': {'index': i}}] for i in range(64)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(usage_cache._signature, fixtures))
    assert results == [original_signature(value) for value in fixtures]
