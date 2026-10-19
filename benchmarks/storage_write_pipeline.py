"""Measure accepted event -> FIFO -> real storage -> push observer -> crash readback.

Entry: CLI; delegates workload, disposable authority and recovery to
conversation_concurrency. The provider is its explicit barrier fixture. This
adds the production EventDeliveryCoordinator instead of bypassing its queue.
Push observes a committed result; it does not emulate browser/network rendering.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def measure_pipeline(*, binary, directory, conversations=64, rounds=4, chunk_bytes=8192, stream_interval_ms=1000, stream_event_kind="delta", delivery_source=None, submission_mode="barrier", delivery_profile_every=0, profile_client=False, native_client="application"):
    from benchmarks.conversation_concurrency import measure, latency_summary
    from lib import turn_lifecycle
    from lib.tasks_pkg.event_delivery import DeliveryLane, EventDeliveryCoordinator, EventDeliveryRevoked, retained_snapshot_bytes

    if delivery_source is not None:
        import importlib.util
        name = '_tofu_delivery_reference'
        spec = importlib.util.spec_from_file_location(name, delivery_source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        DeliveryLane, EventDeliveryCoordinator, EventDeliveryRevoked = module.DeliveryLane, module.EventDeliveryCoordinator, module.EventDeliveryRevoked
    source_inputs = {
        'coordinator': Path(delivery_source or sys.modules[EventDeliveryCoordinator.__module__].__file__),
        'benchmark': Path(__file__),
    }
    if delivery_source is None:
        root = Path(__file__).parents[1]
        source_inputs.update({name: root / name for name in (
            'benchmarks/conversation_concurrency.py',
            'benchmarks/conversation_authority.py',
            'lib/tasks_pkg/event_delivery_profile.py',
            'lib/tasks_pkg/event_delivery_profile_metrics.py',
            'lib/storage/generated_task_event_delivery_profile.py',
            'contracts/task_event_delivery_v1.json',
        )})
    def source_manifest():
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in source_inputs.items()}
    frozen_sources = source_manifest()
    original = turn_lifecycle.record_task_event
    from lib.storage.event_batcher import StorageEventBatcher
    def selected_client(**kwargs):
        from lib.storage import get_storage_client
        return get_storage_client(**kwargs)
    replay = StorageEventBatcher(client_provider=selected_client)
    if delivery_source is not None and delivery_profile_every:
        raise ValueError('delivery profiling requires the current coordinator; frozen controls run with profiling off')
    owner = EventDeliveryCoordinator(**({'profile_every': delivery_profile_every} if delivery_source is None else {}))
    lock = threading.Lock()
    lanes = {}
    stages = defaultdict(list)
    observed = defaultdict(list)
    failures = []
    durable_ack = {}
    progress_push = []
    progress_scheduled = []
    scheduled_at = {}

    def observe_schedule(task_id, sequence, due):
        with lock:
            scheduled_at[(task_id, sequence)] = due

    def elapsed(stage, start, end=None):
        duration_ms = ((time.monotonic() if end is None else end) - start) * 1000
        with lock:
            stages[stage].append(duration_ms)

    def deliver(task, event, **kwargs):
        invocation_started = time.monotonic()
        captured = dict(task)
        for field in ('config', 'toolRounds', 'segments'):
            if field in captured:
                captured[field] = copy.deepcopy(captured[field])
        captured_at = time.monotonic()
        elapsed('projection_capture', invocation_started, captured_at)
        carrier = kwargs['task_event']
        sequence = carrier['sequence']
        with lock:
            lane = lanes.setdefault(task['id'], DeliveryLane(task))
        accepted = time.monotonic()
        result = {}
        terminal = event.get('type') == 'done'
        if submission_mode == 'queued' and terminal:
            prefix_start = time.monotonic()
            owner.flush(lane, timeout=None)
            elapsed('terminal_prefix_barrier', prefix_start)
        def persist_once(frames=None):
            elapsed('producer_to_storage_attempt', accepted)
            start = time.monotonic()
            fence = {}
            call_kwargs = dict(kwargs)
            if frames and len(frames) > 1:
                call_kwargs['task_event'] = {**carrier, 'preceding_events': [
                    {'task_id': task['id'], 'sequence': frame.sequence, 'event': frame.event}
                    for frame in frames[:-1]]}
            primary_start = time.monotonic()
            try:
                result['outcome'] = original(task, event, projection_source=captured, fence_out=fence, **call_kwargs)
            finally:
                elapsed('storage_primary_call', primary_start)
            if result['outcome'] == 'coalesced':
                replay_start = time.monotonic()
                try:
                    appended = replay.append(task_id=task['id'], sequence=sequence,
                        event=event, wait=True, timeout=30,
                        require_attempts_live=fence.get('require_attempts_live'))
                finally:
                    elapsed('storage_replay_call', replay_start)
                if isinstance(appended, dict) and appended.get('rejected') == 'attempt_not_live':
                    raise EventDeliveryRevoked('coalesced event attempt is no longer live')
                result['outcome'] = 'carried'
                elapsed('coalesced_replay_ack', start)
            if result['outcome'] != 'carried':
                raise EventDeliveryRevoked(f'event was not durable: {result["outcome"]!r}')
            elapsed('storage_commit_and_ack', start)
            result['committed_at'] = time.monotonic()
            with lock:
                for committed_sequence in ([frame.sequence for frame in frames] if frames else [sequence]):
                    durable_ack[(task['id'], committed_sequence)] = result['committed_at']
        def persist(frames=None):
            try:
                persist_once(frames)
            except BaseException as error:
                with lock:
                    if len(failures) < 8:
                        row = {'code': getattr(error, 'code', type(error).__name__),
                            'message': str(error)[:256], 'sequence': sequence}
                        failures.append(row)
                        print('PIPELINE_STORAGE_FAILURE=' + json.dumps(row), flush=True)
                raise
        def publish():
            pushed_at = time.monotonic()
            committed_at = durable_ack.get((task['id'], sequence))
            if committed_at is None:
                raise RuntimeError('push preceded the durable acknowledgement')
            elapsed('commit_ack_to_push', committed_at, pushed_at)
            elapsed('producer_to_push', accepted, pushed_at)
            with lock:
                observed[task['id']].append(sequence)
                if not terminal:
                    progress_push.append((pushed_at - accepted) * 1000)
                    due = scheduled_at.get((task['id'], sequence))
                    if due is not None:
                        progress_scheduled.append((pushed_at - due) * 1000)
        options = {}
        if submission_mode == 'queued' and not terminal and event.get('type') == 'delta':
            frame_type = sys.modules[EventDeliveryCoordinator.__module__].ProgressFrame
            from lib.storage.protocol import canonical_json
            options = dict(projection_identity=lane, progress_frame=frame_type(sequence, event, publish),
                persist_progress=persist, progress_wire_bytes=retained_snapshot_bytes(event),
                progress_payload_bytes=len(canonical_json(event)) + 256)
        owner.submit(lane, sequence, retained_snapshot_bytes(captured, event), persist, publish, **options)
        if submission_mode == 'queued' and not terminal:
            return 'carried'  # Workload admission; measured commit-to-push lag is reported below.
        owner.flush(lane, timeout=None)
        elapsed('producer_to_barrier_return', accepted)
        return result['outcome']

    try:
        with patch.object(turn_lifecycle, 'record_task_event', deliver):
            report = measure(binary, directory, conversations, rounds,
                chunk_bytes=chunk_bytes, restart_while_running=True,
                profile_client=profile_client, stream_event_kind=stream_event_kind, native_client=native_client,
                carry_stream_events=True, terminal_barrier=True, stream_interval_ms=stream_interval_ms,
                on_stream_scheduled=observe_schedule)
        expected = list(range(1, rounds + 2))
        push_exact = len(observed) == conversations and all(rows == expected for rows in observed.values())
        if submission_mode == 'queued':
            report['latency_ms']['stream_submission'] = report['latency_ms']['stream']
            report['latency_ms']['stream_submission_scheduled'] = report['latency_ms'].pop('stream_scheduled')
            report['latency_ms']['stream'] = latency_summary(progress_push)
            report['latency_ms']['stream_scheduled'] = latency_summary(progress_scheduled)
        unchanged_sources = source_manifest() == frozen_sources
        report['latency_comparison_eligible'] = (str(report.get('requested_native_profile_every', '0')) == '0'
            and delivery_profile_every == 0 and not profile_client and unchanged_sources)
        report['delivery_pipeline'] = {
            'contract': 'tofu.storage-write-pipeline/v1',
            'submission_mode': submission_mode,
            'delivery_profile_every': delivery_profile_every,
            'source_manifest_sha256': frozen_sources,
            'sources_unchanged': unchanged_sources,
            'anchor_semantics': {
                'producer_to_storage_attempt': 'wrapper preparation/terminal prefix wait/queue plus retry elapsed; not FIFO-only',
                'storage_primary_call': 'each primary client call including failures; nested within storage_commit_and_ack',
                'storage_replay_call': 'each coalesced replay client call including failures; nested within storage_commit_and_ack',
                'commit_ack_to_push': 'client ACK to callback entry; excludes observer lock and browser/network rendering',
                'producer_to_push': 'wrapper ready to callback entry; includes queued terminal prefix barrier; not FIFO acceptance',
                'coalesced_replay_ack': 'primary plus replay wrapper span; not replay-only',
                'production_fifo': 'coordinator performance_profile records per-frame successful FIFO admission',
                'overlap': 'nested wall spans and their percentiles cannot be summed',
            },
            'delivery_source_sha256': frozen_sources['coordinator'],
            'benchmark_source_sha256': frozen_sources['benchmark'],
            'anchors_ms': {name: latency_summary(values) for name, values in stages.items()},
            'coordinator': owner.snapshot(), 'push_order_exact': push_exact,
            'coalesced_replay_frames': len(stages.get('coalesced_replay_ack', [])),
            'storage_failure_examples': failures,
            'observed_push_frames': sum(map(len, observed.values())),
            'scope': 'production FIFO, real semantic storage, committed push observer, SIGKILL recovery; provider is a barrier fixture; browser/network rendering excluded',
        }
        profile = report['delivery_pipeline']['coordinator'].get('performance_profile', {})
        anchor_integrity = None
        if profile.get('enabled'):
            anchor_integrity = (profile.get('available') is not False
                and profile.get('invalidClocks') == profile.get('observerErrors') == profile.get('activeSamples') == 0
                and profile.get('selectedBatches') == profile.get('completedSamples')
                and profile.get('acceptedFrameAnchors') == profile.get('acknowledgedFrameAnchors')
                == profile.get('stages', {}).get('fifo_wait', {}).get('count')
                == profile.get('stages', {}).get('publish_callback', {}).get('count'))
        report['delivery_pipeline']['anchor_integrity_passed'] = anchor_integrity
        report['correctness_passed'] = report['correctness_passed'] and push_exact
        if not push_exact:
            report['errors'].append('delivery push count/order does not match the accepted workload')
            report['error_count'] += 1
        from benchmarks.conversation_acceptance import assess
        report['acceptance'] = assess(report)
        report['latency_comparison_eligible'] = report['acceptance']['latency_comparison_eligible']
        report['passed'] = report['acceptance']['passed']
        return report
    finally:
        delivery_joined = owner.close(timeout=30)
        replay_joined = replay.close(timeout=30)
        if not delivery_joined or not replay_joined:
            raise RuntimeError('pipeline did not join accepted event delivery/replay')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--conversations', type=int, default=64)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--chunk-bytes', type=int, default=8192)
    parser.add_argument('--native-client', choices=('application', 'embedded'), default='application')
    parser.add_argument('--stream-event-kind', choices=('delta', 'phase'), default='delta')
    parser.add_argument('--delivery-source', type=Path, help='frozen exact coordinator for paired controls')
    parser.add_argument('--submission-mode', choices=('barrier', 'queued'), default='barrier')
    parser.add_argument('--stream-interval-ms', type=float, default=1000)
    parser.add_argument('--native-profile-every', type=int, default=0)
    parser.add_argument('--profile-client', action='store_true', help='diagnostic client stage timings; disqualifies latency comparison')
    parser.add_argument('--delivery-profile-every', type=int, default=0, help='diagnostic FIFO sampling; nonzero disqualifies latency comparison')
    args = parser.parse_args()
    if not 0 <= args.stream_interval_ms <= 60000 or args.native_profile_every < 0 or args.delivery_profile_every < 0:
        parser.error('stream interval must be 0..60000 ms; profile frequency must be nonnegative')
    os.environ['TOFU_DB_REQUEST_PROFILE_EVERY'] = str(args.native_profile_every)
    if not (1 <= args.conversations <= 1000 and 1 <= args.rounds <= 64 and 1 <= args.chunk_bytes <= 1048576):
        parser.error('workload must have 1..1000 conversations, 1..64 rounds and 1..1048576 byte chunks')
    args.directory.mkdir(parents=True, exist_ok=True)
    report = measure_pipeline(binary=args.binary, directory=args.directory,
        conversations=args.conversations, rounds=args.rounds, chunk_bytes=args.chunk_bytes,
        stream_interval_ms=args.stream_interval_ms, stream_event_kind=args.stream_event_kind,
        submission_mode=args.submission_mode, delivery_source=args.delivery_source,
        delivery_profile_every=args.delivery_profile_every, profile_client=args.profile_client, native_client=args.native_client)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'correctness_passed': report['correctness_passed'],
        'latency_ms': report['latency_ms'], 'acceptance': report['acceptance'], 'delivery_pipeline': report['delivery_pipeline']}, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
