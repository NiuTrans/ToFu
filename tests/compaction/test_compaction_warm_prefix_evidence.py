"""A freshly written cache prefix is warm; only failed readbacks cool it.

Regression (2026-10-23 logs): optional pressure-L1 measured "warm" as the
previous round's cache *read*. Right after a cold full write (read 0, write =
whole prompt) it saw nothing warm, rewrote the prefix, and the next round paid
a second full write — 76 of 76 Claude economic passes followed such a write.
"""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._cache_evidence import (
    COLD_ROUTE_READBACK_FAILURES, current_cache_read, current_warm_prefix_tokens,
)
from lib.tasks_pkg.compaction._pressure import try_pressure_l1
from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

pytestmark = pytest.mark.unit

PROVIDER = 'unit-warm-prefix-route'
MODEL = 'unit-warm-prefix-model'


def _task():
    return {'id': 'warm-task', '_userId': 7, 'convId': 'warm-conv',
            'model': MODEL, 'provider_id': PROVIDER, 'config': {'model': MODEL}}


def _emit(task, round_num, *, read, write, provider=PROVIDER):
    from lib.tasks_pkg.llm_fallback import _usage
    _usage._emit_round_usage(task, round_num, MODEL, {
        'input_tokens': 50, 'output_tokens': 100,
        'cache_read_input_tokens': read,
        'cache_creation_input_tokens': write,
        '_dispatch': {'provider_id': provider},
    })


@pytest.fixture(autouse=True)
def _quiet_events(monkeypatch):
    from lib.tasks_pkg.llm_fallback import _usage
    monkeypatch.setattr(_usage, 'append_event', lambda *a, **k: None)


@pytest.fixture
def opus_like_prices(monkeypatch):
    import lib.cost as cost
    import lib.pricing as pricing

    monkeypatch.setitem(pricing.PROVIDER_PRICING, PROVIDER, {})
    data = {'usdToCny': 7, 'inputPrice': 99, 'outputPrice': 999}
    monkeypatch.setattr(pricing, 'get_pricing_data', lambda: dict(data))
    monkeypatch.setattr(cost, 'get_pricing_data', lambda: dict(data))
    pricing.set_provider_pricing(PROVIDER, MODEL, {
        'input': 4, 'output': 20, 'cacheReadMul': .05, 'cacheWriteMul': 1.25})


@pytest.fixture
def pressure_harness(monkeypatch):
    notices = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.notify_compaction',
                        lambda *a, **k: notices.append(k))
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._count_tokens_authoritative',
                        lambda messages, task, **kw: (_estimate_total_tokens(messages), 'test'))
    return notices


def _history():
    messages = [{'role': 'system', 'content': 'Do not lose the objective.'},
                {'role': 'user', 'content': 'Investigate the bug.'}]
    for index in range(6):
        call_id = f'call-{index}'
        messages.extend([
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': call_id, 'type': 'function',
                             'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'read_files', 'tool_call_id': call_id,
             'content': 'observed evidence 0123456789\n' * 1500},
        ])
    return messages


def test_cold_full_write_is_the_warm_prefix():
    task = _task()
    _emit(task, 1, read=0, write=150_000)
    assert current_cache_read(task) == 0
    assert current_warm_prefix_tokens(task) == 150_000


def test_route_is_cold_only_after_consecutive_failed_readbacks():
    assert COLD_ROUTE_READBACK_FAILURES == 2
    task = _task()
    _emit(task, 1, read=0, write=100_000)
    _emit(task, 2, read=0, write=105_000)  # one failure: visibility race/expiry
    assert current_warm_prefix_tokens(task) == 105_000
    _emit(task, 3, read=27_000, write=83_000)  # static-floor read: second failure
    assert current_warm_prefix_tokens(task) == 27_000
    _emit(task, 4, read=110_000, write=4_000)  # reads back again
    assert current_warm_prefix_tokens(task) == 114_000


def test_readback_streak_never_crosses_routes():
    task = _task()
    _emit(task, 1, read=0, write=100_000)
    _emit(task, 2, read=0, write=100_000)
    assert task['_compactionCacheObservation']['readbackFailures'] == 1
    _emit(task, 3, read=0, write=100_000, provider='another-route')
    assert task['_compactionCacheObservation']['readbackFailures'] == 0
    assert current_warm_prefix_tokens(task) == 0  # observation is for another route


def test_economics_prices_the_warm_prefix_but_reports_the_observed_read(opus_like_prices):
    from lib.tasks_pkg.compaction._cache_economics import _proactive_cache_economics
    task = _task()
    _emit(task, 1, read=0, write=100_000)
    economics = _proactive_cache_economics(
        task, tokens_before=120_000, candidate_tokens=90_000)
    assert economics['cache_read_tokens'] == 0
    assert economics['warm_prefix_tokens'] == 100_000
    assert economics['cache_replay_tokens_before'] == 100_000
    assert economics['cache_rewrite_tokens'] == 90_000
    assert economics['payback_rounds'] > 1


def test_uncached_route_has_recurring_input_savings_without_a_prior_cache_write(opus_like_prices):
    from lib.tasks_pkg.compaction._cache_economics import _proactive_cache_economics
    task = _task()
    for round_num in range(1, 5):
        _emit(task, round_num, read=0, write=0)
    economics = _proactive_cache_economics(
        task, tokens_before=200_000, candidate_tokens=100_000,
        summary_cost={'costUsd': .08, 'known': True})
    assert economics['warm_prefix_tokens'] == 0
    assert economics['uncached_dropped_tokens'] == 100_000
    assert economics['savings_per_round_usd'] == pytest.approx(.4)
    assert economics['payback_rounds'] == pytest.approx(.2)


def test_economic_pressure_l1_keeps_a_freshly_written_prefix(opus_like_prices, pressure_harness):
    task = _task()
    messages = _history()
    total = _estimate_total_tokens(messages)
    _emit(task, 1, read=0, write=total)
    original = deepcopy(messages)
    assert not try_pressure_l1(messages, task, target_tokens=30_000, total_tokens=total)
    assert messages == original
    assert not pressure_harness
    assert task['_lastPressureL1']['accepted'] is False


def test_economic_pressure_l1_proceeds_when_the_route_does_not_read_back(
        opus_like_prices, pressure_harness):
    task = _task()
    messages = _history()
    total = _estimate_total_tokens(messages)
    for round_num in range(1, COLD_ROUTE_READBACK_FAILURES + 2):
        _emit(task, round_num, read=0, write=total)
    assert current_warm_prefix_tokens(task) == 0
    assert try_pressure_l1(messages, task, target_tokens=30_000, total_tokens=total)
    assert _estimate_total_tokens(messages) <= 30_000
    assert len(pressure_harness) == 1


def test_cold_route_counts_dropped_uncached_tokens_as_recurring_savings(opus_like_prices):
    from lib.tasks_pkg.compaction._cache_economics import _proactive_cache_economics
    task = _task()
    for round_num in range(1, COLD_ROUTE_READBACK_FAILURES + 2):
        _emit(task, round_num, read=27_000, write=120_000)
    economics = _proactive_cache_economics(
        task, tokens_before=147_000, candidate_tokens=100_000)
    assert economics['route_cache_cold'] is True
    assert economics['warm_prefix_tokens'] == 27_000
    # The 47k dropped tokens outside the read prefix are billed again every round.
    assert economics['savings_per_round_usd'] == pytest.approx(47_000 * 4 / 1_000_000)
    assert economics['payback_rounds'] < 1


def test_warm_route_claims_no_savings_for_dropped_uncached_tokens(opus_like_prices):
    from lib.tasks_pkg.compaction._cache_economics import _proactive_cache_economics
    task = _task()
    _emit(task, 1, read=0, write=50_000)
    economics = _proactive_cache_economics(
        task, tokens_before=120_000, candidate_tokens=60_000)
    assert economics['route_cache_cold'] is False
    assert economics['savings_per_round_usd'] == 0


def test_economic_pressure_l1_compacts_on_a_cold_route_with_a_static_read(
        opus_like_prices, pressure_harness):
    task = _task()
    messages = _history()
    total = _estimate_total_tokens(messages)
    static_floor = 5_000
    for round_num in range(1, COLD_ROUTE_READBACK_FAILURES + 2):
        _emit(task, round_num, read=static_floor, write=total - static_floor)
    assert current_warm_prefix_tokens(task) == static_floor
    assert try_pressure_l1(messages, task, target_tokens=30_000, total_tokens=total)
    assert len(pressure_harness) == 1
