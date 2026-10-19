"""Owner-scoped model context read with semantic-revision continuations.

Compact authority headers prove the prior main-lane prefix. Display-only
settled enrichment advances ordinary projection CAS without invalidating model
context. Real edits/execution changes, ownership and current-target fences stay
mandatory. This query never substitutes for same-turn Continue recovery.
"""
from __future__ import annotations

import hashlib
import json

from lib.storage.errors import StorageError
from lib.storage.model_continuation import settled_witness
from ._common import _required_text, _integer
from ._turns_core import _turn_public, _optional_row_value
from ._model_continuation import continuation_get

MAX_CONTEXT_HEADERS = 10000


def _header_bytes(row):
    revision = int(row['projection_revision']) - int(_optional_row_value(row, 'model_context_display_offset') or 0)
    if revision < 0:
        raise StorageError('database_integrity', 'context display offset exceeds revision')
    return (json.dumps([
        row['turn_id'], row['parent_turn_id'], row['status'],
        row['current_attempt_id'], revision,
    ], ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def read_model_context(session, payload):
    conv = _required_text(payload, 'conversation_id', 256)
    target_id = _required_text(payload, 'turn_id', 128)
    owner = _integer(payload, 'user_id', minimum=1)
    replay = payload.get('replay', True)
    if type(replay) is not bool:
        raise StorageError('database_protocol_error', 'replay must be boolean')
    after_turn_id = payload.get('after_turn_id')
    if after_turn_id is not None:
        if not isinstance(after_turn_id, str) or not 0 < len(after_turn_id) <= 128:
            raise StorageError('database_protocol_error', 'invalid context page cursor')
    target = session.fetch_one(
        'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
        'AND user_id=? AND turn_id=?', (conv, owner, target_id))
    if target is None:
        return None
    if target['lane_id'] != 'main':
        return {'supported': False}
    headers = session.fetch_all(
        'SELECT turn_id,parent_turn_id,status,current_attempt_id,projection_revision,'
        'model_context_display_offset,ordinal '
        'FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? '
        'AND lane_id=? AND ordinal<=? ORDER BY ordinal,turn_id LIMIT ?',
        (conv, owner, 'main', target['ordinal'], MAX_CONTEXT_HEADERS + 1))
    if len(headers) > MAX_CONTEXT_HEADERS:
        return {'supported': False}
    if not headers or headers[-1]['turn_id'] != target_id:
        raise StorageError('database_internal', 'Model context target order changed')
    cached = continuation_get(session, {
        'conversation_id': conv, 'user_id': owner, 'lane_id': 'main'}) if replay else None
    proof = (cached or {}).get('historyProof')
    proof = proof if isinstance(proof, dict) else {}
    count = proof.get('count')
    usable = (type(proof.get('version')) is int and proof.get('version') == 2
              and type(count) is int and 0 <= count < len(headers) - 1
              and len((cached or {}).get('turns') or []) == 1)
    if not replay:
        continuation_status = 'replay_disabled'
    elif not cached:
        continuation_status = 'checkpoint_missing'
    elif 'historyProof' not in cached:
        continuation_status = 'legacy_checkpoint'
    elif proof.get('version') != 2:
        continuation_status = 'legacy_proof'
    elif not usable:
        continuation_status = 'checkpoint_not_before_target'
    else:
        continuation_status = 'history_changed'
    digest = hashlib.sha256()
    prior_matches = False
    for index, header in enumerate(headers[:-1]):
        if usable and index == count:
            prior_matches = digest.hexdigest() == proof.get('digest')
        digest.update(_header_bytes(header))
    covered = 0
    if prior_matches:
        continuation_status = 'terminal_changed'
        final_id = cached['turns'][0][0]
        if headers[count]['turn_id'] == final_id:
            row = session.fetch_one(
                'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
                'AND user_id=? AND turn_id=?', (conv, owner, final_id))
            if row is not None and settled_witness(_turn_public(session, row)) == cached['turns'][0]:
                covered = count + 1
                continuation_status = 'available'
    selected = headers[covered:]
    start = 0
    if after_turn_id is not None:
        start = next((index + 1 for index, row in enumerate(selected)
                      if row['turn_id'] == after_turn_id), 0)
        if not 0 < start < len(selected):
            raise StorageError('database_protocol_error', 'context page cursor is outside the selected suffix')
    continuation = cached if covered or (cached and 'historyProof' not in cached) else None
    continuation_bytes = len(json.dumps(continuation, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')) if continuation else 0
    page_limit = min(67_104_768 - 8 * 1024 * 1024 - 128 * 1024,
                     67_104_768 - continuation_bytes - 128 * 1024)
    turns = []
    page_bytes = 0
    for header in selected[start:]:
        row = session.fetch_one(
            'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
            'AND user_id=? AND turn_id=?', (conv, owner, header['turn_id']))
        if row is None:
            raise StorageError('database_internal', 'Model context Turn disappeared')
        turn = _turn_public(session, row)
        size = len(json.dumps(turn, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if turns and page_bytes + size > page_limit:
            break
        page_bytes += size
        turns.append(turn)
    return {
        'supported': True, 'covered': covered, 'turns': turns,
        'nextTurnId': turns[-1]['turnId'] if start + len(turns) < len(selected) else None,
        'continuation': continuation, 'continuationStatus': continuation_status,
        'historyProof': {'version': 2, 'count': len(headers) - 1, 'digest': digest.hexdigest()},
        'proofEligible': all(not (row.get('projection') or {}).get('attachments') for row in turns),
    }
