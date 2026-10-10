"""Provider input measurements authorize only their unchanged request prefix."""
from copy import deepcopy

import pytest

from lib.token_counter import usage_cache
from lib.tasks_pkg.compaction import _tokens

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_cache():
    usage_cache._reset_usage_cache_for_tests()
    yield
    usage_cache._reset_usage_cache_for_tests()


def record(messages, **kwargs):
    usage_cache.record_usage('scope', prompt_tokens=68_037, model='gpt-4o',
                             message_count=len(messages), messages=messages,
                             **kwargs)


@pytest.mark.parametrize('change', ['early', 'late_text', 'tool', 'image', 'opaque', 'model'])
def test_changed_prefix_or_model_is_not_measured(change):
    messages = [{'role': 'user', 'content': 'a' * 200} for _ in range(8)]
    record(messages)
    candidate = deepcopy(messages)
    model = 'gpt-4o'
    if change == 'early':
        candidate[0]['content'] = 'cold reconstructed bulk' * 1000
    elif change == 'late_text':
        candidate[-1]['content'] += 'changed after character 120'
    elif change == 'tool':
        candidate[0]['tool_calls'] = [{'id': 'x', 'function': {'arguments': 'bulk'}}]
    elif change == 'image':
        candidate[0]['content'] = [{'type': 'image_url', 'image_url': {'url': 'new'}}]
    elif change == 'opaque':
        candidate[0]['_responses_items'] = [{'type': 'reasoning', 'encrypted_content': 'changed'}]
    else:
        model = 'gpt-5'
    assert usage_cache.UsageCacheCounter().count(candidate, model=model, conv_id='scope') is None


def test_request_envelope_drift_rejects_measurement():
    messages = [{'role': 'user', 'content': 'hello'}]
    tools = [{'type': 'function', 'function': {'name': 'one'}}]
    record(messages, tools=tools, system='policy')
    counter = usage_cache.UsageCacheCounter()
    assert counter.count(messages, model='gpt-4o', conv_id='scope', tools=tools, system='policy') == 68_037
    assert counter.count(messages, model='gpt-4o', conv_id='scope', tools=tools, system='changed') is None
    assert counter.count(messages, model='gpt-4o', conv_id='scope', tools=[]) is None


def test_cold_reconstruction_cannot_downclamp_to_previous_usage(monkeypatch):
    import lib.token_counter
    record([{'role': 'user', 'content': 'compact prior request'}])
    messages = [{'role': 'user', 'content': 'reconstructed'} for _ in range(3361)]
    monkeypatch.setattr(lib.token_counter, 'count_tokens', lambda *a, **k: {'tokens': 2_183_576, 'method': 'tiktoken'})
    monkeypatch.setattr(_tokens, '_estimate_total_tokens', lambda m: 2_183_576)
    count, method = _tokens._count_tokens_authoritative(messages, {'convId': 'scope', 'config': {'model': 'gpt-4o'}})
    assert count == 2_183_576
    assert 'anchor:' not in method


def test_append_counts_only_suffix_and_preserves_opaque_reserve(monkeypatch):
    import lib.token_counter.heuristic as heuristic
    prefix = [{'role': 'user', 'content': 'large unchanged prefix' * 10000}]
    record(prefix, opaque_replay_tokens=12000)
    seen = []
    monkeypatch.setattr(heuristic, 'cheap_estimate_text', lambda text: seen.append(text) or 3)
    suffix = {'role': 'assistant', 'content': 'answer', '_responses_items': [{'type': 'reasoning', 'encrypted_content': 'ciphertext' * 10000}]}
    assert usage_cache.UsageCacheCounter().count(deepcopy(prefix) + [suffix], model='gpt-4o', conv_id='scope') == 68037 + 3 + 12000
    assert seen == ['answer']


def test_native_compaction_does_not_bind_smaller_input_to_original_body():
    from lib.tasks_pkg.manager._stream import _record_stream_prompt_usage
    messages = [{'role': 'user', 'content': 'original'}]
    record(messages)
    total = _record_stream_prompt_usage(
        {'convId': 'scope'}, {'messages': messages},
        {'input_tokens': 100000, 'effective_prompt_tokens': 1000}, {},
        model='gpt-4o', prefix='test')
    assert total == 1000
    assert usage_cache.UsageCacheCounter().count(messages, model='gpt-4o', conv_id='scope') is None


def test_missing_recording_prefix_invalidates_old_evidence():
    messages = [{'role': 'user', 'content': 'original'}]
    record(messages)
    usage_cache.record_usage('scope', prompt_tokens=1, model='gpt-4o', message_count=1)
    assert usage_cache.UsageCacheCounter().count(messages, model='gpt-4o', conv_id='scope') is None
