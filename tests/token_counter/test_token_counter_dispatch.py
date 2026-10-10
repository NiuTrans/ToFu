"""Token-counter dispatch cost contracts.

The request-wide heuristic is a network-tier prefilter and final fallback. It
must stay lazy when a usage-cache or local-tokenizer tier succeeds, and a
network skip followed by heuristic fallback must reuse the same scan.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

import lib.token_counter.api as token_api
from lib.token_counter.heuristic import HeuristicCounter
import lib.token_counter.usage_cache as usage_cache

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _default_dispatch_mode(monkeypatch):
    """Keep operator environment overrides out of dispatcher unit tests."""
    usage_cache._reset_usage_cache_for_tests()
    monkeypatch.setattr(token_api, 'MODE', 'auto')
    yield
    usage_cache._reset_usage_cache_for_tests()


@dataclass
class _Counter:
    name: str
    result: int | None
    needs_network: bool = False
    confidence: str = 'good'
    calls: int = 0

    def supports(self, _model: str) -> bool:
        return True

    def count(self, _messages: list, *, model: str, **_kwargs) -> int | None:
        self.calls += 1
        return self.result


@pytest.mark.parametrize('counter_name', ['usage_cache', 'tiktoken'])
def test_successful_non_network_tier_skips_heuristic_scan(
    monkeypatch, counter_name,
):
    """A result already chosen by policy must not pay for a discarded scan."""
    counter = _Counter(counter_name, 77)
    monkeypatch.setattr(token_api, 'resolve', lambda _model: [counter])
    monkeypatch.setattr(
        token_api, 'cheap_estimate',
        lambda *a, **k: pytest.fail('eager request-wide heuristic scan'))

    result = token_api.count_tokens(
        [{'role': 'user', 'content': 'x' * 100_000}],
        model='gpt-4o', conv_id='lazy-counter', context_limit=128_000)

    assert result['tokens'] == 77
    assert result['method'] == counter_name
    assert counter.calls == 1


def test_network_skip_and_heuristic_fallback_share_one_scan(monkeypatch):
    """The prefilter becomes the fallback result instead of being recomputed."""
    network = _Counter('network', 99, needs_network=True)
    estimates = []

    def estimate(messages, *, system=None, tools=None):
        estimates.append((messages, system, tools))
        return 40

    monkeypatch.setattr(
        token_api, 'resolve',
        lambda _model: [network, HeuristicCounter()])
    monkeypatch.setattr(token_api, 'cheap_estimate', estimate)
    monkeypatch.setattr(token_api, 'API_THRESHOLD', 0.5)

    result = token_api.count_tokens(
        [{'role': 'user', 'content': 'small'}],
        model='claude-test', context_limit=100)

    assert result['tokens'] == 40
    assert result['method'] == 'heuristic'
    assert network.calls == 0
    assert len(estimates) == 1


def test_network_dispatch_computes_prefilter_once(monkeypatch):
    """An eligible network tier still receives the unchanged threshold gate."""
    network = _Counter('network', 88, needs_network=True, confidence='exact')
    estimates = []
    monkeypatch.setattr(token_api, 'resolve', lambda _model: [network])
    monkeypatch.setattr(
        token_api, 'cheap_estimate',
        lambda *a, **k: estimates.append((a, k)) or 60)
    monkeypatch.setattr(token_api, 'API_THRESHOLD', 0.5)

    result = token_api.count_tokens(
        [{'role': 'user', 'content': 'near limit'}],
        model='claude-test', context_limit=100)

    assert result['tokens'] == 88
    assert result['method'] == 'network'
    assert network.calls == 1
    assert len(estimates) == 1


def test_usage_cache_verifies_only_bounded_prefix_tail():
    """A hit must not allocate a copy of every historical message."""

    class TrackingList(list):
        def __init__(self, values):
            super().__init__(values)
            self.slices = []

        def __getitem__(self, key):
            if isinstance(key, slice):
                self.slices.append(key)
            return super().__getitem__(key)

    conv_id = 'bounded-prefix-signature'
    recorded_count = 1_000
    messages = TrackingList([
        {'role': 'user', 'content': f'message {index}'}
        for index in range(recorded_count)
    ])
    usage_cache.record_usage(
        conv_id,
        prompt_tokens=50_000,
        model='gpt-4o',
        message_count=recorded_count,
        messages=messages,
    )
    messages.append({'role': 'assistant', 'content': 'small delta'})
    messages.slices.clear()
    try:
        result = usage_cache.UsageCacheCounter().count(
            messages, model='gpt-4o', conv_id=conv_id)
    finally:
        usage_cache.invalidate(conv_id)

    assert result is not None and result > 50_000
    spans = [key.indices(len(messages)) for key in messages.slices]
    assert spans
    assert all(
        (stop - start <= 3) or start >= recorded_count
        for start, stop, _step in spans
    ), spans


def test_usage_cache_capacity_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(usage_cache, '_USAGE_CACHE_CAPACITY', 3)
    monkeypatch.setattr(usage_cache.time, 'time', lambda: 100.0)
    for index, conv_id in enumerate(('a', 'b', 'c'), start=1):
        usage_cache.record_usage(
            conv_id,
            prompt_tokens=index,
            model='gpt-5.6-sol',
            message_count=1,
            messages=[{'role': 'user', 'content': conv_id}],
        )
    assert usage_cache._lookup('a') is not None

    usage_cache.record_usage(
        'd',
        prompt_tokens=4,
        model='gpt-5.6-sol',
        message_count=1,
        messages=[{'role': 'user', 'content': 'd'}],
    )

    assert tuple(usage_cache._cache) == ('c', 'a', 'd')
    snapshot = usage_cache.usage_cache_snapshot()
    assert snapshot['entries'] == snapshot['capacity'] == 3
    assert snapshot['capacityEvictions'] == 1


def test_usage_cache_capacity_reclaims_expired_before_live_entry(
        monkeypatch):
    clock = {'now': 100.0}
    monkeypatch.setattr(usage_cache, '_USAGE_CACHE_CAPACITY', 2)
    monkeypatch.setattr(
        usage_cache.time, 'time', lambda: clock['now'])
    for conv_id in ('expired-a', 'expired-b'):
        usage_cache.record_usage(
            conv_id,
            prompt_tokens=1,
            model='gpt-5.6-sol',
            message_count=1,
            messages=[{'role': 'user', 'content': conv_id}],
        )
    clock['now'] += usage_cache.USAGE_CACHE_TTL_SEC + 1

    usage_cache.record_usage(
        'live',
        prompt_tokens=2,
        model='gpt-5.6-sol',
        message_count=1,
        messages=[{'role': 'user', 'content': 'live'}],
    )

    snapshot = usage_cache.usage_cache_snapshot()
    assert tuple(usage_cache._cache) == ('live',)
    assert snapshot['expiredEvictions'] == 2
    assert snapshot['capacityEvictions'] == 0


def test_usage_cache_expiration_cannot_delete_concurrent_fresh_write(
        monkeypatch):
    conv_id = 'atomic-expiration'
    usage_cache._cache[conv_id] = usage_cache._UsageEntry(
        prompt_tokens=1,
        model='gpt-5.6-sol',
        ts=0.0,
        message_count=1,
        tail_signature='user:old',
    )
    lookup_in_expiration = threading.Event()
    release_lookup = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()

    def blocked_time():
        lookup_in_expiration.set()
        assert release_lookup.wait(1.0)
        return usage_cache.USAGE_CACHE_TTL_SEC + 1

    monkeypatch.setattr(usage_cache.time, 'time', blocked_time)
    lookup_thread = threading.Thread(target=usage_cache._lookup, args=(conv_id,))

    def write_fresh_entry():
        writer_started.set()
        with usage_cache._lock:
            usage_cache._cache[conv_id] = usage_cache._UsageEntry(
                prompt_tokens=999,
                model='gpt-5.6-sol',
                ts=usage_cache.USAGE_CACHE_TTL_SEC + 1,
                message_count=1,
                tail_signature='user:fresh',
            )
        writer_done.set()

    writer_thread = threading.Thread(target=write_fresh_entry)
    try:
        lookup_thread.start()
        assert lookup_in_expiration.wait(1.0)
        writer_thread.start()
        assert writer_started.wait(1.0)
        assert not writer_done.wait(0.05)
    finally:
        release_lookup.set()
        lookup_thread.join(1.0)
        writer_thread.join(1.0)

    assert writer_done.is_set()
    assert usage_cache._cache[conv_id].prompt_tokens == 999


def test_usage_cache_carries_latest_opaque_reasoning_into_next_prompt():
    conv_id = 'opaque-reasoning-reserve'
    prefix = [{'role': 'user', 'content': 'question'}]
    usage_cache.record_usage(
        conv_id,
        prompt_tokens=100_000,
        model='gpt-5.6-sol',
        message_count=len(prefix),
        messages=prefix,
        opaque_replay_tokens=12_000,
    )
    try:
        without_opaque = prefix + [
            {'role': 'assistant', 'content': 'short answer'}]
        visible_only = usage_cache.UsageCacheCounter().count(
            without_opaque, model='gpt-5.6-sol', conv_id=conv_id)

        with_opaque = prefix + [{
            'role': 'assistant',
            'content': 'short answer',
            '_responses_items': [{
                'type': 'reasoning',
                'encrypted_content': 'opaque-state',
            }],
        }]
        replayed = usage_cache.UsageCacheCounter().count(
            with_opaque, model='gpt-5.6-sol', conv_id=conv_id)
    finally:
        usage_cache.invalidate(conv_id)

    assert visible_only is not None
    assert replayed == visible_only + 12_000


def test_usage_cache_recognizes_claude_redacted_thinking_replay():
    conv_id = 'redacted-thinking-reserve'
    prefix = [{'role': 'user', 'content': 'question'}]
    usage_cache.record_usage(
        conv_id,
        prompt_tokens=80_000,
        model='claude-opus-5',
        message_count=1,
        messages=prefix,
        opaque_replay_tokens=7_500,
    )
    try:
        plain_messages = prefix + [{
            'role': 'assistant', 'content': 'answer'}]
        plain_count = usage_cache.UsageCacheCounter().count(
            plain_messages, model='claude-opus-5', conv_id=conv_id)
        messages = prefix + [{
            'role': 'assistant',
            'content': 'answer',
            '_anthropic_content_blocks': [{
                'type': 'redacted_thinking', 'data': 'encrypted-state'},
                {'type': 'text', 'text': 'answer'},
            ],
        }]
        counted = usage_cache.UsageCacheCounter().count(
            messages, model='claude-opus-5', conv_id=conv_id)
    finally:
        usage_cache.invalidate(conv_id)

    assert plain_count is not None
    assert counted == plain_count + 7_500
