"""Exact metric snapshots, exposition and test-store lifecycle contracts."""

import math

import pytest

from lib import observability


pytestmark = pytest.mark.unit


def test_reset_readmits_labels_previously_folded_by_cardinality(monkeypatch):
    store = observability._Store()
    monkeypatch.setattr(observability, '_STORE', store)
    for index in range(128):
        store.inc('operations', kind=f'kind-{index}')
    store.inc('operations', kind='new-kind')
    assert store.snapshot()[0][('operations', (('kind', 'other'),))] == 1

    observability.reset_for_tests()
    store.inc('operations', kind='new-kind')
    assert store.snapshot() == (
        {('operations', (('kind', 'new-kind'),)): 1.0}, {}, {})


@pytest.mark.parametrize('buckets', [
    (), (0.0, 0.5, 1.0), (1.0, 0.5, 1.0, -1.0),
    (float('nan'), float('inf'), -float('inf'), 0.0),
])
def test_histogram_snapshot_matches_inclusive_observations(buckets):
    store = observability._Store()
    values = (-1.0, -0.0, 0.125, 0.5, 1.0, 2.0, float('nan'), float('inf'))
    for value in values:
        store.observe('latency', value, buckets)
    state = store.snapshot()[2][('latency', ())]
    numbers = [max(0.0, value) for value in values]
    assert state['counts'] == [sum(value <= upper for value in numbers)
                               for upper in buckets]
    assert state['count'] == len(values)
    assert state['sum'] == sum(numbers)
    assert all(left == right or math.isnan(left) and math.isnan(right)
               for left, right in zip(state['buckets'], buckets))

    # Scraping or changing a detached snapshot cannot consume live samples.
    state['counts'][:] = [99] * len(buckets)
    store.observe('latency', 0.5, buckets)
    assert store.snapshot()[2][('latency', ())]['counts'] == [
        sum(value <= upper for value in [*numbers, 0.5]) for upper in buckets]


def test_exposition_retains_order_counter_precedence_and_escaping(monkeypatch):
    store = observability._Store()
    monkeypatch.setattr(observability, '_STORE', store)
    monkeypatch.setattr(observability, '_publish_executor_observations', lambda: None)
    store.set('z_gauge', 3)
    store.set('a_counter', 99, hidden='gauge')
    store.inc('a_counter', 2, z='quote"', a='slash\\')
    store.observe('b_histogram', 0.5, (0.5, 1.0), kind='query')
    store.observe('b_histogram', 2.0, (0.5, 1.0), kind='query')
    store.observe('c_histogram', 0.5, (1.0,))

    assert observability.prometheus_lines() == [
        '# HELP a_counter a_counter', '# TYPE a_counter counter',
        'a_counter{a="slash\\\\",z="quote\\\""} 2.0',
        '# HELP z_gauge z_gauge', '# TYPE z_gauge gauge', 'z_gauge 3.0',
        '# HELP b_histogram b_histogram', '# TYPE b_histogram histogram',
        'b_histogram_bucket{kind="query",le="0.5"} 1',
        'b_histogram_bucket{kind="query",le="1.0"} 1',
        'b_histogram_bucket{kind="query",le="+Inf"} 2',
        'b_histogram_sum{kind="query"} 2.5',
        'b_histogram_count{kind="query"} 2',
        '# HELP c_histogram c_histogram', '# TYPE c_histogram histogram',
        'c_histogram_bucket{le="1.0"} 1',
        'c_histogram_bucket{le="+Inf"} 1',
        'c_histogram_sum 0.5', 'c_histogram_count 1',
    ]
