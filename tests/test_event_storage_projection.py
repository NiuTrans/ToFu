"""Defense-in-depth storage projections for durable task events.

Normal round emitters now remove consumed ``_wire_*`` evidence before live
retention. Legacy/imported/raw callers may still hand this boundary an
unprojected object, so persistence must remove the private bulk without
mutating its caller. Both the pure copy contract and real batch lane are pinned.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _payload(row):
    value = row.get('payload', row.get('event'))
    return value if isinstance(value, dict) else json.loads(value)


def test_round_usage_projection_is_storage_only_and_forward_compatible():
    from lib.tasks_pkg.event_log import _project_usage_diagnostics_for_storage

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
    projected = _project_usage_diagnostics_for_storage(event)

    assert projected is not event
    assert projected['usage'] is not event['usage']
    assert not any(k.startswith('_wire_') for k in projected['usage'])
    assert projected['usage']['trace_id'] == 'trace-keep'
    assert projected['usage']['_future_public_field'] == 'keep-me'
    # The live event and nested diagnostic structures remain untouched.
    assert event['usage']['_wire_fp'] is wire_fp
    assert event['usage']['_wire_field_bytes']['messages'] == 42000


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


def test_terminal_persist_omits_api_round_wire_bulk(chat_sidecar):
    from lib.storage import get_storage_client
    from lib.tasks_pkg.event_log import append_persistent_event, flush_pending

    tid = f'wire-terminal-{uuid.uuid4().hex[:10]}'
    live = {
        'type': 'done', 'usage': {'input_tokens': 4, '_wire_bytes': [1] * 100},
        'apiRounds': [{'round': 1, 'usage': {
            'trace_id': 'trace-keep', '_wire_field_bytes': {'x': 'y' * 10000},
        }}],
    }
    append_persistent_event(tid, 1, live)
    assert flush_pending(tid) is True
    rows = get_storage_client().query(
        'event.list', {'task_id': tid, 'after_sequence': -1, 'limit': 10})
    stored = _payload(rows[0])
    assert not any(k.startswith('_wire_') for k in stored['usage'])
    assert not any(k.startswith('_wire_')
                   for k in stored['apiRounds'][0]['usage'])
    assert stored['apiRounds'][0]['usage']['trace_id'] == 'trace-keep'
    assert '_wire_field_bytes' in live['apiRounds'][0]['usage']


def test_persisted_round_usage_omits_wire_bulk_but_keeps_inspector_fields(
        chat_sidecar):
    from lib.storage import get_storage_client
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
    append_persistent_event(tid, 1, live_event)
    assert flush_pending(tid) is True
    rows = get_storage_client().query(
        'event.list', {'task_id': tid, 'after_sequence': -1, 'limit': 10})
    stored = _payload(rows[0])
    assert stored['usage']['trace_id'] == 'trace-durable'
    assert stored['usage']['stream_elapsed_ms'] == 321
    assert stored['usage']['cache_read_input_tokens'] == 5
    assert not any(k.startswith('_wire_') for k in stored['usage'])
    # The object subsequently sent to live subscribers was not changed.
    assert '_wire_fp' in live_event['usage']


def test_sidecar_authority_projects_raw_event_append_with_byte_budget(
        chat_sidecar):
    """Generic producers cannot bypass the canonical durable projection."""
    from lib.storage import get_storage_client

    tid = f'wire-authority-{uuid.uuid4().hex[:10]}'
    raw_event = {
        'type': 'round_usage', 'roundNum': 2, 'model': 'm-authority',
        'usage': {
            'trace_id': 'trace-authority', 'stream_elapsed_ms': 654,
            '_future_public_field': {'keep': True},
            '_wire_fp': [
                {'role': 'user', 'content': 'x' * 12_000},
            ],
            '_wire_field_bytes': [
                {'messages': index * 10} for index in range(400)
            ],
            '_wire_bytes': list(range(1_000)),
        },
    }
    raw_bytes = len(json.dumps(
        raw_event, ensure_ascii=False, separators=(',', ':')).encode())
    client = get_storage_client(write=True)
    client.command('event.append', {
        'task_id': tid, 'sequence': 1, 'event': raw_event,
    }, None, priority='event')

    rows = client.query(
        'event.list', {'task_id': tid, 'after_sequence': -1, 'limit': 10})
    stored = _payload(rows[0])
    stored_bytes = len(json.dumps(
        stored, ensure_ascii=False, separators=(',', ':')).encode())
    assert stored['usage']['trace_id'] == 'trace-authority'
    assert stored['usage']['stream_elapsed_ms'] == 654
    assert stored['usage']['_future_public_field'] == {'keep': True}
    assert not any(key.startswith('_wire_') for key in stored['usage'])
    assert stored_bytes <= raw_bytes * 0.05
    # The storage authority's defense remains copy-on-write.
    assert '_wire_fp' in raw_event['usage']
