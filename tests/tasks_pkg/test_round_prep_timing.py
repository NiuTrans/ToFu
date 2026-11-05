"""Round-preparation timing anchors (ROUND_START -> logical MODEL_REQUEST_START).

Specs: nested steps charge self time only; one ``[RoundPrepTiming]`` record at
>= 1 s and silence below; the request builder names each sizeable sub-step in
``build_bodyMs`` while ``build_body`` keeps the whole window, failure included;
an awaited durability barrier (tool intent, ready,
terminal) warns once when slow (emitting no task event), names its wait
``barrierMs.events`` and is silent when fast; the logical request start
consumes the carrier and the TTFT line names its request clock. The
pre-request checkpoint never awaits diagnostics: test_pre_request_checkpoint.
"""

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from lib.agent_core.events import EventType
from lib.tasks_pkg import persistence_wait
from lib.tasks_pkg import round_prep_timing as rpt
from lib.tasks_pkg.event_delivery import DeliveryLane, EventDeliveryCoordinator

pytestmark = pytest.mark.unit

_TASK_ID = 'task-7f3c2a91d4e64b0c9a5e1f2d3c4b5a69'


class _Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_800_000_000.0

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(rpt, 'time', fake)
    return fake


def _prep_records(caplog):
    prefix = '[RoundPrepTiming] '
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix)]


def test_nested_steps_charge_self_time_and_slow_prep_logs_one_record(clock, caplog):
    caplog.set_level(logging.DEBUG, logger=rpt.logger.name)
    task = {'id': _TASK_ID, 'convId': 'conv-1', '_turnId': 'turn-1'}
    rpt.begin_round_prep(task, 2)
    with rpt.round_prep_step(task, 'hygiene'):
        clock.advance(0.2)
        with rpt.round_prep_step(task, 'compaction'):
            clock.advance(0.5)
        clock.advance(0.1)
    with rpt.round_prep_step(task, 'inputs.guidance'):
        clock.advance(0.3)
    with rpt.round_prep_step(task, 'inputs.working_facts'):
        clock.advance(0.1)
    clock.advance(0.05)
    with rpt.round_prep_step(task, 'barrier'):
        clock.advance(0.25)
    rpt.note_round_prep(task, barrierPendingEvents=4)

    record = rpt.finish_round_prep(task, span_id='model:a:1', request_tag='R3')

    steps = record['stepsMs']
    assert steps['hygiene'] == 300.0
    assert steps['compaction'] == 500.0
    assert steps['inputs'] == 400.0
    assert record['inputsMs'] == {'guidance': 300.0, 'working_facts': 100.0}
    assert steps['barrier'] == 250.0
    assert steps['other'] == 50.0
    assert record['totalMs'] == 1500.0
    assert sum(steps.values()) == pytest.approx(record['totalMs'])
    assert set(steps) == set(rpt.ROUND_PREP_STEPS) | {'other'}
    assert steps['admission'] == steps['build_body'] == steps['receipts'] == 0.0
    # 1-based like MODEL_REQUEST_START.roundNum / requestTag R<n>.
    assert record['roundNum'] == 3
    assert record['task_id'] == _TASK_ID
    assert record['conversation_id'] == 'conv-1'
    assert record['requestTag'] == 'R3' and record['spanId'] == 'model:a:1'
    assert record['barrierPendingEvents'] == 4
    assert _prep_records(caplog) == [record]
    assert rpt.finish_round_prep(task) is None


def test_laps_name_unwrapped_calls_and_leave_only_the_unbounded_tail_as_other(clock):
    task = {'id': _TASK_ID}
    rpt.begin_round_prep(task, 0)
    clock.advance(0.4)                      # e.g. ROUND_START append
    rpt.round_prep_lap(task, 'round_open')
    clock.advance(0.01)
    with rpt.round_prep_step(task, 'admission'):
        clock.advance(0.2)
        rpt.round_prep_lap(task, 'inside')  # an open step owns its time
        clock.advance(0.1)
    clock.advance(0.05)                     # unwrapped call after the step
    rpt.round_prep_lap(task, 'dispatch')
    clock.advance(0.02)                     # after the last boundary

    record = rpt.finish_round_prep(task)

    steps = record['stepsMs']
    assert steps['round_open'] == 400.0
    assert steps['admission'] == 300.0
    # Unnamed time between the previous boundary and the lap, around the step.
    assert steps['dispatch'] == pytest.approx(60.0)
    assert 'inside' not in steps and 'insideMs' not in record
    assert steps['other'] == pytest.approx(20.0)
    assert sum(steps.values()) == pytest.approx(record['totalMs'])
    rpt.round_prep_lap({'id': 'untimed'}, 'dispatch')  # no carrier: no-op


def test_finish_names_the_tail_after_the_last_boundary(clock):
    task = {'id': _TASK_ID}
    rpt.begin_round_prep(task, 0)
    rpt.round_prep_lap(task, 'round_open')
    clock.advance(0.3)                      # request setup + start-event appends

    record = rpt.finish_round_prep(task, tail='request')

    assert record['stepsMs']['request'] == 300.0
    assert record['stepsMs']['other'] == 0.0
    assert record['totalMs'] == 300.0


def test_fast_prep_and_unstarted_requests_log_nothing(clock, caplog):
    caplog.set_level(logging.DEBUG, logger=rpt.logger.name)
    task = {'id': _TASK_ID}
    rpt.begin_round_prep(task, 0)
    with rpt.round_prep_step(task, 'admission'):
        clock.advance(0.999)
    assert rpt.finish_round_prep(task)['totalMs'] == 999.0
    assert _prep_records(caplog) == []

    rpt.begin_round_prep(task, 0)
    clock.advance(1.0)
    assert rpt.finish_round_prep(task)['totalMs'] == 1000.0
    assert [record['totalMs'] for record in _prep_records(caplog)] == [1000.0]
    caplog.clear()

    timing = rpt.begin_round_prep(task, 1)
    clock.advance(30)
    rpt.end_round_prep(task, timing)
    assert rpt.finish_round_prep(task) is None

    untimed = {'id': 'native'}
    with rpt.round_prep_step(untimed, 'barrier'):
        rpt.note_round_prep(untimed, barrierPendingEvents=1)
    assert untimed == {'id': 'native'}
    assert _prep_records(caplog) == []


# Fake-clock cost of each build_round_request sub-step, keyed by its child name.
_REQUEST_BUILD_MS = {'mcp_authority': 400.0, 'model_images': 10.0, 'request_body': 300.0,
                     'snapshot': 20.0, 'tool_metadata': 200.0, 'context_telemetry': 50.0}


@pytest.fixture
def request_prep(clock, monkeypatch):
    import lib.context_telemetry as context_telemetry
    import lib.tasks_pkg.model_images as model_images
    import lib.tasks_pkg.orchestrator._messages_snapshot as snapshot
    import lib.tasks_pkg.orchestrator._round_request_prep as prep
    import lib.tasks_pkg.orchestrator._tool_assembly_prep as tool_assembly
    import lib.tasks_pkg.orchestrator._tool_request_metadata as tool_metadata

    def costs(child, result=None):
        def step(*_args, **_kwargs):
            clock.advance(_REQUEST_BUILD_MS[child] / 1000 if child else 0)
            return result
        return step

    def request_body(model, messages, **kwargs):
        costs('request_body')()
        return {'model': model, 'messages': list(messages), 'tools': kwargs['tools']}

    monkeypatch.setattr(tool_assembly, 'refresh_root_mcp_authority', costs('mcp_authority'))
    monkeypatch.setattr(model_images, 'pin_model_images', costs('model_images'))
    monkeypatch.setattr(prep.orchestrator_ports, 'build_request_body', request_body)
    # The real snapshot adapter runs, so its own ``build_body.snapshot`` step is measured.
    monkeypatch.setattr(snapshot, 'begin_round_evidence', costs('', 'ref'))
    monkeypatch.setattr(snapshot, 'build_round_evidence', costs('', {'type': 'messages_snapshot'}))
    monkeypatch.setattr(snapshot, 'append_event', costs('snapshot'))
    monkeypatch.setattr(tool_metadata, 'attach_tool_request_metadata', costs('tool_metadata'))
    monkeypatch.setattr(context_telemetry, 'capture_round_context', costs('context_telemetry'))
    task = {'id': _TASK_ID, 'convId': 'conv-1', '_userId': 1, 'config': {}}
    state = SimpleNamespace(model='gpt-5.6-sol', preset='medium', thinking_enabled=False)

    def build():
        rpt.begin_round_prep(task, 0)
        with rpt.round_prep_step(task, 'build_body'):
            clock.advance(0.03)             # the parent's own unnamed glue
            prep.build_round_request(
                task, state, [{'role': 'user', 'content': 'go'}], [], round_num=0,
                tid='prep', thinking_depth=None, temperature=1.0, max_tokens=4096,
                response_format=None)

    return SimpleNamespace(task=task, build=build, ports=prep.orchestrator_ports)


def test_request_build_names_each_sub_step_and_build_body_keeps_the_window(request_prep):
    request_prep.build()

    record = rpt.finish_round_prep(request_prep.task)

    assert record['build_bodyMs'] == _REQUEST_BUILD_MS
    # Nested children pause the parent: it charged only its own 30 ms of glue,
    # and the group still reports parent plus children, each counted once.
    assert record['stepsMs']['build_body'] == pytest.approx(
        sum(_REQUEST_BUILD_MS.values()) + 30.0)
    assert record['totalMs'] == pytest.approx(record['stepsMs']['build_body'])
    assert sum(record['stepsMs'].values()) == pytest.approx(record['totalMs'])


def test_failed_request_body_keeps_its_error_and_names_the_fallback_snapshot(
        request_prep, clock, monkeypatch):
    failure = RuntimeError('body construction failed')

    def fail(*_args, **_kwargs):
        clock.advance(0.3)
        raise failure

    monkeypatch.setattr(request_prep.ports, 'build_request_body', fail)
    with pytest.raises(RuntimeError) as raised:
        request_prep.build()

    assert raised.value is failure
    record = rpt.finish_round_prep(request_prep.task)
    assert record['build_bodyMs'] == {'mcp_authority': 400.0, 'model_images': 10.0,
                                      'request_body': 300.0, 'snapshot': 20.0}
    assert sum(record['stepsMs'].values()) == pytest.approx(record['totalMs'])


@pytest.fixture
def delivery(monkeypatch):
    owner = EventDeliveryCoordinator(workers=2, capacity=32, byte_capacity=4096,
                                     retry_attempts=2, retry_delay=0, idle_timeout=.02)
    monkeypatch.setattr('lib.tasks_pkg.event_delivery.coordinator', lambda: owner)
    yield owner
    assert owner.close(timeout=2)


def _barrier_lines(caplog):
    return [record for record in caplog.records
            if record.name == persistence_wait.logger.name
            and record.getMessage().startswith('[EventBarrier]')]


def test_slow_event_barrier_warns_once_without_emitting_a_task_event(delivery, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=persistence_wait.logger.name)
    monkeypatch.setattr(persistence_wait, 'SLOW_EVENT_BARRIER_SECONDS', 0.05)
    task = {'id': _TASK_ID, 'convId': 'conv-slow'}
    lane = DeliveryLane(task)
    task['_eventDeliveryLane'] = lane
    release = threading.Event()
    announce = persistence_wait._announce_slow_event_barrier

    def announce_then_release(*args, **kwargs):
        announce(*args, **kwargs)
        release.set()

    monkeypatch.setattr(persistence_wait, '_announce_slow_event_barrier', announce_then_release)
    delivery.submit(lane, 1, 128, lambda: release.wait(3), lambda: None)
    delivery.submit(lane, 2, 128, lambda: None, lambda: None)
    rpt.begin_round_prep(task, 0)

    with rpt.round_prep_step(task, 'barrier'):
        awaited = persistence_wait.checkpoint_invocation(task, [], 0, phase='tools_in_flight')

    assert awaited == 2
    assert lane.accepted == lane.completed == 2
    lines = _barrier_lines(caplog)
    warnings = [record for record in lines if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert message.startswith(f'[EventBarrier] slow task={_TASK_ID} conv=conv-slow '
                              'checkpoint=tools_in_flight checkpointRound=0 elapsedMs=')
    assert message.endswith(' pendingEvents=2')
    assert int(message.split('elapsedMs=')[1].split()[0]) >= 50
    assert warnings[0].tofu_event_name == 'event_barrier_slow'
    assert warnings[0].tofu_event_fields['pending_events'] == 2
    [released] = [record.getMessage() for record in lines if record.levelno == logging.INFO]
    assert released.startswith(f'[EventBarrier] released task={_TASK_ID} ')
    assert ' awaitedEvents=2 persistCalls=2 persistMs=' in released
    assert released.endswith(' outcome=ok')
    # The head's storage wait is persist time; nothing queued for a pump.
    assert int(released.split('persistMs=')[1].split()[0]) >= 50
    assert int(released.split('queuedMs=')[1].split()[0]) < 50
    record = rpt.finish_round_prep(task)
    assert record['stepsMs']['barrier'] >= 50
    assert record['barrierMs']['events'] >= 50


def test_fast_event_barrier_is_silent(delivery, caplog):
    caplog.set_level(logging.DEBUG, logger=persistence_wait.logger.name)
    task = {'id': _TASK_ID, 'convId': 'conv-fast'}
    lane = DeliveryLane(task)
    task['_eventDeliveryLane'] = lane
    delivery.submit(lane, 1, 128, lambda: None, lambda: None)

    assert persistence_wait.checkpoint_invocation(task, [], 0, phase='ready') in (0, 1)
    assert persistence_wait.checkpoint_invocation({'id': 'no-lane'}, [], 0, phase='tools_in_flight') == 0
    assert lane.completed == 1
    assert _barrier_lines(caplog) == []


def test_logical_request_start_consumes_prep_and_ttft_names_its_request_clock(monkeypatch, caplog):
    import lib.tasks_pkg.manager._stream as stream_module

    caplog.set_level(logging.INFO, logger=stream_module.logger.name)
    caplog.set_level(logging.INFO, logger=rpt.logger.name)
    task = {
        'id': _TASK_ID, 'convId': '', '_userId': 1, 'status': 'running',
        'content': '', 'thinking': '', 'config': {'userId': 1}, 'events': [],
        'toolRounds': [], 'content_lock': threading.Lock(),
        'events_lock': threading.Lock(), '_dispatch_heartbeat': 0.0,
        '_t_prep_done': time.monotonic() - 0.5,
    }
    monkeypatch.setattr(stream_module, 'append_event',
                        lambda current, event: current['events'].append(event))
    monkeypatch.setattr(stream_module, 'dispatch_stream',
                        lambda _body, **kwargs: ({'role': 'assistant', 'content': 'ok'}, 'stop', {}))
    timing = rpt.begin_round_prep(task, 2)
    timing.started -= 2.0

    from lib.tasks_pkg.message_input_receipts import stream_with_receipts
    stream_with_receipts(
        stream_module.stream_llm_response, task,
        {'model': 'kimi-k3', 'messages': [{'role': 'user', 'content': 'go'}]}, tag='R3')

    assert '_roundPrepTiming' not in task
    [start] = [event for event in task['events']
               if event.get('type') == EventType.MODEL_REQUEST_START]
    [record] = _prep_records(caplog)
    assert record['roundNum'] == start['roundNum'] == 3
    assert record['spanId'] == start['spanId']
    assert record['requestTag'] == 'R3'
    assert record['totalMs'] >= 2000
    # Pre-dispatch time is named at the receipt boundary; the request start
    # names its own setup and start-event appends, leaving no residue.
    assert record['stepsMs']['dispatch'] >= 2000
    assert record['stepsMs']['request'] < 1000
    assert record['stepsMs']['other'] == 0.0
    [ttft] = [record.getMessage() for record in caplog.records if 'TTFT=' in record.getMessage()]
    assert '(context-ready→first-token), request=' in ttft
    assert '(stream-entry→first-token, after build_body+barrier)' in ttft
    # Monotonic anchors on both sides: 0.5 s before stream entry, never a
    # wall-minus-monotonic value.
    assert 0.5 <= float(ttft.split('TTFT=')[1].split('s')[0]) < 60
