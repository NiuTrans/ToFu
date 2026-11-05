"""An early-announced call shows queued until its handler is actually entered.

The streaming accumulator announces every call as soon as its arguments
finish streaming, but only read-only prefetches really start then. Every other
call is announced ``queued`` and flips to running (``searching``) at the single
dispatcher choke point where its worker enters the handler, published as a
status-bearing ``tool_progress`` frame that the turn projection folds
structurally. Refused, orphaned and dangling queued rows still settle.
"""
from __future__ import annotations

import json
import threading

import pytest

from lib.project_mod.config import build_root_policy, clear_conv_state

pytestmark = pytest.mark.unit


@pytest.fixture
def task(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'module.py').write_text('needle = 1\n')
    owner = {
        'id': f'queued-status-{tmp_path.name}',
        'convId': f'queued-status-chat-{tmp_path.name}',
        '_userId': 7, '_invocationStorageFree': True, 'aborted': False,
        'config': {'projectPath': str(root)},
        '_workspaceRootPolicy': build_root_policy(str(root)),
        'messages': [], 'toolRounds': [], 'events': [],
        'events_lock': threading.Lock(),
    }
    # The task is not registered with a delivery lane; record the frames the
    # emitters hand to the canonical append seam.
    import lib.tasks_pkg.manager as manager
    recorded = owner['events']
    monkeypatch.setattr(manager, 'append_event',
                        lambda _task, event: recorded.append(dict(event)))
    yield owner
    clear_conv_state(owner['id'])


def _call(call_id, name, args):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


def _events(task, kind):
    return [event for event in task['events'] if event.get('type') == kind]


def _announce(task, *calls):
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    for call in calls:
        accumulator.on_tool_call_ready(call)
    return accumulator


def test_unprefetched_call_is_announced_queued_and_prefetched_read_running(task):
    accumulator = _announce(
        task,
        _call('call-write', 'write_file', {'path': 'new.txt', 'content': 'x'}),
        _call('call-read', 'read_files', {'path': 'module.py'}))
    try:
        rows = accumulator.announced_tc_map
        assert rows['call-write'][1]['status'] == 'queued'
        assert rows['call-read'][1]['status'] == 'searching'
        starts = {event['toolCallId']: event for event in _events(task, 'tool_start')}
        assert starts['call-write']['status'] == 'queued'
        assert 'status' not in starts['call-read']
    finally:
        accumulator.close()


def test_declined_read_prefetch_is_announced_queued(task):
    accumulator = _announce(task, _call(
        'call-bound', 'read_files', {'path': 'module.py', 'bind_sources': True}))
    try:
        assert accumulator.announced_tc_map['call-bound'][1]['status'] == 'queued'
    finally:
        accumulator.close()


def _dispatch(task, items, monkeypatch, handler):
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    monkeypatch.setattr(pipeline, '_execute_tool_one', handler)
    monkeypatch.setattr(pipeline, '_execute_tool_one_in_pool', handler)
    pipeline.execute_tool_pipeline(
        task, items, task['config'], task['config']['projectPath'], True,
        None, [], [], 1, 'test-model')


def test_queued_call_flips_to_running_exactly_at_handler_entry(task, monkeypatch):
    call = _call('call-write', 'write_file', {'path': 'new.txt', 'content': 'x'})
    accumulator = _announce(task, call)
    rn, row = accumulator.announced_tc_map['call-write']
    accumulator.close()
    observed = {}

    def handler(_task, _tc, _name, call_id, _args, _rn, entry, *_rest, **_kwargs):
        progress = [event for event in _events(task, 'tool_progress')
                    if event.get('status')]
        observed['status'] = entry['status']
        observed['progress'] = progress
        return call_id, 'ok', False

    _dispatch(task, [(call, 'write_file', 'call-write',
                      json.loads(call['function']['arguments']), rn, row, None)],
              monkeypatch, handler)

    assert observed['status'] == 'searching'
    assert [(event['toolCallId'], event['roundNum'], event['status'])
            for event in observed['progress']] == [('call-write', rn, 'searching')]
    assert row['status'] == 'done'


def test_queued_call_stopped_before_entry_never_shows_running(task, monkeypatch):
    call = _call('call-write', 'write_file', {'path': 'new.txt', 'content': 'x'})
    accumulator = _announce(task, call)
    rn, row = accumulator.announced_tc_map['call-write']
    accumulator.close()
    task['aborted'] = True

    def handler(*_args, **_kwargs):
        raise AssertionError('a stopped call must not enter its handler')

    _dispatch(task, [(call, 'write_file', 'call-write',
                      json.loads(call['function']['arguments']), rn, row, None)],
              monkeypatch, handler)

    assert not [event for event in _events(task, 'tool_progress') if event.get('status')]
    assert row['status'] == 'aborted'


def test_orphaned_queued_announcement_is_settled(task):
    accumulator = _announce(task, _call(
        'call-orphan', 'write_file', {'path': 'new.txt', 'content': 'x'}))
    try:
        _rn, row = accumulator.announced_tc_map['call-orphan']
        assert accumulator.reconcile_announced_rounds({'tool_calls': []}) == 1
        assert row['status'] == 'aborted'
    finally:
        accumulator.close()


def test_dangling_queued_round_is_swept_at_task_end(task):
    from lib.tasks_pkg.orchestrator._finalize import _finalize_dangling_tool_rounds
    accumulator = _announce(task, _call(
        'call-dangling', 'write_file', {'path': 'new.txt', 'content': 'x'}))
    accumulator.close()
    assert _finalize_dangling_tool_rounds(task) == 1
    assert task['toolRounds'][0]['status'] == 'aborted'


def test_status_transition_frame_is_a_structural_projection_update():
    from lib.turn_lifecycle import task_event_projection_is_progress
    assert task_event_projection_is_progress({'type': 'tool_progress', 'chunk': 'x'})
    assert not task_event_projection_is_progress(
        {'type': 'tool_progress', 'status': 'searching'})


def test_activity_timeline_waits_for_queued_start_then_runs():
    from lib.agent_core.events import EventType, build_event
    from lib.turn_activity_timeline import fold_activity_timeline as fold
    task = {'id': 'task-a', '_attemptId': 'attempt-a'}
    start = build_event(EventType.TOOL_START, roundNum=1, toolCallId='c1',
                        toolName='write_file', query='write', status='queued')
    timeline = fold(None, start, task)
    assert [entry['status'] for entry in timeline['entries']] == ['waiting']
    progress = build_event(EventType.TOOL_PROGRESS, roundNum=1, toolCallId='c1',
                           toolName='write_file', status='searching')
    timeline = fold(timeline, progress, task)
    assert [entry['status'] for entry in timeline['entries']] == ['running']
