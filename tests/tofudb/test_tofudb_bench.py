"""Pytest gate for the Tofu-DB vs SQLite-sidecar benchmark harness.

The full performance evidence run is a CLI invocation of
``scripts/tofudb_bench.py --profile full --mount both``; this module is the
reproducibility gate: it executes the bounded ``smoke`` profile end to end
against BOTH authorities on a native temporary mount and asserts the
machine-readable report is complete and clean — every cell measured
operations on both authorities with no unexpected operation errors (the
size-ceiling probe cell records rejections as its observation, not errors).

A red gate here means the harness is broken (or an authority regressed into
rejecting previously accepted smoke-scale semantic operations), never a
performance assertion — absolute numbers live in the JSON report, not in
test expectations.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts import tofudb_bench  # noqa: E402

# serial: spawns the native engine and measures real wall-clock
# benchmark behavior — a timing/resource contract that cannot overlap
# the parallel unit lane.
pytestmark = [pytest.mark.unit, pytest.mark.serial]

_REPORT_KEY = 'tofudb_bench_smoke_report'


def _tofudb_binary_available() -> bool:
    try:
        tofudb_bench._resolve_tofudb_binary()  # noqa: SLF001
    except RuntimeError:
        return False
    return True


@pytest.fixture(scope='module')
def bench_report(tmp_path_factory):
    if not _tofudb_binary_available():
        pytest.skip('no tofu-db binary available for the bench smoke gate')
    output = tmp_path_factory.mktemp('tofudb-bench') / 'report.json'
    exit_code = tofudb_bench.main([
        '--profile', 'smoke', '--mount', 'native', '--authority', 'both',
        '--output', str(output),
    ])
    assert exit_code == 0
    return json.loads(output.read_text())


def test_bench_smoke_report_schema(bench_report):
    assert bench_report['suite'] == 'tofudb-bench/v1'
    assert bench_report['profile'] == 'smoke'
    assert bench_report['seed'] == tofudb_bench._DEFAULT_SEED  # noqa: SLF001
    environment = bench_report['environment']
    assert environment['mounts']['native']['fstype']
    # The probe-derived daemon budget is the launch-profile witness.
    budget = environment['tofudb']['readiness']['resourceBudget']
    assert budget['maximumConnections'] >= 1
    assert environment['cpu_count'] >= 1
    assert environment['memory_total_bytes'] > 0


def test_bench_smoke_cells_cover_all_workloads_both_authorities(bench_report):
    cells = bench_report['cells']
    assert cells, 'smoke run produced no cells'
    workloads = {cell['workload'] for cell in cells}
    assert workloads == {
        'turn_ingest', 'event_stream', 'queue_claim', 'task_results',
        'read_planes', 'group_commit', 'footprint',
    }
    authorities = {cell['authority'] for cell in cells}
    assert authorities == {'tofudb', 'legacy_sqlite_sidecar'}
    for cell in cells:
        assert cell['mount'] == 'native'
        assert cell['measured_ops'] > 0, cell['name']
        assert cell['wall_s'] > 0, cell['name']
        if cell['workload'] == 'footprint':
            # The footprint cell's output is the resource witness itself.
            assert cell['fd_count_max'], cell['name']
            assert cell['data_bytes_end'] is not None, cell['name']
        else:
            assert cell['latency_ms']['p50'] is not None, cell['name']
        assert cell['rss_bytes']['max'], cell['name']
        # Identical semantic workload replay: no unexpected rejections.
        assert cell['errors'] == {}, (
            f"{cell['authority']}/{cell['name']}: {cell['errors']} "
            f"{cell['error_samples']}"
        )


def test_bench_smoke_size_ceiling_probe_records_evidence(bench_report):
    probes = [
        cell for cell in bench_report['cells']
        if cell['name'] == 'turn_ingest/size_ceiling_probe'
    ]
    assert len(probes) == 2  # one per authority
    for probe in probes:
        for size in (1_500_000, 3_500_000, 8_000_000):
            observation = probe['params'][f'probe_{size}_bytes']
            assert observation == 'ok' or observation.startswith('rejected:')
        # The smallest probe is inside every documented bound (legacy:
        # 4 MiB decoded receipt echo; tofu-db: 8 MiB transaction literal):
        # both authorities must accept it for the gate to stay honest.
        assert probe['params']['probe_1500000_bytes'] == 'ok'
