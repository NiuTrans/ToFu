"""The structural batch benchmark compares equal committed work.

``benchmarks/turn_event_batch.py`` is valid evidence only while the per-event
path and ``record_task_events`` commit the identical projection, revisions,
task-event rows and sync patches for the same prepared run, and while the
batch really uses one storage command for K events.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'turn_event_batch_measure', ROOT / 'benchmarks' / 'turn_event_batch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_modes_commit_identical_work_and_only_the_batch_uses_one_command(
        benchmark, monkeypatch):
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    with benchmark.authority('sqlite') as supervisor:
        sequential = benchmark.run_case(supervisor.client, mode='sequential', events=6)
        batch = benchmark.run_case(supervisor.client, mode='batch', events=6)
    assert sequential['digest'] == batch['digest']
    assert (sequential['rpcs'], batch['rpcs']) == (6, 1)
    assert batch['persist_ms'] >= batch['rpc_ms'] > 0
