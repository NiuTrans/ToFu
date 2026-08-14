#!/usr/bin/env python3
"""Repeatable Storage Sidecar load certification on the project filesystem."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import threading
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.storage import StorageError, StorageEventBatcher, StorageSupervisor


def _mount(path: Path) -> dict[str, str]:
    if sys.platform.startswith('linux'):
        result = subprocess.run(
            ['findmnt', '-r', '-n', '-T', str(path),
             '-o', 'SOURCE,TARGET,FSTYPE'],
            text=True, capture_output=True, check=False, timeout=10)
        if result.returncode == 0:
            fields = result.stdout.strip().split(None, 2)
            if len(fields) == 3:
                return {
                    'source': fields[0], 'target': fields[1],
                    'fs_type': fields[2],
                }
    return {'source': '', 'target': '', 'fs_type': 'unknown'}


def _percentile(values, percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1,
                       math.ceil(percentile * len(ordered)) - 1))
    return round(float(ordered[index]), 3)


def _latency_summary(values) -> dict[str, float | int]:
    return {
        'samples': len(values),
        'p50_ms': _percentile(values, 0.50),
        'p95_ms': _percentile(values, 0.95),
        'p99_ms': _percentile(values, 0.99),
        'max_ms': round(max(values), 3) if values else 0.0,
    }


def _run_backend(
    *, project_root: Path, backend: str, duration: float, warmup: float,
    workers: int, write_ratio: float, critical_ratio: float,
    operation_interval_ms: float,
) -> dict:
    run_root = project_root / backend
    supervisor = StorageSupervisor(
        project_root=run_root, backend=backend, startup_timeout=90)
    try:
        supervisor.start()
    except BaseException:
        supervisor.stop()
        raise
    batcher = None
    try:
        client = supervisor.client
        batcher = StorageEventBatcher(
            client_provider=lambda **_kwargs: client,
            max_batch=500, max_window_ms=250, coalesce_ms=50)
        baseline = client.metrics()
    except BaseException:
        if batcher is not None:
            batcher.close(timeout=5)
        supervisor.stop()
        raise
    reads = deque(maxlen=200_000)
    critical_writes = deque(maxlen=200_000)
    errors: Counter[str] = Counter()
    counters: Counter[str] = Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(workers)
    started = time.monotonic()
    record_after = started + warmup
    finish_at = record_after + duration

    def record(kind: str, elapsed_ms: float) -> None:
        if time.monotonic() < record_after:
            return
        with lock:
            (critical_writes if kind == 'critical_write' else reads).append(
                elapsed_ms)
            counters[kind] += 1

    def worker(index: int) -> None:
        rng = random.Random(index)
        sequence = 0
        critical_sequence = 0
        barrier.wait(timeout=30)
        while time.monotonic() < finish_at:
            iteration_started = time.monotonic()
            choice = rng.random()
            before = time.perf_counter()
            try:
                if choice < critical_ratio:
                    client.command('record.put', {
                        'namespace': 'certification-critical',
                        'key': f'worker-{index}',
                        'value': {'sequence': critical_sequence},
                    }, f'cert-critical:{index}:{critical_sequence}',
                        priority='user', deadline=2.0)
                    critical_sequence += 1
                    record(
                        'critical_write',
                        (time.perf_counter() - before) * 1000)
                elif choice < critical_ratio + write_ratio:
                    batcher.append(
                        f'cert-task-{index}', sequence,
                        {'kind': 'delta', 'worker': index},
                        timeout=2.0, wait=False)
                    sequence += 1
                    if time.monotonic() >= record_after:
                        with lock:
                            counters['event_accepted'] += 1
                else:
                    client.query('record.get', {
                        'namespace': 'certification', 'key': 'read-probe',
                    }, deadline=2.0)
                    record('read', (time.perf_counter() - before) * 1000)
            except StorageError as exc:
                if time.monotonic() >= record_after:
                    with lock:
                        errors[exc.code] += 1
            except Exception as exc:
                if time.monotonic() >= record_after:
                    with lock:
                        errors[f'unclassified:{type(exc).__name__}'] += 1
            remaining = (
                operation_interval_ms / 1000
                - (time.monotonic() - iteration_started))
            if remaining > 0:
                time.sleep(remaining)

    try:
        with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix='storage-cert') as pool:
            futures = [pool.submit(worker, index) for index in range(workers)]
            for future in futures:
                future.result()
        if not batcher.close(timeout=30):
            raise RuntimeError('storage event batcher did not drain')

        receipt_payload = {
            'namespace': 'certification', 'key': 'receipt-once',
            'value': {'amount': 1},
        }
        receipt_id = f'cert-receipt:{uuid.uuid4().hex}'
        first = client.command('record.put', receipt_payload, receipt_id)
        replay = client.command('record.put', receipt_payload, receipt_id)
        stored = client.query('record.get', {
            'namespace': 'certification', 'key': 'receipt-once',
        })
        receipt_ok = first == replay and stored.get('version') == 1

        time.sleep(2.0)
        final = client.metrics()
        integrity = client.maintenance('system.integrity_check', deadline=60)
    finally:
        batcher.close(timeout=5)
        supervisor.stop()

    read_stats = _latency_summary(reads)
    write_stats = _latency_summary(critical_writes)
    event_metrics = batcher.metrics
    writer = final.get('writer') or {}
    acceptance = {
        'zero_errors': not errors,
        'receipt_exactly_once': receipt_ok,
        'integrity_ok': bool(integrity.get('ok')),
        'event_batch_failures_zero': int(event_metrics['failed']) == 0,
        'event_persist_window_le_300ms': float(
            event_metrics['persist_lag_max_ms']) <= 300,
        'read_p95_le_100ms': read_stats['p95_ms'] <= 100,
        'read_p99_le_250ms': read_stats['p99_ms'] <= 250,
        'write_p95_le_200ms': write_stats['p95_ms'] <= 200,
        'write_p99_le_500ms': write_stats['p99_ms'] <= 500,
        'critical_write_samples_present': write_stats['samples'] > 0,
        'zero_writer_timeouts': int(writer.get('timed_out') or 0) == 0,
        'rpc_capacity_not_rejected': int(
            (final.get('rpc') or {}).get('rejected') or 0) == 0,
        'rss_returned_near_baseline': int(
            (final.get('process') or {}).get('rss_bytes') or 0)
            <= int((baseline.get('process') or {}).get('rss_bytes') or 0)
            + 128 * 1024 * 1024,
    }
    return {
        'backend': backend, 'workers': workers,
        'duration_seconds': duration, 'warmup_seconds': warmup,
        'write_ratio': write_ratio, 'critical_ratio': critical_ratio,
        'operation_interval_ms': operation_interval_ms,
        'operations': dict(counters), 'errors': dict(errors),
        'event_batcher': event_metrics,
        'read_latency': read_stats, 'critical_write_latency': write_stats,
        'baseline_metrics': baseline, 'final_metrics': final,
        'integrity': integrity, 'acceptance': acceptance,
        'passed': all(acceptance.values()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        '--backend', choices=('sqlite', 'postgres', 'both'), default='both')
    parser.add_argument('--duration-seconds', type=float, default=3600)
    parser.add_argument('--warmup-seconds', type=float, default=10)
    parser.add_argument('--workers', type=int, default=200)
    # With the default 50 ms operation interval this is one regenerable event
    # per second for each of the 200 active streams (200 events/s total).
    parser.add_argument('--write-ratio', type=float, default=0.05)
    parser.add_argument('--critical-ratio', type=float, default=0.001)
    parser.add_argument('--operation-interval-ms', type=float, default=50)
    parser.add_argument('--allow-non-fuse', action='store_true')
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    mount = _mount(root)
    is_fuse = 'fuse' in mount['fs_type'].lower()
    if sys.platform.startswith('linux') and not is_fuse and not args.allow_non_fuse:
        parser.error(
            f'project path is not a FUSE mount (fs_type={mount["fs_type"]})')
    if not 1 <= args.workers <= 256:
        parser.error('--workers must be between 1 and 256')
    if args.duration_seconds <= 0 or args.warmup_seconds < 0:
        parser.error('durations must be positive')
    if (not 0 < args.write_ratio <= 1 or not 0 < args.critical_ratio <= 1
            or args.write_ratio + args.critical_ratio >= 1):
        parser.error('write ratios must be positive and sum to less than 1')
    if args.operation_interval_ms < 0:
        parser.error('--operation-interval-ms must be non-negative')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output_root = root / 'data' / 'storage-certification' / f'load-{stamp}'
    output_root.mkdir(parents=True, exist_ok=False)
    selected = ('sqlite', 'postgres') if args.backend == 'both' else (args.backend,)
    results = []
    for backend in selected:
        results.append(_run_backend(
            project_root=output_root, backend=backend,
            duration=args.duration_seconds, warmup=args.warmup_seconds,
            workers=args.workers, write_ratio=args.write_ratio,
            critical_ratio=args.critical_ratio,
            operation_interval_ms=args.operation_interval_ms))
    summary = {
        'storage_protocol': 'storage.v1', 'mount': mount,
        'fuse_verified': is_fuse, 'started_at_utc': stamp,
        'results': results, 'passed': all(item['passed'] for item in results),
    }
    summary_path = output_root / 'summary.json'
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    print(json.dumps({
        'summary': str(summary_path), 'passed': summary['passed'],
        'backends': [item['backend'] for item in results],
    }, separators=(',', ':')))
    return 0 if summary['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
