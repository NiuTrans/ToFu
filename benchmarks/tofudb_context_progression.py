"""Measure complete context-frontier advancement through production authorities.

Each fresh process owns a disposable store. Initial certification, appends,
suffix reads and subsequent checkpoint writes have separate clocks. An
independent public-header digest verifies every read outside its request clock.
The SQLite arm uses the existing SQL context algorithm; this is a production
operation comparison, not an equal-algorithm physical-engine comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.tofudb_context_read import fixture, history_proof
from benchmarks.tofudb_context_controls import source_snapshot
from lib.storage.model_continuation import settled_witness
from scripts.tofudb_bench import _mount_type
from scripts.tofudb_perf_compare import (
    LegacyEngine, TofuDbEngine, _operation_runtime_evidence,
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def unwrap(value):
    if isinstance(value, dict) and '_storageCommitContract' in value:
        if value['_storageCommitContract'] != 'storage.committed-events/v1':
            raise ValueError('unknown committed response contract')
        return value['value']
    return value


class TimedSetup:
    def __init__(self, engine):
        self.engine = engine
        self.initial_checkpoint = []

    def execute(self, operation, payload, command):
        started = time.perf_counter_ns()
        value = self.engine.execute(operation, payload, command)
        elapsed = time.perf_counter_ns() - started
        if operation == 'model_continuation.put':
            self.initial_checkpoint.append(elapsed)
        return value


def verify_suffix(value, appended, checkpoint, covered):
    if (value.get('supported') is not True
            or value.get('continuationStatus') != 'available'
            or value.get('covered') != covered
            or value.get('nextTurnId') is not None
            or value.get('proofEligible') is not True
            or value.get('continuation') != checkpoint
            or value.get('historyProof') != history_proof(appended)
            or value.get('turns') != appended[covered:]):
        raise ValueError('context differs from exact public history and checkpoint')


def append_pair(engine, base, appended, expected):
    elapsed = 0
    for _ in range(2):
        rank = len(appended)
        turn_id = f'context-turn-{rank:06d}'
        projection = {
            'content': f'new work {rank}: 中文 \\ "',
            'toolRounds': [{'toolName': 'read', 'toolContent': f'evidence-{rank}'}],
        }
        request = base | {
            'turn_id': turn_id, 'command_id': f'append-{rank}',
            'actor': 'assistant' if rank % 2 else 'human',
            'projection': projection,
            'created_at': 1_800_000_000_000 + rank,
            'now': 1_800_000_000_000 + rank,
        }
        started = time.perf_counter_ns()
        result = unwrap(engine.execute('turn.append_settled', request, True))
        elapsed += time.perf_counter_ns() - started
        row = result['turn']
        if row['turnId'] != turn_id or row['projection'] != projection:
            raise ValueError('append returned different work')
        appended.append(row)
        expected.append((turn_id, projection))
    return elapsed


def next_checkpoint(appended, value):
    return {
        'turns': [settled_witness(appended[-1])],
        'historyProof': value['historyProof'], 'policy': 'context-progression-v1',
        'messages': [{'role': 'assistant',
                      'content': f'retained prefix through {len(appended)} turns'}],
    }


def source_identity():
    manifest = source_snapshot()['files']
    owners = [
        'benchmarks/tofudb_context_progression.py',
        'contracts/model_context_v1.schema.json',
    ]
    manifest.update({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                     for name in owners})
    return manifest


def measure(args):
    engine = (TofuDbEngine(args.binary, client_mode='embedded',
                          authority_dir_parent=args.directory)
              if args.engine == 'tofudb' else
              LegacyEngine('sqlite', authority_dir_parent=args.directory))
    sources = source_identity()
    artifacts = {}
    if args.engine == 'tofudb':
        artifacts = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (args.binary, args.binary.parent / 'libtofu_db.so')}
    report = {
        'schema': 'tofu.context-progression.v1', 'engine': args.engine,
        'history': args.history, 'rounds': args.rounds,
        'payload_bytes': args.payload_bytes, 'pid': os.getpid(),
        'python': sys.version, 'sqlite': sqlite3.sqlite_version,
        'platform': platform.platform(),
        'authority_parent': str(args.directory or Path(tempfile.gettempdir())),
        'filesystem': _mount_type(args.directory or Path(tempfile.gettempdir())),
        'affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
        'scope': 'native Strict embedded versus existing SQLite WAL FULL sidecar; storage operations only',
        'source_sha256': sources, 'artifacts_sha256': artifacts,
        'rows': [],
    }
    started = time.perf_counter_ns()
    engine.start()
    report['open_ns'] = time.perf_counter_ns() - started
    try:
        report['runtime_before'] = _operation_runtime_evidence(args.engine, engine)
        if args.engine == 'tofudb':
            if (report['runtime_before']['wal_durability'] != 'strict'
                    or report['runtime_before']['profiling'] is not False):
                raise ValueError('native measurement requires Strict durability and profiling off')
        elif (report['runtime_before']['synchronous'] != 2
              or report['runtime_before']['fastpath_active'] is not False):
            raise ValueError('SQLite measurement requires the pinned FULL writer without relocation')
        if args.engine == 'tofudb':
            identity = engine._embedded.build_identity()
            if identity.get('source_sha256') != engine._build_identity.get('source_sha256'):
                raise ValueError('executable and library source identities differ')
            report['build_identity'] = identity
        setup = TimedSetup(engine)
        started = time.perf_counter_ns()
        payload, expected, checkpoint, appended = fixture(
            setup, args.history, args.payload_bytes, 'escaped')
        report['setup_ns'] = time.perf_counter_ns() - started
        if [(row['turnId'], row['projection']) for row in appended] != expected:
            raise ValueError('initial appends differ from exact input projections')
        if len(setup.initial_checkpoint) != 1:
            raise ValueError('fixture must certify exactly one initial checkpoint')
        report['initial_checkpoint_ns'] = setup.initial_checkpoint[0]
        report['initial_fixture_sha256'] = digest(expected)
        covered = args.history
        base = {'conversation_id': payload['conversation_id'], 'user_id': 11}
        for index in range(args.rounds):
            append_ns = append_pair(engine, base, appended, expected) if index else 0
            payload['turn_id'] = appended[-1]['turnId']
            started = time.perf_counter_ns()
            value = engine.execute('turn.context.read', payload | {'replay': True}, False)
            read_ns = time.perf_counter_ns() - started
            verify_suffix(value, appended, checkpoint, covered)
            checkpoint = next_checkpoint(appended, value)
            started = time.perf_counter_ns()
            stored = engine.execute('model_continuation.put', base | {
                'lane_id': 'main', 'projection': checkpoint,
            }, True)
            put_ns = time.perf_counter_ns() - started
            if stored is not True:
                raise ValueError('later checkpoint was not accepted')
            actual = engine.execute('model_continuation.get', base | {'lane_id': 'main'}, False)
            if actual != checkpoint:
                raise ValueError('durable checkpoint differs from candidate')
            report['rows'].append({
                'round': index, 'history_count': len(appended),
                'covered': covered, 'returned_turns': len(value['turns']),
                'append_ns': append_ns if index else None,
                'read_ns': read_ns, 'checkpoint_ns': put_ns,
                'advance_ns': read_ns + put_ns,
                'progress_ns': append_ns + read_ns + put_ns if index else None,
                'history_proof': value['historyProof'],
                'suffix_projection_sha256': digest(expected[covered:]),
            })
            covered = len(appended)
        # The private certificate is deliberately bound to one authority open.
        # Measure its real cold costs on this same populated database, apart
        # from warm progression and initial empty-database startup.
        cold_append_ns = append_pair(engine, base, appended, expected)
        payload['turn_id'] = appended[-1]['turnId']
        shutdown_ms, reopen_ms = engine.restart_cycle()
        started = time.perf_counter_ns()
        value = engine.execute('turn.context.read', payload | {'replay': True}, False)
        cold_read_ns = time.perf_counter_ns() - started
        verify_suffix(value, appended, checkpoint, covered)
        checkpoint = next_checkpoint(appended, value)
        started = time.perf_counter_ns()
        stored = engine.execute('model_continuation.put', base | {
            'lane_id': 'main', 'projection': checkpoint,
        }, True)
        cold_put_ns = time.perf_counter_ns() - started
        if stored is not True or engine.execute('model_continuation.get', base | {
                'lane_id': 'main'}, False) != checkpoint:
            raise ValueError('reopened checkpoint advancement differs')
        report['reopen_control'] = {
            'history_count': len(appended), 'covered': covered,
            'returned_turns': len(value['turns']), 'append_ns': cold_append_ns,
            'shutdown_ns': shutdown_ms * 1e6, 'reopen_ns': reopen_ms * 1e6,
            'first_read_ns': cold_read_ns, 'first_checkpoint_ns': cold_put_ns,
            'history_proof': value['historyProof'], 'verified': True,
            'scope': 'same populated store, graceful close/open, separately from warm rounds',
        }
        report['final_fixture_sha256'] = digest(expected)
        # Both real ingesters mint attempt UUIDs. Exact UUIDs are verified on
        # every response above; only the cross-process digest substitutes ranks.
        normalized = [row | {
            'currentAttemptId': f'attempt-{index}' if row['currentAttemptId'] else None,
        } for index, row in enumerate(appended)]
        report['logical_turns_sha256'] = digest(normalized)
        report['summary'] = {key: statistics.median(row[key] for row in report['rows'])
                             for key in ('read_ns', 'checkpoint_ns', 'advance_ns')}
        report['summary'].update({
            key: statistics.median(row[key] for row in report['rows'][1:])
            if len(report['rows']) > 1 else None
            for key in ('append_ns', 'progress_ns')
        })
        report['runtime_after'] = _operation_runtime_evidence(args.engine, engine)
        if report['runtime_after'] != report['runtime_before']:
            raise ValueError('runtime durability or profiling changed during measurement')
        if source_identity() != sources:
            raise ValueError('measurement sources changed during run')
        if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != value
               for path, value in artifacts.items()):
            raise ValueError('native artifacts changed during run')
        report['verified_rounds'] = len(report['rows'])
        return report
    finally:
        started = time.perf_counter_ns()
        engine.stop()
        report['close_ns'] = time.perf_counter_ns() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('tofudb', 'sqlite'), required=True)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--history', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=16)
    parser.add_argument('--payload-bytes', type=int, default=64)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if (args.history < 2 or args.history % 2 or not 1 <= args.rounds <= 1000
            or args.history + 2 * args.rounds + 2 > 10000
            or not 0 <= args.payload_bytes <= 4096):
        parser.error('require even positive history, 1..1000 rounds, total including reopen pair <=10000 and 0..4096 payload bytes')
    if args.engine == 'tofudb':
        if args.binary is None:
            parser.error('native measurement requires --binary')
        args.binary = args.binary.resolve(strict=True)
    if args.output.exists():
        parser.error('output already exists; preserve earlier observations')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = measure(args)
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'verified_rounds': result['verified_rounds'],
                      'summary': result['summary']}, sort_keys=True))


if __name__ == '__main__':
    main()
