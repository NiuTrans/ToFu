from __future__ import annotations

import pytest

from lib.observability import (
    prometheus_lines,
    record_runtime_probe_failure,
    reset_for_tests,
)
from lib.server_runtime_probes import stall_pressure_context


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_metrics():
    reset_for_tests()
    yield
    reset_for_tests()


def _fail(message: str):
    raise OSError(message)


def test_pressure_context_keeps_partial_result_and_counts_each_failed_source():
    context = stall_pressure_context(
        loadavg_reader=lambda: _fail('proc unavailable'),
        pressure_reader=lambda: {'pct': 91.42},
    )

    assert context == 'cgmem=91.4%'
    metrics = '\n'.join(prometheus_lines())
    assert (
        'tofu_runtime_probe_failures_total{source="loadavg"} 1.0'
        in metrics
    )
    assert 'proc unavailable' not in metrics


def test_pressure_context_never_raises_and_aggregates_cgroup_failure():
    context = stall_pressure_context(
        loadavg_reader=lambda: '7.20',
        pressure_reader=lambda: _fail('restricted cgroup'),
    )

    assert context == 'load1=7.20'
    metrics = '\n'.join(prometheus_lines())
    assert (
        'tofu_runtime_probe_failures_total{source="cgroup_memory"} 1.0'
        in metrics
    )


def test_runtime_probe_metric_folds_unknown_sources_into_other():
    record_runtime_probe_failure('request-123456')

    metrics = '\n'.join(prometheus_lines())
    assert 'source="other"' in metrics
    assert 'request-123456' not in metrics
