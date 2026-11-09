"""Projection capture owns the exact detached structure and its byte charge."""
import threading

import pytest

from lib.tasks_pkg.event_delivery import retained_snapshot_bytes
from lib.turn_lifecycle import capture_task_projection_snapshot

pytestmark = pytest.mark.unit


def _task():
    shared = {'count': 5, 'values': ['one', 'two']}
    return {
        'id': 'snapshot-task', 'status': 'running',
        'content': 'visible text', 'thinking': 'visible reasoning',
        'content_lock': threading.Lock(), 'phase': {'phase': 'tool_exec'},
        'segments': [shared, shared], 'usage': shared,
        'toolRounds': [{'round': 1, 'toolName': 'read_files', 'result': 'result'}],
        'config': {'checkpointToolRounds': [{'round': 0, 'result': 'checkpoint'}]},
        # Runtime objects and prompt history are not projection inputs.
        'socket': object(), 'messages': [{'content': 'private prompt'}],
    }


def _structure(source):
    return {key: value for key, value in source.items()
            if key not in {'content', 'thinking', 'phase'}}


def test_captured_structure_charge_matches_its_actual_detached_graph():
    task = _task()
    source, size = capture_task_projection_snapshot(task, {'type': 'round_end'})
    assert size == retained_snapshot_bytes(_structure(source))
    assert source['segments'][0] is source['segments'][1] is source['usage']
    assert source['usage'] is not task['usage']
    assert source['config']['checkpointToolRounds'] == [{'round': 0, 'result': 'checkpoint'}]
    assert 'socket' not in source and 'messages' not in source
    task['usage']['values'].append('later mutation')
    task['config']['checkpointToolRounds'][0]['result'] = 'later checkpoint'
    task['phase']['phase'] = 'later phase'
    assert source['usage']['values'] == ['one', 'two']
    assert source['config']['checkpointToolRounds'][0]['result'] == 'checkpoint'
    assert source['phase'] == {'phase': 'tool_exec'}


def test_progress_reuses_exact_structure_while_structural_events_replace_it():
    task = _task()
    prior, prior_size = capture_task_projection_snapshot(task, {'type': 'round_end'})
    task['content'] = 'next text'
    task['usage']['values'].append('new structure')
    progress, progress_size = capture_task_projection_snapshot(
        task, {'type': 'delta'}, previous_source=prior, previous_bytes=prior_size)
    assert progress is not prior
    assert progress['content'] == 'next text'
    assert progress['usage'] is prior['usage']
    assert progress_size == prior_size == retained_snapshot_bytes(_structure(progress))

    fresh, fresh_size = capture_task_projection_snapshot(
        task, {'type': 'round_end'}, previous_source=progress,
        previous_bytes=progress_size)
    assert fresh['usage'] is not prior['usage']
    assert fresh['usage']['values'] == ['one', 'two', 'new structure']
    assert fresh_size == retained_snapshot_bytes(_structure(fresh))


def test_late_frame_of_a_released_terminal_attempt_is_captured_without_its_history():
    """A reaped attempt's tool thread can return after terminal persistence.

    2026-11-09: the stuck-task reaper settled a Coordinator whose
    ``delegate_project_work`` was still running; terminal persistence released
    the attempt's history lists (the settled Turn owns them), and the call's
    late tool frame then crashed capture with ``'NoneType' object is not
    iterable``. Capture must leave released lists absent so the frame reaches
    the stale-attempt fence like every other late frame.
    """
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    task = {**_task(), '_turnId': 'turn-1', '_attemptId': 'attempt-1',
            '_checkpointToolRounds': [{'round': 0, 'result': 'checkpoint'}]}
    capture_task_projection_snapshot(task, {'type': 'round_end'})
    task.update(status='error', finishReason='error')
    assert _release_heavy_task_state(task)
    assert task['toolRounds'] is None and task['segments'] is None
    source, size = capture_task_projection_snapshot(
        task, {'type': 'tool_complete', 'toolCallId': 'late-call'})
    assert source['toolRounds'] is None and source['segments'] is None
    assert source['_checkpointToolRounds'] is None
    assert size == retained_snapshot_bytes(_structure(source))


def test_config_aliases_remain_detached_and_are_charged_once():
    task = _task()
    task['_checkpointToolRounds'] = task['config']['checkpointToolRounds']
    source, size = capture_task_projection_snapshot(task, {'type': 'round_end'})
    assert source['_checkpointToolRounds'] is not task['_checkpointToolRounds']
    assert source['config']['checkpointToolRounds'] is not task['_checkpointToolRounds']
    assert size == retained_snapshot_bytes(_structure(source))
