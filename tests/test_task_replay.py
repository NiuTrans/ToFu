"""Framework-free contracts for shared live/durable task replay pages."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.task_replay import (
    TASK_REPLAY_FORMAT,
    TASK_REPLAY_TERMINAL_EVENT_TYPES,
    TASK_REPLAY_TERMINAL_STATUSES,
    TaskReplayPage,
    memory_replay_page,
    missing_replay_page,
    project_bounded_replay_payload,
    safe_replay_cursor,
    sse_last_event_id_to_cursor,
    sse_resume_serviceable,
    task_memory_replay_page,
    task_replay_request_contract,
    task_replay_contract,
    task_replay_http_status,
    task_terminal_event_type,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(None, 0), ('', 0), (-4, 0), ('12', 12), (3.8, 3), ('bad', 0)],
)
def test_safe_replay_cursor_normalizes_untrusted_transport_values(
        value, expected):
    assert safe_replay_cursor(value) == expected


def test_replay_request_contract_owns_query_identity_and_bounds():
    first = task_replay_request_contract()
    assert first == {
        'queryField': 'cursor',
        'minimum': 0,
        'default': 0,
        'description': 'Producer-owned next event sequence.',
    }
    first['queryField'] = 'changed'
    assert task_replay_request_contract()['queryField'] == 'cursor'


def test_memory_page_clamps_future_cursor_to_authoritative_boundary():
    page = memory_replay_page(
        [{'type': 'start', 'seq': 0}], 99,
        status='running', done=False,
    )

    assert page.events == []
    assert page.next_cursor == 1
    assert page.cursor_reset is True
    assert page.payload() == {
        'format': TASK_REPLAY_FORMAT,
        'ok': True,
        'events': [],
        'next_cursor': 1,
        'status': 'running',
        'done': False,
        'cursor': {'requested': 99, 'next': 1, 'reset': True},
    }


def test_memory_page_frames_keep_absolute_sequences_after_head_eviction():
    page = memory_replay_page(
        [
            {'type': 'progress', 'seq': 40},
            {'type': 'progress', 'seq': 41},
            {'type': 'progress', 'seq': 42},
        ],
        41,
        status='running', done=False, base_cursor=40,
    )

    assert page.first_cursor == 41
    assert page.frames == [
        (41, {'type': 'progress', 'seq': 41}),
        (42, {'type': 'progress', 'seq': 42}),
    ]
    assert page.next_cursor == 43
    assert page.cursor_reset is False


def test_durable_frames_and_http_cursor_preserve_sparse_sequences():
    page = TaskReplayPage(
        events=[
            {'type': 'phase', 'seq': 3},
            {'type': 'done', 'seq': 9},
        ],
        next_cursor=10,
        run_status='done',
        done=True,
        requested_cursor=0,
        cursor_reset=True,
    )

    assert page.frames == [
        (3, {'type': 'phase', 'seq': 3}),
        (9, {'type': 'done', 'seq': 9}),
    ]
    projected = project_bounded_replay_payload(
        page.payload(), max_events=1, max_event_bytes=10_000)
    assert [event['seq'] for event in projected['events']] == [3]
    assert projected['next_cursor'] == 4
    assert projected['caught_up'] is False


def test_interrupted_status_is_terminal_without_becoming_an_event_type():
    assert 'interrupted' in TASK_REPLAY_TERMINAL_STATUSES
    assert 'interrupted' not in TASK_REPLAY_TERMINAL_EVENT_TYPES
    page = task_memory_replay_page(
        {'status': 'interrupted', 'events': [], '_eventNextSeq': 7}, 7)
    assert page.done is True
    assert page.next_cursor == 7


def test_bounded_http_page_advances_only_past_delivered_absolute_events():
    events = [
        {'type': 'progress', 'seq': sequence}
        for sequence in range(40, 43)
    ]
    full = memory_replay_page(
        events, 0, status='done', done=True, base_cursor=40,
    ).payload({
        'finishedAt': 123,
        'artifact_quality': {'degraded': False},
        'result': {'answer': 'complete'},
    })

    first = project_bounded_replay_payload(
        full, max_events=2, max_event_bytes=10_000)

    assert [event['seq'] for event in first['events']] == [40, 41]
    assert first['next_cursor'] == 42
    assert first['cursor'] == {'requested': 0, 'next': 42, 'reset': True}
    assert first['status'] == 'done'
    assert first['done'] is False
    assert first['caught_up'] is False
    assert 'finishedAt' not in first
    assert 'artifact_quality' not in first
    assert 'result' not in first
    assert len(full['events']) == 3
    assert full['next_cursor'] == 43

    final = project_bounded_replay_payload(
        memory_replay_page(
            events, first['next_cursor'], status='done', done=True,
            base_cursor=40,
        ).payload({
            'finishedAt': 123,
            'artifact_quality': {'degraded': False},
            'result': {'answer': 'complete'},
        }),
        max_events=2,
        max_event_bytes=10_000,
    )
    assert [event['seq'] for event in final['events']] == [42]
    assert final['next_cursor'] == 43
    assert final['caught_up'] is True
    assert final['done'] is True
    assert final['result'] == {'answer': 'complete'}


def test_bounded_http_page_targets_bytes_without_splitting_one_event():
    events = [
        {'type': 'delta', 'seq': sequence, 'content': '界' * 200}
        for sequence in range(2)
    ]
    full = memory_replay_page(
        events, 0, status='running', done=False,
    ).payload()

    page = project_bounded_replay_payload(
        full, max_events=128, max_event_bytes=700)

    assert [event['seq'] for event in page['events']] == [0]
    assert page['next_cursor'] == 1
    assert page['caught_up'] is False

    oversized = project_bounded_replay_payload(
        full, max_events=128, max_event_bytes=1)
    assert [event['seq'] for event in oversized['events']] == [0]
    assert oversized['next_cursor'] == 1


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(None, None), ('', None), ('bad', None), (-1, None), ('0', 1), (2047, 2048)],
)
def test_sse_last_event_id_has_one_canonical_off_by_one_conversion(
        value, expected):
    assert sse_last_event_id_to_cursor(value) == expected


def test_sse_resume_serviceability_checks_both_rolling_window_edges():
    assert sse_resume_serviceable(39, base_cursor=40, next_cursor=43)
    assert sse_resume_serviceable(42, base_cursor=40, next_cursor=43)
    assert not sse_resume_serviceable(38, base_cursor=40, next_cursor=43)
    assert not sse_resume_serviceable(43, base_cursor=40, next_cursor=43)


def test_task_memory_page_uses_absolute_cursor_after_runtime_rollover():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('cursor-rollover', max_events=3, push_channel='')
    task = runtime.create(user_id=1, task_id='cursor-rollover-task')
    for index in range(5):
        runtime.append_event(task['id'], {'type': 'progress', 'index': index})

    page = task_memory_replay_page(task, 3)
    assert [(seq, event['index']) for seq, event in page.frames] == [
        (3, 3), (4, 4),
    ]
    assert page.next_cursor == 5
    assert page.cursor_reset is False


def test_task_memory_page_delivers_exactly_once_through_many_rollovers():
    """A caught-up reader must never freeze when physical length plateaus."""
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('cursor-soak', max_events=8, push_channel='')
    task = runtime.create(user_id=1, task_id='cursor-soak-task')
    cursor = 0
    seen: list[tuple[int, int]] = []

    for index in range(5000):
        runtime.append_event(task['id'], {'type': 'progress', 'index': index})
        if index % 4 == 3:
            page = task_memory_replay_page(task, cursor)
            assert page.cursor_reset is False
            seen.extend((seq, event['index']) for seq, event in page.frames)
            cursor = page.next_cursor

    assert seen == [(index, index) for index in range(5000)]
    assert cursor == 5000
    assert len(task['events']) == 8
    assert task['_eventBaseSeq'] == 4992


@pytest.mark.parametrize('stale_hint', [0, 999])
def test_runtime_reconciles_stale_private_sequence_hint_from_wire_tail(
        stale_hint):
    """Recovered metadata cannot create duplicate or future event ids."""
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('cursor-recover', max_events=8, push_channel='')
    task = runtime.create(user_id=1, task_id=f'cursor-recover-{stale_hint}')
    with task['events_lock']:
        task['events'] = [
            {'type': 'progress', 'seq': 40},
            {'type': 'progress', 'seq': 41},
        ]
        task['_eventBaseSeq'] = 40
        task['_eventNextSeq'] = stale_hint

    assert runtime.append_event(task['id'], {'type': 'progress'}) == 42
    assert [event['seq'] for event in task['events']] == [40, 41, 42]
    assert task['_eventNextSeq'] == 43


def test_all_in_process_task_stream_consumers_use_absolute_replay_pages():
    """A new adapter must not resurrect physical-list cursor semantics."""
    root = Path(__file__).resolve().parents[1]
    consumers = [
        'routes/api_v1/chat.py',
        'routes/api_v1/agent_run.py',
        'routes/api_v1/tasks.py',
        'lib/compat/openai.py',
        'lib/compat/anthropic.py',
        'lib/tasks_pkg/entry.py',
        'lib/tasks_pkg/sync_run.py',
    ]
    for relative in consumers:
        source = (root / relative).read_text(encoding='utf-8')
        assert "task['events'][cursor:]" not in source, relative
        assert "cursor = len(task['events'])" not in source, relative
        assert 'task_memory_replay_page' in source, relative


def test_missing_page_uses_same_wire_shape_without_inventing_run_state():
    payload = missing_replay_page('7').payload({'message': 'Run not found'})

    assert payload == {
        'format': TASK_REPLAY_FORMAT,
        'ok': False,
        'events': [],
        'next_cursor': 7,
        'status': '',
        'done': True,
        'cursor': {'requested': 7, 'next': 7, 'reset': False},
        'error': 'not_found',
        'message': 'Run not found',
    }


def test_replay_http_status_is_shared_by_live_and_durable_adapters():
    assert task_replay_http_status(memory_replay_page(
        [], 0, status='running', done=False,
    ).payload()) == 200
    assert task_replay_http_status(missing_replay_page(0).payload()) == 404
    assert task_replay_http_status({'ok': False, 'error': 'corrupt'}) == 500
    assert task_replay_http_status(None) == 500


def test_terminal_event_type_is_owned_by_the_replay_protocol():
    for status in TASK_REPLAY_TERMINAL_EVENT_TYPES:
        assert task_terminal_event_type(status) == status
    with pytest.raises(ValueError, match='unsupported terminal task status'):
        task_terminal_event_type('running')


def test_replay_contract_declares_producer_cursor_ownership():
    assert task_replay_contract() == {
        'format': TASK_REPLAY_FORMAT,
        'httpStatuses': {
            'success': 200,
            'notFound': 404,
            'failure': 500,
        },
        'notFoundReason': 'not_found',
        'statusField': 'status',
        'nextCursorField': 'next_cursor',
        'pageFields': [
            'format', 'ok', 'events', 'next_cursor', 'status', 'done',
            'cursor',
        ],
        'cursor': {
            'queryField': 'cursor',
            'minimum': 0,
            'default': 0,
            'description': 'Producer-owned next event sequence.',
            'field': 'cursor',
            'requestedField': 'requested',
            'nextField': 'next',
            'resetField': 'reset',
            'unit': 'next event sequence',
            'producerOwned': True,
            'futureCursorReset': True,
        },
        'terminalField': 'done',
        'caughtUpField': 'caught_up',
        'eventsField': 'events',
        'eventTypeField': 'type',
        'eventSequenceField': 'seq',
        'eventRequiredFields': ['type', 'seq'],
        'unknownEventTypes': 'allow',
        'terminalEventTypes': ['done', 'error', 'aborted'],
        'terminalSnapshot': {
            'field': 'run',
            'when': {'field': 'done', 'equals': True},
            'optional': True,
        },
    }
