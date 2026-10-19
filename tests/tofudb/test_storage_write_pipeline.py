"""The performance observer must retain real delivery ordering and refusals."""
from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('requested', [None, False, True])
def test_client_stage_sampling_is_opt_in_and_controls_qualification(monkeypatch, tmp_path, requested):
    from benchmarks import conversation_concurrency as workload
    from benchmarks import storage_write_pipeline as pipeline
    from lib import turn_lifecycle

    def measure(*args, **kwargs):
        assert kwargs['profile_client'] is bool(requested)
        assert kwargs['native_client'] == 'application'
        task = {'id': 'profile-fixture', 'config': {}, 'abort_event': threading.Event()}
        turn_lifecycle.record_task_event(task, {'type': 'done'},
            task_event={'task_id': task['id'], 'sequence': 1})
        report = {'correctness_passed': True, 'errors': [], 'error_count': 0,
                  'conversations': 1, 'rounds': 0, 'requested_native_profile_every': '0',
                  'requested_client_profile': kwargs['profile_client']}
        if kwargs['profile_client']:
            report['client_stages_ms'] = {}
        return report

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', lambda *a, **k: 'carried')
    monkeypatch.setattr(workload, 'measure', measure)
    options = {} if requested is None else {'profile_client': requested}
    report = pipeline.measure_pipeline(binary=tmp_path / 'binary', directory=tmp_path,
        conversations=1, rounds=0, **options)
    assert report['correctness_passed']
    assert report['latency_comparison_eligible'] is not bool(requested)
    assert report['acceptance']['latency_comparison_eligible'] is not bool(requested)


@pytest.mark.parametrize('native_client', ['application', 'embedded'])
def test_pipeline_uses_live_projection_owner_and_detached_input(monkeypatch, tmp_path, native_client):
    from benchmarks import conversation_concurrency as workload
    from benchmarks import storage_write_pipeline as pipeline
    from lib import turn_lifecycle
    expected = []
    def persist(task, event, **kwargs):
        assert kwargs['projection_source'] is not task
        assert kwargs['projection_source']['config'] is not task['config']
        expected.append(kwargs['task_event']['sequence'])
        task['_durableRevision'] = len(expected)
        return 'carried'
    def measure(*args, **kwargs):
        assert kwargs['stream_event_kind'] == 'phase'
        assert kwargs['native_client'] == native_client
        task = {'id': 'task-1', '_userId': 11, 'config': {}, 'toolRounds': [], 'segments': [],
                'abort_event': threading.Event(), 'content': ''}
        for sequence in range(1, 4):
            assert turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'x'},
                task_event={'task_id': 'task-1', 'sequence': sequence}) == 'carried'
            assert task['_durableRevision'] == sequence
        return {'correctness_passed': True, 'errors': [], 'error_count': 0,
                'conversations': 1, 'rounds': 2, 'requested_native_profile_every': '1'}
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', persist)
    monkeypatch.setattr(workload, 'measure', measure)
    report = pipeline.measure_pipeline(binary=tmp_path / 'binary', directory=tmp_path,
        conversations=1, rounds=2, stream_event_kind='phase', native_client=native_client)
    assert not report['latency_comparison_eligible']
    assert report['correctness_passed']
    assert report['delivery_pipeline']['push_order_exact']
    assert report['delivery_pipeline']['observed_push_frames'] == 3
    assert report['delivery_pipeline']['anchors_ms']['storage_commit_and_ack']['count'] == 3


def test_unconfirmed_event_cannot_become_a_passing_push_observation(monkeypatch, tmp_path):
    from benchmarks import conversation_concurrency as workload
    from benchmarks import storage_write_pipeline as pipeline
    from lib import turn_lifecycle
    def measure(*args, **kwargs):
        task = {'id': 'task-1', '_userId': 11, 'config': {}, 'abort_event': threading.Event()}
        assert turn_lifecycle.record_task_event(task, {'type': 'delta'},
            task_event={'task_id': 'task-1', 'sequence': 1}) is False
        return {'correctness_passed': True, 'errors': [], 'error_count': 0,
                'conversations': 1, 'rounds': 1, 'passed': True}
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', lambda *a, **k: False)
    monkeypatch.setattr(workload, 'measure', measure)
    report = pipeline.measure_pipeline(binary=tmp_path / 'binary', directory=tmp_path,
        conversations=1, rounds=1)
    assert not report['correctness_passed']
    assert not report['passed']
    assert not report['acceptance']['passed']
    assert report['delivery_pipeline']['observed_push_frames'] == 0
    assert report['error_count'] == 1


def test_coalesced_delta_uses_fenced_replay_before_push(monkeypatch, tmp_path):
    from benchmarks import conversation_concurrency as workload
    from benchmarks import storage_write_pipeline as pipeline
    from lib import turn_lifecycle
    from lib.storage import event_batcher
    appended = []
    class Replay:
        def __init__(self, **kwargs):
            pass
        def append(self, **kwargs):
            assert kwargs['require_attempts_live'] == ['attempt-1']
            assert 'v' not in kwargs['event'], 'storage owns the event version envelope'
            appended.append(kwargs['sequence'])
            return {'inserted': True}
        def close(self, **kwargs):
            return True
    def persist(task, event, **kwargs):
        kwargs['fence_out']['require_attempts_live'] = ['attempt-1']
        return 'coalesced'
    def measure(*args, **kwargs):
        task = {'id': 'task-1', 'config': {}, 'abort_event': threading.Event()}
        for sequence in range(1, 4):
            assert turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'x'},
                task_event={'task_id': 'task-1', 'sequence': sequence}) == 'carried'
            assert appended == list(range(1, sequence + 1))
        return {'correctness_passed': True, 'errors': [], 'error_count': 0,
                'conversations': 1, 'rounds': 2}
    monkeypatch.setattr(event_batcher, 'StorageEventBatcher', Replay)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', persist)
    monkeypatch.setattr(workload, 'measure', measure)
    report = pipeline.measure_pipeline(binary=tmp_path / 'binary', directory=tmp_path,
        conversations=1, rounds=2)
    assert report['delivery_pipeline']['push_order_exact']
    assert report['delivery_pipeline']['coalesced_replay_frames'] == 3
    assert report['delivery_pipeline']['observed_push_frames'] == 3


def test_queued_pipeline_measures_committed_frames_and_keeps_the_entire_prefix(monkeypatch, tmp_path):
    from benchmarks import conversation_concurrency as workload
    from benchmarks import storage_write_pipeline as pipeline
    from lib import turn_lifecycle
    import time
    entered, release = threading.Event(), threading.Event()
    durable = []

    def persist(task, event, **kwargs):
        carrier = kwargs['task_event']
        if carrier['sequence'] == 1:
            entered.set()
            assert release.wait(3)
        preceding = carrier.get('preceding_events', [])
        durable.extend(row['sequence'] for row in preceding)
        durable.append(carrier['sequence'])
        return 'carried'

    def measure(*args, **kwargs):
        task = {'id': 'task-1', '_userId': 11, 'config': {}, 'content': '',
                'abort_event': threading.Event()}
        for sequence in range(1, 65):
            kwargs['on_stream_scheduled']('task-1', sequence, time.monotonic())
            task['content'] += 'x'
            assert turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'x'},
                task_event={'task_id': 'task-1', 'sequence': sequence, 'event': {'type': 'delta', 'content': 'x'}}) == 'carried'
            if sequence == 1:
                assert entered.wait(2)
        assert durable == []
        release.set()
        assert turn_lifecycle.record_task_event(task, {'type': 'done'},
            task_event={'task_id': 'task-1', 'sequence': 65, 'event': {'type': 'done'}}) == 'carried'
        assert durable == list(range(1, 66))
        return {'correctness_passed': True, 'errors': [], 'error_count': 0,
                'conversations': 1, 'rounds': 64,
                'latency_ms': {'stream': {'count': 64}, 'stream_scheduled': {'count': 64}}}

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', persist)
    monkeypatch.setattr(workload, 'measure', measure)
    try:
        report = pipeline.measure_pipeline(binary=tmp_path / 'binary', directory=tmp_path,
            conversations=1, rounds=64, submission_mode='queued', delivery_profile_every=1)
    finally:
        release.set()
    assert report['correctness_passed'] and report['delivery_pipeline']['push_order_exact']
    assert report['delivery_pipeline']['coordinator']['merged_progress'] == 62
    assert report['delivery_pipeline']['observed_push_frames'] == 65
    assert report['latency_ms']['stream_scheduled']['count'] == 64
    assert report['latency_ms']['stream']['p99'] > 0
    profile = report['delivery_pipeline']['coordinator']['performance_profile']
    assert profile['stages']['fifo_wait']['count'] == 65
    assert profile['stages']['storage_attempt']['count'] == 3
    assert profile['activeFrameAnchors'] == profile['invalidClocks'] == profile['observerErrors'] == 0
    assert report['delivery_pipeline']['sources_unchanged']
    assert report['delivery_pipeline']['anchor_integrity_passed']
    assert not report['latency_comparison_eligible']
    assert any('diagnostic profiling' in s for s in report['acceptance']['failures'])
    assert not report['passed'], 'exact replay alone must not pass the performance/coverage gate'


def test_source_drift_is_reported_without_mutating_the_checkout(monkeypatch, tmp_path):
    from pathlib import Path
    from benchmarks import conversation_concurrency as workload
    from benchmarks import storage_write_pipeline as pipeline
    from lib import turn_lifecycle
    from lib.tasks_pkg import event_delivery

    coordinator_source = Path(event_delivery.__file__)
    read_bytes = Path.read_bytes
    reads = []

    def changed_source_bytes(path):
        data = read_bytes(path)
        if path == coordinator_source:
            reads.append(path)
            if len(reads) > 1:
                return data + b'\n# synthetic source drift\n'
        return data

    def measure(*args, **kwargs):
        task = {'id': 'source-fixture', 'config': {}, 'abort_event': threading.Event()}
        turn_lifecycle.record_task_event(task, {'type': 'done'},
            task_event={'task_id': task['id'], 'sequence': 1})
        return {'correctness_passed': True, 'errors': [], 'error_count': 0,
                'conversations': 1, 'rounds': 0, 'requested_native_profile_every': '0'}

    monkeypatch.setattr(Path, 'read_bytes', changed_source_bytes)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', lambda *a, **k: 'carried')
    monkeypatch.setattr(workload, 'measure', measure)
    report = pipeline.measure_pipeline(binary=tmp_path / 'binary', directory=tmp_path,
        conversations=1, rounds=0)
    assert report['correctness_passed']
    assert not report['delivery_pipeline']['sources_unchanged']
    assert not report['latency_comparison_eligible']
    assert not report['acceptance']['latency_passed']
