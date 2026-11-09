#!/usr/bin/env python3
"""Production-shaped load test for the native storage authority (TofuDB).

Responsibility: drive the storage traffic Tofu's Agents generate -- Turn
creation, durable-worker dispatch/claim/heartbeat/complete, streamed Turn
events and settlement, browser sync polling, sidebar/status/diagnostic reads,
conversation search and event pruning -- through the production application
storage code against ONE isolated authority, at multiples of a measured
production peak, and report exact per-operation latency percentiles, error
codes and engine-side work (lock waits, CPU, bytes written per change).

Entry:
    python3 scripts/tofudb_load_test.py --root DIR [--scales 1,3,5]
        [--duration 300] [--engine PATH] [--seed-scale 1.0] [--reuse-seed]

Inputs: ``PRODUCTION_PROFILE`` (measured 2026-11-08, sources recorded beside
each value and in docs/STORAGE_PERFORMANCE_CHARTER.md "Production-shaped load
test"), the durable-worker lease contract and an admitted ``tofu-db`` binary.
Outputs: ``DIR/results/<run-id>/report.json`` and ``report.md``.

Safety: DIR is created 0700 and may not lie inside a live Tofu data directory.
The measuring child runs with a scrubbed environment whose data root, HOME and
TMPDIR all live under DIR, so it reads and writes no production state; the
engine binary is pinned by copy and SHA-256. The harness starts the authority
through ``lib.storage.StorageSupervisor`` and stops it on exit. Seed data is
synthetic: same shape as production, no user content.

Dependencies: lib.turn_lifecycle, lib.durable_worker, lib.conversation_sync,
lib.conversations, lib.storage, lib.observability (this checkout's code).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
USER_ID = 1

# Measured on production 2026-11-08; docs/STORAGE_PERFORMANCE_CHARTER.md
# "Production-shaped load test". 1x is the observed peak; a scale multiplies
# concurrency and every rate.
PRODUCTION_PROFILE = {
    # GET /api/v4/conversations?meta=1, all pages, 18:05.
    'conversations': {'chat': 765, 'worker': 401, 'coordinator': 11},
    # Settled history per conversation kind: (median, p90, max) turns.  msgCount
    # counts input and output Turns; coordinators carried 275-700 messages.
    'history_turns': {'chat': (1, 5, 77), 'worker': (1, 5, 30), 'coordinator': (140, 300, 350)},
    # Kinds of the conversations updated on 11-08 (69 worker, 11 chat, 5 coordinator).
    'active_kind_weights': {'worker': 69, 'chat': 11, 'coordinator': 5},
    # Running attempts: 21 tofu-durable executor threads (py-spy 17:42); 23 busy at 18:05.
    'running_attempts': 21,
    # Engine commitStages 18:31-18:41 (calm): turn_event_store 15.3/s over 21 attempts.
    'events_per_attempt_per_s': 15.3 / 21,
    # Engine listener 348 req/s in the same window; the API host issued ~32/s, so the
    # execution host issued ~315/s: ~14/s per running attempt of point bookkeeping
    # reads (in-flight sampling: record.get, turn.get, turn.attempt.get, project and
    # workspace polls). Modelled with the point reads this harness can issue.
    'bookkeeping_reads_per_attempt_per_s': {'turn.get:metadata': 4.0, 'turn.attempt.get': 4.0,
                                            'record.get': 4.0, 'worker_job.claim_state': 2.0},
    # Events per output Turn before settlement (synthetic: production Turns run
    # minutes to hours; this bound lets a 5-minute phase complete Turns).
    'events_per_turn': (60, 240),
    # One tool round every N events; result bytes (median, p90) from inspector
    # exports: toolRounds 50%, segments 32%, apiRounds 12%, timing 4.4% of bytes.
    'tool_round_every_events': 6,
    'tool_result_bytes': (12_000, 120_000),
    'api_round_bytes': 2_400,
    # API-host tofu_storage_rpc_seconds delta 18:31-18:41 (602 s, after the 17:39 herd drained).
    'reads_per_s': {
        'turn.sync.changes': 30.0,
        'conversation.list': 0.045,
        'worker_job.active.list': 0.043,
        'turn.sync.snapshot': 0.020,
        'turn.attempt.get': 0.017,
        # Execution-host tool searches (in-flight 0.06 at ~5 s each).
        'conversation.search': 0.012,
    },
    # sidecar-event-maintenance cadence (event.prune), one sweep per minute.
    'event_prune_per_s': 1 / 60,
}

_TEXT = ('The storage authority commits every streamed frame before the browser '
         'sees it; this synthetic sentence only carries realistic byte volume. ')


def _contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / 'contracts' / name).read_text())


def _lognormal_bytes(rng: random.Random, median: int, p90: int) -> int:
    import math
    sigma = math.log(max(p90, median + 1) / median) / 1.2816
    return max(64, int(median * math.exp(rng.gauss(0.0, sigma))))


def _text(rng: random.Random, size: int) -> str:
    unit = _TEXT * (size // len(_TEXT) + 1)
    start = rng.randrange(len(_TEXT))
    return unit[start:start + size]


# ── parent: isolation ───────────────────────────────────────────────────────

def _refuse_live_root(root: Path) -> None:
    resolved = root.resolve()
    for marker in ('data/tofudb-authority', 'native-storage-connection.json'):
        if (resolved / marker).exists() and not (resolved / '.tofudb-load-test').exists():
            raise SystemExit(f'refusing {resolved}: it looks like a live Tofu data root')
    for parent in [resolved, *resolved.parents]:
        if (parent / 'data' / 'native-storage-connection.json').exists() and parent != resolved:
            raise SystemExit(f'refusing {resolved}: it lies inside a live Tofu checkout ({parent})')


def _pin_engine(engine: Path, root: Path) -> tuple[Path, str]:
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    pinned_dir = root / 'engine' / digest[:16]
    pinned = pinned_dir / 'tofu-db'
    if not pinned.exists():
        pinned_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = pinned_dir / f'.tofu-db.{os.getpid()}'
        shutil.copy2(engine, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise SystemExit('engine binary changed while being pinned')
        temporary.chmod(0o500)
        temporary.replace(pinned)
    return pinned, digest


def _child_environment(root: Path, engine: Path, digest: str, durability: str) -> dict[str, str]:
    keep = ('PATH', 'LANG', 'LC_ALL', 'TERM', 'TZ')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    for name in ('home', 'tmp', 'data', 'project'):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    env.update({
        'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
        'XDG_CACHE_HOME': str(root / 'home' / '.cache'),
        'PYTHONPATH': str(PROJECT_ROOT), 'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1',
        'TOFU_DATA_DIR': str(root / 'data'), 'TOFU_PROJECT_PATH': str(root / 'project'),
        'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb', 'TOFUDB_TRANSPORT': 'daemon',
        'TOFUDB_BIN': str(engine), 'TOFUDB_EXPECTED_SHA256': digest,
        'TOFU_STORAGE_FASTPATH': 'off',
        # Engine settings of the production daemon (its /proc environ, 2026-11-08).
        'TOFU_DB_FSYNC_STATS': '1', 'TOFU_DB_WRITE_OVERLAY': '1', 'TOFU_DB_TOMBSTONE_RECLAIM': '1',
        'TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES': '33554432', 'MALLOC_ARENA_MAX': '8',
    })
    if durability != 'auto':
        # Production's launch probe selected `normal` on dolphinfs; a local
        # volume probes `strict`. Pin the tier so media compare like for like.
        env['TOFU_DB_DURABILITY'] = durability
    return env


# ── child: measurement ──────────────────────────────────────────────────────

class Recorder:
    """Exact latency samples and error codes per logical operation."""

    def __init__(self):
        self._lock = threading.Lock()
        self.samples = collections.defaultdict(list)
        self.errors = collections.defaultdict(collections.Counter)

    def timed(self, op, fn, *args, **kwargs):
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            code = getattr(exc, 'code', '') or type(exc).__name__
            with self._lock:
                self.errors[op][str(code)] += 1
                self.samples[op + ':error'].append(time.monotonic() - started)
            raise
        with self._lock:
            self.samples[op].append(time.monotonic() - started)
        return result

    def summary(self, elapsed: float) -> dict:
        out = {}
        with self._lock:
            for op, values in sorted(self.samples.items()):
                ordered = sorted(values)
                q = lambda p: ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1) + 0.5))]
                out[op] = {'n': len(ordered), 'per_s': round(len(ordered) / elapsed, 3),
                           'p50_ms': round(q(.5) * 1000, 2), 'p90_ms': round(q(.9) * 1000, 2),
                           'p99_ms': round(q(.99) * 1000, 2), 'max_ms': round(ordered[-1] * 1000, 2),
                           'errors': dict(self.errors.get(op.replace(':error', ''), {}))}
        return out


class Harness:
    def __init__(self, args, root: Path):
        self.args = args
        self.root = root
        self.rng = random.Random(args.random_seed)
        self.stop = threading.Event()
        self.recorder = Recorder()
        self.conversations: dict[str, list[str]] = {}
        self.watched: collections.deque = collections.deque(maxlen=4096)
        self.attempts: collections.deque = collections.deque(maxlen=4096)
        self.busy: set[str] = set()
        self.counters = collections.Counter()
        self.counter_lock = threading.Lock()

    # storage lifecycle -------------------------------------------------------
    def start_storage(self):
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage.service import install_runtime_for_test
        supervisor = StorageSupervisor(project_root=self.root / 'project', backend='tofudb',
                                       startup_timeout=float(self.args.startup_timeout))
        self.runtime = StorageRuntime(supervisor, auto_restart=False)
        install_runtime_for_test(self.runtime)
        self.runtime.start()
        status = self.runtime.status()
        self.engine_pid = status.get('pid')
        return status

    def stop_storage(self):
        from lib.storage.service import install_runtime_for_test
        install_runtime_for_test(None)

    def client(self, write=False):
        from lib.storage import get_storage_client
        return get_storage_client(write=write)

    def engine_metrics(self) -> dict:
        return self.client().query('system.metrics', {})

    def engine_proc(self) -> dict:
        pid = self.engine_pid
        out = {}
        try:
            status = Path(f'/proc/{pid}/status').read_text()
            out['rss_bytes'] = int(re.search(r'VmRSS:\s+(\d+)', status).group(1)) * 1024
            out['threads'] = int(re.search(r'Threads:\s+(\d+)', status).group(1))
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            out['cpu_s'] = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
            io = Path(f'/proc/{pid}/io').read_text()
            out['write_bytes'] = int(re.search(r'write_bytes: (\d+)', io).group(1))
            out['wchar'] = int(re.search(r'wchar: (\d+)', io).group(1))
        except (OSError, AttributeError, TypeError):
            pass
        return out

    def bump(self, key, n=1):
        with self.counter_lock:
            self.counters[key] += n

    # projections ------------------------------------------------------------
    def assistant_projection(self, rng: random.Random, rounds: int) -> dict:
        tool_rounds, api_rounds, segments = [], [], []
        for index in range(rounds):
            result = _text(rng, _lognormal_bytes(rng, *PRODUCTION_PROFILE['tool_result_bytes']))
            call_id = f'call_{index}_{rng.getrandbits(32):08x}'
            tool_rounds.append({'id': call_id, 'name': rng.choice(('run_command', 'read_files', 'search_files', 'edit_file')),
                                'arguments': {'command': _text(rng, 160)}, 'result': result, 'status': 'done',
                                'roundNum': index + 1, 'durationMs': rng.randrange(50, 30_000)})
            api_rounds.append({'round': index + 1, 'usage': {'input_tokens': rng.randrange(20_000, 600_000),
                               'output_tokens': rng.randrange(50, 8_000), 'cache_read_tokens': rng.randrange(0, 500_000)},
                               'cost': {'costCny': round(rng.random() * 3, 4)},
                               'route': {'snapshot': _text(rng, PRODUCTION_PROFILE['api_round_bytes'])}})
            segments.append({'type': 'tool', 'toolCallId': call_id})
            segments.append({'type': 'text', 'text': _text(rng, 400)})
        return {'content': _text(rng, rng.randrange(200, 4_000)), 'thinking': _text(rng, rng.randrange(0, 2_000)),
                'toolRounds': tool_rounds, 'apiRounds': api_rounds, 'segments': segments,
                'timingTrace': [{'stage': 'round', 'ms': rng.randrange(1, 90_000)} for _ in range(rounds)],
                'model': 'load-test-model'}

    # seed ----------------------------------------------------------------------
    def seed(self, scale: float) -> dict:
        from lib.turn_lifecycle import append_settled_turn
        rng = random.Random(self.args.random_seed + 1)
        started = time.monotonic()
        totals = collections.Counter()
        for kind, count in PRODUCTION_PROFILE['conversations'].items():
            median, p90, maximum = PRODUCTION_PROFILE['history_turns'][kind]
            ids = []
            for index in range(max(1, int(round(count * scale)))):
                conv_id = (f'{kind}-loadtest-{index:05d}' if kind != 'chat' else f'loadtest{index:05d}')
                turns = min(maximum, max(1, _lognormal_bytes(rng, median, p90) if median > 1 else
                                         (1 if rng.random() < 0.6 else rng.randrange(2, p90 + 1))))
                defaults = {'allowCreate': True, 'title': f'load test {kind} {index}', 'settings': {}}
                for turn in range(turns):
                    self.recorder.timed('seed:turn.append_settled', append_settled_turn, conv_id,
                                        command_id=f'seed:{conv_id}:{turn}:human', actor='human',
                                        projection={'content': _text(rng, rng.randrange(80, 6_000))},
                                        user_id=USER_ID, conversation_defaults=defaults)
                    projection = self.assistant_projection(rng, rng.randrange(0, 12))
                    self.recorder.timed('seed:turn.append_settled', append_settled_turn, conv_id,
                                        command_id=f'seed:{conv_id}:{turn}:assistant', actor='assistant',
                                        projection=projection, user_id=USER_ID, conversation_defaults=defaults)
                    totals['turns'] += 2
                    totals['bytes'] += len(json.dumps(projection))
                ids.append(conv_id)
            self.conversations[kind] = ids
            totals[kind] = len(ids)
        totals['seconds'] = round(time.monotonic() - started, 1)
        (self.root / 'seed.json').write_text(json.dumps({'conversations': self.conversations, 'totals': totals}))
        return dict(totals)

    def seed_records(self):
        rng = random.Random(self.args.random_seed + 2)
        for index in range(256):
            self.recorder.timed('seed:record.put', self.client(write=True).command, 'record.put', {
                'namespace': 'loadtest', 'key': f'config-{index:03d}',
                'value': {'index': index, 'body': _text(rng, 2_048)}}, f'seed:record:{index}')

    def bookkeeping(self, state: dict, rng: random.Random):
        """Execution-host point reads that accompany one running attempt."""
        from lib.durable_worker import StorageWorkerJobStore
        from lib.turn_lifecycle import get_attempt, get_turn
        mix = PRODUCTION_PROFILE['bookkeeping_reads_per_attempt_per_s']
        ops, weights = list(mix), list(mix.values())
        rate = sum(weights)
        store = StorageWorkerJobStore()
        timed = self.recorder.timed
        while not state['done'].wait(rng.expovariate(rate)):
            op = rng.choices(ops, weights=weights)[0]
            try:
                if op == 'turn.get:metadata':
                    timed(op, get_turn, state['conversation_id'], state['turn_id'], user_id=USER_ID,
                          include_projection=False)
                elif op == 'turn.attempt.get':
                    timed(op, get_attempt, state['attempt_id'], user_id=USER_ID)
                elif op == 'record.get':
                    timed(op, self.client().query, 'record.get',
                          {'namespace': 'loadtest', 'key': f'config-{rng.randrange(256):03d}'})
                elif op == 'worker_job.claim_state':
                    timed(op, store.claim_state, state['claim'], now_ms=int(time.time() * 1000))
            except Exception:
                pass

    def load_seed(self) -> dict:
        document = json.loads((self.root / 'seed.json').read_text())
        self.conversations = document['conversations']
        return document['totals']

    # actors ---------------------------------------------------------------------
    def pick_conversation(self, rng):
        weights = PRODUCTION_PROFILE['active_kind_weights']
        kind = rng.choices(list(weights), weights=list(weights.values()))[0]
        return rng.choice(self.conversations[kind])

    def claim_idle_conversation(self, rng):
        """One running Turn per conversation lane, as production admission enforces."""
        for _ in range(64):
            conv_id = self.pick_conversation(rng)
            with self.counter_lock:
                if conv_id not in self.busy:
                    self.busy.add(conv_id)
                    return conv_id
        return None

    def release_conversation(self, conv_id):
        with self.counter_lock:
            self.busy.discard(conv_id)

    def agent(self, index: int, scale: float):
        from lib.durable_worker import StorageWorkerJobStore, WorkerExecutionContext, WorkerJobClaim, WorkerJobOutcome
        from lib.identity import PrincipalContext
        from lib.turn_lifecycle import create_turn_pair, dispatch_attempt_to_worker, mark_task_started, record_task_event
        rng = random.Random(self.args.random_seed * 1000 + index)
        policy = _contract('durable_worker_runtime_v1.json')
        lease_ms, heartbeat_s = policy['workerLeaseMilliseconds'], policy['heartbeatIntervalMilliseconds'] / 1000
        store = StorageWorkerJobStore()
        principal = PrincipalContext.user(subject_id='load-test', owner_user_id=USER_ID,
                                          scopes={'conversation.execute'})
        worker_id = f'load-test/agent-{index}-{uuid.uuid4().hex[:8]}'
        event_gap = 1.0 / PRODUCTION_PROFILE['events_per_attempt_per_s']
        timed = self.recorder.timed
        while not self.stop.is_set():
            conv_id = None
            state = None
            try:
                conv_id = self.claim_idle_conversation(rng)
                if conv_id is None:
                    self.bump('no_idle_conversation')
                    self.stop.wait(1.0)
                    continue
                nonce = uuid.uuid4().hex
                created = timed('turn.create_pair', create_turn_pair, conv_id, command_id=f'load:{nonce}',
                                input_projection={'content': _text(rng, rng.randrange(80, 4_000))},
                                config={'model': 'load-test-model'}, user_id=USER_ID)
                attempt_id = created['attempt']['attemptId']
                self.attempts.append(attempt_id)
                timed('turn.attempt.dispatch_worker', dispatch_attempt_to_worker, principal, attempt_id)
                document = timed('worker_job.claim_next', store.claim_next, worker_id=worker_id,
                                 task_kinds=('conversation-attempt',), now_ms=int(time.time() * 1000), lease_ms=lease_ms)
                if not document:
                    self.bump('claim_empty')
                    conv_id = None  # the queued Turn stays busy until another agent runs it
                    continue
                claim = WorkerJobClaim.from_document(document, expected_worker_id=worker_id)
                job = document['payload']
                attempt_id, task_id = job['attemptId'], claim.task_id
                conv_id = job['conversationId']
                timed('turn.attempt.start', mark_task_started, attempt_id, task_id, user_id=USER_ID,
                      worker_claim={'worker_id': worker_id, 'fencing_token': claim.fencing_token})
                self.watched.append(job['conversationId'])
                state = {'done': threading.Event(), 'conversation_id': job['conversationId'],
                         'turn_id': job['turnId'], 'attempt_id': attempt_id, 'claim': claim}
                poller = threading.Thread(target=self.bookkeeping, args=(state, random.Random(rng.random())),
                                          daemon=True, name=f'load-bookkeeping-{index}')
                poller.start()
                task = {'_attemptId': attempt_id, 'id': task_id, 'status': 'running', '_userId': USER_ID,
                        '_principalContext': principal.to_payload(), 'content': '', 'thinking': '',
                        'toolRounds': [], 'apiRounds': [], 'model': 'load-test-model',
                        'config': {'model': 'load-test-model'},
                        '_conversationWorkerContext': WorkerExecutionContext(claim)}
                total = rng.randrange(*PRODUCTION_PROFILE['events_per_turn'])
                last_heartbeat = time.monotonic()
                for sequence in range(total):
                    if self.stop.is_set():
                        break
                    if sequence and sequence % PRODUCTION_PROFILE['tool_round_every_events'] == 0:
                        round_number = len(task['toolRounds']) + 1
                        task['toolRounds'].append({
                            'id': f'call_{round_number}', 'name': 'run_command',
                            'arguments': {'command': _text(rng, 160)}, 'status': 'done', 'roundNum': round_number,
                            'result': _text(rng, _lognormal_bytes(rng, *PRODUCTION_PROFILE['tool_result_bytes']))})
                        task['apiRounds'].append({'round': round_number, 'usage': {'input_tokens': rng.randrange(20_000, 600_000),
                                                  'output_tokens': rng.randrange(50, 8_000)},
                                                  'route': {'snapshot': _text(rng, PRODUCTION_PROFILE['api_round_bytes'])}})
                        wire = {'type': 'round_end', 'roundNum': round_number, 'reason': 'tool_calls',
                                'seq': sequence}
                    else:
                        chunk = _text(rng, rng.randrange(20, 400))
                        task['content'] += chunk
                        wire = {'type': 'delta', 'content': chunk, 'seq': sequence}
                    outcome = timed('turn.event.record', record_task_event, task, wire,
                                    task_event={'task_id': task_id, 'sequence': sequence, 'event': wire})
                    self.bump('events')
                    self.bump(f'event_outcome:{outcome}')
                    if time.monotonic() - last_heartbeat >= heartbeat_s:
                        timed('worker_job.heartbeat', store.heartbeat, claim, now_ms=int(time.time() * 1000),
                              lease_ms=lease_ms, replay_cursor=sequence)
                        last_heartbeat = time.monotonic()
                    self.stop.wait(rng.expovariate(1.0 / event_gap) / 1.0)
                # The phase stop settles every running Turn at the same moment; that herd
                # queues on this one client process and is not one Turn's settlement latency.
                terminal_op = ('turn.event.record:terminal:phase_stop' if self.stop.is_set()
                               else 'turn.event.record:terminal')
                timed(terminal_op, record_task_event, task,
                      {'type': 'done', 'seq': total}, task_event={'task_id': task_id, 'sequence': total,
                                                                   'event': {'type': 'done', 'seq': total}})
                state['done'].set()
                timed('worker_job.complete', store.complete, claim, now_ms=int(time.time() * 1000),
                      outcome=WorkerJobOutcome(status='succeeded', replay_cursor=total))
                self.bump('turns_completed')
            except Exception as exc:  # recorded by Recorder; keep the actor alive
                self.bump(f'agent_error:{getattr(exc, "code", "") or type(exc).__name__}')
                self.stop.wait(0.5)
            finally:
                if state is not None:
                    state['done'].set()
                if conv_id is not None:
                    self.release_conversation(conv_id)

    def reader(self, op: str, rate: float, index: int):
        from lib.conversation_sync.repository import SidecarConversationSyncRepository
        from lib.conversations.repository import ConversationRepository, search_conversation_page
        from lib.turn_lifecycle import get_attempt
        rng = random.Random(self.args.random_seed * 7919 + index)
        sync = SidecarConversationSyncRepository()
        repository = ConversationRepository()
        cursors: dict[str, int] = {}
        timed = self.recorder.timed
        while not self.stop.wait(rng.expovariate(rate)):
            try:
                if op == 'turn.sync.changes':
                    conv = rng.choice(list(self.watched)) if self.watched else self.pick_conversation(rng)
                    page = timed(op, sync.changes, conv, USER_ID, after_sequence=cursors.get(conv, 0))
                    if isinstance(page, dict):
                        cursors[conv] = int(page.get('syncSequence') or page.get('nextAfter') or cursors.get(conv, 0))
                elif op == 'turn.sync.snapshot':
                    conv = rng.choice(list(self.watched)) if self.watched else self.pick_conversation(rng)
                    timed(op, sync.snapshot, conv, USER_ID, turn_limit=96)
                elif op == 'conversation.list':
                    timed(op, repository.list, user_id=USER_ID, limit=500, include_messages=False, settings_keys=[])
                elif op == 'conversation.search':
                    timed(op, search_conversation_page, rng.choice(('storage authority', 'synthetic sentence',
                                                                    'browser sees', 'volume')), user_id=USER_ID)
                elif op == 'worker_job.active.list':
                    timed(op, self.client().query, 'worker_job.active.list',
                          {'user_id': USER_ID, 'task_kinds': ['conversation-attempt']})
                elif op == 'turn.attempt.get' and self.attempts:
                    timed(op, get_attempt, rng.choice(list(self.attempts)), user_id=USER_ID)
            except Exception:
                pass

    def maintenance(self, rate: float):
        while not self.stop.wait(1.0 / rate):
            try:
                self.recorder.timed('event.prune', self.client(write=True).command, 'event.prune', {
                    'created_before_ms': int(time.time() * 1000) - 3_600_000, 'limit': 500,
                    'legacy_recovery_limit': 64, 'retention_class': 'streaming'}, None,
                    priority='maintenance', deadline=60)
            except Exception:
                pass

    # phases -----------------------------------------------------------------------
    def run_phase(self, scale: float, duration: float) -> dict:
        self.recorder = Recorder()
        self.counters = collections.Counter()
        self.stop.clear()
        threads = []
        agents = max(1, int(round(PRODUCTION_PROFILE['running_attempts'] * scale)))
        for index in range(agents):
            threads.append(threading.Thread(target=self.agent, args=(index, scale), daemon=True,
                                            name=f'load-agent-{index}'))
        index = 0
        for op, rate in PRODUCTION_PROFILE['reads_per_s'].items():
            total = rate * scale
            workers = max(1, int(round(total / 2.0)))  # each reader thread issues ~2/s at most on average
            for _ in range(workers):
                threads.append(threading.Thread(target=self.reader, args=(op, total / workers, index),
                                                daemon=True, name=f'load-read-{op}-{index}'))
                index += 1
        threads.append(threading.Thread(target=self.maintenance, args=(PRODUCTION_PROFILE['event_prune_per_s'] * scale,),
                                        daemon=True, name='load-maintenance'))
        metrics_before, proc_before = self.engine_metrics(), self.engine_proc()
        client_before = os.times()
        started = time.monotonic()
        for thread in threads:
            thread.start()
        self.stop.wait(duration)
        client_threads = threading.active_count()
        self.stop.set()
        for thread in threads:
            thread.join(timeout=120)
        elapsed = time.monotonic() - started
        client_after = os.times()
        metrics_after, proc_after = self.engine_metrics(), self.engine_proc()
        return {'scale': scale, 'agents': agents, 'elapsed_s': round(elapsed, 1),
                'wal_durability': metrics_after.get('walDurability'),
                # One Python process issues all client traffic; near 1 core it is GIL-bound.
                'client_cpu_cores': round((client_after.user + client_after.system
                                           - client_before.user - client_before.system) / elapsed, 2),
                'client_threads': client_threads,
                'operations': self.recorder.summary(elapsed), 'counters': dict(self.counters),
                'engine': _engine_delta(metrics_before, metrics_after, proc_before, proc_after, elapsed,
                                        changes=self.counters['events'] + self.counters['turns_completed']),
                'stragglers': sum(1 for thread in threads if thread.is_alive())}


def _engine_delta(before, after, proc_before, proc_after, elapsed, *, changes) -> dict:
    def stages(m):
        return {s['stage']: (s.get('calls', 0), s.get('totalNanos', 0)) for s in m.get('commitStages', [])}
    a, b = stages(before), stages(after)
    selected = {}
    for stage in ('authority_wait', 'command_execute', 'wal', 'wal_barrier_sync', 'wal_barrier_wait',
                  'ir_steps', 'turn_event_store', 'turn_event_load', 'projection_head_materialize',
                  'projection_head_checkpoint', 'parallel_prepare', 'engine_commit', 'block_get'):
        calls = b.get(stage, (0, 0))[0] - a.get(stage, (0, 0))[0]
        nanos = b.get(stage, (0, 0))[1] - a.get(stage, (0, 0))[1]
        if calls:
            selected[stage] = {'calls': calls, 'avg_ms': round(nanos / calls / 1e6, 3),
                               'thread_eq': round(nanos / 1e9 / elapsed, 3)}
    durability = {key: after['durability'][key] - before['durability'].get(key, 0)
                  for key in ('walEncodedBytes', 'projectionCheckpointBytes', 'overlayMaterializedBytes')
                  if key in after.get('durability', {})}
    listener = {key: after['listener'][key] - before['listener'].get(key, 0)
                for key in ('requestFrames', 'acceptedConnections') if key in after.get('listener', {})}
    listener['peakActiveConnections'] = after.get('listener', {}).get('peakActiveConnections')
    proc = {}
    for key in ('cpu_s', 'write_bytes', 'wchar'):
        if key in proc_before and key in proc_after:
            proc[key] = proc_after[key] - proc_before[key]
    proc['rss_bytes'] = proc_after.get('rss_bytes')
    if changes and 'write_bytes' in proc:
        proc['write_bytes_per_change'] = int(proc['write_bytes'] / changes)
    if changes and 'walEncodedBytes' in durability:
        proc['wal_bytes_per_change'] = int(durability['walEncodedBytes'] / changes)
    return {'stages': selected, 'durability': durability, 'listener': listener, 'process': proc,
            'cpu_cores': round(proc.get('cpu_s', 0) / elapsed, 2), 'growth': _growth_delta(before, after)}


# Persistent growth and its reclamation over a phase (docs/STORAGE_OPERATIONS.md):
# counters are deltas, the listed gauges are the values at the phase's end.
_GROWTH_COUNTERS = {
    'onlineGarbageCollector': ('epochs_completed', 'failed_epochs', 'total_reclaimed_blocks',
                               'total_reclaimed_bytes', 'total_redundant_copies_removed',
                               'total_compacted_blocks', 'total_compaction_segments',
                               'total_segments_rewritten', 'total_segments_retired',
                               'total_consolidations', 'total_orphan_segments_removed'),
    'maintenance': ('historyCompactions', 'retiredHistorySegments', 'publishedBackgroundPacks'),
    'historyPromotion': ('preparedPacks', 'catalogReplacements', 'packedSourceBytes', 'attemptedLooseBlocks'),
    'backgroundPacker': ('packedSegments', 'packedBytes', 'discardedSegments', 'retiredWals'),
}
_GROWTH_GAUGES = {
    'onlineGarbageCollector': ('epoch_error', 'last_epoch_duration_ms'),
    'maintenance': ('retainedHistorySegments',),
}


def _growth_delta(before, after) -> dict:
    growth = {}
    for section, keys in _GROWTH_COUNTERS.items():
        old, new = before.get(section) or {}, after.get(section) or {}
        growth[section] = {key: new[key] - old.get(key, 0) for key in keys
                           if isinstance(new.get(key), (int, float))}
    for section, keys in _GROWTH_GAUGES.items():
        new = after.get(section) or {}
        growth.setdefault(section, {}).update({key: new.get(key) for key in keys if key in new})
    return growth


def _markdown(report: dict) -> str:
    lines = [f"# TofuDB production-shaped load test {report['run_id']}", '',
             f"engine sha256 `{report['engine_sha256'][:16]}`, root `{report['root']}`, seed {json.dumps(report['seed'])}", '']
    for phase in report['phases']:
        lines += [f"## {phase['scale']}x  ({phase['agents']} agents, {phase['elapsed_s']} s, "
                  f"WAL {phase.get('wal_durability')})", '',
                  '| operation | n | per s | p50 ms | p90 ms | p99 ms | max ms | errors |',
                  '|---|---:|---:|---:|---:|---:|---:|---|']
        for op, s in phase['operations'].items():
            lines.append(f"| {op} | {s['n']} | {s['per_s']} | {s['p50_ms']} | {s['p90_ms']} | {s['p99_ms']} | "
                         f"{s['max_ms']} | {s['errors'] or ''} |")
        engine = phase['engine']
        lines += ['', f"counters {json.dumps(phase['counters'])}",
                  f"client cpu {phase.get('client_cpu_cores')} cores, {phase.get('client_threads')} threads",
                  f"engine cpu {engine['cpu_cores']} cores; "
                  f"process {json.dumps(engine['process'])}; durability {json.dumps(engine['durability'])}; "
                  f"listener {json.dumps(engine['listener'])}", '', '| engine stage | calls | avg ms | thread-eq |',
                  '|---|---:|---:|---:|']
        for stage, s in engine['stages'].items():
            lines.append(f"| {stage} | {s['calls']} | {s['avg_ms']} | {s['thread_eq']} |")
        lines.append('')
    return '\n'.join(lines) + '\n'


def child_main(args) -> int:
    root = Path(args.root).resolve()
    harness = Harness(args, root)
    report = {'run_id': args.run_id, 'root': str(root), 'engine_sha256': os.environ['TOFUDB_EXPECTED_SHA256'],
              'profile': PRODUCTION_PROFILE, 'phases': []}
    from serverctl_pkg.native_engine import EngineAdmissionRefusedError
    try:
        status = harness.start_storage()
    except EngineAdmissionRefusedError as exc:
        raise SystemExit(f'{exc}\nThe engine binary must match this checkout\'s engine source: run the harness from '
                         f'a checkout (or `git archive <commit>` export) of the commit the binary was built from.')
    report['storage_status'] = {k: status.get(k) for k in ('backend', 'pid', 'port', 'state')}
    try:
        if args.reuse_seed and (root / 'seed.json').exists():
            report['seed'] = harness.load_seed()
        else:
            report['seed'] = harness.seed(args.seed_scale)
            harness.seed_records()
            report['seed_operations'] = harness.recorder.summary(max(report['seed']['seconds'], 1e-3))
        print(f"seed ready: {json.dumps(report['seed'])}", flush=True)
        for scale in [float(s) for s in args.scales.split(',')]:
            phase = harness.run_phase(scale, args.duration)
            report['phases'].append(phase)
            print(f"phase {scale}x done: events={phase['counters'].get('events', 0)} "
                  f"turns={phase['counters'].get('turns_completed', 0)}", flush=True)
            out = root / 'results' / args.run_id
            out.mkdir(parents=True, exist_ok=True, mode=0o700)
            (out / 'report.json').write_text(json.dumps(report, indent=1))
            (out / 'report.md').write_text(_markdown(report))
    finally:
        harness.stop_storage()
    print(f"report: {root / 'results' / args.run_id / 'report.md'}", flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', required=True, help='private working directory (created 0700)')
    parser.add_argument('--engine', default=os.environ.get('TOFUDB_BIN', ''),
                        help='tofu-db binary to pin (default: $TOFUDB_BIN)')
    parser.add_argument('--scales', default='1,3,5')
    parser.add_argument('--duration', type=float, default=300.0, help='seconds per scale')
    parser.add_argument('--seed-scale', type=float, default=1.0,
                        help='fraction of the production conversation census to seed')
    parser.add_argument('--reuse-seed', action='store_true', help='reuse DIR/seed.json and its authority')
    parser.add_argument('--random-seed', type=int, default=20261108)
    parser.add_argument('--startup-timeout', type=float, default=600.0)
    parser.add_argument('--durability', choices=('normal', 'strict', 'auto'), default='normal',
                        help="WAL tier; production runs 'normal' (default), 'auto' keeps the launch probe")
    parser.add_argument('--run-id', default='')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args)
    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    _refuse_live_root(root)
    (root / '.tofudb-load-test').touch()
    if not args.engine or not Path(args.engine).is_file():
        raise SystemExit('--engine (or $TOFUDB_BIN) must name a tofu-db binary')
    engine, digest = _pin_engine(Path(args.engine), root)
    run_id = args.run_id or time.strftime('%Y%m%d-%H%M%S')
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--root', str(root.resolve()),
               '--scales', args.scales, '--duration', str(args.duration), '--seed-scale', str(args.seed_scale),
               '--random-seed', str(args.random_seed), '--startup-timeout', str(args.startup_timeout),
               '--run-id', run_id, '--engine', str(engine)] + (['--reuse-seed'] if args.reuse_seed else [])
    return subprocess.run(command, env=_child_environment(root.resolve(), engine, digest, args.durability),
                          cwd=str(root.resolve())).returncode


if __name__ == '__main__':
    sys.exit(main())
