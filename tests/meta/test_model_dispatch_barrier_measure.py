"""The pre-request barrier benchmark compares equal work with honest clocks.

``benchmarks/model_dispatch_barrier.py`` is valid evidence only while both
boundaries commit the identical ordered event log, the deferred boundary really
dispatches with request evidence still in flight, and the awaited boundary's
recorded ``barrier`` step contains at least the slow snapshot write it waited
for. The child lane holds the same properties for a child's Inspector evidence,
whose former port charged the write to ``build_bodyMs.snapshot``. Lower bounds
only: a loaded host may lengthen clocks, never shorten them.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_WRITE_SECONDS = 0.4


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'model_dispatch_barrier_measure', ROOT / 'benchmarks' / 'model_dispatch_barrier.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(benchmark, mode):
    return benchmark.run_case(mode=mode, write_seconds=_WRITE_SECONDS, rounds=1,
                              prep_seconds=0.0, model_seconds=0.05, deltas=2)


def test_both_boundaries_commit_the_same_log_and_only_the_old_one_waits(benchmark):
    awaited = _case(benchmark, 'awaited')
    deferred = _case(benchmark, 'deferred')

    assert awaited['committed_sha256'] == deferred['committed_sha256']
    assert awaited['committed_events'] == deferred['committed_events'] > 0
    [old] = awaited['samples']
    [new] = deferred['samples']
    assert old['pre_request_ms'] >= _WRITE_SECONDS * 1000 * 0.9
    assert new['pending_at_dispatch'] >= 1


def _child_case(benchmark, mode):
    return benchmark.run_child_case(mode=mode, write_seconds=_WRITE_SECONDS, rounds=1,
                                    prep_seconds=0.0, model_seconds=0.05, deltas=2)


def test_child_lane_commits_the_same_evidence_and_only_the_old_port_waits(benchmark):
    awaited = _child_case(benchmark, 'awaited')
    deferred = _child_case(benchmark, 'deferred')

    assert awaited['committed_sha256'] == deferred['committed_sha256']
    assert awaited['committed_events'] == deferred['committed_events'] == 2
    [old] = awaited['samples']
    [new] = deferred['samples']
    assert old['snapshot_ms'] >= _WRITE_SECONDS * 1000 * 0.9
    assert new['pending_at_dispatch'] >= 1
