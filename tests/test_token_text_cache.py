"""Bounded large-text token-count reuse contracts.

Stable tool-schema and compaction projections are counted on consecutive model
rounds.  Reuse must avoid another tokenizer pass without retaining prompt text,
crossing tokenizer encodings, or growing beyond the deployment budget.
"""

from __future__ import annotations

import pytest

import lib.token_counter.tiktoken_counter as counter


pytestmark = pytest.mark.unit


class _FakeEncoder:
    def __init__(self):
        self.calls = 0

    def encode(self, text, *, disallowed_special=()):
        self.calls += 1
        return list(range(max(1, len(text) // 4)))


@pytest.fixture(autouse=True)
def _isolated_cache():
    counter._text_count_cache.clear()
    yield
    counter._text_count_cache.clear()


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


def test_short_text_bypasses_digest_cache(monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(counter, '_get_encoder', lambda _name: encoder)
    monkeypatch.setattr(counter, '_TEXT_COUNT_CACHE_MIN_CHARS', 16)

    counter.count_text('short', model='kimi-k3')
    counter.count_text('short', model='kimi-k3')

    assert encoder.calls == 2
    assert not counter._text_count_cache
