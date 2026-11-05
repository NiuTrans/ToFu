"""The model-request CPU benchmark replays production stages with stable digests.

``benchmarks/model_request_cpu.py`` is valid evidence only while its
per-request digests (sealed payload, semantic and provider-prefix identities,
admitted tokens and both clamped completion budgets) depend on the work and
not on timing, and while every measured stage really runs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WINDOW = 40_000


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'model_request_cpu_measure', ROOT / 'benchmarks' / 'model_request_cpu.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def history(benchmark):
    return benchmark.synthetic_history(6_000, seed=7)


@pytest.mark.parametrize('model,protocol', [('gpt-5.6', 'openai'),
                                            ('claude-sonnet-4-6', 'anthropic')])
def test_replayed_requests_have_timing_independent_digests(
        benchmark, history, model, protocol):
    tools = benchmark.tools()[:2]

    first = benchmark.run_requests(history, tools, rounds=2, protocol=protocol,
                                   window=WINDOW, model=model)
    with benchmark.Contention(1):
        second = benchmark.run_requests(history, tools, rounds=2, protocol=protocol,
                                        window=WINDOW, model=model)

    assert first['digests'] == second['digests']
    assert [row['messages'] for row in first['digests']][-1] == len(history)
    assert history[-1]['role'] == 'tool'
    assert set(first['clocks']) == set(benchmark.STAGES)
    assert all(len(values) == 2 and min(values) > 0
               for clocks in first['clocks'].values() for values in clocks.values())
    # The slot clamp had no admitted count, estimated the input and trimmed.
    assert all(row['adaptedMaxTokens'] < row['builtMaxTokens'] <= benchmark.MAX_TOKENS
               for row in first['digests'])


@pytest.mark.parametrize('protocol', ['anthropic', 'responses'])
def test_digests_cover_the_sealed_request(benchmark, history, protocol):
    tools = benchmark.tools()[:2]
    changed = [dict(message) for message in history]
    changed[-1]['content'] += ' changed'

    baseline = benchmark.run_requests(history, tools, rounds=1, protocol=protocol,
                                      window=WINDOW)['digests'][0]
    edited = benchmark.run_requests(changed, tools, rounds=1, protocol=protocol,
                                    window=WINDOW)['digests'][0]

    assert baseline['payload'] != edited['payload']
    assert baseline['semanticId'] != edited['semanticId']
    assert baseline['prefixId'] != edited['prefixId']
