"""Bounded repeated-text token-count reuse contracts.

Stable tool-schema and compaction projections are counted on consecutive model
rounds.  Reuse must avoid another tokenizer pass without retaining prompt text,
crossing tokenizer encodings, or growing beyond the deployment budget.
"""

from __future__ import annotations

import sys

import pytest

import lib.token_counter.tiktoken_counter as counter
from lib.token_counter import count_text as public_count_text
from lib.token_counter.base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
)


pytestmark = pytest.mark.unit


class _FakeEncoder:
    def __init__(self):
        self.calls = 0
        self.batch_calls = 0
        self.batches = []

    @staticmethod
    def _tokens(text):
        return list(range(max(1, len(text) // 4)))

    def encode(self, text, *, disallowed_special=()):
        self.calls += 1
        return self._tokens(text)

    def encode_batch(self, texts, *, disallowed_special=()):
        self.batch_calls += 1
        self.batches.append(list(texts))
        return [self._tokens(text) for text in texts]


@pytest.fixture(autouse=True)
def _isolated_cache():
    counter._reset_text_count_cache_for_tests()
    yield
    counter._reset_text_count_cache_for_tests()


def test_default_cache_budget_covers_repeated_tool_results():
    assert counter._TEXT_COUNT_CACHE_MIN_CHARS == 4096
    assert counter._TEXT_COUNT_REUSABLE_MIN_CHARS == 512
    assert 1 <= int(counter._TEXT_COUNT_CACHE_CAPACITY) <= 4096
    assert counter.REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX == 4096


def test_exact_and_heuristic_value_payload_stays_below_half_megabyte_ceiling():
    entry = counter._TextCountCacheEntry(
        exact_tokens=123_456_789,
        heuristic_tokens=987_654_321,
    )

    retained_value_bytes = (
        sys.getsizeof(entry)
        + sys.getsizeof(entry.exact_tokens)
        + sys.getsizeof(entry.heuristic_tokens)
    )

    assert not hasattr(entry, '__dict__')
    assert retained_value_bytes <= 128
    assert retained_value_bytes * 4096 <= 512 * 1024


def test_large_text_count_reuses_digest_without_retaining_text(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 16)
    text = 'private schema text ' * 100

    first = counter.count_text(text, model='kimi-k3')
    second = counter.count_text(text, model='kimi-k3')

    assert first == second
    assert encoder.calls == 1
    assert len(counter._text_count_cache) == 1
    assert text not in repr(counter._text_count_cache)
    assert counter.text_count_cache_snapshot() == {
        'hits': 1,
        'misses': 1,
        'heuristicHits': 0,
        'heuristicMisses': 0,
        'evictions': 0,
        'entries': 1,
        'capacity': max(1, int(counter._TEXT_COUNT_CACHE_CAPACITY)),
        'minChars': 16,
        'reusableMinChars': 512,
    }


def test_entropy_estimate_shares_exact_digest_without_retaining_text(
    monkeypatch,
):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 16)
    private_text = 'private entropy evidence ' * 30
    estimate_calls = []
    original_estimate = counter._cheap_estimate_text
    monkeypatch.setattr(
        counter,
        '_cheap_estimate_text',
        lambda text: estimate_calls.append(text) or original_estimate(text),
    )

    exact = counter.count_text(
        private_text, model='gpt-5.6-sol', reusable=True)
    first = counter.cached_cheap_estimate_text(private_text, reusable=True)
    second = counter.cached_cheap_estimate_text(private_text, reusable=True)

    assert exact == len(_FakeEncoder._tokens(private_text))
    assert first == second == original_estimate(private_text)
    assert estimate_calls == [private_text]
    snapshot = counter.text_count_cache_snapshot()
    assert snapshot['entries'] == 1
    assert snapshot['heuristicMisses'] == 1
    assert snapshot['heuristicHits'] == 1
    assert private_text not in repr(counter._text_count_cache)


def test_entropy_only_entry_consolidates_into_later_exact_scope(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 16)
    private_text = 'standalone private estimate ' * 30

    estimate = counter.cached_cheap_estimate_text(
        private_text, reusable=True)
    assert estimate > 0
    assert counter.text_count_cache_snapshot()['entries'] == 1

    exact = counter.count_text(
        private_text, model='gpt-5.6-sol', reusable=True)

    assert exact == len(_FakeEncoder._tokens(private_text))
    assert encoder.calls == 1
    assert counter.text_count_cache_snapshot()['entries'] == 1
    only_key = next(iter(counter._text_count_cache))
    assert only_key[0] == 'o200k_base'


def test_entropy_estimate_uses_existing_resource_ceiling(monkeypatch):
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 4)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_CAPACITY', 2)

    for text in ('aaaa private', 'bbbb private', 'cccc private'):
        counter.cached_cheap_estimate_text(text, reusable=True)

    snapshot = counter.text_count_cache_snapshot()
    assert snapshot['entries'] == snapshot['capacity'] == 2
    assert snapshot['evictions'] == 1


def test_large_text_cache_is_encoding_scoped(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 16)
    text = 'stable schema ' * 100

    counter.count_text(text, model='kimi-k3')
    counter.count_text(text, model='gpt-5.6-sol')

    assert encoder.calls == 2
    assert len(counter._text_count_cache) == 2


def test_large_text_cache_evicts_at_the_resource_ceiling(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 4)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_CAPACITY', 2)

    for text in ('aaaa', 'bbbb', 'cccc', 'aaaa'):
        counter.count_text(text, model='kimi-k3')

    assert encoder.calls == 4
    assert len(counter._text_count_cache) == 2
    assert counter.text_count_cache_snapshot()['evictions'] == 2


def test_short_text_bypasses_digest_cache(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 16)

    counter.count_text('short', model='kimi-k3')
    counter.count_text('short', model='kimi-k3')

    assert encoder.calls == 2
    assert not counter._text_count_cache
    assert counter.text_count_cache_snapshot()['hits'] == 0
    assert counter.text_count_cache_snapshot()['misses'] == 0


def test_reusable_hint_does_not_charge_general_medium_text(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 4096)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 512)
    text = 'repeatable tool result ' * 50

    public_count_text(text, model='kimi-k3')
    public_count_text(text, model='kimi-k3')
    assert encoder.calls == 2
    assert not counter._text_count_cache

    public_count_text(text, model='kimi-k3', reusable=True)
    public_count_text(text, model='kimi-k3', reusable=True)
    assert encoder.calls == 3
    assert counter.text_count_cache_snapshot()['entries'] == 1
    assert counter.text_count_cache_snapshot()['hits'] == 1
    assert counter.text_count_cache_snapshot()['misses'] == 1


def test_full_request_batches_cold_text_and_reuses_large_prefix(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 16)
    private_message = 'private repeated message ' * 20
    private_tool = {
        'type': 'function',
        'function': {'name': 'read', 'description': 'private schema ' * 20},
    }
    messages = [{'role': 'user', 'content': private_message}]
    token_counter = counter.TiktokenCounter()

    first = token_counter.count(
        messages, model='kimi-k3', tools=[private_tool])
    second = token_counter.count(
        messages, model='kimi-k3', tools=[private_tool])

    assert first == second
    assert encoder.batch_calls == 1
    assert len(encoder.batches[0]) == 2
    assert counter.text_count_cache_snapshot()['hits'] == 2
    assert counter.text_count_cache_snapshot()['misses'] == 2
    assert private_message not in repr(counter._text_count_cache)
    assert 'private schema' not in repr(counter._text_count_cache)


def test_full_request_exposes_only_call_local_tool_string_counts(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 16)
    large_tool_result = 'private tool result ' * 40
    short_tool_result = 'ok'
    messages = [
        {'role': 'user', 'content': 'inspect the results'},
        {'role': 'tool', 'content': large_tool_result},
        {'role': 'tool', 'content': short_tool_result},
    ]
    token_counter = counter.TiktokenCounter()

    first_measurement = {}
    first = token_counter.count(
        messages,
        model='gpt-5.6-sol',
        measurement_out=first_measurement,
    )
    second_measurement = {}
    second = token_counter.count(
        messages,
        model='gpt-5.6-sol',
        measurement_out=second_measurement,
    )

    expected_counts = {
        id(large_tool_result): len(_FakeEncoder._tokens(large_tool_result)),
        id(short_tool_result): len(_FakeEncoder._tokens(short_tool_result)),
    }
    assert first == second
    assert first_measurement == {
        REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY: expected_counts}
    assert second_measurement == first_measurement
    assert large_tool_result not in repr(counter._text_count_cache)


def test_call_local_tool_string_counts_stop_at_the_hard_ceiling(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(
        counter, 'REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX', 2)
    contents = [f'private tool result {index} ' * 4 for index in range(3)]
    measurement = {}

    result = counter.TiktokenCounter().count(
        [{'role': 'tool', 'content': content} for content in contents],
        model='gpt-5.6-sol',
        measurement_out=measurement,
    )

    assert result is not None
    reusable_counts = measurement[
        REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY]
    assert len(reusable_counts) == 2
    assert set(reusable_counts) == {id(contents[0]), id(contents[1])}
    assert id(contents[2]) not in reusable_counts


def test_full_request_batches_only_short_text_and_new_large_tail(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 16)
    stable = 'stable historical evidence ' * 20
    token_counter = counter.TiktokenCounter()

    token_counter.count(
        [{'role': 'user', 'content': stable}], model='kimi-k3')
    token_counter.count(
        [
            {'role': 'user', 'content': stable},
            {'role': 'assistant', 'content': 'short tail'},
        ],
        model='kimi-k3',
    )

    assert encoder.batch_calls == 2
    assert encoder.batches[0] == [stable]
    assert encoder.batches[1] == ['short tail']


def test_full_request_encodes_same_large_miss_once_per_batch(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 16)
    repeated = 'same private evidence ' * 20
    messages = [
        {'role': 'user', 'content': repeated},
        {'role': 'assistant', 'content': repeated},
    ]

    result = counter.TiktokenCounter().count(messages, model='kimi-k3')

    text_tokens = len(_FakeEncoder._tokens(repeated))
    assert result == text_tokens * 2 + 4 * len(messages) + 400
    assert encoder.batches == [[repeated]]
    assert counter.text_count_cache_snapshot()['misses'] == 1
    assert counter.text_count_cache_snapshot()['hits'] == 1


def test_full_request_batch_fill_respects_existing_capacity(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 4)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_CAPACITY', 2)
    private_texts = ['aaaa private', 'bbbb private', 'cccc private']

    counter.TiktokenCounter().count(
        [
            {'role': 'user', 'content': private_texts[0]},
            {'role': 'assistant', 'content': private_texts[1]},
            {'role': 'user', 'content': private_texts[2]},
        ],
        model='kimi-k3',
    )

    snapshot = counter.text_count_cache_snapshot()
    assert snapshot['entries'] == snapshot['capacity'] == 2
    assert snapshot['evictions'] == 1
    assert not any(text in repr(counter._text_count_cache)
                   for text in private_texts)


def test_failed_full_request_batch_publishes_no_cache_entry(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_REUSABLE_MIN_CHARS', 4)
    monkeypatch.setattr(
        encoder,
        'encode_batch',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('broken')),
    )

    private_tool_result = 'private failure payload'
    measurement = {}
    result = counter.TiktokenCounter().count(
        [{'role': 'tool', 'content': private_tool_result}],
        model='kimi-k3', measurement_out=measurement,
    )

    assert result is None
    assert counter.text_count_cache_snapshot()['entries'] == 0
    assert REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY not in measurement
