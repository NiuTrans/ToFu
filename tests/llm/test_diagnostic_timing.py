"""Diagnostic clocks must not become execution, payload or retry authority."""
from concurrent.futures import Future
import json
import threading

import pytest
from lib.llm.diagnostic_timing import (
    DiagnosticTiming, MAX_PHASE_EVENTS, MAX_PHASE_SAMPLES,
    bind_timing, current_timing, timing_stage, timed_call,
)
from lib.translate.observability import observed_submission

pytestmark = pytest.mark.unit


def test_monotonic_nested_spans_are_not_an_additive_total():
    now = [0.0]
    records = []
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=records.append)
    with bind_timing(timing):
        with timing_stage('engine_total'):
            now[0] = 1
            with timing_stage('http_request', trace_id='physical-id', headers={'Authorization': 'secret'}):
                now[0] = 3
            now[0] = 4
    timing.complete()
    snapshot = timing.snapshot()
    assert snapshot['elapsed_ms'] == 4000
    assert snapshot['stages']['engine_total']['total_ms'] == 4000
    assert snapshot['stages']['http_request']['total_ms'] == 2000
    parent, child = sorted(snapshot['samples'], key=lambda item: item['span_id'])
    assert child['parent_span_id'] == parent['span_id']
    assert 'secret' not in json.dumps(records)
    snapshot['stages']['http_request']['count'] = 0
    assert timing.snapshot()['stages']['http_request']['count'] == 1
    assert current_timing() is None


def test_high_retry_counts_stay_bounded_but_aggregate_counts_are_exact():
    records = []
    timing = DiagnosticTiming(kind='synthetic', emit=records.append)
    with bind_timing(timing):
        for _ in range(1000):
            with timing_stage('dispatcher_wait'):
                pass
    timing.complete()
    snapshot = timing.snapshot()
    assert snapshot['stages']['dispatcher_wait']['count'] == 1000
    assert len(snapshot['samples']) == MAX_PHASE_SAMPLES
    assert len(records) == MAX_PHASE_EVENTS + 1  # one terminal summary
    assert snapshot['coverage'] == 'sampled' and snapshot['events_suppressed'] > 0
    assert snapshot['active_spans'] == []
    assert 'samples' not in records[-1]


def test_observer_failure_cannot_change_result_or_exception():
    def broken(_record):
        raise RuntimeError('observer failed')
    timing = DiagnosticTiming(kind='synthetic', emit=broken)
    with bind_timing(timing):
        assert timed_call('postprocess', lambda: 'exact output') == 'exact output'
        error = ValueError('private provider payload')
        with pytest.raises(ValueError) as caught:
            timed_call('postprocess', lambda: (_ for _ in ()).throw(error))
        assert caught.value is error
    timing.complete()
    assert timing.snapshot()['stages']['postprocess']['errors'] == 1


def test_queue_clock_crosses_worker_boundary_but_scope_does_not_leak():
    now = [0.0]
    records, submitted, observed = [], [], []
    timing = DiagnosticTiming(kind='send', clock=lambda: now[0], emit=records.append)
    future = Future()
    def submit(worker):
        submitted.append(worker)
        return future
    with bind_timing(timing):
        returned = observed_submission(submit, 'safe-job-id',
            lambda: observed.append(current_timing().timing_id), kind='attended_worker')
    assert returned is future and current_timing() is None
    now[0] = 2
    def run():
        submitted[0]()
        assert current_timing() is None
        future.set_result('original future result')
    thread = threading.Thread(target=run)
    thread.start(); thread.join()
    timing.complete()
    assert observed == [timing.timing_id]
    assert timing.snapshot()['stages']['lane_queue']['total_ms'] == 2000
    assert returned.result() == 'original future result'


def test_cancelled_queue_and_late_work_preserve_terminal_caller_snapshot():
    records, queued = [], []
    future = Future()
    timing = DiagnosticTiming(kind='send', emit=records.append)
    with bind_timing(timing):
        observed_submission(lambda work: queued.append(work) or future, 'safe-job',
            lambda: pytest.fail('cancelled worker ran'), kind='attended_worker')
    assert future.cancel()
    timing.complete('timed_out')
    assert timing.snapshot()['samples'][0]['queue_cancelled'] is True
    assert 'lane_worker' not in timing.snapshot()['stages']
    before = timing.snapshot()
    with bind_timing(timing):
        with timing_stage('http_request'):
            pass
    assert timing.snapshot() == before
    assert any(record['event'] == 'late_phase_complete' for record in records)


def test_worker_exception_resets_reused_thread_scope():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        for kind in ('one', 'two'):
            timing = DiagnosticTiming(kind=kind, emit=lambda _record: None)
            with bind_timing(timing):
                def work():
                    assert current_timing() is timing
                    raise ValueError('private output')
                future = observed_submission(executor.submit, kind, work, kind='attended_worker')
            with pytest.raises(ValueError):
                future.result()
            assert executor.submit(current_timing).result() is None
            timing.complete('failed')


def test_exception_code_getter_cannot_replace_original_operation_error():
    class ProviderError(Exception):
        @property
        def code(self):
            raise RuntimeError('diagnostic property failed')
    error = ProviderError('private payload')
    timing = DiagnosticTiming(kind='synthetic', emit=lambda _record: None)
    with bind_timing(timing):
        with pytest.raises(ProviderError) as caught:
            timed_call('provider_call', lambda: (_ for _ in ()).throw(error))
    assert caught.value is error
    assert timing.snapshot()['active_spans'] == []


def test_retry_rich_command_retains_late_persistence_aggregates():
    stages = ('turn_command_total input_translation language_detection lane_queue '
        'lane_worker engine_total cache_lookup identity_check route_resolution '
        'status_delivery engine_attempt dispatch_total route_selection '
        'provider_call chat_total request_build input_admission request_seal '
        'egress_route provider_send network_route http_pool_acquire http_request '
        'response_parse response_usage quality_validation postprocess cache_store '
        'response_normalize dispatcher_wait engine_backoff transport_backoff '
        'turn_persist executor_start acknowledgement_read').split()
    timing = DiagnosticTiming(kind='synthetic', emit=lambda _record: None)
    with bind_timing(timing):
        for name in stages:
            with timing_stage(name):
                pass
    snapshot = timing.snapshot()
    assert snapshot['uncounted_stage_occurrences'] == 0
    assert snapshot['stages']['turn_persist']['count'] == 1
    assert snapshot['stages']['acknowledgement_read']['count'] == 1
