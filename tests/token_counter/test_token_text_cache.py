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
        self.encoded_texts = []

    @staticmethod
    def _tokens(text):
        return list(range(max(1, len(text) // 4)))

    def encode(self, text, *, disallowed_special=()):
        self.calls += 1
        self.encoded_texts.append(text)
        return self._tokens(text)

    def encode_batch(self, texts, *, disallowed_special=()):
        self.batch_calls += 1
        self.batches.append(list(texts))
        self.encoded_texts.extend(texts)
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
    assert len(encoder.encoded_texts) == 2
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

    assert encoder.encoded_texts == [stable, 'short tail']


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
    assert encoder.encoded_texts == [repeated]
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
    monkeypatch.setattr(encoder, 'encode', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('broken')))

    private_tool_result = 'private failure payload'
    measurement = {}
    result = counter.TiktokenCounter().count(
        [{'role': 'tool', 'content': private_tool_result}],
        model='kimi-k3', measurement_out=measurement,
    )

    assert result is None
    assert counter.text_count_cache_snapshot()['entries'] == 0
    assert REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY not in measurement


_BOUNDARY_TEXTS = [
    '', ' ', 'trailing spaces   ', '\n\nleading newlines', 'tab\t\n',
    "it's 3 o'clock", '12345678', 'Hello 世界', '🙂' * 3, 'café\u0301',
    'spelled <|endoftext|> separator', '<|fim_prefix|>other special',
    'lone \ud800 surrogate', '\udc00 leading low', 'pair \ud83d\ude42 halves',
    'x' * 600, 'base64 ' + 'QUJDRA' * 120,
]


@pytest.mark.parametrize('encoding', ['cl100k_base', 'o200k_base'])
def test_joined_batch_counts_equal_one_encode_per_text(encoding):
    import tiktoken
    encoder = tiktoken.get_encoding(encoding)

    counts = counter._encode_batch_counts(encoder, list(_BOUNDARY_TEXTS))

    assert counts == [len(encoder.encode(text, disallowed_special=()))
                      for text in _BOUNDARY_TEXTS]


class _CountingEncoding:
    """A real tiktoken encoding that counts ``encode`` calls (each releases the GIL)."""

    def __init__(self, encoding):
        self._encoding = encoding
        self.encode_calls = 0
        self.eot_token = encoding.eot_token
        self.special_tokens_set = encoding.special_tokens_set

    def encode(self, text, **kwargs):
        self.encode_calls += 1
        return self._encoding.encode(text, **kwargs)

    def encode_batch(self, texts, **kwargs):
        raise AssertionError('thread-pool batch encoding is not used')


def test_joined_batch_releases_the_gil_once_for_ordinary_texts():
    """tiktoken gives the GIL away per encode call; a batch makes one call."""
    import tiktoken

    encoder = _CountingEncoding(tiktoken.get_encoding('cl100k_base'))
    texts = [f'history text {index} ' * (index % 7 + 1) for index in range(300)]

    counts = counter._encode_batch_counts(encoder, texts)

    assert encoder.encode_calls == 1
    assert counts == [len(encoder._encoding.encode(text, disallowed_special=()))
                      for text in texts]


@pytest.mark.parametrize('encoding,model',[('cl100k_base','gpt-4'),('o200k_base','gpt-4o')])
@pytest.mark.parametrize('texts',[
    ['Hello 世界', '<|endoftext|>🙂 café'],
    ['a'*512, '🙂'*512],
    ['a'*513, '🙂'*512],
])
def test_request_cache_preserves_exact_special_and_unicode_counts(encoding,model,texts):
    import tiktoken
    encoder=tiktoken.get_encoding(encoding)
    expected=sum(len(row) for row in encoder.encode_batch(texts,disallowed_special=()))
    selected={id(text):None for text in texts}
    result=counter._count_request_texts_with_cache(texts,model=model,encoder=encoder,selected_text_token_counts=selected)
    assert result==expected
    assert [selected[id(text)] for text in texts]==[len(encoder.encode(text,disallowed_special=())) for text in texts]


def test_owner_memo_encodes_only_new_texts_on_each_round(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    memo = counter.RequestTextCounts()
    messages = [{'role': 'user', 'content': f'short history {index}'}
                for index in range(40)]
    token_counter = counter.TiktokenCounter()

    first = token_counter.count(messages, model='kimi-k3', text_count_memo=memo)
    assert len(encoder.encoded_texts) == 40
    messages.append({'role': 'assistant', 'content': 'new short tail'})
    encoder.encoded_texts.clear()
    second = token_counter.count(messages, model='kimi-k3', text_count_memo=memo)

    assert encoder.encoded_texts == ['new short tail']
    assert second == token_counter.count(messages, model='kimi-k3')
    assert second > first


def test_owner_memo_holds_only_current_request_and_no_text(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    memo = counter.RequestTextCounts()
    dropped = 'private compacted-away evidence ' * 10
    kept = 'private kept evidence'
    token_counter = counter.TiktokenCounter()

    token_counter.count([{'role': 'user', 'content': dropped},
                         {'role': 'user', 'content': kept}],
                        model='kimi-k3', text_count_memo=memo)
    token_counter.count([{'role': 'user', 'content': kept}],
                        model='kimi-k3', text_count_memo=memo)

    assert list(memo.counts.values()) == [len(_FakeEncoder._tokens(kept))]
    assert not any(isinstance(key, str) for key in memo.counts)
    assert 'private' not in repr(memo.counts)


class _CaseFoldedText(str):
    """Distinct texts that share ``hash`` and claim equality, as a lookup sees them."""

    def __eq__(self, other):
        return isinstance(other, str) and self.lower() == other.lower()

    def __hash__(self):
        return hash(self.lower())


def test_owner_memo_never_reuses_a_count_across_colliding_hashes():
    tiktoken = pytest.importorskip('tiktoken')
    encoder = tiktoken.get_encoding('cl100k_base')
    lower, upper = _CaseFoldedText('remembered words'), _CaseFoldedText('REMEMBERED WORDS')
    assert hash(lower) == hash(upper) and len(lower) == len(upper)
    exact = [len(encoder.encode(str.__str__(text), disallowed_special=()))
             for text in (lower, upper)]
    assert exact[0] != exact[1]
    memo = counter.RequestTextCounts()

    counts = [counter._count_request_texts_with_cache(
        [text], model='gpt-4', encoder=encoder, request_memo=memo)
        for text in (lower, upper)]

    assert counts == exact


def test_owner_memo_tokenizes_a_round_of_new_texts_in_one_call():
    """Memo misses share the one-call batch; per-text encodes each release the GIL."""
    tiktoken = pytest.importorskip('tiktoken')
    encoder = _CountingEncoding(tiktoken.get_encoding('cl100k_base'))
    memo = counter.RequestTextCounts()
    history = [f'history text {index} ' * (index % 7 + 1) for index in range(50)]
    counter._count_request_texts_with_cache(
        history, model='gpt-4', encoder=encoder, request_memo=memo)
    encoder.encode_calls = 0
    grown = history + ['new tool result ' * 40, 'new reasoning', 'x' * 700]

    total = counter._count_request_texts_with_cache(
        grown, model='gpt-4', encoder=encoder, request_memo=memo)

    assert encoder.encode_calls == 1
    assert total == sum(len(encoder._encoding.encode(text, disallowed_special=()))
                        for text in grown)


def test_owner_memo_is_encoding_scoped(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    memo = counter.RequestTextCounts()
    messages = [{'role': 'user', 'content': 'same text'}]

    counter.TiktokenCounter().count(messages, model='kimi-k3', text_count_memo=memo)
    counter.TiktokenCounter().count(messages, model='gpt-4o', text_count_memo=memo)

    assert encoder.encoded_texts == ['same text', 'same text']
    assert memo.encoding == 'o200k_base' and len(memo.counts) == 1


def test_failed_memo_count_keeps_previous_memo(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    memo = counter.RequestTextCounts()
    counter.TiktokenCounter().count(
        [{'role': 'user', 'content': 'kept'}], model='kimi-k3', text_count_memo=memo)
    before = dict(memo.counts)
    broken = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('broken'))  # noqa: E731
    monkeypatch.setattr(encoder, 'encode', broken)
    monkeypatch.setattr(encoder, 'encode_batch', broken)

    result = counter.TiktokenCounter().count(
        [{'role': 'user', 'content': 'kept'}, {'role': 'user', 'content': 'new'}],
        model='kimi-k3', text_count_memo=memo)

    assert result is None
    assert memo.counts == before


@pytest.mark.parametrize('encoding,model', [('cl100k_base', 'gpt-4'), ('o200k_base', 'gpt-4o')])
def test_owner_memo_matches_uncached_exact_counts(encoding, model):
    import tiktoken
    encoder = tiktoken.get_encoding(encoding)
    texts = ['Hello 世界', '<|endoftext|>🙂 café', 'a' * 513, '🙂' * 512, 'Hello 世界']
    texts += [f'row {index} ' * (index % 7 + 1) for index in range(40)]
    expected = sum(len(encoder.encode(text, disallowed_special=())) for text in texts)
    memo = counter.RequestTextCounts()
    selected = {id(text): None for text in texts}

    first = counter._count_request_texts_with_cache(
        texts, model=model, encoder=encoder, request_memo=memo,
        selected_text_token_counts=selected)
    second = counter._count_request_texts_with_cache(
        texts, model=model, encoder=encoder, request_memo=memo)

    assert first == second == expected
    assert [selected[id(text)] for text in texts] == [
        len(encoder.encode(text, disallowed_special=())) for text in texts]


def test_compaction_count_reuses_task_owned_memo(monkeypatch):
    from lib.tasks_pkg.compaction import _tokens

    seen = []

    def fake_count_tokens(messages, **kwargs):
        seen.append(kwargs.get('text_count_memo'))
        return {'tokens': 10, 'method': 'tiktoken'}

    monkeypatch.setattr('lib.token_counter.count_tokens', fake_count_tokens)
    task = {'id': 't', 'config': {'model': 'kimi-k3'}}
    messages = [{'role': 'user', 'content': 'hi'}]
    _tokens._count_tokens_authoritative(messages, task)
    _tokens._count_tokens_authoritative(messages, task, tool_schema=[])

    assert all(isinstance(view, counter.RequestTextCountView) for view in seen)
    assert seen[0].memo is seen[1].memo is task['_requestTextCountMemo']
    assert isinstance(task['_requestTextCountMemo'], counter.RequestTextCounts)
    assert [view.label for view in seen] == ['request', 'admission']


@pytest.mark.parametrize('model', ['gpt-4', 'gpt-4o'])
def test_task_memo_gate_counts_equal_memo_less_counts_across_rounds(model):
    """Growing, rewritten and trimmed rounds count exactly as without the memo."""
    import json

    tiktoken = pytest.importorskip('tiktoken')
    from lib.tasks_pkg.compaction import _tokens
    from lib.token_counter.base import iter_message_texts

    tools = [{'type': 'function', 'function': {
        'name': 'run', 'description': 'Run one shell command. ' * 40,
        'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}}}}]
    remembered_task = {'id': 'memo-rounds', 'config': {'model': model}, '_tool_schema': tools}
    stateless_task = {**remembered_task, '_requestTextCountMemo': None}
    encoder = tiktoken.get_encoding(counter.encoding_for_model(model))
    messages = [{'role': 'system', 'content': 'system rules ' * 300},
                {'role': 'user', 'content': '整理上下文 and plan 🙂 café'}]
    for round_index in range(6):
        call_id = f'call_{round_index}'
        messages.append({
            'role': 'assistant', 'content': '',
            'reasoning_content': f'thinking {round_index} ' * 40,
            'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': 'run', 'arguments': json.dumps({'cmd': f'ls {round_index}'})}}]})
        messages.append({'role': 'tool', 'tool_call_id': call_id,
                         'content': f'out {round_index} <|endoftext|> ' * (60 * round_index + 1)})
        if round_index == 3:
            messages[3] = {**messages[3], 'content': 'rewritten in place'}
        if round_index == 5:
            del messages[2:4]
        remembered_measurement, stateless_measurement = {}, {}

        remembered = _tokens._count_tokens_authoritative(
            messages, remembered_task, measurement_out=remembered_measurement)
        stateless = _tokens._count_tokens_authoritative(
            messages, stateless_task, measurement_out=stateless_measurement)

        assert remembered == stateless
        assert remembered_measurement == stateless_measurement
        assert remembered[1].startswith('tiktoken')
        assert remembered_measurement['raw_estimate_tokens'] == sum(
            len(encoder.encode(text, disallowed_special=()))
            for text in iter_message_texts(messages, None, tools)
        ) + 4 * len(messages) + 400
    memo = remembered_task['_requestTextCountMemo']
    assert isinstance(memo, counter.RequestTextCounts)
    assert len(memo.counts) == len(set(iter_message_texts(messages, None, tools)))


def test_shared_owner_memo_stays_exact_under_concurrent_mixed_encoding_rounds():
    import threading

    pytest.importorskip('tiktoken')
    memo = counter.RequestTextCounts()
    base = [{'role': 'user', 'content': f'history row {index} ' * (index % 9 + 1)}
            for index in range(60)]
    token_counter = counter.TiktokenCounter()
    expected = {}
    for model in ('gpt-4', 'gpt-4o'):
        for extra in range(8):
            messages = base + [{'role': 'tool', 'content': f'tail {extra} ' * 30}]
            expected[(model, extra)] = token_counter.count(messages, model=model)
    mismatches = []
    barrier = threading.Barrier(8)

    def worker(model):
        barrier.wait()
        for _round in range(25):
            for extra in range(8):
                messages = base + [{'role': 'tool', 'content': f'tail {extra} ' * 30}]
                got = token_counter.count(messages, model=model, text_count_memo=memo)
                if got != expected[(model, extra)]:
                    mismatches.append((model, extra, got))

    threads = [threading.Thread(target=worker, args=(('gpt-4', 'gpt-4o')[index % 2],))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert mismatches == []
    assert memo.encoding in {'cl100k_base', 'o200k_base'}
