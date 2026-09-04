"""Finite recent-sample windows for storage latency observability."""

from __future__ import annotations

import sqlite3

import pytest

from lib.storage import StorageEventBatcher
from lib.storage_metric_policy import storage_metric_sample_capacity
from lib.storage_sidecar.adapters.sqlite import _FairWriter


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('writer_queue_capacity', 'expected_samples'),
    [
        ('4', 1_024),
        ('8', 2_048),
        ('16', 4_096),
        ('128', 32_768),
        ('1024', 32_768),
    ],
)
def test_sample_window_scales_from_existing_writer_budget(
    writer_queue_capacity,
    expected_samples,
):
    assert storage_metric_sample_capacity({
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': writer_queue_capacity,
    }) == expected_samples


def test_event_batcher_retains_only_recent_latency_samples():
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: None,
        metric_sample_capacity=3,
    )
    try:
        with batcher._metrics_lock:
            batcher._persist_lags_ms.extend([1.0, 2.0, 3.0, 4.0, 5.0])

        metrics = batcher.metrics

        assert metrics['persist_lag_sample_capacity'] == 3
        assert metrics['persist_lag_samples'] == 3
        assert metrics['persist_lag_p95_ms'] == 5.0
        assert metrics['persist_lag_max_ms'] == 5.0
    finally:
        assert batcher.close()


def test_sqlite_writer_retains_only_recent_commit_samples():
    connection = sqlite3.connect(':memory:', check_same_thread=False)
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        metric_sample_capacity=3,
    )
    try:
        with writer._latency_lock:
            writer._commit_latencies.extend([0.001, 0.002, 0.003, 0.004, 0.005])

        metrics = writer.commit_latency_stats()

        assert metrics == {
            'sample_capacity': 3,
            'samples': 3,
            'p50_ms': 4.0,
            'p95_ms': 5.0,
            'max_ms': 5.0,
        }
    finally:
        writer.close()
