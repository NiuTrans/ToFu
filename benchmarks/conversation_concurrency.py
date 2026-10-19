"""Concurrent live attempts on a disposable production storage authority, never user data.

Entry: CLI below. Uses the production executor, storage client, and Turn facade;
provider latency is an explicit barrier, not a paid model. All attempts must be
running together before streaming/settlement. Reports failure, latency, memory,
and restart readback independently. This is not a real-provider capacity claim.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import functools
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess as subprocess
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.tofudb_perf_compare import _OWNER_ID
from benchmarks.conversation_authority import ConversationAuthority
from benchmarks.conversation_acceptance import POLICY, assess
from benchmarks.conversation_provenance import FORMAT as PROVENANCE_FORMAT, capture_sources
from scripts.tofudb_bench import _mount_type


def record_readiness_probe(health, reported, *, phase, elapsed_seconds, consecutive_unhealthy=0):
    """Retain bounded phase evidence; every unhealthy probe still fails acceptance."""
    health['probes'] += 1
    health['max_contended_milliseconds'] = max(health['max_contended_milliseconds'],
        int(reported.get('authorityContendedMilliseconds') or 0))
    if reported.get('ready'):
        return 0
    consecutive_unhealthy += 1
    health['unhealthy_probes'] += 1
    if len(health['failure_examples']) < 8:
        health['failure_examples'].append({
            'phase': phase, 'elapsed_seconds': round(elapsed_seconds, 3),
            'components': reported.get('components'),
            'authorityContendedMilliseconds': reported.get('authorityContendedMilliseconds'),
            'probe_error': reported.get('probe_error'),
        })
    health['max_consecutive_unhealthy'] = max(health['max_consecutive_unhealthy'], consecutive_unhealthy)
    return consecutive_unhealthy


def latency_summary(values):
    ordered = sorted(values)
    return {'count': len(values), 'p95': ordered[min(len(ordered)-1, int(len(ordered)*.95))], 'total_ms': sum(values),
            'p50': ordered[len(ordered) // 2],
            'p99': ordered[min(len(ordered) - 1, int(len(ordered) * .99))],
            'max': ordered[-1]}


@contextmanager
def profile_client_stages(enabled, report):
    """Opt-in aggregate timings; scoped patches, no payloads or retained samples.

    The workload bounds conversations/rounds. Each histogram uses a fixed
    1 ms -> 10 minute logarithmic bucket set instead of one allocation per RPC.
    Stage percentiles are upper bucket edges; workload percentiles remain exact.
    """
    if not enabled:
        yield
        return
    import math
    from lib.storage import v2_client
    local = threading.local()
    lock = threading.Lock()
    histograms = {}
    with ExitStack() as patches:
        def instrument(target, name, stage, operation=False):
            original = getattr(target, name)
            @functools.wraps(original)
            def measured(self, *args, **kwargs):
                previous = getattr(local, 'operation', 'unscoped')
                if operation:
                    local.operation = args[1] if name == '_call' else args[0]
                label = stage + ':' + getattr(local, 'operation', 'unscoped')
                start = time.monotonic()
                try:
                    return original(self, *args, **kwargs)
                finally:
                    elapsed = (time.monotonic() - start) * 1000
                    bucket = min(140, max(0, math.ceil(math.log(max(1, elapsed), 1.1))))
                    with lock:
                        item = histograms.setdefault(label, {'buckets': [0] * 141,
                            'count': 0, 'total_ms': 0, 'max': 0})
                        item['buckets'][bucket] += 1
                        item['count'] += 1
                        item['total_ms'] += elapsed
                        item['max'] = max(item['max'], elapsed)
                    local.operation = previous
            patches.enter_context(patch.object(target, name, measured))
        instrument(v2_client.StorageV2Client, '_call', 'rpc', True)
        instrument(v2_client.StorageV2Client, '_attempt', 'attempt', True)
        instrument(v2_client._SessionPool, 'acquire', 'pool')
        instrument(v2_client.StorageV2Session, 'request', 'wire', True)
        try:
            yield
        finally:
            # A failed workload may still be draining an in-flight call.
            # Freeze aggregates under the same lock as completion recording.
            with lock:
                frozen = {label: {**item, 'buckets': item['buckets'][:]}
                          for label, item in histograms.items()}
            for label, item in frozen.items():
                cumulative = 0
                summary = {key: item[key] for key in ('count', 'total_ms', 'max')}
                for index, count in enumerate(item['buckets']):
                    cumulative += count
                    for name, percentile in [('p50', .5), ('p99', .99)]:
                        if name not in summary and cumulative >= math.ceil(item['count'] * percentile):
                            summary[name] = min(item['max'], 1.1 ** index)
                report[label] = summary


def capture_storage_metrics(engine, *, incarnation, boundary, quiescent):
    """Bind one metrics snapshot to its live process and authority incarnation.

    Quiescence describes the benchmark's foreground callers; background work
    remains observable. A metrics RPC must not silently cross a process restart.
    """
    import psutil

    pid = engine.pid
    process_started = psutil.Process(pid).create_time()
    observed_start = time.monotonic()
    metrics = engine._client.metrics(deadline=2.0)
    observed_finish = time.monotonic()
    if engine.pid != pid or psutil.Process(pid).create_time() != process_started:
        raise RuntimeError('storage metrics crossed an authority process restart')
    return metrics, {
        'authority_incarnation': incarnation,
        'process_id': pid,
        'process_started_unix_seconds': process_started,
        'boundary': boundary,
        'benchmark_calls_quiescent': quiescent,
        'observed_monotonic_seconds': [observed_start, observed_finish],
    }


def measure(binary: Path | None, directory: Path, conversations: int, rounds: int,
            timeout: float = 300, chunk_bytes: int = 0,
            restart_while_running: bool = False, recover_interrupted: bool = False,
            profile_client: bool = False, backend: str = 'tofudb',
            stream_interval_ms: float = 1000, native_client: str = "application",
            stream_event_kind: str = "phase", carry_stream_events: bool = False, terminal_barrier: bool = False,
            on_stream_scheduled=None) -> dict:
    import psutil
    from lib.agent_core.worker_executor import RecoverableAgentExecutor
    from lib import turn_lifecycle as turns

    sources_before = capture_sources()
    engine = ConversationAuthority(backend, binary, directory, native_client)
    release = threading.Event()
    stop_work = threading.Event()
    condition = threading.Condition()
    active = 0
    peak_active = 0
    latencies: dict[str, list[float]] = {}
    stage_failures: dict[str, int] = {}
    submitted_at = {}
    released_at = 0.0
    client_stages = {}
    authority_incarnation = 0
    errors: list[str] = []
    attempts = []
    sampling_stop = threading.Event()
    restarting = threading.Event()
    health = {'probes': 0, 'unhealthy_probes': 0, 'max_consecutive_unhealthy': 0,
              'max_contended_milliseconds': 0, 'failure_examples': []}
    def observe_metrics(key, boundary, *, quiescent):
        metrics, provenance = capture_storage_metrics(
            engine, incarnation=authority_incarnation,
            boundary=boundary, quiescent=quiescent)
        result[key] = metrics
        result.setdefault('storage_metrics_provenance', {})[key] = provenance

    def observe_terminal_boundary():
        # Queued stream callbacks may still be running. This is submission's
        # boundary before settlement, not a quiescent completed-stream window.
        observe_metrics('terminal_boundary_storage_metrics',
                        'stream-submission-before-settlement', quiescent=False)
    terminal_gate = (threading.Barrier(conversations, action=observe_terminal_boundary, timeout=timeout)
                     if terminal_barrier else None)
    started = time.monotonic()
    result = {
        'format': 'tofu.conversation-concurrency/v2',
        'backend': backend, 'restart_while_running': restart_while_running,
        # Requested launch input; system.metrics reports the effective setting.
        'requested_native_profile_every': os.environ.get('TOFU_DB_REQUEST_PROFILE_EVERY', '0')
            if backend == 'tofudb' else None,
        'requested_client_profile': profile_client,
        'stream_interval_ms': stream_interval_ms,
        'stream_event_kind': stream_event_kind, 'carry_stream_events': carry_stream_events, 'terminal_barrier': terminal_barrier,
        'rpc_deadline_seconds': POLICY['rpc_deadline_seconds'],
        'arrival_model': 'fixed-schedule-per-conversation; queue delay included even when prior events are late',
        'benchmark_source_sha256': sources_before['files']['benchmarks/conversation_concurrency.py'],
        'scope': 'production-executor-and-durable-turns-with-barrier-provider',
        'conversations': conversations, 'rounds': rounds,
        'chunk_bytes': chunk_bytes, 'recover_interrupted': recover_interrupted,
        'filesystem': _mount_type(directory), 'durability': 'strict',
        'binary_sha256': None,
        'provenance': {'format': PROVENANCE_FORMAT, 'sources_before': sources_before},
        'phase_ms': {},
        'phase_ms_scope': {
            'productive': 'setup, admission, running recovery and completed event work',
            'events': 'provider release through joined stream and settlement callers; excludes endpoint metrics, includes terminal submission barrier metrics',
        },
        'resource_observation_scope': 'productive workload and recovery readback; excludes offline verification and cleanup',
    }

    def observe(label, start):
        with condition:
            latencies.setdefault(label, []).append((time.monotonic() - start) * 1000)

    def timed(label, function, *args, scheduled_at=None, **kwargs):
        start = time.monotonic()
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            with condition:
                stage_failures[label] = stage_failures.get(label, 0) + 1
            exc.add_note(f'benchmark stage={label}')
            raise
        finally:
            # Failed and timed-out requests remain in latency distributions.
            observe(label, start)
            if scheduled_at is not None:
                observe(label + '_scheduled', scheduled_at)

    def crash_cycle():
        nonlocal authority_incarnation
        # No worker uses the client at either barrier. Close its idle sessions
        # before replacing it, without asking the daemon to flush or stop.
        restarting.set()
        try:
            closer = getattr(engine._client, 'close', None)
            if closer:
                closer()
            elapsed = engine.crash_cycle()
            authority_incarnation += 1
            return elapsed
        finally:
            restarting.clear()

    def stream_chunk(index, sequence):
        return (hashlib.shake_256(f'{index}:{sequence}'.encode())
                .hexdigest((chunk_bytes + 1) // 2)[:chunk_bytes]
                if chunk_bytes else f'chunk {sequence}\n')

    def run(index):
        nonlocal active, peak_active
        conv = f'concurrency-{index}'
        task_id = f'task-{index}'
        try:
            pair = timed('accept', turns.create_turn_pair, conv,
                command_id=f'command-{index}', user_id=_OWNER_ID,
                input_projection={'content': 'Synthetic concurrency probe'},
                config={'model': 'benchmark'},
                conversation_defaults={'allowCreate': True, 'title': conv, 'settings': {}})
            attempt = pair['attempt']['attemptId']
            timed('bind', turns.bind_task, attempt, task_id, user_id=_OWNER_ID)
            timed('start', turns.mark_task_started, attempt, task_id, user_id=_OWNER_ID)
            observe('submit_to_running', submitted_at[index])
            with condition:
                attempts.append((conv, pair['turn']['turnId'], attempt, index))
                active += 1
                peak_active = max(peak_active, active)
                condition.notify_all()
            if not release.wait(timeout):
                raise TimeoutError('provider barrier did not release')
            if stop_work.is_set():
                return
            task = {'id': task_id, 'convId': conv, '_attemptId': attempt,
                    '_userId': _OWNER_ID, 'status': 'running', 'content': '',
                    'thinking': '', 'toolRounds': [], 'segments': [],
                    'model': 'benchmark', 'config': {'model': 'benchmark'},
                    'created_at': time.time(), 'abort_event': threading.Event()}
            for sequence in range(rounds):
                due = released_at + sequence * stream_interval_ms / 1000
                if stop_work.wait(max(0, due - time.monotonic())):
                    return
                chunk = stream_chunk(index, sequence)
                task['content'] += chunk
                event = ({'type': 'delta', 'content': chunk}
                         if stream_event_kind == 'delta' else {'type': 'phase', 'phase': 'text'})
                carrier = ({'task_id': task_id, 'sequence': sequence + 1, 'event': event}
                           if carry_stream_events else None)
                if on_stream_scheduled is not None:
                    on_stream_scheduled(task_id, sequence + 1, due)
                outcome = timed('stream', turns.record_task_event, task,
                                event, task_event=carrier, scheduled_at=due)
                if carry_stream_events and outcome != 'carried':
                    raise RuntimeError(f'frame {sequence + 1} was not atomically carried: {outcome}')
            if terminal_gate is not None:
                terminal_gate.wait()
            task.update(status='done', finishReason='stop')
            terminal = {'type': 'done', 'finishReason': 'stop'}
            carrier = ({'task_id': task_id, 'sequence': rounds + 1, 'event': terminal}
                       if carry_stream_events else None)
            outcome = timed('settle', turns.record_task_event, task, terminal, task_event=carrier,
                            scheduled_at=released_at + (rounds - 1) * stream_interval_ms / 1000)
            if carry_stream_events and outcome != 'carried':
                raise RuntimeError(f'terminal frame was not atomically carried: {outcome}')
        except Exception as exc:
            if terminal_gate is not None:
                terminal_gate.abort()
            with condition:
                errors.append(f'{index}: {type(exc).__name__}: {exc}; {getattr(exc, "__notes__", [])}; operation={getattr(exc, "operation_id", "")}')
                condition.notify_all()
            raise
        finally:
            with condition:
                if any(row[0] == conv for row in attempts):
                    active -= 1

    try:
        engine.start()
        result['authority'] = engine.describe()
        result['provenance'].update(declared_artifact=engine.declared_artifact,
                                    artifact_before=engine.artifact_before)
        if engine.declared_artifact is not None:
            result['binary_sha256'] = engine.declared_artifact['binary_sha256']
        initial_health = engine._client.health(deadline=.5)
    except BaseException:
        engine.stop()
        raise
    result['engine_logs'] = [engine.log_path]
    result['readiness_observation'] = health
    record_readiness_probe(health, initial_health, phase='startup', elapsed_seconds=time.monotonic() - started)
    def sample_rss():
        next_health = 0.0
        consecutive_unhealthy = 0
        while not sampling_stop.wait(.1):
            for name, pid in [('client', __import__('os').getpid()),
                              ('engine', engine.pid)]:
                try:
                    if pid is not None:
                        key = name + '_peak_rss_bytes'
                        result[key] = max(result.get(key, 0), psutil.Process(pid).memory_info().rss)
                except psutil.Error:
                    pass  # The crash/reopen interval intentionally has no live daemon.
            process = engine._process
            if (time.monotonic() < next_health or restarting.is_set()
                    or not engine.alive):
                continue
            next_health = time.monotonic() + .5
            try:
                reported = engine._client.health(deadline=.5)
            except Exception as exc:
                reported = {'ready': False, 'probe_error': f'{type(exc).__name__}: {exc}'}
            if restarting.is_set() or engine._process is not process or not engine.alive:
                continue
            consecutive_unhealthy = record_readiness_probe(
                health, reported, phase='productive', elapsed_seconds=time.monotonic() - started,
                consecutive_unhealthy=consecutive_unhealthy)
    sampler = threading.Thread(target=sample_rss, name='concurrency-rss', daemon=True)
    sampler.start()
    executor = RecoverableAgentExecutor(max_workers=conversations,
        queue_capacity=conversations, max_abandoned_workers=1,
        thread_name_prefix='concurrency-probe')
    futures = []
    try:
        with (patch('lib.storage.get_storage_client', lambda **_kwargs: engine._client),
              profile_client_stages(profile_client, client_stages)):
            try:
                for i in range(conversations):
                    submitted_at[i] = time.monotonic()
                    futures.append(executor.submit_task(f'task-{i}', run, i))
                with condition:
                    condition.wait_for(lambda: active == conversations or errors,
                                       timeout=timeout)
                    result['peak_simultaneous_running'] = peak_active
                    result['admission_seconds'] = time.monotonic() - started
                    result['client_rss_bytes_at_barrier'] = psutil.Process().memory_info().rss
                    result['engine_rss_bytes_at_barrier'] = psutil.Process(engine.pid).memory_info().rss
                # Retain the complete content-free metrics envelope: packer pacing,
                # maintenance and rotation diagnostics are part of latency evidence.
                observe_metrics('admission_storage_metrics',
                                'admission-before-running-restart', quiescent=True)
                if restart_while_running and peak_active == conversations:
                    result['running_restart_ms'] = crash_cycle()
                    result['engine_logs'].append(engine.log_path)
                    def running_status(row):
                        try:
                            return turns.get_attempt(row[2], user_id=_OWNER_ID)['status']
                        except Exception as exc:
                            with condition:
                                errors.append(f'running recovery {row}: {type(exc).__name__}: {exc}')
                            return 'missing'
                    with ThreadPoolExecutor(max_workers=min(32, conversations)) as readers:
                        statuses = list(readers.map(running_status, attempts))
                    result['recovered_running'] = sum(status == 'running' for status in statuses)
                    if result['recovered_running'] != conversations:
                        errors.append(f"recovered only {result['recovered_running']}/{conversations} running attempts")
                        stop_work.set()
                if recover_interrupted:
                    stop_work.set()
                observe_metrics('productive_start_storage_metrics',
                                'recovered-before-provider-release', quiescent=True)
                released_at = time.monotonic()
                release.set()
                for future in futures:
                    try:
                        future.result(timeout=timeout)
                    except Exception as exc:
                        if not errors:
                            errors.append(f'{type(exc).__name__}: {exc}')
                if recover_interrupted and not errors:
                    result['settled_interrupted'] = timed('recover_interrupted',
                        turns.recover_running_attempts)
                result['phase_ms']['productive'] = (time.monotonic() - started) * 1000
                events_completed_at = time.monotonic()
                result['phase_ms']['events'] = (events_completed_at - released_at) * 1000
                result['event_window_monotonic_seconds'] = {
                    'start': released_at, 'finish': events_completed_at,
                    'clock': 'CLOCK_MONOTONIC',
                }
                observe_metrics('storage_metrics',
                                'completed-before-terminal-restart', quiescent=True)
                if not errors:
                    result['restart_ms'] = crash_cycle()
                    result['engine_logs'].append(engine.log_path)
                def readback(row):
                    conv, turn, attempt, index = row
                    try:
                        restored = turns.get_turn(conv, turn, user_id=_OWNER_ID)
                        expected_content = ''.join(stream_chunk(index, sequence) for sequence in range(rounds))
                        content_matches = recover_interrupted or restored.get('projection', {}).get('content', '') == expected_content
                        replay_matches = True
                        if carry_stream_events and not recover_interrupted:
                            rows = engine._client.query('event.list', {
                                'task_id': f'task-{index}', 'after_sequence': 0,
                                'limit': rounds + 1, 'user_id': _OWNER_ID})
                            expected_events = [
                                ({'type': 'delta', 'content': stream_chunk(index, sequence)}
                                 if stream_event_kind == 'delta' else {'type': 'phase', 'phase': 'text'})
                                for sequence in range(rounds)] + [{'type': 'done', 'finishReason': 'stop'}]
                            replay_matches = [row['sequence'] for row in rows] == list(range(1, rounds + 2)) \
                                and [row['event'] for row in rows] == [{'v': 1, **event} for event in expected_events]
                        return (turns.get_attempt(attempt, user_id=_OWNER_ID)['status'],
                                restored['status'], content_matches, replay_matches)
                    except Exception as exc:
                        with condition:
                            errors.append(f'completion recovery {row}: {type(exc).__name__}: {exc}')
                        return ('missing', 'missing', False, False)
                readback_started = time.monotonic()
                with ThreadPoolExecutor(max_workers=min(32, conversations)) as readers:
                    recovered = list(readers.map(readback, attempts))
                result['phase_ms']['recovery_readback'] = (time.monotonic() - readback_started) * 1000
                expected = ('interrupted', 'interrupted') if recover_interrupted else ('completed', 'completed')
                count = sum(x[:2] == expected for x in recovered)
                if not recover_interrupted:
                    result['recovered_exact_replay'] = sum(x[3] for x in recovered) if carry_stream_events else None
                    if carry_stream_events and result['recovered_exact_replay'] != conversations:
                        errors.append(f"recovered exact replay for only {result['recovered_exact_replay']}/{conversations} attempts")
                    result['recovered_exact_content'] = sum(x[2] for x in recovered)
                    if result['recovered_exact_content'] != conversations:
                        errors.append(f"recovered exact content for only {result['recovered_exact_content']}/{conversations} attempts")
                result['recovered_completed' if not recover_interrupted else 'recovered_interrupted'] = count
                if count != conversations:
                    errors.append(f'recovered only {count}/{conversations} expected terminal states {expected}')
            finally:
                shutdown_started = time.monotonic()
                stop_work.set()
                release.set()
                executor.shutdown(wait=True, cancel_futures=True)
                result['phase_ms']['executor_shutdown'] = (time.monotonic() - shutdown_started) * 1000
    except Exception as exc:
        errors.append(f'benchmark: {type(exc).__name__}: {exc}')
    finally:
        sampler_stop_started = time.monotonic()
        sampling_stop.set()
        sampler.join(timeout=2)
        result['phase_ms']['sampler_stop'] = (time.monotonic() - sampler_stop_started) * 1000
        if not errors:
            verification_started = time.monotonic()
            try:
                result.update(engine.verify(timeout))
                if result['integrity_returncode']:
                    errors.append('integrity verification failed')
            except Exception as exc:
                errors.append(f'integrity verification: {type(exc).__name__}: {exc}')
            finally:
                result['phase_ms']['verification'] = (time.monotonic() - verification_started) * 1000
        freeze_started = time.monotonic()
        engine.freeze()
        result['phase_ms']['final_freeze'] = (time.monotonic() - freeze_started) * 1000
        if not health['probes']:
            errors.append('no daemon readiness sample was collected')
        if health['unhealthy_probes']:
            errors.append(f"authority failed readiness during startup or productive load: {health['unhealthy_probes']} probes")
        if errors:
            # Retain failed synthetic authorities for diagnosis; never graceful
            # checkpoint the failure image or delete the only reproduction.
            result['retained_failure_directories'] = engine.retain_failure()
        cleanup_started = time.monotonic()
        engine.stop()
        result['phase_ms']['cleanup'] = (time.monotonic() - cleanup_started) * 1000
        result['provenance']['artifact_after'] = engine.artifact_after
    result['errors'] = errors[:20]
    result['stage_failures'] = stage_failures
    result['expected_stream_events'] = conversations * rounds
    result['observed_stream_events'] = len(latencies.get('stream', []))
    result['error_count'] = len(errors)
    result['wall_seconds'] = time.monotonic() - started
    result['latency_ms'] = {label: latency_summary(values) for label, values in latencies.items()}
    if profile_client:
        result['client_stages_ms'] = client_stages
    result['correctness_passed'] = (not errors and peak_active == conversations
                        and result.get('recovered_interrupted' if recover_interrupted else 'recovered_completed') == conversations
                        and (not restart_while_running or result.get('recovered_running') == conversations))
    try:
        result['provenance']['sources_after'] = capture_sources()
    except (OSError, RuntimeError, ValueError) as exc:
        result['provenance']['sources_after'] = {'capture_error': str(exc)}
    result['acceptance'] = assess(result, current_sources=result['provenance']['sources_after'])
    result['passed'] = result['acceptance']['passed']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--backend', choices=('tofudb', 'sqlite'), default='tofudb')
    parser.add_argument('--native-client', choices=('application', 'embedded'), default='application')
    parser.add_argument('--stream-interval-ms', type=float, default=1000)
    parser.add_argument('--stream-event-kind', choices=('phase', 'delta'), default='phase',
                        help='phase exercises structural commits; delta exercises text producer/coalescing')
    parser.add_argument('--carry-stream-events', action='store_true',
                        help='persist and verify exact reconnect frames atomically with Turn writes; requires uncoalesced frames')
    parser.add_argument('--terminal-barrier', action='store_true',
                        help='synchronize terminal arrival after all text frames to compare settlement under equal contention')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--conversations', type=int, default=32)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--chunk-bytes', type=int, default=0)
    parser.add_argument('--restart-while-running', action='store_true')
    parser.add_argument('--recover-interrupted', action='store_true',
                        help='exercise application boot settlement instead of continuing provider output')
    parser.add_argument('--profile-client', action='store_true',
                        help='include bounded per-operation pool and wire timings')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.backend == 'tofudb' and args.binary is None:
        parser.error('--binary is required for tofudb')
    if not 0 <= args.stream_interval_ms <= 60000:
        parser.error('stream-interval-ms must be 0..60000')
    if (not 1 <= args.conversations <= 4096 or not 1 <= args.rounds <= 100
            or not 0 <= args.chunk_bytes <= 65536):
        parser.error('conversations=1..4096, rounds=1..100, chunk-bytes=0..65536')
    try:
        report = measure(args.binary, args.directory, args.conversations, args.rounds,
            backend=args.backend, stream_interval_ms=args.stream_interval_ms, native_client=args.native_client,
            stream_event_kind=args.stream_event_kind, carry_stream_events=args.carry_stream_events, terminal_barrier=args.terminal_barrier,
            profile_client=args.profile_client, chunk_bytes=args.chunk_bytes,
            restart_while_running=args.restart_while_running or args.recover_interrupted,
            recover_interrupted=args.recover_interrupted)
    except Exception as exc:
        report = {'backend': args.backend, 'passed': False, 'correctness_passed': False,
                  'errors': [f'{type(exc).__name__}: {exc}'], 'error_count': 1}
        report['acceptance'] = assess(report)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
