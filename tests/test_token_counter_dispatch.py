"""Token-counter dispatch cost contracts.

The request-wide heuristic is a network-tier prefilter and final fallback. It
must stay lazy when a usage-cache or local-tokenizer tier succeeds, and a
network skip followed by heuristic fallback must reuse the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import lib.token_counter.api as token_api
from lib.token_counter.heuristic import HeuristicCounter
import lib.token_counter.usage_cache as usage_cache

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _default_dispatch_mode(monkeypatch):
    """Keep operator environment overrides out of dispatcher unit tests."""
    monkeypatch.setattr(token_api, 'MODE', 'auto')


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
