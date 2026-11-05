"""The round token-count benchmark replays production stages with stable digests.

``benchmarks/round_token_count_cpu.py`` is valid evidence only while its
per-round digests (gate and admission measurements, both clamped completion
budgets, the sealed payload and its admitted tokens) depend on the work and not
on timing, while every measured stage really runs, and while its counting clock
and tokenizer receipts observe the counting entry points.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WINDOW = 400_000


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'round_token_count_cpu_measure', ROOT / 'benchmarks' / 'round_token_count_cpu.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def history(benchmark):
    return benchmark.synthetic_history(6_000, model='gpt-5.6', seed=7)


@pytest.mark.parametrize('model', ['gpt-5.6', 'claude-opus-4.6'])
def test_replayed_rounds_have_timing_independent_digests(benchmark, history, model):
    tools = benchmark.tools()[:3]
    protocol = benchmark.MODELS[model]

    first = benchmark.run_rounds(history, tools, rounds=3, model=model, protocol=protocol,
                                 window=WINDOW, task_id='round-count-a')
    with benchmark.Contention(1):
        second = benchmark.run_rounds(history, tools, rounds=3, model=model,
                                      protocol=protocol, window=WINDOW,
                                      task_id='round-count-a')

    assert first['digests'] == second['digests']
    assert first['digests'][-1]['messages'] == len(history)
    assert set(first['clocks']) == {*benchmark.STAGES, 'counting'}
    assert all(len(values) == 3 and min(values) > 0
               for values in first['clocks'].values())
    # The cold first round tokenizes the whole request; the clamp estimated it.
    assert first['receipts']['tokenizedTexts'][0] > 0
    assert first['receipts']['entropyScans'][0] > 0
    assert all(row['adaptedMaxTokens'] <= row['builtMaxTokens'] for row in first['digests'])


def test_digests_cover_the_sealed_request(benchmark, history):
    tools = benchmark.tools()[:3]
    changed = [dict(message) for message in history]
    changed[-1]['content'] += ' changed'

    baseline = benchmark.run_rounds(history, tools, rounds=1, model='gpt-5.6',
                                    protocol='openai', window=WINDOW,
                                    task_id='round-count-b')['digests'][0]
    edited = benchmark.run_rounds(changed, tools, rounds=1, model='gpt-5.6',
                                  protocol='openai', window=WINDOW,
                                  task_id='round-count-b')['digests'][0]

    assert baseline['payload'] != edited['payload']
    assert baseline['compactedMessages'] != edited['compactedMessages']
