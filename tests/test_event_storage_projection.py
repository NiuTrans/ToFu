"""Storage-only projections for durable task events.

The live SSE object must retain cache-debugging diagnostics while the durable
``round_usage`` copy omits backend-only ``_wire_*`` bulk.  This is deliberately
tested both as a pure non-mutation contract and through the real batch lane.
"""

from __future__ import annotations

import copy
import json
import uuid

import pytest

pytestmark = pytest.mark.unit


def _payload(row):
    value = row['payload']
    return value if isinstance(value, dict) else json.loads(value)


def test_round_usage_projection_is_storage_only_and_forward_compatible():
    from lib.tasks_pkg.event_log import _project_round_usage_for_storage

    wire_fp = [{'role': 'user', 'bytes': 42000}]
    event = {
        'type': 'round_usage',
        'roundNum': 3,
        'usage': {
            'prompt_tokens': 1200,
            'completion_tokens': 80,
            'trace_id': 'trace-keep',
            '_wire_fp': wire_fp,
            '_wire_field_bytes': {'messages': 42000},
            '_wire_bytes': [10, 20, 30],
            '_future_public_field': 'keep-me',
        },
    }
    projected = _project_round_usage_for_storage(event)

    assert projected is not event
    assert projected['usage'] is not event['usage']
    assert not any(k.startswith('_wire_') for k in projected['usage'])
    assert projected['usage']['trace_id'] == 'trace-keep'
    assert projected['usage']['_future_public_field'] == 'keep-me'
    # The live event and nested diagnostic structures remain untouched.
    assert event['usage']['_wire_fp'] is wire_fp
    assert event['usage']['_wire_field_bytes']['messages'] == 42000


def test_non_round_event_is_not_projected():
    from lib.tasks_pkg.event_log import _project_round_usage_for_storage

    event = {'type': 'diagnostic', 'usage': {'_wire_fp': ['must-stay']}}
    assert _project_round_usage_for_storage(event) is event


def test_terminal_event_projection_strips_all_usage_nests_without_mutation():
    from lib.tasks_pkg.event_log import _project_usage_diagnostics_for_storage

    event = {
        'type': 'done',
        'usage': {'input_tokens': 10, '_wire_bytes': [1, 2, 3]},
        'apiRounds': [
            {'round': 1, 'usage': {
                'trace_id': 'keep', '_dispatch': {'provider': 'keep'},
                '_wire_field_bytes': {'messages': 'x' * 1000},
            }},
            {'round': 2, 'usage': {'output_tokens': 4}},
        ],
    }
    projected = _project_usage_diagnostics_for_storage(event)
    assert projected is not event
    assert '_wire_bytes' not in projected['usage']
    assert '_wire_field_bytes' not in projected['apiRounds'][0]['usage']
    assert projected['apiRounds'][0]['usage']['trace_id'] == 'keep'
    assert projected['apiRounds'][0]['usage']['_dispatch'] == {'provider': 'keep'}
    assert '_wire_bytes' in event['usage']
    assert '_wire_field_bytes' in event['apiRounds'][0]['usage']


def test_terminal_projection_covers_nested_committed_and_parent_messages():
    from lib.tasks_pkg.event_log import _project_usage_diagnostics_for_storage

    event = {
        'type': 'done',
        'committedMessage': {
            'content': 'kept',
            'usage': {'total_tokens': 3, '_wire_region': {'a': 1}},
            'apiRounds': [{'usage': {
                'input_tokens': 2, '_wire_bytes': [1, 2, 3],
            }}],
        },
        'parentMessage': {
            'content': 'also kept',
            'apiRounds': [{'usage': {
                'output_tokens': 1, '_wire_field_bytes': [4, 5],
            }}],
        },
    }
    original = copy.deepcopy(event)

    projected = _project_usage_diagnostics_for_storage(event)

    assert projected['committedMessage']['content'] == 'kept'
    assert projected['committedMessage']['usage'] == {'total_tokens': 3}
    assert projected['committedMessage']['apiRounds'][0]['usage'] == {
        'input_tokens': 2,
    }
    assert projected['parentMessage']['content'] == 'also kept'
    assert projected['parentMessage']['apiRounds'][0]['usage'] == {
        'output_tokens': 1,
    }
    assert event == original


def test_terminal_persist_omits_api_round_wire_bulk():
    from lib.database import DOMAIN_CHAT, pooled_db
    from lib.tasks_pkg.event_log import append_persistent_event, flush_pending

    tid = f'wire-terminal-{uuid.uuid4().hex[:10]}'
    live = {
        'type': 'done', 'usage': {'input_tokens': 4, '_wire_bytes': [1] * 100},
        'apiRounds': [{'round': 1, 'usage': {
            'trace_id': 'trace-keep', '_wire_field_bytes': {'x': 'y' * 10000},
        }}],
    }
    try:
        append_persistent_event(tid, 1, live)
        assert flush_pending(tid) is True
        with pooled_db(DOMAIN_CHAT) as db:
            row = db.execute(
                'SELECT payload FROM task_events WHERE task_id=? AND event_id=?',
                (tid, 1)).fetchone()
        stored = _payload(row)
        assert not any(k.startswith('_wire_') for k in stored['usage'])
        assert not any(k.startswith('_wire_')
                       for k in stored['apiRounds'][0]['usage'])
        assert stored['apiRounds'][0]['usage']['trace_id'] == 'trace-keep'
        assert '_wire_field_bytes' in live['apiRounds'][0]['usage']
    finally:
        with pooled_db(DOMAIN_CHAT) as db:
            db.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
            db.commit()


def test_persisted_round_usage_omits_wire_bulk_but_keeps_inspector_fields():
    from lib.database import DOMAIN_CHAT, pooled_db
    from lib.tasks_pkg.event_log import append_persistent_event, flush_pending

    tid = f'wire-projection-{uuid.uuid4().hex[:10]}'
    live_event = {
        'type': 'round_usage', 'roundNum': 1, 'tag': 'R1', 'model': 'm-test',
        'tokensIn': 11, 'tokensOut': 7,
        'usage': {
            'trace_id': 'trace-durable', 'stream_elapsed_ms': 321,
            'cache_read_input_tokens': 5,
            '_wire_fp': [{'large': 'x' * 10000}],
            '_wire_bytes': list(range(100)),
        },
    }
    try:
        append_persistent_event(tid, 1, live_event)
        assert flush_pending(tid) is True
        with pooled_db(DOMAIN_CHAT) as db:
            row = db.execute(
                'SELECT payload FROM task_events WHERE task_id=? AND event_id=?',
                (tid, 1)).fetchone()
        stored = _payload(row)
        assert stored['usage']['trace_id'] == 'trace-durable'
        assert stored['usage']['stream_elapsed_ms'] == 321
        assert stored['usage']['cache_read_input_tokens'] == 5
        assert not any(k.startswith('_wire_') for k in stored['usage'])
        # The object subsequently sent to live subscribers was not changed.
        assert '_wire_fp' in live_event['usage']
    finally:
        with pooled_db(DOMAIN_CHAT) as db:
            db.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
            db.commit()
