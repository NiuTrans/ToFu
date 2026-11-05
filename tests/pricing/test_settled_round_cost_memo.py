"""Settled API-round costs are priced once per pricing state, exactly.

The pricing revision counts every runtime change to price inputs; a settled
round's memoized cost is reused only while the revision, the snapshot rates
the cost engine reads and the peak multiplier in force are all unchanged.
"""
import copy
import threading
from datetime import datetime, timezone

import pytest

import lib.cost as cost_module
import lib.pricing._peak as peak_module
from lib.conversation_sync.settled_rows import SettledRowMutation, is_settled, settle
from lib.cost import compute_api_rounds_cost
from lib.pricing import (
    MODEL_PRICING,
    PROVIDER_PRICING,
    clear_provider_pricing,
    get_pricing_data,
    lookup_pricing,
    pricing_revision,
    set_provider_pricing,
)
from lib.pricing import _provider as provider_module
from lib.pricing import _refresh as refresh_module
from lib.tasks_pkg.round_costs import stamp_round_costs, stamp_terminal_cost
from tests.turn_lifecycle import _projection_oracle as oracle

pytestmark = pytest.mark.unit

_PROVIDER = 'memo-test-provider'
_MODEL = 'memo-test-model'


def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def _round(index, *, model=_MODEL, provider=_PROVIDER):
    return {
        'round': index, 'model': model, 'provider_id': provider, 'tag': 'main',
        'usage': {
            'prompt_tokens': 10_000 + index * 100, 'completion_tokens': 300 + index,
            'prompt_tokens_details': {'cached_tokens': 8_000 + index * 90},
        },
    }


@pytest.fixture
def priced_provider(monkeypatch):
    monkeypatch.setitem(PROVIDER_PRICING, _PROVIDER, {
        _MODEL: {'input': 2.0, 'output': 8.0, 'cacheWriteMul': 1.25, 'cacheReadMul': 0.1},
    })
    yield


@pytest.fixture
def pricing_calls(monkeypatch):
    calls = []
    real = cost_module.compute_cost

    def counting(*args, **kwargs):
        calls.append(args[1] if len(args) > 1 else kwargs.get('model_id'))
        return real(*args, **kwargs)

    monkeypatch.setattr(cost_module, 'compute_cost', counting)
    return calls


def _oracle_total(rows):
    return oracle.compute_api_rounds_cost(copy.deepcopy(rows))


def test_settled_rounds_are_priced_once_and_match_the_oracle(priced_provider, pricing_calls):
    rows = [settle(_round(index)) for index in range(6)]
    first = compute_api_rounds_cost(rows)
    assert len(pricing_calls) == 6
    second = compute_api_rounds_cost(rows)
    assert len(pricing_calls) == 6
    assert first == second == _oracle_total(rows)
    # Plain rows are never memoized.
    plain = [_round(index) for index in range(3)]
    compute_api_rounds_cost(plain)
    compute_api_rounds_cost(plain)
    assert len(pricing_calls) == 12


def test_ledger_fold_reads_one_pricing_snapshot(priced_provider, monkeypatch):
    reads = []
    real = cost_module.get_pricing_data
    monkeypatch.setattr(cost_module, 'get_pricing_data',
                        lambda: reads.append(1) or real())
    compute_api_rounds_cost([_round(index) for index in range(5)])
    assert len(reads) == 1


@pytest.mark.parametrize('change', [
    'set_provider_pricing', 'provider_table_patch', 'model_table_write',
    'clear_provider_pricing',
])
def test_every_pricing_writer_invalidates_settled_costs(priced_provider, monkeypatch,
                                                         pricing_calls, change):
    rows = [settle(_round(index)) for index in range(4)]
    before = compute_api_rounds_cost(rows)
    revision = pricing_revision()
    if change == 'set_provider_pricing':
        set_provider_pricing(_PROVIDER, _MODEL, {'input': 3.0, 'output': 9.0})
    elif change == 'provider_table_patch':
        monkeypatch.setitem(PROVIDER_PRICING, _PROVIDER, {
            _MODEL: {'input': 5.0, 'output': 7.0}})
    elif change == 'model_table_write':
        clear_provider_pricing(_PROVIDER)
        revision = pricing_revision()
        monkeypatch.setitem(MODEL_PRICING, _MODEL, {'input': 1.0, 'output': 4.0})
    else:
        clear_provider_pricing(_PROVIDER)
    assert pricing_revision() > revision
    calls_before = len(pricing_calls)
    after = compute_api_rounds_cost(rows)
    assert len(pricing_calls) == calls_before + len(rows)
    assert after == _oracle_total(rows)
    assert after != before


def test_online_refresh_publishes_a_new_snapshot_and_revision(monkeypatch, priced_provider,
                                                               pricing_calls):
    rows = [settle(_round(index)) for index in range(3)]
    before = compute_api_rounds_cost(rows)
    monkeypatch.setattr(refresh_module, '_fetch_exchange_rates',
                        lambda: {'CNY': 9.5, 'USD': 1.0, 'JPY': 150.0, 'KRW': 1300.0})
    monkeypatch.setattr(refresh_module, '_fetch_model_pricing_online', lambda _model: None)

    class _Storage:
        def command(self, *args, **kwargs):
            return None

    monkeypatch.setattr(refresh_module, '_storage', lambda **_kwargs: _Storage())
    published = refresh_module._published_pricing_data
    monkeypatch.setattr(refresh_module, '_published_pricing_data', published)
    monkeypatch.setitem(refresh_module._pricing_data, 'usdRates',
                        dict(refresh_module._pricing_data['usdRates']))
    monkeypatch.setitem(refresh_module._pricing_data, 'usdToCny',
                        refresh_module._pricing_data['usdToCny'])
    revision = pricing_revision()
    refresh_module._do_update_pricing()
    assert pricing_revision() > revision
    assert get_pricing_data()['usdToCny'] == 9.5
    after = compute_api_rounds_cost(rows)
    assert len(pricing_calls) == 6
    assert after['costCny'] != before['costCny']
    assert after == _oracle_total(rows)


def test_patched_snapshot_rates_invalidate_without_a_revision(priced_provider, monkeypatch,
                                                              pricing_calls):
    rows = [settle(_round(index)) for index in range(3)]
    compute_api_rounds_cost(rows)
    data = dict(get_pricing_data(), usdToCny=11.0)
    monkeypatch.setattr(cost_module, 'get_pricing_data', lambda: dict(data))
    patched = compute_api_rounds_cost(rows)
    assert len(pricing_calls) == 6
    assert patched == _oracle_total_with(rows, monkeypatch, data)


def _oracle_total_with(rows, monkeypatch, data):
    monkeypatch.setattr(oracle, 'get_pricing_data', lambda: dict(data))
    return _oracle_total(rows)


def test_peak_window_crossing_reprices_settled_rounds(monkeypatch, pricing_calls):
    rows = [settle(_round(index, model='deepseek-flash', provider=None)) for index in range(3)]
    monkeypatch.setattr(peak_module, '_utc_now', lambda: _utc(2026, 9, 11, 7, 0))
    peak = compute_api_rounds_cost(rows)
    assert peak == _oracle_total(rows)
    assert len(pricing_calls) == 3
    compute_api_rounds_cost(rows)
    assert len(pricing_calls) == 3
    monkeypatch.setattr(peak_module, '_utc_now', lambda: _utc(2026, 9, 11, 12, 0))
    off_peak = compute_api_rounds_cost(rows)
    assert len(pricing_calls) == 6
    assert off_peak == _oracle_total(rows)
    assert off_peak['costUsd'] < peak['costUsd']


def test_pricing_readers_take_no_writer_lock(priced_provider):
    results = []

    def read():
        results.append((get_pricing_data()['usdToCny'],
                        lookup_pricing(_MODEL, _PROVIDER)['input']))

    with refresh_module._pricing_lock, provider_module._provider_pricing_lock:
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive(), 'a pricing reader waited for a writer lock'
    assert results and results[0][1] == 2.0


def test_provider_writers_replace_row_maps_copy_on_write(priced_provider):
    published = PROVIDER_PRICING[_PROVIDER]
    set_provider_pricing(_PROVIDER, 'other-model', {'input': 1.0, 'output': 1.0})
    assert PROVIDER_PRICING[_PROVIDER] is not published
    assert set(published) == {_MODEL}
    set_provider_pricing(_PROVIDER, 'other-model', None)
    assert set(PROVIDER_PRICING[_PROVIDER]) == {_MODEL}


def test_round_cost_stamp_settles_priced_rounds_and_later_stamps_copy_on_write(
        priced_provider):
    task = {'id': 'stamp-task'}
    rounds = [_round(0), {'round': 1, 'model': _MODEL, 'usage': {}}, settle(_round(2))]
    stamp_round_costs(task, rounds, model=_MODEL)
    assert is_settled(rounds[0]) and rounds[0]['cost']
    assert not is_settled(rounds[1])
    assert is_settled(rounds[2]) and rounds[2]['cost']
    with pytest.raises(SettledRowMutation):
        rounds[0]['cost'] = {}
    # A settled row that still lacks a cost is priced by replacement.
    unpriced = settle(_round(3))
    done_event = {'apiRounds': [unpriced], 'model': _MODEL}
    stamp_terminal_cost(task, done_event)
    assert done_event['apiRounds'][0] is not unpriced
    assert is_settled(done_event['apiRounds'][0]) and done_event['apiRounds'][0]['cost']
    assert 'cost' not in unpriced and done_event['cost']
