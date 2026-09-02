"""Codex subscription prompt-cache stability regressions.

Incident 2026-08-10: real GPT-5.6 tool loops reported ``0, 0, 5504`` on
their first three rounds and later ``6528 -> 5504`` despite byte-identical
prefix growth.  The subscription wire doesn't meter cache writes, so the
generic 30k Anthropic settle gate couldn't protect a 5-6k Codex prefix.
"""

from __future__ import annotations

import asyncio

import pytest

from lib.llm_dispatch import cache_settle as cache


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '1')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_CODEX_VISIBILITY_MS', '5000')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_CODEX_SEND_INTERVAL_MS', '4200')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_CODEX_MAX_MS', '6000')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_CODEX_THRESHOLD_TOKENS', '1024')
    cache._reset_settle_for_tests()
    yield
    cache._reset_settle_for_tests()


def test_unmetered_codex_write_is_inferred_from_usage():
    assert cache.codex_cache_write_pending({
        'prompt_tokens': 5066,
        'prompt_tokens_details': {'cached_tokens': 0},
    }) is True
    assert cache.codex_cache_write_pending({
        'prompt_tokens': 6998,
        'prompt_tokens_details': {'cached_tokens': 5504},
    }) is True
    # A sub-chunk uncached suffix doesn't imply that a new 1,024-token
    # breakpoint was written.
    assert cache.codex_cache_write_pending({
        'prompt_tokens': 5111,
        'prompt_tokens_details': {'cached_tokens': 4480},
    }) is False
    assert cache.codex_cache_write_pending({
        'prompt_tokens': 900,
        'prompt_tokens_details': {'cached_tokens': 0},
    }) is False


def test_second_codex_round_waits_for_unmetered_write(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        'lib.llm._transport.abortable_sleep',
        lambda seconds, _abort=None: sleeps.append(seconds),
    )

    # First request starts cold at t=1000 and ends at t=1002.  Although usage
    # reports no cache write, inference arms the visibility window.
    assert cache.settle_before_send(
        'conv-codex', 5000, now=1000.0, cache_profile='codex') == 0.0
    usage = {'prompt_tokens': 5066,
             'prompt_tokens_details': {'cached_tokens': 0}}
    cache.record_stream_end(
        'conv-codex', now=1002.0, cache_profile='codex',
        pending_write=cache.codex_cache_write_pending(usage),
    )
    waited = cache.settle_before_send(
        'conv-codex', 5100, now=1002.1, cache_profile='codex')

    assert waited == pytest.approx(4.9)
    assert sleeps == [pytest.approx(4.9)]


def test_warm_codex_round_obeys_per_key_send_interval(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        'lib.llm._transport.abortable_sleep',
        lambda seconds, _abort=None: sleeps.append(seconds),
    )
    cache.settle_before_send(
        'conv-rate', 5000, now=1000.0, cache_profile='codex')
    cache.record_stream_end(
        'conv-rate', now=1002.0, cache_profile='codex',
        pending_write=False)

    waited = cache.settle_before_send(
        'conv-rate', 5100, now=1002.5, cache_profile='codex')
    assert waited == pytest.approx(1.7)
    assert sleeps == [pytest.approx(1.7)]


def test_async_codex_wait_uses_same_timing(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds, _abort=None):
        sleeps.append(seconds)

    monkeypatch.setattr(
        'lib.llm._transport.async_abortable_sleep', fake_sleep)
    asyncio.run(cache.async_settle_before_send(
        'conv-async', 5000, now=1000.0, cache_profile='codex'))
    cache.record_stream_end(
        'conv-async', now=1001.5, cache_profile='codex',
        pending_write=True)
    waited = asyncio.run(cache.async_settle_before_send(
        'conv-async', 5100, now=1001.6, cache_profile='codex'))

    # Visibility (5.0 - 0.1 = 4.9s) is stricter than the per-key rate
    # interval (4.2 - 1.6 = 2.6s), so the max of both protections wins.
    assert waited == pytest.approx(4.9)
    assert sleeps == [pytest.approx(4.9)]


def _usage(cached: int, wire: list[dict], *, prompt: int = 7301) -> dict:
    return {
        'prompt_tokens': prompt,
        'prompt_tokens_details': {'cached_tokens': cached},
        '_wire_bytes': list(wire),
        '_wire_region': {'system': 'same', 'tools': 'same'},
        '_wire_routing': {'key': 'same', 'beta': '', 'endpoint': 'same'},
    }


def test_exact_1024_drop_is_named_implicit_breakpoint_fallback():
    first = cache.observe_codex_cache(
        'conv-fallback', _usage(6528, [{'h': 'a'}, {'h': 'b'}]))
    second_usage = _usage(
        5504, [{'h': 'a'}, {'h': 'b'}, {'h': 'new-tail'}])
    second = cache.observe_codex_cache('conv-fallback', second_usage)

    assert first['status'] == 'prefix_extended'
    assert second == second_usage['_codex_cache']
    assert second['status'] == 'implicit_breakpoint_fallback'
    assert second['drop_tokens'] == 1024
    assert second['wire_append_only'] is True


def test_wire_change_is_not_laundered_into_upstream_fallback():
    cache.observe_codex_cache(
        'conv-mutated', _usage(6528, [{'h': 'a'}, {'h': 'b'}]))
    result = cache.observe_codex_cache(
        'conv-mutated', _usage(5504, [{'h': 'CHANGED'}, {'h': 'b'}]))

    assert result['wire_append_only'] is False
    assert result['status'] != 'implicit_breakpoint_fallback'


def test_codex_fallback_is_stamped_on_api_round(monkeypatch):
    import lib.tasks_pkg.orchestrator._cache_round_accounting as accounting
    monkeypatch.setattr(accounting, 'detect_cache_break', lambda *a, **k: None)
    monkeypatch.setattr(accounting, 'get_prev_turn_cache_read', lambda _cid: 0)
    monkeypatch.setattr(accounting, '_compute_write_breakdown',
                        lambda *a, **k: {})
    monkeypatch.setattr(accounting, 'log_round_cache_stats',
                        lambda *a, **k: None)
    usage = {
        'prompt_tokens': 7301,
        'prompt_tokens_details': {'cached_tokens': 5504},
        '_codex_cache': {
            'status': 'implicit_breakpoint_fallback',
            'cached_tokens': 5504,
            'previous_cached_tokens': 6528,
            'max_cached_tokens': 6528,
            'drop_tokens': 1024,
            'wire_append_only': True,
        },
    }
    rounds = [{'round': 2}]

    accounting.stamp_round_cache_accounting(
        {'id': 'task-1', 'convId': 'conv-accounting', '_userId': 1},
        round_num=1,
        tid='task-1',
        model='gpt-5.6-luna',
        tools=[],
        usage=usage,
        assistant_msg={},
        api_rounds=rounds,
        messages=[],
    )

    stamped = rounds[0]['cacheBreak']['codex_cache']
    assert stamped['status'] == 'implicit_breakpoint_fallback'
    assert stamped['drop_tokens'] == 1024
    assert stamped['wire_append_only'] is True
