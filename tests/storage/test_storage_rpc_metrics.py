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


def test_metrics_export_the_execution_hosts_histogram_beside_the_api_family(monkeypatch):
    """The resident execution host has no listener; /metrics reads it per scrape."""
    import asyncio
    from quart import Quart
    import routes.metrics as metrics
    from lib.tasks_pkg import command_control_runtime
    from lib.tasks_pkg.command_control_transport import CommandControlUnavailable

    row = {'labels': {'kind': 'query', 'operation': 'turn.get', 'outcome': 'success'},
           'buckets': [0.001, 0.01], 'counts': [1, 3], 'sum': 0.02, 'count': 4}
    answers = [{'histograms': [row]}, CommandControlUnavailable('not confirmed'), None]
    owners = []

    def remote(app, *, user_id):
        owners.append(user_id)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(command_control_runtime, 'execution_storage_rpc_metrics', remote)
    monkeypatch.setattr('routes.api_v1.auth.request_user_id', lambda: 7)
    app = Quart(__name__)

    async def scrape():
        async with app.test_request_context('/metrics'):
            out = []
            metrics._collect_execution_storage_metrics(out)
            return '\n'.join(out)
    text = asyncio.run(scrape())
    series = 'kind="query",operation="turn.get",outcome="success"'
    assert '# TYPE tofu_execution_storage_rpc_seconds histogram' in text
    assert f'tofu_execution_storage_rpc_seconds_bucket{{{series},le="0.001"}} 1' in text
    assert f'tofu_execution_storage_rpc_seconds_bucket{{{series},le="+Inf"}} 4' in text
    assert f'tofu_execution_storage_rpc_seconds_sum{{{series}}} 0.02' in text
    assert f'tofu_execution_storage_rpc_seconds_count{{{series}}} 4' in text
    assert 'tofu_execution_metrics_available 1' in text
    # An unconfirmed read is unavailable, never an empty histogram.
    unavailable = asyncio.run(scrape())
    assert 'tofu_execution_metrics_available 0' in unavailable and '_bucket' not in unavailable
    # Without a separate execution host nothing is added.
    assert asyncio.run(scrape()) == ''
    assert owners == [7, 7, 7]


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
