"""Sidecar-backed seeding helpers for business-logic tests (storage.v1-only).

These replace the legacy ``get_thread_db(...).execute('INSERT ...')`` seed
pattern: every helper goes through the semantic storage client, never raw
SQL.  Pair with the ``chat_sidecar`` fixture (``tests/_chat_sidecar.py``) so
the process-wide runtime points at the module-scoped isolated sidecar.
"""

from __future__ import annotations

import time
import uuid


def _write_client():
    from lib.storage import get_storage_client
    return get_storage_client(write=True)


def clear_records(namespace):
    """Delete every record in a namespace (per-test isolation for sidecar
    runs).  The module-scoped sidecar is shared, so suites that reuse a fixed
    project path must clear between tests just like the legacy ``DELETE FROM``
    fixtures did."""
    client = _write_client()
    records = client.query('record.list', {'namespace': namespace}) or []
    for rec in records:
        client.command('record.delete', {
            'namespace': namespace, 'key': rec['key'],
        }, f'test-clear:{namespace}:{rec["key"]}:{uuid.uuid4().hex[:8]}')


def clear_events():
    """Prune all sidecar events (per-test isolation for feed/status lanes)."""
    client = _write_client()
    cutoff = int(time.time() * 1000) + 60_000
    for retention_class in ('streaming', 'structural'):
        while True:
            result = client.command('event.prune', {
                'created_before_ms': cutoff,
                'limit': 1000,
                'retention_class': retention_class,
            }, f'test-prune:{retention_class}:{uuid.uuid4().hex[:8]}')
            if result.get('deferred') or not result['deleted']:
                break


def clear_watch(*paths, user_id):
    """Delete all watch items for the given project paths (per-test
    isolation).  The op-side delete cascades to the item's response trail."""
    client = _write_client()
    for path in paths:
        items = (client.query('watch.list', {
            'user_id': int(user_id), 'project_path': path,
        })
                 or {}).get('items', [])
        for item in items:
            client.command('watch.mutate', {
                'user_id': int(user_id),
                'action': 'delete', 'item_id': item['item_id'],
            }, f'test-clear-watch:{item["item_id"]}:{uuid.uuid4().hex[:8]}')


def seed_charter(
    project_path,
    *,
    user_id,
    content='',
    decisions=None,
    updated_by_conv='human',
):
    """Direct charter-record seed (replaces the legacy ``INSERT INTO
    project_charter`` test pattern) — writes the record the sidecar read path
    projects, with record version 1."""
    return _write_client().command('project.charter.put', {
        'project_path': project_path,
        'user_id': int(user_id),
        'value': {
            'content': content,
            'decisions': list(decisions or []),
            'updated_by_conv': updated_by_conv,
            'updated_at': int(time.time() * 1000),
        },
    }, f'seed-charter:{int(user_id)}:{project_path}:{uuid.uuid4().hex[:8]}')


def seed_conversation(conv_id, *, user_id=1, messages=None, title='',
                      settings=None, created_at=None, updated_at=None):
    """Create a header and append canonical settled turns one at a time."""
    now = int(time.time() * 1000)
    created = int(created_at if created_at is not None else now)
    updated = int(updated_at if updated_at is not None else now)
    client = _write_client()
    result = client.command(
        'conversation.create', {
            'conv_id': conv_id,
            'user_id': user_id,
            'title': title,
            'created_at': created,
            'updated_at': updated,
            'settings': settings or {},
        }, f'seed-conv-create:{conv_id}:{uuid.uuid4().hex[:10]}')
    transcript = list(messages or [])
    for index, message in enumerate(transcript):
        if not isinstance(message, dict):
            raise ValueError('seed messages must be objects')
        role = message.get('role')
        actor = {'user': 'human', 'assistant': 'assistant'}.get(role)
        if actor is None:
            raise ValueError(f'unsupported seed message role: {role!r}')
        projection = {
            key: value for key, value in message.items()
            if key not in {'role', 'finishReason', '_turnSettlement'}
        }
        raw_settlement = message.get('_turnSettlement')
        settlement = (
            dict(raw_settlement)
            if isinstance(raw_settlement, dict)
            else {
                'outcome': 'completed',
                'cause': 'ingested',
                'resumeOptions': [],
            }
        )
        finish_reason = message.get('finishReason')
        if finish_reason and not settlement.get('providerFinishReason'):
            settlement['providerFinishReason'] = str(finish_reason)
        timestamp = projection.get('timestamp')
        turn_created = (
            int(timestamp) if isinstance(timestamp, (int, float))
            and not isinstance(timestamp, bool)
            else created + index
        )
        client.command(
            'turn.append_settled', {
                'conversation_id': conv_id,
                'user_id': user_id,
                'command_id': f'seed-turn:{conv_id}:{index}',
                'actor': actor,
                'kind': 'fixture',
                'projection': projection,
                'settlement': settlement,
                'created_at': turn_created,
            }, f'seed-turn:{conv_id}:{index}:{uuid.uuid4().hex[:10]}')
    if transcript and updated_at is not None:
        client.command(
            'conversation.metadata.update', {
                'conv_id': conv_id,
                'user_id': user_id,
                'updates': {'updated_at': updated},
            }, f'seed-conv-time:{conv_id}:{uuid.uuid4().hex[:10]}')
    return {
        **result,
        'turnCount': len(transcript),
    }


def wait_for_conversation_search(
    query,
    *,
    user_id=1,
    expected_ids=(),
    absent_ids=(),
    timeout_s=5.0,
    client=None,
):
    """Wait for the independent search projection to reflect an authority write.

    Conversation commands commit a bounded dirty marker with durable state;
    the disposable search projection consumes it asynchronously.  Tests that
    assert read-after-write search behavior must therefore wait on the public
    semantic query instead of assuming the authority writer also rebuilt an
    index synchronously.
    """
    search_client = client or _write_client()
    expected = {str(value) for value in expected_ids}
    absent = {str(value) for value in absent_ids}
    deadline = time.monotonic() + float(timeout_s)
    hits = []
    observed_ids = set()
    while True:
        hits = search_client.query('conversation.search', {
            'user_id': int(user_id),
            'query': str(query),
            'limit': 50,
            'snippet_radius': 40,
        }) or []
        observed_ids = {
            str(row.get('id') or '') for row in hits
            if isinstance(row, dict)
        }
        if expected <= observed_ids and observed_ids.isdisjoint(absent):
            return hits
        if time.monotonic() >= deadline:
            raise AssertionError(
                'conversation search projection did not converge: '
                f'query={query!r}, expected={sorted(expected)!r}, '
                f'absent={sorted(absent)!r}, observed={sorted(observed_ids)!r}'
            )
        time.sleep(0.025)


def delete_conversation(conv_id, *, user_id=1):
    """Permanently remove one fixture conversation from every lifecycle state."""
    return _write_client().command(
        'conversation.purge', {
            'conv_id': conv_id,
            'user_id': user_id,
        }, f'purge-conv:{conv_id}:{uuid.uuid4().hex[:10]}')


def conv_document(conv_id, *, user_id=1):
    """Return the full conversation document (metadata + messages)."""
    from lib.storage import get_storage_client
    return get_storage_client().query(
        'conversation.get', {'conv_id': conv_id, 'user_id': user_id})


def conv_settings(conv_id, *, user_id=1):
    doc = conv_document(conv_id, user_id=user_id)
    if not doc:
        return None
    return (doc.get('metadata') or {}).get('settings') or {}


# ── Board (project brain) ────────────────────────────────────────────────

def _board_task_document(task_id, project_path, **fields):
    now = int(time.time() * 1000)
    doc = {
        'id': task_id,
        'project_path': project_path,
        'title': fields.pop('title', ''),
        'status': fields.pop('status', 'open'),
        'owner_conv_id': fields.pop('owner_conv_id', ''),
        'lease_expires_at': int(fields.pop('lease_expires_at', 0) or 0),
        'created_by_conv': fields.pop('created_by_conv', ''),
        'depends_on': list(fields.pop('depends_on', []) or []),
        'kind': fields.pop('kind', ''),
        'dispatched': int(fields.pop('dispatched', 0) or 0),
        'blocked_until': int(fields.pop('blocked_until', 0) or 0),
        'block_count': int(fields.pop('block_count', 0) or 0),
        'block_reason': fields.pop('block_reason', ''),
        'wait_paths': list(fields.pop('wait_paths', []) or []),
        'dispatch_target': fields.pop('dispatch_target', ''),
        'write_set': list(fields.pop('write_set', []) or []),
        'block_question': fields.pop('block_question', ''),
        'human_answer': fields.pop('human_answer', ''),
        'blocked_by': fields.pop('blocked_by', ''),
        'created_at': int(fields.pop('created_at', now) or now),
        'updated_at': int(fields.pop('updated_at', now) or now),
    }
    if fields:
        raise ValueError(f'unknown board fields: {sorted(fields)}')
    return doc


def seed_board_task(task_id, project_path, *, user_id, **fields):
    """Insert a board task with arbitrary column values (incl. an expired
    ``lease_expires_at``) via the idempotent ``board.import_batch`` op."""
    doc = _board_task_document(task_id, project_path, **fields)
    return _write_client().command(
        'board.import_batch', {
            'user_id': int(user_id), 'documents': [doc],
        },
        f'seed-board:{project_path}:{task_id}')


def clear_board(*paths, user_id):
    """Delete Sidecar-backed board tasks for explicit project paths.

    Tests use the semantic delete operation so fixture cleanup exercises the
    same owner and dependency boundaries as production instead of opening the
    Sidecar database directly.
    """
    client = _write_client()
    for project_path in paths:
        board = client.query('board.list', {
            'project_path': project_path,
            'user_id': int(user_id),
        }) or {}
        tasks = list(board.get('tasks') or [])
        # Delete dependents before their prerequisites. Test fixtures only
        # need a bounded cleanup pass; retrying resolves either ordering.
        pending = {str(task['id']): task for task in tasks}
        while pending:
            removed = 0
            for task_id in list(pending):
                result = client.command('board.mutate', {
                    'action': 'delete',
                    'project_path': project_path,
                    'user_id': int(user_id),
                    'task_id': task_id,
                }, f'test-clear-board:{task_id}:{uuid.uuid4().hex[:8]}')
                if result.get('ok') or result.get('error') == 'task not found':
                    pending.pop(task_id, None)
                    removed += 1
            if not removed:
                raise AssertionError(
                    f'could not clear dependent board tasks: {sorted(pending)}')
