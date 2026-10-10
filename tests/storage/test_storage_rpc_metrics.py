"""Storage RPC latency is recorded at the single client chokepoint with
bounded labels, and the metric path can never break the RPC itself."""

from __future__ import annotations

import pytest

from lib import observability

pytestmark = pytest.mark.unit


def test_record_storage_rpc_bounds_labels_and_buckets():
    store = observability._STORE
    # The process-global store is shared with every other test on this xdist
    # worker: snapshot-and-restore so a neighbor's recorded RPCs cannot
    # reorder the assertions below (worksteal order is free).
    with store.lock:
        saved = dict(store.histograms)
        store.histograms.clear()
    try:
        observability.record_storage_rpc(
            'query', 'conversation.get', 0.012, 'success')
        observability.record_storage_rpc(
            'command', 'x' * 500, 0.2, 'explosion')
        keys = list(store.histograms)
        assert keys, 'storage RPC histogram was not recorded'
        name, labels = keys[0][0], dict(keys[0][1])
        assert name == 'tofu_storage_rpc_seconds'
        for _, raw_labels in keys:
            label_map = dict(raw_labels)
            assert label_map['outcome'] in ('success', 'error')
            assert len(label_map['operation']) <= 96
        assert labels['operation'] == 'conversation.get'
    finally:
        with store.lock:
            store.histograms.clear()
            store.histograms.update(saved)


def test_client_call_records_success_and_failure(monkeypatch):
    from lib.storage import client as storage_client

    recorded = []
    monkeypatch.setattr(
        storage_client, '_record_rpc',
        lambda kind, operation, started, outcome: recorded.append(
            (kind, operation, outcome)))

    instance = storage_client.StorageClient.__new__(
        storage_client.StorageClient)
    instance._timeout = 1.0
    instance._read_attempts = 1
    instance._transport_metrics_lock = __import__('threading').Lock()

    monkeypatch.setattr(
        storage_client.StorageClient, '_attempt',
        lambda self, *args, **kwargs: {'ok': True})
    result = instance._call('query', 'conversation.get', None, deadline=1.0)
    assert result == {'ok': True}

    from lib.storage.errors import StorageError

    def _boom(self, *args, **kwargs):
        raise StorageError('database_internal', 'boom')
    monkeypatch.setattr(storage_client.StorageClient, '_attempt', _boom)
    with pytest.raises(StorageError):
        instance._call('query', 'conversation.get', None, deadline=1.0)

    assert [entry[2] for entry in recorded] == ['success', 'error']
    assert all(entry[0] == 'query' for entry in recorded)


@pytest.mark.parametrize('value,limit,expected', [
    (' \t A\x00B\nC\rD\x1fE\x7f中\t ', 96, 'A B C D E\x7f中'),
    (None, 96, 'unknown'),
    (0, 96, 'unknown'),
    (' x\x00y \u2003', 3, 'x y'),
    ('\x00x\n', -1, ' '),
    ('\x00' * 10000, 96, ' ' * 96),
])
def test_metric_label_controls_whitespace_and_prefix_bound(value, limit, expected):
    assert observability._bounded(value, limit=limit) == expected


def test_histogram_preserves_first_buckets_and_inclusive_exact_counts():
    store = observability._Store()
    for value, buckets in [(0.5, (0.5, 1.0)), (1.0, (0.5, 1.0)),
                           (2.0, (0.25, 100.0)), (-1.0, (0.5, 1.0))]:
        store.observe('latency', value, buckets, kind='query')
    _, _, histograms = store.snapshot()
    assert len(histograms) == 1
    state = next(iter(histograms.values()))
    assert state == {'buckets': (0.5, 1.0), 'counts': [2, 3],
                     'sum': 3.5, 'count': 4}


def test_concurrent_first_histogram_observations_keep_every_sample():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    store = observability._Store()
    barrier = threading.Barrier(8)
    def observe(_index):
        barrier.wait(timeout=5)
        for _ in range(1000):
            store.observe('latency', 0.125, (0.1, 0.5), kind='query')
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(observe, range(8)))
    _, _, histograms = store.snapshot()
    assert next(iter(histograms.values())) == {
        'buckets': (0.1, 0.5), 'counts': [0, 8000], 'sum': 1000.0, 'count': 8000}
