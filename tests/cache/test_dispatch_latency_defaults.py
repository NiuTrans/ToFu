"""Latency defaults preserve explicit cache-cost policy and cache diagnostics."""
from __future__ import annotations

import asyncio
import pytest
from lib.llm_dispatch import cache_settle as cache
from lib.llm_dispatch import conv_affinity as affinity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('profile', ['', 'codex'])
@pytest.mark.parametrize('async_dispatch', [False, True])
def test_default_dispatch_skips_hold_and_prompt_scan(monkeypatch, profile, async_dispatch):
    cache._reset_settle_for_tests()
    try:
        # A prior explicit cost-saving round must not hold a later default call.
        monkeypatch.setenv('TOFU_CACHE_SETTLE', '1')
        cache.record_stream_end('conversation', now=1000, cache_profile=profile,
                                pending_write=True)
        monkeypatch.delenv('TOFU_CACHE_SETTLE', raising=False)
        sleeps = []
        monkeypatch.setattr('lib.llm._transport.abortable_sleep',
                            lambda *args: sleeps.append(args))

        async def sleep(*args):
            sleeps.append(args)

        monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', sleep)

        class UnreadableHistory(list):
            def __iter__(self):
                raise AssertionError('disabled settle gate scanned the prompt')

        assert cache.estimate_prefix_tokens({'messages': UnreadableHistory()}) == 0
        args = ('conversation', 200_000)
        kwargs = {'now': 1000.1, 'cache_profile': profile}
        waited = (asyncio.run(cache.async_settle_before_send(*args, **kwargs))
                  if async_dispatch else cache.settle_before_send(*args, **kwargs))
        assert waited == 0
        assert sleeps == []
    finally:
        cache._reset_settle_for_tests()


def test_warm_key_hold_requires_explicit_cost_preference(monkeypatch):
    monkeypatch.delenv('TOFU_CONV_STICKY_HOLD', raising=False)
    assert affinity.sticky_hold_enabled() is False
    monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '1')
    assert affinity.sticky_hold_enabled() is True
    # Warm-key routing itself remains enabled independently of hold policy.
    monkeypatch.delenv('TOFU_CONV_STICKY_ROUTING', raising=False)
    assert affinity.sticky_routing_enabled() is True
