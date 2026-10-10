"""Launch-probed retention policy for recent storage latency samples.

Responsibility
--------------
Bound reconstructible in-process metric history without weakening durable
storage state or introducing another host probe.  The application event
batcher and the SQLite Sidecar writer both consume this module; neither owns a
second, drifting sample-window default.

Entry point
-----------
``storage_metric_sample_capacity`` derives one recent-sample window from the
existing SQLite writer waiting-job budget.  The 8 GiB reference profile keeps
4,096 samples, probe failure keeps 2,048, and distributed mode keeps 32,768.
"""

from __future__ import annotations

from collections.abc import Mapping

from runtime_guards import resolve_resource_budget


STORAGE_METRIC_SAMPLE_MIN_CAPACITY = 1_024
STORAGE_METRIC_SAMPLE_HARD_CAPACITY = 32_768
_SAMPLES_PER_WRITER_QUEUE_SLOT = 256


def storage_metric_sample_capacity(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return the finite recent-latency window shared by storage processes."""
    writer_queue_capacity = resolve_resource_budget(
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY',
        environment,
        minimum=4,
        maximum=1_024,
    )
    return max(
        STORAGE_METRIC_SAMPLE_MIN_CAPACITY,
        min(
            STORAGE_METRIC_SAMPLE_HARD_CAPACITY,
            writer_queue_capacity * _SAMPLES_PER_WRITER_QUEUE_SLOT,
        ),
    )


def bounded_storage_metric_sample_capacity(value: int | None) -> int:
    """Resolve production policy or clamp an explicit constructor seam."""
    if value is None:
        return storage_metric_sample_capacity()
    return max(1, min(STORAGE_METRIC_SAMPLE_HARD_CAPACITY, int(value)))


__all__ = [
    'STORAGE_METRIC_SAMPLE_HARD_CAPACITY',
    'STORAGE_METRIC_SAMPLE_MIN_CAPACITY',
    'bounded_storage_metric_sample_capacity',
    'storage_metric_sample_capacity',
]
