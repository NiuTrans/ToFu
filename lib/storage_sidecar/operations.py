"""Versioned semantic operation catalog; callers never provide SQL."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import time
from typing import Any, Callable

import orjson

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage.manifest import (
    ManifestError, validate_document, validate_manifest,
)
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


def _required_text(payload: Mapping[str, Any], key: str, maximum: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    return value


def _integer(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    if minimum is not None and value < minimum:
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    if maximum is not None and value > maximum:
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    return value


def _number(
    payload: Mapping[str, Any], key: str, *, minimum: float, maximum: float,
) -> float:
    value = payload.get(key)
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not minimum <= float(value) <= maximum):
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    return float(value)


def _expected_version(payload: Mapping[str, Any]) -> int | None:
    if 'expected_version' not in payload:
        return None
    return _integer(payload, 'expected_version', minimum=0)


def _dump(value: Any) -> bytes:
    try:
        return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise StorageError(
            'database_protocol_error', 'Storage value is not serializable') from exc


def _load(value: Any) -> Any:
    # PostgreSQL JSON/JSONB columns are decoded by psycopg before they reach
    # the adapter, while SQLite returns the canonical bytes/text we wrote.
    # Accept both representations so semantic operations stay backend-neutral.
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        value = value.encode('utf-8')
    return orjson.loads(value)


def _wire_document(value: Any) -> Any:
    if isinstance(value, bytes):
        return {'$bytes': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {key: _wire_document(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire_document(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OperationSpec:
    kind: str
    receipt_required: bool
    handler: Callable[[Session, Mapping[str, Any]], Any]


def _schema_version(session: Session, _payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
        ('schema_version',),
    )
    return {'version': int(row['meta_value']) if row else 0}


def _record_get(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, 'namespace', 128)
    key = _required_text(payload, 'key')
    row = session.fetch_one(
        'SELECT value_json, version, updated_at_ms FROM storage_records '
        'WHERE namespace = ? AND record_key = ?',
        (namespace, key),
    )
    if row is None:
        return None
    return {
        'value': _load(row['value_json']),
        'version': int(row['version']),
        'updated_at_ms': int(row['updated_at_ms']),
    }


def _record_list(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, 'namespace', 128)
    prefix = payload.get('prefix', '')
    if not isinstance(prefix, str) or len(prefix) > 512:
        raise StorageError(
            'database_protocol_error', 'Invalid prefix in storage request')
    limit = _integer(payload, 'limit', default=100, minimum=1, maximum=1000)
    rows = session.fetch_all(
        'SELECT record_key, value_json, version, updated_at_ms '
        'FROM storage_records WHERE namespace = ? AND record_key LIKE ? '
        'ORDER BY record_key LIMIT ?',
        (namespace, prefix + '%', limit),
    )
    return [{
        'key': row['record_key'],
        'value': _load(row['value_json']),
        'version': int(row['version']),
        'updated_at_ms': int(row['updated_at_ms']),
    } for row in rows]


def _record_put(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, 'namespace', 128)
    key = _required_text(payload, 'key')
    value = payload.get('value')
    encoded = _dump(value)
    now = int(time.time() * 1000)
    current = session.fetch_one(
        'SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?',
        (namespace, key),
    )
    expected = _expected_version(payload)
    actual = int(current['version']) if current else 0
    if expected is not None and expected != actual:
        raise StorageError(
            'database_conflict', 'Storage record version conflict')
    version = actual + 1
    session.execute(
        'INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) '
        'VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET '
        'value_json = excluded.value_json, version = excluded.version, '
        'updated_at_ms = excluded.updated_at_ms',
        (namespace, key, encoded, version, now),
    )
    return {'key': key, 'version': version, 'updated_at_ms': now}


def _record_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, 'namespace', 128)
    key = _required_text(payload, 'key')
    count = session.execute(
        'DELETE FROM storage_records WHERE namespace = ? AND record_key = ?',
        (namespace, key),
    )
    return {'deleted': bool(count)}


def _append_event_row(session: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    task_id = _required_text(payload, 'task_id')
    sequence = payload.get('sequence')
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise StorageError('database_protocol_error', 'Invalid event sequence')
    encoded = _dump(payload.get('event'))
    now = int(time.time() * 1000)
    count = session.execute(
        'INSERT INTO storage_events(task_id, sequence, event_json, created_at_ms) '
        'VALUES (?, ?, ?, ?) ON CONFLICT(task_id, sequence) DO NOTHING',
        (task_id, sequence, encoded, now),
    )
    if not count:
        row = session.fetch_one(
            'SELECT event_json FROM storage_events '
            'WHERE task_id = ? AND sequence = ?',
            (task_id, sequence),
        )
        existing = None if row is None else _dump(_load(row['event_json']))
        if existing != encoded:
            raise StorageError(
                'database_conflict',
                'Event sequence has a conflicting payload')
    return {'inserted': bool(count), 'task_id': task_id, 'sequence': sequence}


def _event_append(session: Session, payload: Mapping[str, Any]) -> Any:
    return _append_event_row(session, payload)


def _event_append_batch(session: Session, payload: Mapping[str, Any]) -> Any:
    events = payload.get('events')
    if not isinstance(events, list) or not events or len(events) > 500:
        raise StorageError('database_protocol_error', 'Invalid event batch')
    results = []
    for event in events:
        if not isinstance(event, Mapping):
            raise StorageError('database_protocol_error', 'Invalid event batch item')
        results.append(_append_event_row(session, event))
    return {
        'results': results,
        'inserted': sum(1 for item in results if item['inserted']),
        'deduplicated': sum(1 for item in results if not item['inserted']),
    }


def _event_list(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id')
    after = _integer(payload, 'after_sequence', default=-1, minimum=-1)
    limit = _integer(payload, 'limit', default=500, minimum=1, maximum=1000)
    rows = session.fetch_all(
        'SELECT sequence, event_json, created_at_ms FROM storage_events '
        'WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?',
        (task_id, after, limit),
    )
    return [{
        'sequence': int(row['sequence']),
        'event': _load(row['event_json']),
        'created_at_ms': int(row['created_at_ms']),
    } for row in rows]


def _rate_limit_record_and_check(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    endpoint = _required_text(payload, 'endpoint', 256)
    client_key = _required_text(payload, 'client_key', 512)
    event_id = _required_text(payload, 'event_id', 200)
    limit = _integer(payload, 'limit', minimum=1, maximum=1_000_000)
    per_seconds = _integer(
        payload, 'per_seconds', minimum=1, maximum=7 * 24 * 60 * 60)
    now = int(time.time() * 1000)
    window_start = now - per_seconds * 1000
    stale_cutoff = now - per_seconds * 2 * 1000
    # PostgreSQL TEXT cannot carry NUL bytes.  A length prefix preserves an
    # unambiguous composite bucket key for both adapters.
    session.lock_key(
        'rate_limit_bucket', f'{len(endpoint)}:{endpoint}{client_key}')
    row = session.fetch_one(
        'SELECT COUNT(*) AS event_count FROM storage_rate_limit_events '
        'WHERE endpoint = ? AND client_key = ? AND occurred_at_ms >= ?',
        (endpoint, client_key, window_start),
    )
    current = int(row['event_count']) if row else 0
    if current >= limit:
        return {'allowed': False, 'count': current}
    session.execute(
        'INSERT INTO storage_rate_limit_events('
        'event_id, endpoint, client_key, occurred_at_ms) VALUES (?, ?, ?, ?)',
        (event_id, endpoint, client_key, now),
    )
    session.execute(
        'DELETE FROM storage_rate_limit_events '
        'WHERE endpoint = ? AND client_key = ? AND occurred_at_ms < ?',
        (endpoint, client_key, stale_cutoff),
    )
    return {'allowed': True, 'count': current + 1}


_RUN_STATUSES = frozenset({
    'pending', 'running', 'paused', 'done', 'error', 'aborted',
})
_TERMINAL_RUN_STATUSES = frozenset({'done', 'error', 'aborted'})


def _json_text(value: Any) -> str:
    return _dump(value).decode('utf-8')


def _run_status(payload: Mapping[str, Any], *, optional: bool = False) -> str:
    value = payload.get('status', '')
    if optional and value == '':
        return ''
    if not isinstance(value, str) or value not in _RUN_STATUSES:
        raise StorageError(
            'database_protocol_error', 'Invalid orchestration run status')
    return value


def _decode_run_error(value: Any) -> Any:
    if value in (None, ''):
        return None
    try:
        return _load(value)
    except orjson.JSONDecodeError as exc:
        logger.debug('[StorageSidecar] preserving undecodable run error: %s', exc)
        return str(value)


def _run_row(row: Mapping[str, Any], *, detail: bool) -> dict[str, Any]:
    status = str(row['status'] or 'pending')
    result = {
        'id': row['id'],
        'orch_id': row['orch_id'] or '',
        'name': row['name'] or '',
        'status': status,
        'terminal': status in _TERMINAL_RUN_STATUSES,
        'final': row['final'] or '',
        'error': _decode_run_error(row['error']),
        'created_by': row['created_by'] or '',
        'created_at': int(row['created_at'] or 0),
        'updated_at': int(row['updated_at'] or 0),
        'finished_at': int(row['finished_at'] or 0),
    }
    if detail:
        definition = _load(row['definition'])
        if not isinstance(definition, dict):
            raise StorageError(
                'database_integrity',
                'Durable orchestration definition is not an object')
        result['definition'] = definition
        result['input'] = row['input'] or ''
    return result


def _orchestration_run_create(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    definition = payload.get('definition')
    if not isinstance(definition, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid orchestration definition')
    now = int(time.time() * 1000)
    session.execute(
        'INSERT INTO orchestration_runs('
        'id, orch_id, name, definition, input, status, created_by, '
        'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            run_id, str(payload.get('orch_id') or ''),
            str(payload.get('name') or ''), _json_text(dict(definition)),
            str(payload.get('input') or ''), 'pending',
            str(payload.get('created_by') or ''), now, now,
        ),
    )
    return {'created': True}


def _orchestration_run_get(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    row = session.fetch_one(
        'SELECT id, orch_id, name, definition, input, status, final, error, '
        'created_by, created_at, updated_at, finished_at '
        'FROM orchestration_runs WHERE id = ?',
        (run_id,),
    )
    return _run_row(row, detail=True) if row else None


def _orchestration_run_list(session: Session, payload: Mapping[str, Any]) -> Any:
    status = payload.get('status', '')
    orch_id = payload.get('orch_id', '')
    if status and status not in _RUN_STATUSES:
        raise StorageError(
            'database_protocol_error', 'Invalid orchestration run status')
    if not isinstance(status, str) or not isinstance(orch_id, str):
        raise StorageError(
            'database_protocol_error', 'Invalid orchestration run filter')
    limit = _integer(payload, 'limit', default=50, minimum=1, maximum=200)
    rows = session.fetch_all(
        'SELECT id, orch_id, name, status, final, error, created_by, '
        'created_at, updated_at, finished_at FROM orchestration_runs '
        'WHERE (? = \'\' OR status = ?) AND (? = \'\' OR orch_id = ?) '
        'ORDER BY created_at DESC, id DESC LIMIT ?',
        (status, status, orch_id, orch_id, limit),
    )
    return [_run_row(row, detail=False) for row in rows]


def _orchestration_run_update(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    status = _run_status(payload)
    now = int(time.time() * 1000)
    final = payload.get('final')
    error_present = 'error' in payload
    error = payload.get('error')
    row = session.fetch_one(
        'SELECT status, final, error, finished_at FROM orchestration_runs '
        'WHERE id = ?', (run_id,))
    if row is None:
        return {'changed': False}
    if row['status'] in _TERMINAL_RUN_STATUSES and row['status'] != status:
        return {'changed': False}
    next_final = row['final'] if final is None else str(final)
    if not error_present:
        next_error = row['error']
    elif isinstance(error, str):
        next_error = error
    else:
        next_error = _json_text(error)
    finished = int(row['finished_at'] or 0)
    if status in _TERMINAL_RUN_STATUSES:
        finished = finished or now
    else:
        finished = 0
    count = session.execute(
        'UPDATE orchestration_runs SET status = ?, final = ?, error = ?, '
        'updated_at = ?, finished_at = ? WHERE id = ?',
        (status, next_final, next_error, now, finished, run_id),
    )
    return {'changed': bool(count)}


def _orchestration_run_retire(session: Session, payload: Mapping[str, Any]) -> Any:
    error = payload.get('error')
    error_text = error if isinstance(error, str) else _json_text(error)
    now = int(time.time() * 1000)
    count = session.execute(
        "UPDATE orchestration_runs SET status = 'error', final = '', "
        'error = ?, updated_at = ?, finished_at = CASE '
        'WHEN finished_at = 0 THEN ? ELSE finished_at END '
        "WHERE status NOT IN ('done', 'error', 'aborted')",
        (error_text, now, now),
    )
    return {'retired': count}


def _orchestration_event_append(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    sequence = _integer(payload, 'sequence', minimum=0)
    event = payload.get('event')
    if not isinstance(event, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid orchestration event')
    encoded = _json_text(dict(event))
    inserted = session.execute(
        'INSERT INTO orchestration_run_events('
        'run_id, seq, type, node_id, payload, ts) VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(run_id, seq) DO NOTHING',
        (run_id, sequence, str(event.get('type') or ''),
         str(event.get('node_id') or ''), encoded, int(time.time() * 1000)),
    )
    if not inserted:
        row = session.fetch_one(
            'SELECT payload FROM orchestration_run_events '
            'WHERE run_id = ? AND seq = ?', (run_id, sequence))
        existing = None if row is None else _json_text(_load(row['payload']))
        if existing != encoded:
            raise StorageError(
                'database_conflict',
                'Orchestration event sequence has a conflicting payload')
    return {'inserted': bool(inserted), 'accepted': True}


def _orchestration_event_project(session: Session, payload: Mapping[str, Any]) -> Any:
    status = _run_status(payload, optional=True)
    if status in _TERMINAL_RUN_STATUSES:
        raise StorageError(
            'database_protocol_error',
            'Terminal orchestration status requires an explicit transition')
    append = _orchestration_event_append(session, payload)
    if not append['inserted']:
        return {'projected': True, 'inserted': False}
    run_id = str(payload['run_id'])
    now = int(time.time() * 1000)
    if status:
        count = session.execute(
            'UPDATE orchestration_runs SET status = ?, updated_at = ?, '
            "finished_at = 0 WHERE id = ? AND status NOT IN "
            "('done', 'error', 'aborted')",
            (status, now, run_id),
        )
    else:
        count = session.execute(
            'UPDATE orchestration_runs SET updated_at = ? WHERE id = ? '
            "AND status NOT IN ('done', 'error', 'aborted')",
            (now, run_id),
        )
    if not count:
        raise StorageError(
            'database_conflict',
            'Orchestration run header rejected event projection')
    return {'projected': True, 'inserted': True}


def _orchestration_event_page(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    requested = _integer(payload, 'cursor', default=0, minimum=0)
    boundary_row = session.fetch_one(
        'SELECT COALESCE(MAX(seq) + 1, 0) AS next_cursor '
        'FROM orchestration_run_events WHERE run_id = ?', (run_id,))
    boundary = int(boundary_row['next_cursor'] or 0) if boundary_row else 0
    if requested > boundary:
        return {
            'events': [], 'next_cursor': boundary,
            'cursor_reset': True, 'caught_up': True,
        }
    rows = session.fetch_all(
        'SELECT seq, payload FROM orchestration_run_events '
        'WHERE run_id = ? AND seq >= ? ORDER BY seq LIMIT 2000',
        (run_id, requested),
    )
    events = []
    for row in rows:
        event = _load(row['payload'])
        if not isinstance(event, dict):
            raise StorageError(
                'database_integrity',
                'Durable orchestration event is not an object')
        event.setdefault('seq', int(row['seq']))
        events.append(event)
    next_cursor = boundary
    if len(events) >= 2000:
        next_cursor = min(boundary, int(events[-1]['seq']) + 1)
    return {
        'events': events,
        'next_cursor': next_cursor,
        'cursor_reset': False,
        'caught_up': next_cursor >= boundary,
    }


def _orchestration_run_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    session.execute(
        'DELETE FROM orchestration_run_events WHERE run_id = ?', (run_id,))
    count = session.execute(
        'DELETE FROM orchestration_runs WHERE id = ?', (run_id,))
    return {'deleted': bool(count)}


_SWARM_NONTERMINAL = frozenset({'pending', 'running', 'retrying'})


def _swarm_json(value: Any, expected: type, field: str) -> str:
    if not isinstance(value, expected):
        raise StorageError(
            'database_protocol_error', f'Invalid swarm {field}')
    return _json_text(value)


def _optional_text(
    payload: Mapping[str, Any], field: str, *, default: str = '',
    maximum: int = 4096, scope: str = 'storage',
) -> str:
    value = payload.get(field, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            'database_protocol_error', f'Invalid {scope} {field}')
    return value


def _swarm_session_save(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, 'swarm_key', 512)
    specs = payload.get('specs')
    config = payload.get('config')
    specs_json = _swarm_json(specs, list, 'specs')
    if not isinstance(config, Mapping):
        raise StorageError('database_protocol_error', 'Invalid swarm config')
    config_json = _json_text(dict(config))
    now = _integer(payload, 'now_ms', minimum=0)
    session.lock_key('swarm.session', swarm_key)
    session.execute(
        'INSERT INTO swarm_sessions('
        'swarm_key, conv_id, task_id, status, specs_json, config_json, '
        'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(swarm_key) DO UPDATE SET '
        'conv_id = excluded.conv_id, task_id = excluded.task_id, '
        'status = excluded.status, specs_json = excluded.specs_json, '
        'config_json = excluded.config_json, updated_at = excluded.updated_at',
        (
            swarm_key, _optional_text(payload, 'conv_id', scope='swarm'),
            _optional_text(payload, 'task_id', scope='swarm'),
            _optional_text(
                payload, 'status', default='running', maximum=64, scope='swarm'),
            specs_json, config_json, now, now,
        ),
    )
    return {'saved': True}


def _swarm_session_terminate(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, 'swarm_key', 512)
    count = session.execute(
        "UPDATE swarm_sessions SET status = 'terminated', updated_at = ? "
        'WHERE swarm_key = ?',
        (_integer(payload, 'now_ms', minimum=0), swarm_key),
    )
    return {'changed': bool(count)}


def _swarm_session_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, 'swarm_key', 512)
    session.execute('DELETE FROM swarm_agents WHERE swarm_key = ?', (swarm_key,))
    count = session.execute(
        'DELETE FROM swarm_sessions WHERE swarm_key = ?', (swarm_key,))
    return {'deleted': bool(count)}


def _swarm_agent_save(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, 'swarm_key', 512)
    agent_id = _required_text(payload, 'agent_id', 512)
    messages_json = _swarm_json(payload.get('messages'), list, 'messages')
    result = payload.get('result')
    if not isinstance(result, Mapping):
        raise StorageError('database_protocol_error', 'Invalid swarm result')
    result_json = _json_text(dict(result))
    rounds_used = _integer(
        payload, 'rounds_used', default=0, minimum=0, maximum=1_000_000)
    now = _integer(payload, 'now_ms', minimum=0)
    delivered = payload.get('delivered')
    if delivered is not None and not isinstance(delivered, bool):
        raise StorageError('database_protocol_error', 'Invalid swarm delivered flag')
    # PostgreSQL TEXT rejects NUL bytes; a length-prefixed composite key is
    # unambiguous on both backends and safe for advisory-lock hashing.
    session.lock_key('swarm.agent', f'{len(swarm_key)}:{swarm_key}{agent_id}')
    values = (
        swarm_key, agent_id, _optional_text(payload, 'role', scope='swarm'),
        _optional_text(
            payload, 'objective', maximum=100_000, scope='swarm'),
        _optional_text(
            payload, 'status', default='pending', maximum=64, scope='swarm'),
        messages_json, result_json, rounds_used, int(bool(delivered)), now,
    )
    if delivered is None:
        session.execute(
            'INSERT INTO swarm_agents('
            'swarm_key, agent_id, role, objective, status, messages_json, '
            'result_json, rounds_used, delivered, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(swarm_key, agent_id) DO UPDATE SET '
            'role = excluded.role, objective = excluded.objective, '
            'status = excluded.status, messages_json = excluded.messages_json, '
            'result_json = excluded.result_json, '
            'rounds_used = excluded.rounds_used, updated_at = excluded.updated_at',
            values,
        )
    else:
        session.execute(
            'INSERT INTO swarm_agents('
            'swarm_key, agent_id, role, objective, status, messages_json, '
            'result_json, rounds_used, delivered, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(swarm_key, agent_id) DO UPDATE SET '
            'role = excluded.role, objective = excluded.objective, '
            'status = excluded.status, messages_json = excluded.messages_json, '
            'result_json = excluded.result_json, rounds_used = excluded.rounds_used, '
            'delivered = excluded.delivered, updated_at = excluded.updated_at',
            values,
        )
    return {'saved': True}


def _swarm_agents_mark_delivered(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, 'swarm_key', 512)
    agent_ids = payload.get('agent_ids')
    if (not isinstance(agent_ids, list) or len(agent_ids) > 1000
            or any(not isinstance(item, str) or not item or len(item) > 512
                   for item in agent_ids)):
        raise StorageError('database_protocol_error', 'Invalid swarm agent_ids')
    changed = 0
    for agent_id in dict.fromkeys(agent_ids):
        changed += session.execute(
            'UPDATE swarm_agents SET delivered = 1 '
            'WHERE swarm_key = ? AND agent_id = ?',
            (swarm_key, agent_id),
        )
    return {'changed': changed}


def _swarm_session_get(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, 'swarm_key', 512)
    item = session.fetch_one(
        'SELECT swarm_key, conv_id, task_id, status, specs_json, config_json, '
        'created_at, updated_at FROM swarm_sessions WHERE swarm_key = ?',
        (swarm_key,),
    )
    if item is None:
        return None
    agents = session.fetch_all(
        'SELECT agent_id, role, objective, status, messages_json, result_json, '
        'rounds_used, delivered, updated_at FROM swarm_agents '
        'WHERE swarm_key = ? ORDER BY agent_id',
        (swarm_key,),
    )
    specs = _load(item['specs_json'])
    config = _load(item['config_json'])
    if not isinstance(specs, list) or not isinstance(config, dict):
        raise StorageError(
            'database_integrity', 'Durable swarm session JSON is invalid')
    decoded_agents = []
    for agent in agents:
        messages = _load(agent['messages_json'])
        result = _load(agent['result_json'])
        if not isinstance(messages, list) or not isinstance(result, dict):
            raise StorageError(
                'database_integrity', 'Durable swarm agent JSON is invalid')
        decoded_agents.append({
            'agent_id': agent['agent_id'], 'role': agent['role'] or '',
            'objective': agent['objective'] or '',
            'status': agent['status'] or 'pending', 'messages': messages,
            'result': result, 'rounds_used': int(agent['rounds_used'] or 0),
            'delivered': bool(agent['delivered']),
            'updated_at': int(agent['updated_at'] or 0),
        })
    return {
        'swarm_key': item['swarm_key'], 'conv_id': item['conv_id'] or '',
        'task_id': item['task_id'] or '',
        'status': item['status'] or 'running', 'specs': specs,
        'config': config, 'created_at': int(item['created_at'] or 0),
        'updated_at': int(item['updated_at'] or 0), 'agents': decoded_agents,
    }


def _swarm_resumable_list(session: Session, _payload: Mapping[str, Any]) -> Any:
    sessions = session.fetch_all(
        'SELECT swarm_key, conv_id, task_id, status, specs_json, config_json '
        'FROM swarm_sessions ORDER BY updated_at DESC, swarm_key')
    agents = session.fetch_all(
        'SELECT swarm_key, agent_id, role, objective, status, messages_json, '
        'result_json, rounds_used, delivered FROM swarm_agents '
        'ORDER BY swarm_key, agent_id')
    by_session: dict[str, list[Mapping[str, Any]]] = {}
    for agent in agents:
        by_session.setdefault(str(agent['swarm_key']), []).append(agent)
    result = []
    for item in sessions:
        decoded_agents = []
        resumable = False
        for agent in by_session.get(str(item['swarm_key']), []):
            status = str(agent['status'] or 'pending')
            delivered = bool(agent['delivered'])
            resumable = resumable or status in _SWARM_NONTERMINAL
            resumable = resumable or (status == 'completed' and not delivered)
            messages = _load(agent['messages_json'])
            agent_result = _load(agent['result_json'])
            if not isinstance(messages, list) or not isinstance(agent_result, dict):
                raise StorageError(
                    'database_integrity', 'Durable swarm agent JSON is invalid')
            decoded_agents.append({
                'agent_id': agent['agent_id'], 'role': agent['role'] or '',
                'objective': agent['objective'] or '', 'status': status,
                'messages': messages, 'result': agent_result,
                'rounds_used': int(agent['rounds_used'] or 0),
                'delivered': delivered,
            })
        if not resumable:
            continue
        specs = _load(item['specs_json'])
        config = _load(item['config_json'])
        if not isinstance(specs, list) or not isinstance(config, dict):
            raise StorageError(
                'database_integrity', 'Durable swarm session JSON is invalid')
        result.append({
            'swarm_key': item['swarm_key'], 'conv_id': item['conv_id'] or '',
            'task_id': item['task_id'] or '',
            'status': item['status'] or 'running', 'specs': specs,
            'config': config, 'agents': decoded_agents,
        })
    return result


def _research_lang(payload: Mapping[str, Any]) -> str:
    lang = _required_text(payload, 'lang', 32)
    if ':' in lang:
        raise StorageError(
            'database_protocol_error', 'Invalid research language')
    return lang


def _paper_report_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 64)
    meta = payload.get('meta', {})
    if not isinstance(meta, Mapping):
        raise StorageError('database_protocol_error', 'Invalid paper report metadata')
    session.lock_key('paper.report', f'{len(paper_hash)}:{paper_hash}{lang}')
    session.execute(
        'INSERT INTO paper_reports('
        'paper_hash, lang, report, model, meta, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(paper_hash, lang) DO UPDATE SET '
        'report = excluded.report, model = excluded.model, '
        'meta = excluded.meta, created_at = excluded.created_at',
        (
            paper_hash, lang, _optional_text(
                payload, 'report', maximum=10_000_000, scope='paper report'),
            _optional_text(
                payload, 'model', maximum=512, scope='paper report'),
            _json_text(dict(meta)), _integer(payload, 'created_at', minimum=0),
        ),
    )
    return {'saved': True}


def _paper_report_get(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 64)
    row = session.fetch_one(
        'SELECT report, model, meta, created_at FROM paper_reports '
        'WHERE paper_hash = ? AND lang = ?',
        (paper_hash, lang),
    )
    if row is None:
        return None
    try:
        meta = _load(row['meta'])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug('[StorageSidecar] invalid paper report metadata: %s', exc)
        meta = {}
    return {
        'paper_hash': paper_hash, 'lang': lang, 'report': row['report'] or '',
        'model': row['model'] or '',
        'meta': meta if isinstance(meta, dict) else {},
        'created_at': int(row['created_at'] or 0),
    }


def _paper_report_second_pass_merge(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Atomically merge one billed second pass without a callback over RPC."""
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 64)
    name = _required_text(payload, 'name', 64)
    entry = payload.get('entry')
    if not isinstance(entry, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid paper second-pass entry')
    # Validate and detach the entire document before taking the row lock.
    entry = _load(_dump(dict(entry)))
    session.lock_key(
        'paper.report.meta', f'{len(paper_hash)}:{paper_hash}{lang}')
    row = session.fetch_one(
        'SELECT meta FROM paper_reports WHERE paper_hash = ? AND lang = ?',
        (paper_hash, lang),
    )
    if row is None:
        return {'found': False, 'meta': None}
    try:
        current = _load(row['meta'])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug('invalid paper report metadata during merge: %s', exc)
        current = {}
    if not isinstance(current, dict):
        current = {}
    passes = current.get('secondPasses')
    if not isinstance(passes, dict):
        passes = {}
        current['secondPasses'] = passes
    passes[name] = entry

    token_keys = (
        'prompt_tokens', 'completion_tokens', 'cache_read_tokens',
        'cache_write_tokens', 'reasoning_tokens',
    )

    def integer(value: Any) -> int:
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    total = {
        key: integer(current.get(
            key.split('_')[0] + ''.join(
                part.title() for part in key.split('_')[1:])))
        for key in token_keys
    }
    for pass_meta in passes.values():
        usage = pass_meta.get('usage') if isinstance(pass_meta, Mapping) else None
        if isinstance(usage, Mapping):
            for key in token_keys:
                total[key] += integer(usage.get(key))
    current['totalUsage'] = total

    def cost(value: Any) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return 0.0

    for suffix in ('Cny', 'Usd'):
        field = f'cost{suffix}'
        total_cost = cost(current.get(field)) + sum(
            cost(item.get(field)) for item in passes.values()
            if isinstance(item, Mapping)
        )
        if total_cost:
            current[f'totalCost{suffix}'] = total_cost

    session.execute(
        'UPDATE paper_reports SET meta = ? WHERE paper_hash = ? AND lang = ?',
        (_json_text(current), paper_hash, lang),
    )
    return {'found': True, 'meta': current}


def _paper_report_second_pass_accumulate(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Atomically add one pass invocation to an existing aggregate."""
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 64)
    name = _required_text(payload, 'name', 64)
    raw_usage = payload.get('usage')
    if not isinstance(raw_usage, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid paper second-pass usage')
    token_keys = (
        'prompt_tokens', 'completion_tokens', 'cache_read_tokens',
        'cache_write_tokens', 'reasoning_tokens',
    )
    usage = {
        key: _integer(raw_usage, key, default=0, minimum=0,
                      maximum=10_000_000_000)
        for key in token_keys
    }
    incremental_costs = {}
    for suffix in ('Cny', 'Usd'):
        field = f'cost{suffix}'
        incremental_costs[field] = (
            _number(payload, field, minimum=0, maximum=1_000_000_000)
            if field in payload else 0.0)

    session.lock_key(
        'paper.report.meta', f'{len(paper_hash)}:{paper_hash}{lang}')
    row = session.fetch_one(
        'SELECT meta FROM paper_reports WHERE paper_hash = ? AND lang = ?',
        (paper_hash, lang),
    )
    if row is None:
        return {'found': False, 'meta': None}
    try:
        current = _load(row['meta'])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug('invalid paper report metadata during accumulate: %s', exc)
        current = {}
    if not isinstance(current, dict):
        current = {}
    passes = current.get('secondPasses')
    if not isinstance(passes, dict):
        passes = {}
        current['secondPasses'] = passes
    entry = passes.get(name)
    if not isinstance(entry, dict):
        entry = {}
        passes[name] = entry
    previous_usage = entry.get('usage')
    if not isinstance(previous_usage, Mapping):
        previous_usage = {}

    def integer(value: Any) -> int:
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    entry['usage'] = {
        key: integer(previous_usage.get(key)) + usage[key]
        for key in token_keys
    }
    entry['calls'] = integer(entry.get('calls')) + 1
    for field, increment in incremental_costs.items():
        previous = entry.get(field)
        prior = (float(previous) if isinstance(previous, (int, float))
                 and not isinstance(previous, bool) else 0.0)
        if prior or increment:
            entry[field] = prior + increment

    total_usage = {
        key: integer(current.get(
            key.split('_')[0] + ''.join(
                part.title() for part in key.split('_')[1:])))
        for key in token_keys
    }
    for pass_meta in passes.values():
        pass_usage = pass_meta.get('usage') if isinstance(pass_meta, Mapping) else None
        if isinstance(pass_usage, Mapping):
            for key in token_keys:
                total_usage[key] += integer(pass_usage.get(key))
    current['totalUsage'] = total_usage
    for suffix in ('Cny', 'Usd'):
        field = f'cost{suffix}'
        body = current.get(field)
        body_cost = (float(body) if isinstance(body, (int, float))
                     and not isinstance(body, bool) else 0.0)
        total_cost = body_cost + sum(
            float(item.get(field) or 0) for item in passes.values()
            if isinstance(item, Mapping)
            and isinstance(item.get(field), (int, float))
            and not isinstance(item.get(field), bool)
        )
        if total_cost:
            current[f'totalCost{suffix}'] = total_cost

    session.execute(
        'UPDATE paper_reports SET meta = ? WHERE paper_hash = ? AND lang = ?',
        (_json_text(current), paper_hash, lang),
    )
    return {'found': True, 'meta': current}


def _paper_translation_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 128)
    session.lock_key(
        'paper.translation', f'{len(paper_hash)}:{paper_hash}{lang}')
    session.execute(
        'INSERT INTO paper_translations('
        'paper_hash, lang, text, model, created_at) VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT(paper_hash, lang) DO UPDATE SET '
        'text = excluded.text, model = excluded.model, '
        'created_at = excluded.created_at',
        (
            paper_hash, lang, _optional_text(
                payload, 'text', maximum=20_000_000, scope='paper translation'),
            _optional_text(
                payload, 'model', maximum=512, scope='paper translation'),
            _integer(payload, 'created_at', minimum=0),
        ),
    )
    return {'saved': True}


def _paper_translation_get(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _required_text(payload, 'lang', 128)
    row = session.fetch_one(
        'SELECT text, model, created_at FROM paper_translations '
        'WHERE paper_hash = ? AND lang = ?',
        (paper_hash, lang),
    )
    if row is None:
        return None
    return {
        'paper_hash': paper_hash, 'lang': lang, 'text': row['text'] or '',
        'model': row['model'] or '', 'created_at': int(row['created_at'] or 0),
    }


def _paper_library_put(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_id = _required_text(payload, 'id', 256)
    user_id = _integer(payload, 'user_id', minimum=1)
    paper_hash = _optional_text(
        payload, 'paper_hash', maximum=128, scope='paper library')
    session.lock_key('paper.library', f'{user_id}:{paper_id}')
    values = (
        paper_id, user_id,
        _optional_text(payload, 'title', maximum=1000, scope='paper library'),
        _optional_text(payload, 'pdf_url', maximum=10_000, scope='paper library'),
        _optional_text(
            payload, 'pdf_filename', maximum=2000, scope='paper library'),
        _optional_text(payload, 'arxiv_id', maximum=256, scope='paper library'),
        paper_hash,
        _optional_text(
            payload, 'parsed_text', maximum=20_000_000, scope='paper library'),
        _optional_text(
            payload, 'parser_version', maximum=256, scope='paper library'),
        _optional_text(
            payload, 'qa_history', default='[]', maximum=10_000_000,
            scope='paper library'),
        _optional_text(
            payload, 'images', default='[]', maximum=10_000_000,
            scope='paper library'),
        _optional_text(
            payload, 'babel_cache', default='{}', maximum=10_000_000,
            scope='paper library'),
        _integer(payload, 'page_count', default=0, minimum=0),
        _optional_text(payload, 'folder_id', maximum=512, scope='paper library'),
        _integer(payload, 'created_at', minimum=0),
        _integer(payload, 'updated_at', minimum=0),
    )
    session.execute(
        'INSERT INTO paper_library('
        'id, user_id, title, pdf_url, pdf_filename, arxiv_id, paper_hash, '
        'parsed_text, parser_version, qa_history, images, babel_cache, '
        'page_count, folder_id, created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(id, user_id) DO UPDATE SET '
        'title = excluded.title, pdf_url = excluded.pdf_url, '
        'pdf_filename = excluded.pdf_filename, arxiv_id = excluded.arxiv_id, '
        'paper_hash = excluded.paper_hash, parsed_text = excluded.parsed_text, '
        'parser_version = excluded.parser_version, '
        'qa_history = excluded.qa_history, images = excluded.images, '
        'babel_cache = excluded.babel_cache, page_count = excluded.page_count, '
        'folder_id = excluded.folder_id, updated_at = excluded.updated_at',
        values,
    )
    return {'saved': True}


def _paper_library_recent(session: Session, payload: Mapping[str, Any]) -> Any:
    exclude_hash = _optional_text(
        payload, 'exclude_paper_hash', maximum=128, scope='paper library')
    limit = _integer(payload, 'limit', default=40, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT title, arxiv_id FROM paper_library "
        "WHERE paper_hash != ? AND title != '' "
        'ORDER BY updated_at DESC LIMIT ?',
        (exclude_hash, limit),
    )
    return [{
        'title': row['title'] or '', 'arxiv_id': row['arxiv_id'] or '',
    } for row in rows]


def _paper_library_identity(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    row = session.fetch_one(
        'SELECT title, arxiv_id, parsed_text FROM paper_library '
        'WHERE paper_hash = ? ORDER BY updated_at DESC LIMIT 1',
        (paper_hash,),
    )
    if row is None:
        return None
    return {
        'title': row['title'] or '', 'arxiv_id': row['arxiv_id'] or '',
        'parsed_text': row['parsed_text'] or '',
    }


def _paper_library_title_backfill(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Heal placeholder titles for one content-addressed paper atomically."""
    paper_hash = _required_text(payload, 'paper_hash', 128)
    title = _required_text(payload, 'title', 1000).strip()
    if not title:
        raise StorageError(
            'database_protocol_error', 'Invalid title in storage request')
    session.lock_key('paper.library.title', paper_hash)
    rows = session.fetch_all(
        'SELECT id, user_id, title FROM paper_library '
        'WHERE paper_hash = ? ORDER BY updated_at DESC',
        (paper_hash,),
    )
    if not rows:
        return {'title': title, 'updated': 0}

    def is_placeholder(value: Any) -> bool:
        normalized = str(value or '').strip().lower()
        return not normalized or normalized.startswith(('arxiv:', 'arxiv '))

    authoritative = next(
        (str(row['title'] or '').strip() for row in rows
         if not is_placeholder(row['title'])),
        '',
    )
    updated = 0
    now = int(time.time())
    for row in rows:
        if not is_placeholder(row['title']):
            continue
        updated += session.execute(
            'UPDATE paper_library SET title = ?, updated_at = ? '
            'WHERE id = ? AND user_id = ? AND title = ?',
            (title, now, row['id'], int(row['user_id']), row['title']),
        )
    return {'title': authoritative or title, 'updated': updated}


def _daily_cost_date(payload: Mapping[str, Any], key: str = 'date') -> str:
    value = _required_text(payload, key, 10)
    if (len(value) != 10 or value[4] != '-' or value[7] != '-'
            or not (value[:4] + value[5:7] + value[8:]).isdigit()):
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    return value


def _daily_cost_month(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    year = _integer(payload, 'year', minimum=1970, maximum=9999)
    month = _integer(payload, 'month', minimum=1, maximum=12)
    rows = session.fetch_all(
        'SELECT date, cost, conversations_json, computed_at '
        'FROM daily_cost_cache WHERE user_id = ? AND date LIKE ? '
        'ORDER BY date',
        (user_id, f'{year:04d}-{month:02d}-%'),
    )
    return [{
        'date': row['date'], 'cost': float(row['cost'] or 0),
        'conversations': _load(row['conversations_json']) or {},
        'computed_at': int(row['computed_at'] or 0),
    } for row in rows]


def _daily_cost_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    date = _daily_cost_date(payload)
    conversations = payload.get('conversations', {})
    if not isinstance(conversations, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid conversations in storage request')
    cost = _number(payload, 'cost', minimum=0, maximum=1_000_000_000)
    computed_at = _integer(payload, 'computed_at', minimum=0)
    session.lock_key('daily.cost', f'{user_id}:{date}')
    session.execute(
        'INSERT INTO daily_cost_cache('
        'user_id, date, cost, conversations_json, computed_at) '
        'VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, date) DO UPDATE SET '
        'cost = excluded.cost, '
        'conversations_json = excluded.conversations_json, '
        'computed_at = excluded.computed_at',
        (user_id, date, cost, _json_text(dict(conversations)), computed_at),
    )
    return {'saved': True}


def _daily_cost_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    raw_date = payload.get('date')
    if raw_date is None:
        count = session.execute(
            'DELETE FROM daily_cost_cache WHERE user_id = ?', (user_id,))
    else:
        date = _daily_cost_date(payload)
        session.lock_key('daily.cost', f'{user_id}:{date}')
        count = session.execute(
            'DELETE FROM daily_cost_cache WHERE user_id = ? AND date = ?',
            (user_id, date),
        )
    return {'deleted': int(count)}


def _daily_cost_persisted_dates(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    values = payload.get('dates')
    if not isinstance(values, list) or len(values) > 366:
        raise StorageError(
            'database_protocol_error', 'Invalid dates in storage request')
    dates = []
    for value in values:
        dates.append(_daily_cost_date({'date': value}))
    dates = list(dict.fromkeys(dates))
    if not dates:
        return {'dates': []}
    placeholders = ','.join('?' for _ in dates)
    rows = session.fetch_all(
        'SELECT date FROM daily_cost_cache WHERE user_id = ? '
        f'AND date IN ({placeholders})',
        (user_id, *dates),
    )
    return {'dates': [row['date'] for row in rows]}


def _daily_cost_latest(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, 'user_id', minimum=1)
    row = session.fetch_one(
        'SELECT date, cost, conversations_json, computed_at '
        'FROM daily_cost_cache WHERE user_id = ? '
        'ORDER BY date DESC LIMIT 1',
        (user_id,),
    )
    if row is None:
        return None
    return {
        'date': row['date'], 'cost': float(row['cost'] or 0),
        'conversations': _load(row['conversations_json']) or {},
        'computed_at': int(row['computed_at'] or 0),
    }


def _paper_podcast_key(payload: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _required_text(payload, 'paper_hash', 128),
        _required_text(payload, 'mode', 64),
        _required_text(payload, 'lang', 32),
        _optional_text(payload, 'voice', maximum=256, scope='paper podcast'),
    )


def _paper_podcast_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash, mode, lang, voice = _paper_podcast_key(payload)
    script = payload.get('script', {})
    meta = payload.get('meta', {})
    if not isinstance(script, Mapping) or not isinstance(meta, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid paper podcast document')
    status = _required_text(payload, 'status', 64)
    if status not in {
        'generating', 'interrupted', 'done', 'script_only', 'error', 'aborted',
    }:
        raise StorageError(
            'database_protocol_error', 'Invalid paper podcast status')
    now = _integer(payload, 'updated_at', minimum=0)
    created_at = _integer(payload, 'created_at', minimum=0)
    session.lock_key(
        'paper.podcast',
        f'{len(paper_hash)}:{paper_hash}{len(mode)}:{mode}{len(lang)}:{lang}{voice}',
    )
    session.execute(
        'INSERT INTO paper_podcasts('
        'paper_hash, mode, lang, voice, status, script_json, file_path, '
        'duration_sec, model, tts_model, meta, created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(paper_hash, mode, lang, voice) DO UPDATE SET '
        'status = excluded.status, script_json = excluded.script_json, '
        'file_path = excluded.file_path, duration_sec = excluded.duration_sec, '
        'model = excluded.model, tts_model = excluded.tts_model, '
        'meta = excluded.meta, updated_at = excluded.updated_at',
        (
            paper_hash, mode, lang, voice, status, _json_text(dict(script)),
            _optional_text(
                payload, 'file_path', maximum=10_000, scope='paper podcast'),
            _number(payload, 'duration_sec', minimum=0, maximum=10_000_000),
            _optional_text(payload, 'model', maximum=512, scope='paper podcast'),
            _optional_text(
                payload, 'tts_model', maximum=512, scope='paper podcast'),
            _json_text(dict(meta)), created_at, now,
        ),
    )
    return {'saved': True}


def _paper_podcast_get(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash, mode, lang, voice = _paper_podcast_key(payload)
    row = session.fetch_one(
        'SELECT status, script_json, file_path, duration_sec, model, '
        'tts_model, meta, created_at, updated_at FROM paper_podcasts '
        'WHERE paper_hash = ? AND mode = ? AND lang = ? AND voice = ?',
        (paper_hash, mode, lang, voice),
    )
    if row is None:
        return None
    script = _load(row['script_json'])
    meta = _load(row['meta'])
    if not isinstance(script, dict) or not isinstance(meta, dict):
        raise StorageError(
            'database_integrity', 'Paper podcast JSON is invalid')
    return {
        'paper_hash': paper_hash, 'mode': mode, 'lang': lang, 'voice': voice,
        'status': row['status'] or '', 'script_json': script,
        'file_path': row['file_path'] or '',
        'duration_sec': float(row['duration_sec'] or 0),
        'model': row['model'] or '', 'tts_model': row['tts_model'] or '',
        'meta': meta, 'created_at': int(row['created_at'] or 0),
        'updated_at': int(row['updated_at'] or 0),
    }


def _paper_podcast_mark_interrupted(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    count = session.execute(
        "UPDATE paper_podcasts SET status = 'interrupted', updated_at = ? "
        "WHERE status = 'generating'",
        (_integer(payload, 'updated_at', minimum=0),),
    )
    return {'changed': int(count)}


_TENANT_USER_ROLES = {'user', 'admin'}
_TENANT_USER_STATUSES = {'active', 'suspended', 'deleted'}
_TENANT_USER_COLUMNS = (
    'id, email, display_name, role, status, created_at, last_login_at, '
    'email_verified, metadata'
)


def _tenant_user_document(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _load(row['metadata'])
    if not isinstance(metadata, dict):
        raise StorageError('database_integrity', 'Tenant user metadata is invalid')
    return {
        'id': row['id'], 'email': row['email'],
        'display_name': row['display_name'] or '', 'role': row['role'],
        'status': row['status'], 'created_at': int(row['created_at']),
        'last_login_at': int(row['last_login_at'] or 0),
        'email_verified': bool(row['email_verified']), 'metadata': metadata,
    }


def _tenant_user_role(payload: Mapping[str, Any]) -> str:
    role = _required_text(payload, 'role', 32)
    if role not in _TENANT_USER_ROLES:
        raise StorageError('database_protocol_error', 'Invalid tenant user role')
    return role


def _tenant_user_status(payload: Mapping[str, Any], *, optional=False) -> str:
    status = payload.get('status', '')
    if optional and status == '':
        return ''
    if not isinstance(status, str) or status not in _TENANT_USER_STATUSES:
        raise StorageError('database_protocol_error', 'Invalid tenant user status')
    return status


def _tenant_user_create(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _required_text(payload, 'user_id', 256)
    email = _required_text(payload, 'email', 320).strip().lower()
    role = _tenant_user_role(payload)
    metadata = payload.get('metadata', {})
    if not isinstance(metadata, Mapping):
        raise StorageError('database_protocol_error', 'Invalid tenant user metadata')
    session.lock_key('tenant.user.email', email)
    if session.fetch_one('SELECT id FROM tenant_users WHERE email = ?', (email,)):
        raise StorageError('database_conflict', 'Tenant user email already exists')
    session.execute(
        'INSERT INTO tenant_users('
        'id, email, password_hash, display_name, role, status, created_at, '
        'last_login_at, email_verified, metadata) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            user_id, email,
            _optional_text(
                payload, 'password_hash', maximum=512, scope='tenant user'),
            _optional_text(
                payload, 'display_name', maximum=256, scope='tenant user'),
            role, 'active', _integer(payload, 'created_at', minimum=0),
            0, 0, _json_text(dict(metadata)),
        ),
    )
    row = session.fetch_one(
        f'SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?',
        (user_id,),
    )
    if row is None:
        raise StorageError('database_integrity', 'Tenant user insert was not visible')
    return _tenant_user_document(row)


def _tenant_user_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = payload.get('user_id', '')
    email = payload.get('email', '')
    if bool(user_id) == bool(email):
        raise StorageError(
            'database_protocol_error', 'Exactly one tenant user selector is required')
    if user_id:
        value = _required_text(payload, 'user_id', 256)
        predicate = 'id = ?'
    else:
        value = _required_text(payload, 'email', 320).strip().lower()
        predicate = 'email = ?'
    row = session.fetch_one(
        f'SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE {predicate}',
        (value,),
    )
    return None if row is None else _tenant_user_document(row)


def _tenant_user_list(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, 'limit', default=100, minimum=1, maximum=1000)
    offset = _integer(
        payload, 'offset', default=0, minimum=0, maximum=10_000_000)
    status = _tenant_user_status(payload, optional=True)
    if status:
        rows = session.fetch_all(
            f'SELECT {_TENANT_USER_COLUMNS} FROM tenant_users '
            'WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
            (status, limit, offset),
        )
    else:
        rows = session.fetch_all(
            f'SELECT {_TENANT_USER_COLUMNS} FROM tenant_users '
            'ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
            (limit, offset),
        )
    return [_tenant_user_document(row) for row in rows]


def _tenant_user_set_status(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _required_text(payload, 'user_id', 256)
    count = session.execute(
        'UPDATE tenant_users SET status = ? WHERE id = ?',
        (_tenant_user_status(payload), user_id),
    )
    if not count:
        return None
    row = session.fetch_one(
        f'SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?',
        (user_id,),
    )
    return None if row is None else _tenant_user_document(row)


def _tenant_user_set_role(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _required_text(payload, 'user_id', 256)
    count = session.execute(
        'UPDATE tenant_users SET role = ? WHERE id = ?',
        (_tenant_user_role(payload), user_id),
    )
    if not count:
        return None
    row = session.fetch_one(
        f'SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?',
        (user_id,),
    )
    return None if row is None else _tenant_user_document(row)


def _tenant_user_authentication(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    email = _required_text(payload, 'email', 320).strip().lower()
    row = session.fetch_one(
        f'SELECT {_TENANT_USER_COLUMNS}, password_hash FROM tenant_users '
        'WHERE email = ?',
        (email,),
    )
    if row is None:
        return None
    return {
        'user': _tenant_user_document(row),
        'password_hash': row['password_hash'] or '',
    }


def _tenant_user_record_login(session: Session, payload: Mapping[str, Any]) -> Any:
    count = session.execute(
        'UPDATE tenant_users SET last_login_at = ? WHERE id = ?',
        (
            _integer(payload, 'last_login_at', minimum=0),
            _required_text(payload, 'user_id', 256),
        ),
    )
    return {'updated': bool(count)}


_ARTIFACT_FORMATS = {'markdown', 'html', 'svg'}
_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024
_ARTIFACT_COLUMNS = (
    'id, conv_id, task_id, msg_id, source, source_ref, format, title, '
    'content, content_sha256, size_bytes, version, parent_id, pinned, meta, '
    'created_at'
)


def _artifact_document(row: Mapping[str, Any], *, content: bool) -> dict[str, Any]:
    source_ref = _load(row['source_ref'])
    meta = _load(row['meta'])
    if not isinstance(source_ref, dict) or not isinstance(meta, dict):
        raise StorageError('database_integrity', 'Artifact JSON is invalid')
    result = {
        'id': row['id'], 'conv_id': row['conv_id'],
        'task_id': row['task_id'] or '', 'msg_id': row['msg_id'] or '',
        'source': row['source'], 'source_ref': source_ref,
        'format': row['format'], 'title': row['title'] or '',
        'content_sha256': row['content_sha256'],
        'size_bytes': int(row['size_bytes'] or 0),
        'version': int(row['version'] or 1),
        'parent_id': row['parent_id'] or '', 'pinned': bool(row['pinned']),
        'meta': meta, 'created_at': int(row['created_at'] or 0),
    }
    if content:
        result['content'] = row['content'] or ''
    return result


def _artifact_create(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, 'artifact_id', 256)
    conv_id = _required_text(payload, 'conv_id', 512)
    source = _required_text(payload, 'source', 256)
    artifact_format = _required_text(payload, 'format', 32)
    if artifact_format not in _ARTIFACT_FORMATS:
        raise StorageError('database_protocol_error', 'Invalid artifact format')
    content = _optional_text(
        payload, 'content', maximum=_ARTIFACT_MAX_BYTES, scope='artifact')
    size = len(content.encode('utf-8', errors='replace'))
    if size > _ARTIFACT_MAX_BYTES:
        raise StorageError('database_protocol_error', 'Artifact is too large')
    source_ref = payload.get('source_ref', {})
    meta = payload.get('meta', {})
    if not isinstance(source_ref, Mapping) or not isinstance(meta, Mapping):
        raise StorageError('database_protocol_error', 'Invalid artifact metadata')
    source_ref = dict(source_ref)
    meta = dict(meta)
    sha = hashlib.sha256(
        content.encode('utf-8', errors='replace')).hexdigest()
    session.lock_key('artifact.conv', conv_id)
    existing = session.fetch_one(
        f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts '
        'WHERE conv_id = ? AND content_sha256 = ? AND deleted_at = 0 '
        'ORDER BY created_at DESC LIMIT 1',
        (conv_id, sha),
    )
    if existing is not None:
        return {'created': False, 'artifact': _artifact_document(
            existing, content=False)}

    parent_id = _optional_text(
        payload, 'parent_id', maximum=256, scope='artifact')
    version = 1
    path = source_ref.get('path')
    if not parent_id and isinstance(path, str) and path:
        path_predicate = (
            "source_ref ->> 'path' = ?" if session.backend == 'postgres'
            else "json_extract(source_ref, '$.path') = ?"
        )
        candidate = session.fetch_one(
            'SELECT id, version FROM chat_artifacts '
            f'WHERE conv_id = ? AND deleted_at = 0 AND {path_predicate} '
            'ORDER BY version DESC, created_at DESC LIMIT 1',
            (conv_id, path),
        )
        if candidate is not None:
            parent_id = str(candidate['id'])
            version = int(candidate['version'] or 1) + 1
    session.execute(
        'INSERT INTO chat_artifacts('
        'id, conv_id, task_id, msg_id, source, source_ref, format, title, '
        'content, content_sha256, size_bytes, version, parent_id, pinned, '
        'meta, created_at, deleted_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            artifact_id, conv_id,
            _optional_text(payload, 'task_id', maximum=512, scope='artifact'),
            _optional_text(payload, 'msg_id', maximum=512, scope='artifact'),
            source, _json_text(source_ref), artifact_format,
            _optional_text(payload, 'title', maximum=300, scope='artifact').strip(),
            content, sha, size, version, parent_id, False, _json_text(meta),
            _integer(payload, 'created_at', minimum=0), 0,
        ),
    )
    row = session.fetch_one(
        f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts WHERE id = ?',
        (artifact_id,),
    )
    if row is None:
        raise StorageError('database_integrity', 'Artifact insert was not visible')
    return {'created': True, 'artifact': _artifact_document(row, content=False)}


def _artifact_get(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, 'artifact_id', 256)
    include_content = payload.get('include_content', False)
    if not isinstance(include_content, bool):
        raise StorageError(
            'database_protocol_error', 'Invalid artifact content selector')
    row = session.fetch_one(
        f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts '
        'WHERE id = ? AND deleted_at = 0',
        (artifact_id,),
    )
    return None if row is None else _artifact_document(
        row, content=include_content)


def _artifact_list(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _required_text(payload, 'conv_id', 512)
    include_deleted = payload.get('include_deleted', False)
    if not isinstance(include_deleted, bool):
        raise StorageError(
            'database_protocol_error', 'Invalid artifact deleted selector')
    where = 'WHERE conv_id = ?' + ('' if include_deleted else ' AND deleted_at = 0')
    rows = session.fetch_all(
        f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts {where} '
        'ORDER BY created_at DESC',
        (conv_id,),
    )
    return [_artifact_document(row, content=False) for row in rows]


def _artifact_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, 'artifact_id', 256)
    count = session.execute(
        'UPDATE chat_artifacts SET deleted_at = ? '
        'WHERE id = ? AND deleted_at = 0',
        (_integer(payload, 'deleted_at', minimum=1), artifact_id),
    )
    return {'deleted': bool(count)}


def _artifact_versions(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, 'artifact_id', 256)
    row = session.fetch_one(
        f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts '
        'WHERE id = ? AND deleted_at = 0',
        (artifact_id,),
    )
    if row is None:
        return []
    seen_up: set[str] = {str(row['id'])}
    while row.get('parent_id') and row['parent_id'] not in seen_up:
        seen_up.add(str(row['parent_id']))
        parent = session.fetch_one(
            f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts '
            'WHERE id = ? AND deleted_at = 0',
            (row['parent_id'],),
        )
        if parent is None:
            break
        row = parent
    chain = [_artifact_document(row, content=False)]
    current_id = str(row['id'])
    seen_forward = {current_id}
    while True:
        child = session.fetch_one(
            f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts '
            'WHERE parent_id = ? AND deleted_at = 0 '
            'ORDER BY version ASC, created_at ASC LIMIT 1',
            (current_id,),
        )
        if child is None or str(child['id']) in seen_forward:
            break
        seen_forward.add(str(child['id']))
        chain.append(_artifact_document(child, content=False))
        current_id = str(child['id'])
    return chain


def _artifact_library(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, 'limit', default=50, minimum=1, maximum=200)
    rows = session.fetch_all(
        f'SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts '
        'WHERE deleted_at = 0 ORDER BY pinned DESC, created_at DESC LIMIT ?',
        (limit,),
    )
    return [_artifact_document(row, content=False) for row in rows]


def _artifact_pin(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, 'artifact_id', 256)
    pinned = payload.get('pinned')
    if not isinstance(pinned, bool):
        raise StorageError('database_protocol_error', 'Invalid artifact pin flag')
    count = session.execute(
        'UPDATE chat_artifacts SET pinned = ? '
        'WHERE id = ? AND deleted_at = 0',
        (pinned, artifact_id),
    )
    return {'changed': bool(count)}


def _research_artifact_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang_key = _required_text(payload, 'lang_key', 64)
    if not (lang_key.startswith('survey:') or lang_key.startswith('ideate:')):
        raise StorageError(
            'database_protocol_error', 'Invalid research artifact kind')
    meta = payload.get('meta')
    if not isinstance(meta, Mapping):
        raise StorageError('database_protocol_error', 'Invalid research metadata')
    created_at = _integer(payload, 'created_at', minimum=0)
    session.lock_key('research.artifact', f'{len(paper_hash)}:{paper_hash}{lang_key}')
    session.execute(
        'INSERT INTO paper_reports('
        'paper_hash, lang, report, model, meta, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(paper_hash, lang) DO UPDATE SET '
        'report = excluded.report, model = excluded.model, '
        'meta = excluded.meta, created_at = excluded.created_at',
        (
            paper_hash, lang_key, _optional_text(
                payload, 'report', maximum=10_000_000, scope='research'),
            _optional_text(
                payload, 'model', maximum=512, scope='research'),
            _json_text(dict(meta)), created_at,
        ),
    )
    return {'saved': True}


def _research_artifacts_get(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_hash = _required_text(payload, 'paper_hash', 128)
    lang = _research_lang(payload)
    rows = session.fetch_all(
        'SELECT lang, report, meta FROM paper_reports WHERE paper_hash = ? '
        'AND lang IN (?, ?) ORDER BY lang',
        (paper_hash, f'survey:{lang}', f'ideate:{lang}'),
    )
    result = []
    for row in rows:
        try:
            meta = _load(row['meta'])
        except (TypeError, orjson.JSONDecodeError) as exc:
            logger.debug('[StorageSidecar] skipping invalid research artifact: %s', exc)
            continue
        if isinstance(meta, dict):
            result.append({
                'lang_key': row['lang'], 'report': row['report'] or '',
                'meta': meta,
            })
    return result


def _research_directions_list(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, 'limit', default=50, minimum=1, maximum=1000)
    rows = session.fetch_all(
        "SELECT paper_hash, lang, meta, created_at FROM paper_reports "
        'WHERE lang LIKE ? OR lang LIKE ? '
        'ORDER BY created_at DESC LIMIT ?',
        ('survey:%', 'ideate:%', min(2000, limit * 2)),
    )
    folded: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            meta = _load(row['meta'])
        except (TypeError, orjson.JSONDecodeError) as exc:
            logger.debug('[StorageSidecar] skipping invalid research direction: %s', exc)
            continue
        if not isinstance(meta, dict):
            continue
        direction = str(meta.get('direction') or '').strip()
        if not direction:
            continue
        lang_key = str(row['lang'] or '')
        lang = lang_key.split(':', 1)[1] if ':' in lang_key else 'en'
        key = (str(row['paper_hash']), lang)
        item = folded.setdefault(key, {
            'direction': direction, 'lang': lang,
            'created_at': int(row['created_at'] or 0),
            'accepted': 0, 'rejected': 0, 'gate_reached': '',
            'degraded': False, 'has_survey': False, 'has_ideas': False,
        })
        item['created_at'] = max(
            int(item['created_at']), int(row['created_at'] or 0))
        if meta.get('kind') == 'survey':
            item['has_survey'] = True
        elif meta.get('kind') == 'ideate':
            item['has_ideas'] = True
            item['accepted'] = len(meta.get('accepted') or [])
            item['rejected'] = len(meta.get('rejected') or [])
            item['gate_reached'] = meta.get('gate_reached') or ''
            item['degraded'] = bool(meta.get('degraded'))
    return sorted(
        folded.values(), key=lambda item: item['created_at'], reverse=True)[:limit]


_OPT_PROPOSAL_COLUMNS = (
    'id, created_at, title, rationale, action_type, action_args, severity, '
    'confidence, evidence, status, status_reason'
)
_OPT_ACTION_COLUMNS = (
    'id, proposal_id, applied_at, expires_at, pre_metric, outcome_metric, '
    'outcome_recorded_at, reverted_at, revert_reason'
)


def _optimizer_proposal_create(session: Session, payload: Mapping[str, Any]) -> Any:
    proposal_id = _required_text(payload, 'proposal_id', 128)
    session.execute(
        'INSERT INTO optimizer_proposals('
        + _OPT_PROPOSAL_COLUMNS + ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            proposal_id, _required_text(payload, 'created_at', 64),
            _optional_text(payload, 'title', maximum=500, scope='optimizer'),
            _optional_text(
                payload, 'rationale', maximum=4000, scope='optimizer'),
            _required_text(payload, 'action_type', 256),
            _required_text(payload, 'action_args', 2_000_000),
            _optional_text(
                payload, 'severity', default='low', maximum=64,
                scope='optimizer'),
            _number(payload, 'confidence', minimum=0, maximum=1),
            _required_text(payload, 'evidence', 2_000_000),
            _optional_text(
                payload, 'status', default='pending_review', maximum=64,
                scope='optimizer'),
            _optional_text(
                payload, 'status_reason', maximum=500, scope='optimizer'),
        ),
    )
    return {'proposal_id': proposal_id}


def _optimizer_proposal_update(session: Session, payload: Mapping[str, Any]) -> Any:
    count = session.execute(
        'UPDATE optimizer_proposals SET status = ?, status_reason = ? WHERE id = ?',
        (
            _required_text(payload, 'status', 64),
            _optional_text(payload, 'reason', maximum=500, scope='optimizer'),
            _required_text(payload, 'proposal_id', 128),
        ),
    )
    return {'changed': bool(count)}


def _optimizer_proposal_get(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        'SELECT ' + _OPT_PROPOSAL_COLUMNS
        + ' FROM optimizer_proposals WHERE id = ?',
        (_required_text(payload, 'proposal_id', 128),),
    )
    return row


def _optimizer_proposal_list(session: Session, payload: Mapping[str, Any]) -> Any:
    status = _optional_text(payload, 'status', maximum=64, scope='optimizer')
    limit = _integer(payload, 'limit', default=50, minimum=1, maximum=500)
    return session.fetch_all(
        'SELECT ' + _OPT_PROPOSAL_COLUMNS + ' FROM optimizer_proposals '
        'WHERE (? = ? OR status = ?) ORDER BY created_at DESC LIMIT ?',
        (status, '', status, limit),
    )


def _optimizer_action_record(session: Session, payload: Mapping[str, Any]) -> Any:
    log_id = _required_text(payload, 'log_id', 128)
    proposal_id = _required_text(payload, 'proposal_id', 128)
    if session.fetch_one(
            'SELECT id FROM optimizer_proposals WHERE id = ?',
            (proposal_id,)) is None:
        raise StorageError(
            'database_integrity', 'Optimizer proposal does not exist')
    session.execute(
        'INSERT INTO optimizer_action_log('
        'id, proposal_id, applied_at, expires_at, pre_metric) '
        'VALUES (?, ?, ?, ?, ?)',
        (
            log_id, proposal_id, _required_text(payload, 'applied_at', 64),
            _required_text(payload, 'expires_at', 64),
            _required_text(payload, 'pre_metric', 2_000_000),
        ),
    )
    return {'log_id': log_id}


def _optimizer_action_outcome(session: Session, payload: Mapping[str, Any]) -> Any:
    count = session.execute(
        'UPDATE optimizer_action_log SET outcome_metric = ?, '
        'outcome_recorded_at = ? WHERE id = ?',
        (
            _required_text(payload, 'outcome_metric', 2_000_000),
            _required_text(payload, 'recorded_at', 64),
            _required_text(payload, 'log_id', 128),
        ),
    )
    return {'changed': bool(count)}


def _optimizer_action_revert(session: Session, payload: Mapping[str, Any]) -> Any:
    count = session.execute(
        'UPDATE optimizer_action_log SET reverted_at = ?, revert_reason = ? '
        'WHERE id = ?',
        (
            _required_text(payload, 'reverted_at', 64),
            _optional_text(
                payload, 'reason', maximum=500, scope='optimizer'),
            _required_text(payload, 'log_id', 128),
        ),
    )
    return {'changed': bool(count)}


def _optimizer_action_list(session: Session, payload: Mapping[str, Any]) -> Any:
    include_reverted = payload.get('include_reverted', False)
    if not isinstance(include_reverted, bool):
        raise StorageError(
            'database_protocol_error', 'Invalid include_reverted in storage request')
    limit = _integer(payload, 'limit', default=50, minimum=1, maximum=500)
    return session.fetch_all(
        'SELECT a.' + _OPT_ACTION_COLUMNS.replace(', ', ', a.')
        + ', p.title AS p_title, p.action_type AS p_action_type, '
        'p.action_args AS p_action_args, p.status AS p_status '
        'FROM optimizer_action_log a JOIN optimizer_proposals p '
        'ON p.id = a.proposal_id WHERE (? = 1 OR a.reverted_at = ?) '
        'ORDER BY a.applied_at DESC LIMIT ?',
        (int(include_reverted), '', limit),
    )


def _optimizer_action_expired(session: Session, payload: Mapping[str, Any]) -> Any:
    now_iso = _required_text(payload, 'now_iso', 64)
    return session.fetch_all(
        'SELECT a.' + _OPT_ACTION_COLUMNS.replace(', ', ', a.')
        + ', p.action_type AS p_action_type, p.action_args AS p_action_args, '
        'p.status AS p_status FROM optimizer_action_log a '
        'JOIN optimizer_proposals p ON p.id = a.proposal_id '
        'WHERE a.reverted_at = ? AND p.status = ? '
        'AND a.expires_at != ? AND a.expires_at <= ?',
        ('', 'applied', '', now_iso),
    )


def _optimizer_action_for_proposal(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    return session.fetch_one(
        'SELECT ' + _OPT_ACTION_COLUMNS + ' FROM optimizer_action_log '
        'WHERE proposal_id = ? ORDER BY applied_at DESC LIMIT 1',
        (_required_text(payload, 'proposal_id', 128),),
    )


def _log_aggregate_flush(session: Session, payload: Mapping[str, Any]) -> Any:
    rows = payload.get('rows')
    if not isinstance(rows, list) or len(rows) > 500:
        raise StorageError(
            'database_protocol_error', 'Invalid log aggregate batch')
    for item in rows:
        if not isinstance(item, Mapping):
            raise StorageError(
                'database_protocol_error', 'Invalid log aggregate row')
        session.execute(
            'INSERT INTO log_aggregates('
            'fingerprint, level, logger, template, sample, count, '
            'first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(fingerprint) DO UPDATE SET '
            'count = log_aggregates.count + excluded.count, '
            'last_seen = excluded.last_seen, sample = excluded.sample',
            (
                _required_text(item, 'fingerprint', 64),
                _required_text(item, 'level', 32),
                _optional_text(item, 'logger', maximum=256, scope='log aggregate'),
                _optional_text(item, 'template', maximum=200, scope='log aggregate'),
                _optional_text(item, 'sample', maximum=2000, scope='log aggregate'),
                _integer(item, 'count', minimum=1, maximum=1_000_000_000),
                _integer(item, 'first_seen', minimum=0),
                _integer(item, 'last_seen', minimum=0),
            ),
        )
    swept = 0
    cutoff = payload.get('cutoff_ms')
    if cutoff is not None:
        if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 0:
            raise StorageError(
                'database_protocol_error', 'Invalid log aggregate cutoff')
        swept = session.execute(
            'DELETE FROM log_aggregates WHERE last_seen < ?', (cutoff,))
    return {'flushed': len(rows), 'swept': swept}


def _log_aggregate_query(session: Session, payload: Mapping[str, Any]) -> Any:
    level = _optional_text(
        payload, 'level', maximum=32, scope='log aggregate')
    sort = _optional_text(
        payload, 'sort', default='count', maximum=32, scope='log aggregate')
    orders = {
        'count': 'count DESC, last_seen DESC',
        'last_seen': 'last_seen DESC',
        'level': 'level ASC, count DESC',
    }
    if sort not in orders:
        raise StorageError(
            'database_protocol_error', 'Invalid log aggregate sort')
    limit = _integer(payload, 'limit', default=100, minimum=1, maximum=500)
    q = _optional_text(payload, 'q', maximum=200, scope='log aggregate')
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    pattern = f'%{escaped}%'
    where = 'WHERE (? = ? OR level = ?) AND (? = ? OR template LIKE ? ESCAPE \'\\\') '
    params = (level, '', level, q, '', pattern)
    rows = session.fetch_all(
        'SELECT fingerprint, level, logger, template, sample, count, '
        'first_seen, last_seen FROM log_aggregates ' + where
        + 'ORDER BY ' + orders[sort] + ' LIMIT ?',
        params + (limit,),
    )
    totals = session.fetch_one(
        'SELECT COUNT(*) AS n, COALESCE(SUM(count), 0) AS events '
        'FROM log_aggregates ' + where,
        params,
    )
    return {
        'items': rows, 'total_rows': int(totals['n'] if totals else 0),
        'total_events': int(totals['events'] if totals else 0),
    }


def _plugin_register(session: Session, payload: Mapping[str, Any]) -> Any:
    try:
        manifest = validate_manifest(payload.get('manifest'))
    except (ManifestError, TypeError) as exc:
        raise StorageError(
            'plugin_storage_incompatible', 'Plugin storage manifest is incompatible') from exc
    namespace = manifest['namespace']
    encoded = _dump(manifest)
    current = session.fetch_one(
        'SELECT manifest_version, manifest_json FROM storage_plugin_manifests '
        'WHERE namespace = ?',
        (namespace,),
    )
    if current:
        current_version = int(current['manifest_version'])
        if manifest['version'] < current_version:
            raise StorageError(
                'plugin_storage_incompatible', 'Plugin storage version moved backwards')
        if manifest['version'] == current_version and bytes(current['manifest_json']) != encoded:
            raise StorageError(
                'plugin_storage_incompatible', 'Plugin storage version was redefined')
        if manifest['version'] > current_version:
            previous = _load(current['manifest_json'])
            previous_tables = {item['name']: item for item in previous['tables']}
            next_tables = {item['name']: item for item in manifest['tables']}
            previous_operations = {item['name']: item for item in previous['operations']}
            next_operations = {item['name']: item for item in manifest['operations']}
            incompatible = False
            for name, table in previous_tables.items():
                upgraded = next_tables.get(name)
                if upgraded is None:
                    incompatible = True
                    break
                old_columns = table['columns']
                new_columns = upgraded['columns']
                old_indexes = {item['name']: item for item in table.get('indexes', [])}
                new_indexes = {item['name']: item for item in upgraded.get('indexes', [])}
                if (new_columns[:len(old_columns)] != old_columns
                        or upgraded['primary_key'] != table['primary_key']
                        or any(new_indexes.get(index) != definition
                               for index, definition in old_indexes.items())
                        or any(item.get('required')
                               for item in new_columns[len(old_columns):])
                        or any(item.get('unique') and item['name'] not in old_indexes
                               for item in upgraded.get('indexes', []))):
                    incompatible = True
                    break
            incompatible = incompatible or any(
                name not in next_operations or next_operations[name] != operation
                for name, operation in previous_operations.items()
            )
            if incompatible:
                raise StorageError(
                    'plugin_storage_incompatible',
                    'Plugin storage migration is not append-only compatible')
    now = int(time.time() * 1000)
    session.execute(
        'INSERT INTO storage_plugin_manifests(namespace, manifest_version, manifest_json, updated_at_ms) '
        'VALUES (?, ?, ?, ?) ON CONFLICT(namespace) DO UPDATE SET '
        'manifest_version = excluded.manifest_version, manifest_json = excluded.manifest_json, '
        'updated_at_ms = excluded.updated_at_ms',
        (namespace, manifest['version'], encoded, now),
    )
    return {'namespace': namespace, 'version': manifest['version']}


def _plugin_manifest_get(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, 'namespace', 128)
    row = session.fetch_one(
        'SELECT manifest_json FROM storage_plugin_manifests WHERE namespace = ?',
        (namespace,),
    )
    return _load(row['manifest_json']) if row else None


def _plugin_context(session: Session, operation: str):
    parts = operation.split('.')
    if len(parts) < 3 or parts[0] != 'plugin':
        raise StorageError('database_protocol_error', 'Unknown storage operation')
    namespace = '.'.join(parts[1:-1])
    operation_name = parts[-1]
    row = session.fetch_one(
        'SELECT manifest_json FROM storage_plugin_manifests WHERE namespace = ?',
        (namespace,),
    )
    if row is None:
        raise StorageError(
            'plugin_storage_incompatible', 'Plugin storage namespace is not registered')
    manifest = _load(row['manifest_json'])
    operation_spec = next(
        (item for item in manifest['operations'] if item['name'] == operation_name), None)
    if operation_spec is None:
        raise StorageError('database_protocol_error', 'Unknown plugin storage operation')
    table = next(item for item in manifest['tables'] if item['name'] == operation_spec['table'])
    return namespace, operation_spec, table


def _plugin_dynamic(
    session: Session,
    operation: str,
    kind: str,
    payload: Mapping[str, Any],
) -> Any:
    namespace, spec, table = _plugin_context(session, operation)
    if spec['kind'] != kind:
        raise StorageError('database_protocol_error', 'Plugin operation kind mismatch')
    action = spec['action']
    table_name = table['name']
    primary_key = table['primary_key'][0]
    if action in {'get', 'delete'}:
        key = _required_text(payload, primary_key)
    if action == 'get':
        row = session.fetch_one(
            'SELECT document_json, version, updated_at_ms FROM storage_plugin_rows '
            'WHERE namespace = ? AND table_name = ? AND row_key = ?',
            (namespace, table_name, key),
        )
        if row is None:
            return None
        return {
            'document': _load(row['document_json']),
            'version': int(row['version']),
            'updated_at_ms': int(row['updated_at_ms']),
        }
    if action == 'list':
        limit = _integer(
            payload, 'limit', default=100, minimum=1,
            maximum=spec['limit_max'])
        filters = payload.get('filters') or {}
        if not isinstance(filters, Mapping):
            raise StorageError('database_protocol_error', 'Plugin filters must be an object')
        declared = {column['name'] for column in table['columns']}
        if set(filters) - declared:
            raise StorageError('database_protocol_error', 'Plugin filter is undeclared')
        # The validated query model is deliberately evaluated after a bounded
        # table read; no plugin expression is interpolated into SQL.
        rows = session.fetch_all(
            'SELECT document_json, version, updated_at_ms FROM storage_plugin_rows '
            'WHERE namespace = ? AND table_name = ? ORDER BY row_key LIMIT ?',
            (namespace, table_name, min(1000, max(limit * 10, limit))),
        )
        result = []
        for row in rows:
            document = _load(row['document_json'])
            if all(document.get(key) == value for key, value in filters.items()):
                result.append({
                    'document': document,
                    'version': int(row['version']),
                    'updated_at_ms': int(row['updated_at_ms']),
                })
                if len(result) >= limit:
                    break
        return result
    if action == 'put':
        try:
            document = validate_document(table, payload.get('document'))
        except ManifestError as exc:
            raise StorageError(
                'plugin_storage_incompatible', 'Plugin document violates its manifest') from exc
        key_value = document.get(primary_key)
        if not isinstance(key_value, (str, int)) or isinstance(key_value, bool):
            raise StorageError(
                'plugin_storage_incompatible', 'Plugin primary key is invalid')
        key = str(key_value)
        current = session.fetch_one(
            'SELECT version FROM storage_plugin_rows '
            'WHERE namespace = ? AND table_name = ? AND row_key = ?',
            (namespace, table_name, key),
        )
        actual = int(current['version']) if current else 0
        expected = _expected_version(payload)
        if expected is not None and expected != actual:
            raise StorageError('database_conflict', 'Plugin row version conflict')
        version = actual + 1
        now = int(time.time() * 1000)
        unique_values = []
        for index in table.get('indexes', []):
            if not index.get('unique'):
                continue
            values = [document.get(column) for column in index['columns']]
            if any(value is None for value in values):
                continue
            unique_value = hashlib.sha256(_dump(values)).hexdigest()
            owner = session.fetch_one(
                'SELECT row_key FROM storage_plugin_unique_values '
                'WHERE namespace = ? AND table_name = ? AND index_name = ? '
                'AND index_value = ?',
                (namespace, table_name, index['name'], unique_value),
            )
            if owner is not None and owner['row_key'] != key:
                raise StorageError(
                    'database_conflict', 'Plugin unique constraint conflict')
            unique_values.append((index['name'], unique_value))
        session.execute(
            'DELETE FROM storage_plugin_unique_values '
            'WHERE namespace = ? AND table_name = ? AND row_key = ?',
            (namespace, table_name, key),
        )
        for index_name, unique_value in unique_values:
            session.execute(
                'INSERT INTO storage_plugin_unique_values('
                'namespace, table_name, index_name, index_value, row_key) '
                'VALUES (?, ?, ?, ?, ?)',
                (namespace, table_name, index_name, unique_value, key),
            )
        session.execute(
            'INSERT INTO storage_plugin_rows(namespace, table_name, row_key, document_json, version, updated_at_ms) '
            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(namespace, table_name, row_key) DO UPDATE SET '
            'document_json = excluded.document_json, version = excluded.version, '
            'updated_at_ms = excluded.updated_at_ms',
            (namespace, table_name, key, _dump(_wire_document(document)), version, now),
        )
        return {'key': key, 'version': version, 'updated_at_ms': now}
    if action == 'delete':
        session.execute(
            'DELETE FROM storage_plugin_unique_values '
            'WHERE namespace = ? AND table_name = ? AND row_key = ?',
            (namespace, table_name, key),
        )
        count = session.execute(
            'DELETE FROM storage_plugin_rows '
            'WHERE namespace = ? AND table_name = ? AND row_key = ?',
            (namespace, table_name, key),
        )
        return {'deleted': bool(count)}
    raise StorageError('database_protocol_error', 'Unsupported plugin action')


_OPERATIONS: dict[str, OperationSpec] = {
    'system.schema_version': OperationSpec('query', False, _schema_version),
    'record.get': OperationSpec('query', False, _record_get),
    'record.list': OperationSpec('query', False, _record_list),
    'record.put': OperationSpec('command', True, _record_put),
    'record.delete': OperationSpec('command', True, _record_delete),
    'event.append': OperationSpec('command', False, _event_append),
    'event.append_batch': OperationSpec('command', False, _event_append_batch),
    'event.list': OperationSpec('query', False, _event_list),
    'rate_limit.record_and_check': OperationSpec(
        'command', True, _rate_limit_record_and_check),
    'orchestration.run.create': OperationSpec(
        'command', True, _orchestration_run_create),
    'orchestration.run.get': OperationSpec(
        'query', False, _orchestration_run_get),
    'orchestration.run.list': OperationSpec(
        'query', False, _orchestration_run_list),
    'orchestration.run.update_status': OperationSpec(
        'command', True, _orchestration_run_update),
    'orchestration.run.retire_interrupted': OperationSpec(
        'command', True, _orchestration_run_retire),
    'orchestration.event.append': OperationSpec(
        'command', False, _orchestration_event_append),
    'orchestration.event.project': OperationSpec(
        'command', False, _orchestration_event_project),
    'orchestration.event.page': OperationSpec(
        'query', False, _orchestration_event_page),
    'orchestration.run.delete': OperationSpec(
        'command', True, _orchestration_run_delete),
    'swarm.session.save': OperationSpec('command', True, _swarm_session_save),
    'swarm.session.terminate': OperationSpec(
        'command', True, _swarm_session_terminate),
    'swarm.session.delete': OperationSpec(
        'command', True, _swarm_session_delete),
    'swarm.agent.save': OperationSpec('command', True, _swarm_agent_save),
    'swarm.agents.mark_delivered': OperationSpec(
        'command', True, _swarm_agents_mark_delivered),
    'swarm.session.get': OperationSpec('query', False, _swarm_session_get),
    'swarm.resumable.list': OperationSpec(
        'query', False, _swarm_resumable_list),
    'research.artifact.upsert': OperationSpec(
        'command', True, _research_artifact_upsert),
    'research.artifacts.get': OperationSpec(
        'query', False, _research_artifacts_get),
    'research.directions.list': OperationSpec(
        'query', False, _research_directions_list),
    'paper.report.upsert': OperationSpec(
        'command', True, _paper_report_upsert),
    'paper.report.get': OperationSpec('query', False, _paper_report_get),
    'paper.report.second_pass.merge': OperationSpec(
        'command', True, _paper_report_second_pass_merge),
    'paper.report.second_pass.accumulate': OperationSpec(
        'command', True, _paper_report_second_pass_accumulate),
    'paper.translation.upsert': OperationSpec(
        'command', True, _paper_translation_upsert),
    'paper.translation.get': OperationSpec(
        'query', False, _paper_translation_get),
    'paper.library.put': OperationSpec('command', True, _paper_library_put),
    'paper.library.recent': OperationSpec('query', False, _paper_library_recent),
    'paper.library.identity': OperationSpec(
        'query', False, _paper_library_identity),
    'paper.library.title.backfill': OperationSpec(
        'command', True, _paper_library_title_backfill),
    'daily_cost.month': OperationSpec('query', False, _daily_cost_month),
    'daily_cost.upsert': OperationSpec('command', True, _daily_cost_upsert),
    'daily_cost.delete': OperationSpec('command', True, _daily_cost_delete),
    'daily_cost.persisted_dates': OperationSpec(
        'query', False, _daily_cost_persisted_dates),
    'daily_cost.latest': OperationSpec('query', False, _daily_cost_latest),
    'paper.podcast.upsert': OperationSpec(
        'command', True, _paper_podcast_upsert),
    'paper.podcast.get': OperationSpec('query', False, _paper_podcast_get),
    'paper.podcast.mark_interrupted': OperationSpec(
        'command', True, _paper_podcast_mark_interrupted),
    'tenant.user.create': OperationSpec(
        'command', True, _tenant_user_create),
    'tenant.user.get': OperationSpec('query', False, _tenant_user_get),
    'tenant.user.list': OperationSpec('query', False, _tenant_user_list),
    'tenant.user.set_status': OperationSpec(
        'command', True, _tenant_user_set_status),
    'tenant.user.set_role': OperationSpec(
        'command', True, _tenant_user_set_role),
    'tenant.user.authentication': OperationSpec(
        'query', False, _tenant_user_authentication),
    'tenant.user.record_login': OperationSpec(
        'command', True, _tenant_user_record_login),
    'artifact.create': OperationSpec('command', True, _artifact_create),
    'artifact.get': OperationSpec('query', False, _artifact_get),
    'artifact.list': OperationSpec('query', False, _artifact_list),
    'artifact.delete': OperationSpec('command', True, _artifact_delete),
    'artifact.versions': OperationSpec('query', False, _artifact_versions),
    'artifact.library': OperationSpec('query', False, _artifact_library),
    'artifact.pin': OperationSpec('command', True, _artifact_pin),
    'optimizer.proposal.create': OperationSpec(
        'command', True, _optimizer_proposal_create),
    'optimizer.proposal.update': OperationSpec(
        'command', True, _optimizer_proposal_update),
    'optimizer.proposal.get': OperationSpec(
        'query', False, _optimizer_proposal_get),
    'optimizer.proposal.list': OperationSpec(
        'query', False, _optimizer_proposal_list),
    'optimizer.action.record': OperationSpec(
        'command', True, _optimizer_action_record),
    'optimizer.action.outcome': OperationSpec(
        'command', True, _optimizer_action_outcome),
    'optimizer.action.revert': OperationSpec(
        'command', True, _optimizer_action_revert),
    'optimizer.action.list': OperationSpec(
        'query', False, _optimizer_action_list),
    'optimizer.action.expired': OperationSpec(
        'query', False, _optimizer_action_expired),
    'optimizer.action.for_proposal': OperationSpec(
        'query', False, _optimizer_action_for_proposal),
    'log_aggregate.flush': OperationSpec(
        'command', False, _log_aggregate_flush),
    'log_aggregate.query': OperationSpec(
        'query', False, _log_aggregate_query),
    'plugin.register': OperationSpec('command', True, _plugin_register),
    'plugin.manifest.get': OperationSpec('query', False, _plugin_manifest_get),
}


def resolve_operation(operation: str, kind: str, payload: Mapping[str, Any]):
    spec = _OPERATIONS.get(operation)
    if spec is not None:
        if spec.kind != kind:
            raise StorageError('database_protocol_error', 'Storage operation kind mismatch')
        return spec.receipt_required, lambda session: spec.handler(session, payload)
    if operation.startswith('plugin.'):
        return kind == 'command', lambda session: _plugin_dynamic(
            session, operation, kind, payload)
    raise StorageError('database_protocol_error', 'Unknown storage operation')


__all__ = ['OperationSpec', 'resolve_operation']
