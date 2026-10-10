"""Producer clocks keep elapsed time honest across observation and wall jumps."""

import pytest

from lib.agent_core import execution_clock as clocks
from lib.agent_core.task_runtime import TaskRuntime
from lib.event_envelope import EventFormatError, execution_event_time_ms

pytestmark = pytest.mark.unit


def test_runtime_retains_request_origin_through_registration_and_wall_jump(monkeypatch):
    ticks = [10_000_000_000]
    monkeypatch.setattr(clocks.time, 'monotonic_ns', lambda: ticks[0])
    ingress = clocks.ExecutionClock.start(
        origin_at_ms=1_000_000, origin_monotonic_ns=ticks[0])
    ticks[0] += 750_000_000
    monkeypatch.setattr(clocks.time, 'time', lambda: -1000.0)
    runtime = TaskRuntime(kind='ingress-clock-test')
    task = runtime.create(user_id=1, execution_clock=ingress)
    event = {'type': 'round_start', 'roundNum': 1}
    runtime.append_event(task['id'], event, deliver_push=False)
    assert task['_executionClock'] is ingress
    assert event['executionAtMs'] == 1_000_750
    assert event['executionOriginAtMs'] == 1_000_000
    assert event['executionClockId'] == ingress.clock_id


def test_captured_origin_requires_matching_epoch_anchor():
    with pytest.raises(ValueError, match='epoch anchor'):
        clocks.ExecutionClock.start(origin_monotonic_ns=123)


def test_runtime_rejects_client_shaped_clock():
    with pytest.raises(TypeError, match='ExecutionClock'):
        TaskRuntime(kind='invalid-clock').create(user_id=1, execution_clock={})


def test_runtime_captures_before_delayed_observation_and_preserves_capture(monkeypatch):
    wall = [1_000.0]
    ticks = [10_000_000_000]
    monkeypatch.setattr(clocks.time, 'time', lambda: wall[0])
    monkeypatch.setattr(clocks.time, 'monotonic_ns', lambda: ticks[0])
    runtime = TaskRuntime(kind='clock-test')
    task = runtime.create(task_id='clock-test', user_id=1)
    event = {'type': 'phase', 'phase': 'working'}
    ticks[0] += 123_000_000
    clocks.stamp_execution_clock(task, event)
    captured = dict(event)
    wall[0] = -5000.0
    ticks[0] += 30_000_000_000
    runtime.append_event(task['id'], event, deliver_push=False)
    assert event['executionAtMs'] == captured['executionAtMs'] == 1_000_123
    assert event['executionOriginAtMs'] == 1_000_000
    assert event['executionClockId'] == captured['executionClockId']
    assert clocks.task_execution_time_ms(task) == 1_030_123
    wall[0] = 500_000.0
    ticks[0] += 2_000_000_000
    assert clocks.task_execution_time_ms(task) == 1_032_123
    delta = {'type': 'delta', 'content': 'x'}
    clocks.stamp_execution_clock(task, delta)
    assert delta['executionAtMs'] == 1_032_123
    assert 'executionClockId' not in delta
    assert 'executionOriginAtMs' not in delta
    assert execution_event_time_ms(delta, 500_000_000) == 1_032_123


def test_display_carrier_does_not_retime_child_or_claim_recovered_clock():
    parent = {'id': 'parent', '_executionClock': clocks.ExecutionClock.start()}
    child = {'type': 'tool_start', 'taskId': 'child', 'executionAtMs': 123}
    clocks.stamp_execution_clock(parent, child)
    assert child == {'type': 'tool_start', 'taskId': 'child', 'executionAtMs': 123}
    legacy = {'id': 'recovered'}
    event = {'type': 'phase'}
    clocks.stamp_execution_clock(legacy, event)
    assert event == {'type': 'phase'}


@pytest.mark.parametrize('timestamp', [True, False, None, '123', -1, 0, 1.5, 9_007_199_254_740_992])
def test_invalid_producer_timestamp_cannot_enter_storage(timestamp):
    with pytest.raises(EventFormatError, match='execution clock'):
        execution_event_time_ms({'executionAtMs': timestamp,
                                'executionOriginAtMs': 1, 'executionClockId': 'clock'}, 500)


def test_metadata_timestamp_is_producer_time_and_legacy_fallback_is_explicit():
    assert execution_event_time_ms({'executionAtMs': 123,
                                   'executionOriginAtMs': 100, 'executionClockId': 'clock'}, 9000) == 123
    assert execution_event_time_ms({'type': 'delta'}, 9000) == 9000
    with pytest.raises(EventFormatError):
        execution_event_time_ms({'executionClockId': 'clock'}, 9000)


def test_tool_terminal_clocks_share_task_axis_across_wall_clock_jump(monkeypatch):
    from lib.tasks_pkg.executor import _finalize
    wall = [1000.0]
    ticks = [10_000_000_000]
    monkeypatch.setattr(clocks.time, 'time', lambda: wall[0])
    monkeypatch.setattr(clocks.time, 'monotonic_ns', lambda: ticks[0])
    task = {'id': 'tool-clock', '_executionClock': clocks.ExecutionClock.start()}
    row = {'query': 'synthetic', 'toolName': 'read_files', 'toolCallId': 'call',
           'tStart': clocks.task_execution_time_ms(task)}
    events = []
    monkeypatch.setattr(_finalize, 'append_event', lambda _task, event: events.append(event))
    wall[0] += 50_000
    ticks[0] += 2_000_000_000
    _finalize._finalize_tool_round(task, 1, row, [])
    assert row['tEnd'] - row['tStart'] == 2000
    assert events[0]['tEnd'] - events[0]['tStart'] == 2000
