"""Semantic durable continuation operations over storage_records.

The conversation lock serializes publication with delete/purge. A settled Turn
ordinal is the monotonic CAS frontier, and its model-visible witness fences
edited or foreign writers. Equal-frontier retries are read-only: no command
receipt stores a second payload. At most one 8 MiB record per existing lane;
conversation deletion discards derived state rather than archiving it.
"""
from __future__ import annotations

import hashlib
import time

from lib.storage.model_continuation import encode_projection, settled_witness
from lib.storage_authority_sql.operations_pkg._common import _integer, _required_text, _load
from lib.storage_authority_sql.operations_pkg._turns_core import _turn_public

_NAMESPACE = 'model.continuation.v1'


def _prefix(user_id, conversation_id):
    return f'{user_id}:' + hashlib.sha256(conversation_id.encode('utf-8')).hexdigest() + ':'


def _identity(session, payload, *, lock=False):
    user_id = _integer(payload, 'user_id', minimum=1)
    conversation_id = _required_text(payload, 'conversation_id', 256)
    lane_id = _required_text(payload, 'lane_id', 256)
    if lock:
        session.lock_key('conversation', f'{user_id}:{conversation_id}')
        session.lock_key('turn_conversation', f'{user_id}:{conversation_id}')
    owned = session.fetch_one(
        'SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?',
        (conversation_id, user_id))
    key = _prefix(user_id, conversation_id) + hashlib.sha256(lane_id.encode('utf-8')).hexdigest()
    return user_id, conversation_id, lane_id, key, owned is not None


def continuation_get(session, payload):
    _, _, _, key, owned = _identity(session, payload)
    if not owned:
        return None
    row = session.fetch_one(
        'SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?',
        (_NAMESPACE, key))
    if row is None:
        return None
    projection = _load(row['value_json'])
    return projection if encode_projection(projection) is not None else None


def continuation_put(session, payload):
    user_id, conversation_id, lane_id, key, owned = _identity(session, payload, lock=True)
    if not owned:
        return False
    projection = payload.get('projection')
    encoded = encode_projection(projection)
    if encoded is None:
        return False
    witness = projection['turns'][-1]
    session.lock_key('turn_attempt', witness[0])
    row = session.fetch_one(
        'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
        'AND user_id=? AND lane_id=? AND turn_id=?',
        (conversation_id, user_id, lane_id, witness[0]))
    if (row is None or row['status'] not in ('completed', 'truncated')
            or settled_witness(_turn_public(session, row)) != witness):
        return False
    # An ordinal comes only from authority, never an untrusted client counter.
    frontier = int(row['ordinal']) + 1
    current = session.fetch_one(
        'SELECT value_json,version FROM storage_records WHERE namespace=? AND record_key=?',
        (_NAMESPACE, key))
    if current is not None:
        actual = int(current['version'])
        if frontier < actual:
            return False
        if frontier == actual:
            return _load(current['value_json']) == projection
        changed = session.execute(
            'UPDATE storage_records SET value_json=?,version=?,updated_at_ms=? '
            'WHERE namespace=? AND record_key=? AND version=?',
            (encoded, frontier, int(time.time() * 1000), _NAMESPACE, key, actual))
        return changed == 1
    session.execute(
        'INSERT INTO storage_records(namespace,record_key,value_json,version,updated_at_ms) '
        'VALUES (?,?,?,?,?)',
        (_NAMESPACE, key, encoded, frontier, int(time.time() * 1000)))
    return True


def settle_continuation(session, turn, candidate, *, status):
    """Called in the terminal transaction; storage failures roll back settlement."""
    if status not in ('completed', 'truncated'):
        return 'not_completed'
    if not isinstance(candidate, dict):
        return 'unavailable'
    if 'projection' not in candidate:
        reason = candidate.get('status')
        return reason if isinstance(reason, str) and len(reason) <= 64 else 'invalid'
    projection = candidate['projection']
    if encode_projection(projection) is None:
        return 'invalid_or_over_budget'
    row = session.fetch_one(
        'SELECT * FROM storage_conversation_turns WHERE turn_id=? AND user_id=?',
        (turn['turn_id'], turn['user_id']))
    target = _turn_public(session, row)
    if (projection['turns'][-1][0] != target['turnId']
            or candidate.get('content') != (target.get('projection') or {}).get('content')):
        return 'authority_changed'
    projection = dict(projection)
    projection['turns'] = projection['turns'][:-1] + [settled_witness(target)]
    stored = continuation_put(session, {
        'user_id': turn['user_id'], 'conversation_id': turn['conversation_id'],
        'lane_id': turn['lane_id'], 'projection': projection})
    return 'stored' if stored else 'rejected'


def continuation_delete(session, payload):
    _, _, _, key, owned = _identity(session, payload, lock=True)
    if not owned:
        return False
    return bool(session.execute(
        'DELETE FROM storage_records WHERE namespace=? AND record_key=?', (_NAMESPACE, key)))


def delete_conversation_continuations(session, conversation_id, user_id):
    """Called inside conversation lifecycle transaction with its lock held."""
    session.execute(
        'DELETE FROM storage_records WHERE namespace=? AND record_key LIKE ?',
        (_NAMESPACE, _prefix(user_id, conversation_id) + '%'))
