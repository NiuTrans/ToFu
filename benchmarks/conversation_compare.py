"""Paired active-conversation acceptance on one explicit disposable volume.

Each leg runs in a fresh process through conversation_concurrency.py; repeats
alternate backend order. JSON preserves failed legs and separates correctness,
absolute latency, coverage, and SQLite comparison. No live models/user data.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.conversation_acceptance import POLICY, assess
from benchmarks.conversation_provenance import capture_sources
from scripts.tofudb_bench import _mount_type


def compare(legs: list[dict], repeats: int, filesystem: str, *, current_sources: dict | None = None) -> dict:
    if current_sources is None:
        try:
            current_sources = capture_sources()
        except (OSError, RuntimeError, ValueError) as exc:
            current_sources = {'capture_error': str(exc)}
    gaps = []
    comparisons = []
    if repeats < POLICY['minimum_repeats']:
        gaps.append('fewer than required paired repeats')
    for repeat in range(repeats):
        selected = [row for row in legs if row['repeat'] == repeat]
        pair = {row['backend']: row['report'] if isinstance(row.get('report'), dict) else {}
                for row in selected}
        authorities = {backend: report['authority'] if isinstance(report.get('authority'), dict) else {}
                       for backend, report in pair.items()}
        if len(selected) != len(POLICY['required_backends']):
            gaps.append(f'repeat {repeat}: missing or duplicated leg')
        if any(row.get('error') or row.get('exit_code', 0) != 0 for row in selected):
            gaps.append(f'repeat {repeat}: a leg failed or timed out')
        if set(pair) != set(POLICY['required_backends']):
            gaps.append(f'repeat {repeat}: missing backend')
            continue
        evaluations = {backend: assess(report, current_sources=current_sources)
                       for backend, report in pair.items()}
        labels_match = all(backend == report.get('backend') for backend, report in pair.items())
        if not labels_match:
            gaps.append(f'repeat {repeat}: leg backend differs from measured authority')
        for backend, report in pair.items():
            if not evaluations[backend]['passed']:
                gaps.append(f'repeat {repeat}: {backend} did not pass workload acceptance')
            if authorities[backend].get('filesystem') != filesystem:
                gaps.append(f'repeat {repeat}: {backend} authority volume not verified')
        shape = ('conversations', 'rounds', 'chunk_bytes', 'stream_interval_ms', 'stream_event_kind', 'carry_stream_events', 'terminal_barrier',
                 'restart_while_running', 'durability', 'benchmark_source_sha256', 'rpc_deadline_seconds')
        same_sources = all(item['current_source_qualified'] for item in evaluations.values())
        def pipeline_shape(report):
            if 'delivery_pipeline' not in report:
                return None
            pipeline = report['delivery_pipeline']
            if not isinstance(pipeline, dict):
                return ('invalid-pipeline',)
            return tuple(pipeline.get(key) for key in (
                'contract', 'submission_mode', 'source_manifest_sha256'))

        same_shape = (labels_match and same_sources
                      and pipeline_shape(pair['tofudb']) == pipeline_shape(pair['sqlite'])
                      and all(pair['tofudb'].get(key) == pair['sqlite'].get(key) for key in shape))
        source_known = all(isinstance(item.get('benchmark_source_sha256'), str)
                           and re.fullmatch(r'[0-9a-f]{64}', item['benchmark_source_sha256'])
                           for item in pair.values())
        if not source_known:
            gaps.append(f'repeat {repeat}: benchmark source identity missing or invalid')
        devices = [item.get('volume_device') for item in authorities.values()]
        same_volume = (all(item.get('filesystem') == filesystem for item in authorities.values())
                       and all(isinstance(device, int) and not isinstance(device, bool) and device >= 0
                               for device in devices) and len(set(devices)) == 1)
        if not same_volume:
            gaps.append(f'repeat {repeat}: common authority device not verified')
        if not same_shape:
            gaps.append(f'repeat {repeat}: workload, durability or harness identity differs')
        for metric in POLICY['p99_budget_ms']:
            native = pair['tofudb'].get('latency_ms', {}).get(metric, {}).get('p99')
            sqlite = pair['sqlite'].get('latency_ms', {}).get(metric, {}).get('p99')
            complete = all(item['correctness_passed'] and item['coverage_passed']
                           and item['current_source_qualified']
                           and item['latency_comparison_eligible']
                           and item['metrics'][metric]['samples'] == item['metrics'][metric]['expected_samples']
                           for item in evaluations.values())
            valid = (complete and same_shape and source_known and same_volume
                     and isinstance(native, (float, int)) and isinstance(sqlite, (float, int))
                     and math.isfinite(native) and math.isfinite(sqlite) and native >= 0 and sqlite > 0)
            wins = valid and native <= sqlite
            comparisons.append({'repeat': repeat, 'metric': metric, 'tofudb_p99_ms': native,
                                'sqlite_p99_ms': sqlite, 'tofudb_over_sqlite': native / sqlite if valid else None,
                                'comparable': valid, 'passed': wins})
            if not wins:
                gaps.append(f'repeat {repeat}: {metric} did not meet SQLite P99')
    return {'passed': not gaps, 'failures': gaps, 'comparisons': comparisons,
            'scope': 'bounded-scheduled-stream-burst-and-recovery', 'uncovered': POLICY['uncovered']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-client', choices=('application', 'embedded'), default='application')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--conversations', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--chunk-bytes', type=int, default=8192)
    parser.add_argument('--stream-event-kind', choices=('phase', 'delta'), default='phase')
    parser.add_argument('--terminal-barrier', action='store_true')
    parser.add_argument('--carry-stream-events', action='store_true')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--stream-interval-ms', type=float, default=1000)
    parser.add_argument('--leg-timeout', type=float, default=900)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 30 <= args.leg_timeout <= 3600:
        parser.error('repeats=1..10, leg-timeout=30..3600')
    if not args.directory.is_dir() or not args.binary.is_file():
        parser.error('directory and binary must already exist')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = args.output.parent / (args.output.stem + '-legs')
    artifacts.mkdir(exist_ok=False)
    report = {'format': 'tofu.conversation-comparison/v1', 'policy': POLICY,
              'filesystem': _mount_type(args.directory), 'legs': []}
    for repeat in range(args.repeats):
        order = POLICY['required_backends'][::1 if repeat % 2 == 0 else -1]
        for backend in order:
            name = f'{repeat}-{backend}'
            output = artifacts / (name + '.json')
            log = artifacts / (name + '.log')
            command = [sys.executable, str(Path(__file__).with_name('conversation_concurrency.py')),
                       '--backend', backend, '--native-client', args.native_client, '--binary', str(args.binary.resolve()),
                       '--directory', str(args.directory.resolve()), '--output', str(output.resolve()),
                       '--conversations', str(args.conversations), '--rounds', str(args.rounds),
                       '--chunk-bytes', str(args.chunk_bytes), '--restart-while-running',
                       '--stream-interval-ms', str(args.stream_interval_ms),
                       '--stream-event-kind', args.stream_event_kind]
            if args.terminal_barrier:
                command.append('--terminal-barrier')
            if args.carry_stream_events:
                command.append('--carry-stream-events')
            print(f'Running repeat={repeat} backend={backend}', flush=True)
            leg = {'repeat': repeat, 'backend': backend, 'log': str(log), 'output': str(output)}
            with log.open('w') as stream:
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                try:
                    leg['exit_code'] = process.wait(timeout=args.leg_timeout)
                except subprocess.TimeoutExpired:
                    # Kill the private process group, including its scratch daemon.
                    import os
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
                    leg['error'] = 'leg deadline exceeded; incomplete measurements retained'
            if output.exists():
                leg['report'] = json.loads(output.read_text())
            report['legs'].append(leg)
            report['acceptance'] = compare(report['legs'], args.repeats, report['filesystem'])
            args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['acceptance'], indent=2))
    return 0 if report['acceptance']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
