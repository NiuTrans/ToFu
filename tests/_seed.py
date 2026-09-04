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
