#!/usr/bin/env python3
"""Bounded soak for the native engine under the personal-computer budget.

The harness certification contract lists "long duration soak under an 8 GiB
host budget" as an uncovered claim; this tool produces bounded evidence
toward it without pretending to be the full claim. It drives the production
client stack through a repeating agent-harness mix (80/10/10 blended with
search and sidebar paging) for an explicit duration, samples engine/client
RSS, and reports per-minute latency windows plus error tallies by stable
code — a degradation curve, not a single percentile. It is a measurement
tool, not a gate: CI must never assert wall-clock thresholds from it.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.tofudb_perf_compare as perf  # noqa: E402

REPORT_FORMAT = 'tofudb-soak-report.v1'
_ROUND = (('mixed_80_10_10', 300), ('conversation_search', 10), ('sidebar_list_page', 5))
_MAX_MINUTES = 120
_RSS_SAMPLE_INTERVAL_S = 0.05


def _percentile(sorted_samples: list[float], fraction: float) -> float:
    if not sorted_samples:
        return 0.0
    rank = max(1, math.ceil(fraction * len(sorted_samples)))
    return sorted_samples[min(rank, len(sorted_samples)) - 1]


def _window_summary(samples: list[float], errors: dict[str, int], started: float) -> dict:
    ordered = sorted(samples)
    return {
        'wall_seconds': round(time.monotonic() - started, 3),
        'operations': len(samples),
        'errors': dict(sorted(errors.items())),
        'p50_ms': round(_percentile(ordered, 0.50), 3),
        'p99_ms': round(_percentile(ordered, 0.99), 3),
        'ops_per_sec': round(len(samples) / max(time.monotonic() - started, 1e-9), 1),
    }


def _rss_sampler(engine, result: dict, stopped: threading.Event) -> None:
    import psutil
    client = psutil.Process()
    while not stopped.is_set():
        process = getattr(engine, '_process', None)
        try:
            if process is not None:
                rss = psutil.Process(process.pid).memory_info().rss
                result['engine_peak_rss_observed_bytes'] = max(
                    result['engine_peak_rss_observed_bytes'], rss)
            result['client_peak_rss_observed_bytes'] = max(
                result['client_peak_rss_observed_bytes'], client.memory_info().rss)
            result['resource_sample_count'] += 1
        except psutil.Error:
            pass  # A restart interval has no live authority to sample.
        stopped.wait(_RSS_SAMPLE_INTERVAL_S)


def build_report(*, minutes: int, scale_name: str, engine, windows: list[dict],
                 totals: dict, rss: dict, metadata: dict) -> dict:
    return {
        'format': REPORT_FORMAT,
        'scope': 'bounded-soak-not-the-full-uncovered-claim',
        'duration_minutes_requested': minutes,
        'scale': scale_name,
        'windows': windows,
        'totals': totals,
        'resources': rss,
        'budgets': {
            'personal_computer_engine_peak_rss_bytes': 512 * 1024 * 1024,
            'engine_peak_rss_within_budget': bool(
                rss.get('engine_peak_rss_observed_bytes', 0) < 512 * 1024 * 1024),
        },
        'measurement': metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--minutes', type=int, default=10,
                        help=f'soak duration in minutes (1..{_MAX_MINUTES})')
    parser.add_argument('--scale', choices=('small', 'medium'), default='small',
                        help='workload corpus shape (default small)')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--expected-binary-sha256', default='')
    parser.add_argument('--tofudb-durability', default='strict',
                        choices=('strict', 'normal'))
    parser.add_argument('--output', default='')
    args = parser.parse_args()
    if not 1 <= args.minutes <= _MAX_MINUTES:
        parser.error(f'--minutes must be within 1..{_MAX_MINUTES}')

    engine = perf.TofuDbEngine(
        args.binary, durability=args.tofudb_durability,
        client_mode='application', expected_sha256=args.expected_binary_sha256)
    engine.start()
    stopped = threading.Event()
    rss = {'engine_peak_rss_observed_bytes': 0,
           'client_peak_rss_observed_bytes': 0,
           'resource_sample_count': 0}
    sampler = threading.Thread(
        target=_rss_sampler, args=(engine, rss, stopped),
        name='soak-rss', daemon=True)
    windows: list[dict] = []
    totals = {'operations': 0, 'errors': {}, 'rounds': 0}
    sampler.start()
    metadata: dict = {}
    try:
        scale = dict(perf._SCALES[args.scale], large_turn_bytes=262144,
                     large_turn_pattern='repetitive', large_turn_history=0)
        entries = {name: (setup, op)
                   for name, setup, op, *_ in perf._workloads(scale, perf._OWNER_ID)}
        for name, _count in _ROUND:
            if name not in entries:
                raise perf.BenchError(f'workload missing from catalog: {name}')
            setup, _op = entries[name]
            if setup is not None:
                setup(engine)
        deadline = time.monotonic() + args.minutes * 60
        window_started = time.monotonic()
        window_samples: list[float] = []
        window_errors: dict[str, int] = {}
        index = 0
        while time.monotonic() < deadline:
            for name, count in _ROUND:
                _setup, op = entries[name]
                for _ in range(count):
                    if time.monotonic() >= deadline:
                        break
                    began = time.perf_counter_ns()
                    try:
                        engine.execute(*op(index))
                    except Exception as exc:  # noqa: BLE001 — counted, never hidden
                        code = getattr(exc, 'code', type(exc).__name__)
                        window_errors[code] = window_errors.get(code, 0) + 1
                        totals['errors'][code] = totals['errors'].get(code, 0) + 1
                        first = totals.setdefault('first_error_messages', [])
                        if len(first) < 3:
                            first.append(f'{code}: {exc}'[:200])
                    else:
                        window_samples.append((time.perf_counter_ns() - began) / 1e6)
                    index += 1
                    totals['operations'] += 1
                if time.monotonic() - window_started >= 60:
                    windows.append(_window_summary(
                        window_samples, window_errors, window_started))
                    window_started = time.monotonic()
                    window_samples, window_errors = [], {}
            totals['rounds'] += 1
        if window_samples or window_errors:
            windows.append(_window_summary(window_samples, window_errors, window_started))
        # Capture metadata while the pinned binary still exists: engine.stop()
        # removes the pin directory along with the other temporary artifacts.
        metadata = perf._measurement_metadata({'tofudb': engine})
    finally:
        stopped.set()
        if sampler.is_alive():
            sampler.join(timeout=5)
        engine.stop()
    report = build_report(minutes=args.minutes, scale_name=args.scale,
                          engine=engine, windows=windows, totals=totals,
                          rss=rss, metadata=metadata)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
        print(f'report written to {args.output}')
    else:
        print(rendered)
    return int(bool(totals['errors']))


if __name__ == '__main__':
    raise SystemExit(main())
