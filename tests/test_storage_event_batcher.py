"""Bounded client-side event coalescing through a real Sidecar."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib.storage import StorageError, StorageEventBatcher, StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.mark.ci_serial
def test_two_hundred_confirmed_streams_share_transactions(tmp_path):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=20)
    supervisor.start()
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: supervisor.client,
        coalesce_ms=20)
    barrier = threading.Barrier(200)

    def append(index):
        barrier.wait(timeout=10)
        return batcher.append(
            f'task-{index}', 0, {'kind': 'delta', 'index': index},
            timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=200) as pool:
            results = list(pool.map(append, range(200)))
        assert all(item['inserted'] for item in results)
        metrics = batcher.metrics
        assert metrics['inserted'] == 200
        assert metrics['batches'] < 20
        assert metrics['max_queue_depth'] <= 10_000

        for index in range(200, 250):
            accepted = batcher.append(
                f'task-{index}', 0, {'kind': 'delta', 'index': index},
                wait=False)
            assert accepted['accepted'] is True
        # This synchronous append is FIFO behind the async events, proving
        # they reached durable storage before it returns.
        assert batcher.append(
            'async-fence', 0, {'kind': 'fence'})['inserted'] is True
        assert batcher.metrics['persist_lag_max_ms'] <= 300

        assert batcher.append(
            'task-0', 0, {'kind': 'delta', 'index': 0})['inserted'] is False
        with pytest.raises(StorageError) as raised:
            batcher.append(
                'task-0', 0, {'kind': 'different', 'index': 0})
        assert raised.value.code == 'database_conflict'
    finally:
        assert batcher.close(timeout=10)
        supervisor.stop()
