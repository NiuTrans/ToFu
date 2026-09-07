#!/usr/bin/env python3
"""Deterministic in-process workloads for the live task replay protocol.

This is deliberately provider-free: it measures the state/event path shared by
chat, paper and motion tasks without network variance or paid inference.  The
default scale is large enough to expose accidental quadratic folds, unbounded
retention and broken reconnect cursors while still fitting a developer laptop.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.agent_core.task_runtime import TaskRuntime
from lib.identity import PrincipalContext


_BENCHMARK_PRINCIPAL = PrincipalContext.system(
    subject_id='benchmark.task-replay',
    owner_user_id=900_001,
    scopes={'benchmark:task-replay'},
)


@dataclass(frozen=True)
class WorkloadResult:
    name: str
    event_count: int
    iterations: int
    median_ms: float
    p90_ms: float
    peak_kib: float
    cursor_reset: bool
    replayed_events: int


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(
        0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _measure(
    name: str,
    event_count: int,
    iterations: int,
    operation: Callable[[], tuple[dict[str, Any], int]],
) -> WorkloadResult:
    samples: list[float] = []
    peak = 0
    final_page: dict[str, Any] = {}
    final_replayed = 0
    # Import/metric registration is process startup cost, not replay latency.
    # Warm each workload once so p90 remains comparable across iteration order.
    operation()
    for _ in range(iterations):
        tracemalloc.start()
        started = time.perf_counter_ns()
        final_page, final_replayed = operation()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        _, iteration_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        samples.append(elapsed_ms)
        peak = max(peak, iteration_peak)
    return WorkloadResult(
        name=name,
        event_count=event_count,
        iterations=iterations,
        median_ms=round(statistics.median(samples), 3),
        p90_ms=round(_percentile(samples, 0.9), 3),
        peak_kib=round(peak / 1024, 1),
        cursor_reset=bool((final_page.get('cursor') or {}).get('reset')),
        replayed_events=final_replayed,
    )


def _append(runtime: TaskRuntime, task_id: str, count: int, kind: str) -> None:
    for index in range(count):
        event_type = 'tool_done' if kind == 'tool' and index % 4 == 3 else 'delta'
        runtime.append_event(task_id, {
            'type': event_type,
            'delta': 'x' * 24,
            'round': index // 4,
        })


def _short_conversation(event_count: int) -> tuple[dict[str, Any], int]:
    runtime = TaskRuntime(
        'bench-short', max_events=event_count + 1, push_channel='')
    runtime.create(principal=_BENCHMARK_PRINCIPAL, task_id='short')
    _append(runtime, 'short', event_count, 'chat')
    page = runtime.poll('short', 0)
    assert page['next_cursor'] == event_count
    return page, len(page['events'])


def _long_tool_loop(event_count: int) -> tuple[dict[str, Any], int]:
    runtime = TaskRuntime(
        'bench-tool', max_events=event_count + 1, push_channel='')
    runtime.create(principal=_BENCHMARK_PRINCIPAL, task_id='tool')
    _append(runtime, 'tool', event_count, 'tool')
    page = runtime.poll('tool', 0)
    assert page['events'][-1]['seq'] == event_count - 1
    return page, len(page['events'])


def _disconnect_recovery(event_count: int) -> tuple[dict[str, Any], int]:
    retained = max(8, event_count // 4)
    runtime = TaskRuntime(
        'bench-reconnect', max_events=retained, push_channel='')
    runtime.create(principal=_BENCHMARK_PRINCIPAL, task_id='reconnect')
    _append(runtime, 'reconnect', event_count, 'chat')
    page = runtime.poll('reconnect', 0)
    assert page['cursor']['reset'] is True
    assert len(page['events']) == retained
    caught_up = runtime.poll('reconnect', page['next_cursor'])
    assert caught_up['events'] == []
    return page, len(page['events'])


def _paper_flow(event_count: int) -> tuple[dict[str, Any], int]:
    runtime = TaskRuntime(
        'bench-paper', max_events=event_count + 2,
        push_channel='')
    runtime.create(principal=_BENCHMARK_PRINCIPAL, task_id='paper')
    _append(runtime, 'paper', event_count, 'tool')
    runtime.finish('paper', result={'report': 'done'})
    page = runtime.poll('paper', 0)
    assert page['done'] is True and page['status'] == 'done'
    assert page['events'][-1]['type'] == 'done'
    return page, len(page['events'])


def run_benchmarks(*, scale: float = 1.0, iterations: int = 5) -> dict:
    counts = {
        'short_conversation': max(8, round(64 * scale)),
        'long_tool_loop': max(32, round(4096 * scale)),
        'disconnect_recovery': max(32, round(2048 * scale)),
        'paper_flow': max(32, round(1024 * scale)),
    }
    operations = {
        'short_conversation': _short_conversation,
        'long_tool_loop': _long_tool_loop,
        'disconnect_recovery': _disconnect_recovery,
        'paper_flow': _paper_flow,
    }
    rows = [
        _measure(name, count, iterations,
                 lambda op=operations[name], n=count: op(n))
        for name, count in counts.items()
    ]
    return {
        'format': 'tofu.task-replay-benchmark/v1',
        'scale': scale,
        'iterations': iterations,
        'workloads': [asdict(row) for row in rows],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale', type=float, default=1.0)
    parser.add_argument('--iterations', type=int, default=5)
    parser.add_argument('--output', help='Optional JSON output path')
    args = parser.parse_args()
    report = run_benchmarks(
        scale=max(0.01, args.scale), iterations=max(1, args.iterations))
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(encoded + '\n', encoding='utf-8')
    print(encoded)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
