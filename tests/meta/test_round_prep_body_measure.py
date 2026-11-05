"""The round-prep body benchmark replays production steps with stable digests.

``benchmarks/round_prep_body.py`` is valid evidence only while its per-round
digests (request body, projected snapshot row, compaction measurement) depend on
the work and not on timing, and while every measured step really runs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_TOOL = {'type': 'function', 'function': {
    'name': 'run_command', 'description': 'Run a shell command.',
    'parameters': {'type': 'object', 'properties': {
        'command': {'type': 'string'}}, 'required': ['command']}}}


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'round_prep_body_measure', ROOT / 'benchmarks' / 'round_prep_body.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replayed_rounds_have_timing_independent_digests(benchmark):
    history = benchmark.synthetic_history(26, seed=7)

    first = benchmark.run_rounds(history, [_TOOL], rounds=2, task_id='measure-a')
    with benchmark.Contention(1):
        second = benchmark.run_rounds(history, [_TOOL], rounds=2, task_id='measure-a')

    assert first['digests'] == second['digests']
    assert [row['messages'] for row in first['digests']] == [24, 26]
    assert set(first['clocks']) == {
        'compaction', 'gate', 'admission', 'build_body', 'projection'}
    assert all(len(values) == 2 and min(values) > 0
               for values in first['clocks'].values())


def test_projection_digest_covers_the_persisted_delta_row(benchmark):
    history = benchmark.synthetic_history(26, seed=7)
    changed = [dict(message) for message in history]
    changed[-1]['content'] += ' changed'

    baseline = benchmark.run_rounds(history, [_TOOL], rounds=1, task_id='measure-b')
    edited = benchmark.run_rounds(changed, [_TOOL], rounds=1, task_id='measure-b')

    assert baseline['digests'][0]['projected'] != edited['digests'][0]['projected']
    assert baseline['digests'][0]['body'] != edited['digests'][0]['body']
