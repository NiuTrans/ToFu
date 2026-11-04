"""Compare two saved /metrics scrapes without touching application state.

Responsibility: report observed task, executor, delivery and storage-RPC work
across one explicitly timed interval. This is evidence for architecture review,
not a load generator or a capacity/admission policy. Counter deltas require the
same serving-process incarnation; aggregate scrapes alone cannot prove that.

Entry: python3 scripts/analyze_agent_load.py BEFORE.prom AFTER.prom
       --elapsed-seconds SECONDS
Dependencies: standard library; the exposition owned by routes/metrics.py.
No network, database, provider, profiler or application imports are performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path


_GAUGES = {
    'tofu_tasks_inflight': {'kind'},
    'tofu_executor_active': {'pool'},
    'tofu_executor_resident_threads': {'pool'},
    'tofu_event_loop_lag_seconds': set(),
    'tofu_push_subscribers': set(),
    **{f'tofu_task_event_delivery_{name}': set() for name in (
        'pending_events', 'pending_bytes', 'pending_batches', 'workers',
        'active_workers', 'failed_lanes', 'max_lag_ms', 'peak_events',
        'peak_bytes', 'peak_workers',
    )},
}
_COUNTERS = {f'tofu_task_event_delivery_{name}_total': set() for name in (
    'accepted', 'completed', 'committed', 'retries', 'failures',
    'revoked_events', 'merged_progress',
)}
_RPC = 'tofu_storage_rpc_seconds'
_LABELS = {
    **_GAUGES, **_COUNTERS,
    f'{_RPC}_count': {'kind', 'operation', 'outcome'},
    f'{_RPC}_sum': {'kind', 'operation', 'outcome'},
}
_SAMPLE = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)=("(?:[^"\\]|\\[\\"n])*")(?:,|$)')


def parse_metrics(text: str) -> dict:
    """Read the selected exporter series; reject ambiguous or corrupt evidence."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name = re.split(r'[\s{]', line, maxsplit=1)[0]
        if name not in _LABELS:
            continue
        match = _SAMPLE.fullmatch(line)
        if match is None:
            raise ValueError(f'invalid selected sample at line {number}')
        labels, position = {}, 0
        encoded = match[2] or ''
        while position < len(encoded):
            label = _LABEL.match(encoded, position)
            if label is None or label[1] in labels:
                raise ValueError(f'invalid labels at line {number}')
            labels[label[1]] = json.loads(label[2])
            position = label.end()
        if set(labels) != _LABELS[name]:
            raise ValueError(f'unexpected labels for {name} at line {number}')
        try:
            value = float(match[3])
        except ValueError:
            raise ValueError(f'invalid value for {name} at line {number}') from None
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'non-finite or negative value for {name} at line {number}')
        key = name, tuple(sorted(labels.items()))
        if key in values:
            raise ValueError(f'duplicate selected series at line {number}')
        values[key] = value
    if not values:
        raise ValueError('scrape contains no selected agent-load series')
    return values


def _delta(before, after):
    if before is None or after is None:
        return None, 'missing_endpoint'
    if after < before:
        return None, 'counter_decreased'
    return after - before, 'observed'


def analyze(before_text: str, after_text: str, elapsed_seconds: float) -> dict:
    if not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        raise ValueError('elapsed_seconds must be finite and positive')
    before, after = parse_metrics(before_text), parse_metrics(after_text)
    keys = set(before) | set(after)
    gauges, counters, rpc = [], [], []
    for key in sorted(keys):
        name, labels = key
        row = {'metric': name, 'labels': dict(labels),
               'before': before.get(key), 'after': after.get(key)}
        if name in _GAUGES:
            gauges.append(row)
        elif name in _COUNTERS:
            change, status = _delta(before.get(key), after.get(key))
            counters.append({**row, 'status': status, 'delta': change,
                             'per_second': None if change is None else change / elapsed_seconds})
    rpc_labels = {labels for name, labels in keys if name.startswith(_RPC)}
    for labels in sorted(rpc_labels):
        count_key, sum_key = (f'{_RPC}_count', labels), (f'{_RPC}_sum', labels)
        count, count_status = _delta(before.get(count_key), after.get(count_key))
        seconds, sum_status = _delta(before.get(sum_key), after.get(sum_key))
        status = (count_status if count_status != 'observed' else sum_status)
        if status == 'observed' and (count % 1 or (count == 0 and seconds != 0)):
            status = 'inconsistent_histogram'
        valid = status == 'observed'
        rpc.append({
            'labels': dict(labels), 'status': status,
            'calls': int(count) if valid else None,
            'wall_seconds_sum': seconds if valid else None,
            'mean_wall_ms': seconds / count * 1000 if valid and count else None,
            'calls_per_second': count / elapsed_seconds if valid else None,
        })
    rpc.sort(key=lambda row: row['wall_seconds_sum'] or 0, reverse=True)
    return {
        'elapsed_seconds': elapsed_seconds,
        'source_sha256': [hashlib.sha256(text.encode()).hexdigest()
                          for text in (before_text, after_text)],
        'gauges': gauges, 'delivery_counters': counters, 'storage_rpc': rpc,
        'interpretation': [
            'Deltas assume the same process incarnation. A decrease is refused; '
            'a restart followed by larger counters cannot be detected from these scrapes.',
            'Missing endpoints are unknown, never zero. Gauges are two observations, '
            'not interval peaks, averages or percentiles.',
            'Delivery pending_bytes includes retained structural projection graphs; '
            'it is neither unique heap memory nor process RSS. max_lag_ms and peak_* '
            'are lifetime high-water marks, not current lag or interval peaks.',
            'RPC means use interval sum/count deltas. Summed wall time overlaps '
            'concurrent calls and is neither database CPU nor end-to-end task latency.',
            'Task counts, executor jobs, threads and logical agents are different '
            'populations. These observations do not establish supported capacity.',
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--elapsed-seconds', type=float, required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.before.read_text(encoding='utf-8'),
                         args.after.read_text(encoding='utf-8'), args.elapsed_seconds)
    except (OSError, ValueError) as exc:
        parser.exit(2, f'agent-load evidence unavailable: {exc}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
