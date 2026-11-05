"""Owner-scoped model context read with semantic-revision continuations.

Compact authority headers prove the prior main-lane prefix. Display-only
settled enrichment advances ordinary projection CAS without invalidating model
context. Real edits/execution changes, ownership and current-target fences stay
mandatory. This query never substitutes for same-turn Continue recovery.
The proof digest streams over every header in keyset pages: memory holds one
header page plus the response page, and lane length is never a ceiling. The
native authority (packages/tofu-db/src/turn/model_context.rs) has the same
selection semantics.
"""
from __future__ import annotations

import hashlib
import json

from lib.storage.errors import StorageError
from lib.storage.model_continuation import settled_witness
from ._common import _required_text, _integer
from ._turns_core import _turn_public, _optional_row_value
from ._model_continuation import continuation_get

# Rows per keyset statement, mirroring the native engine's compact index page
# (packages/tofu-db/src/turn.rs INDEX_PAGE_ROWS). It bounds one result set,
# never the walk.
_HEADER_PAGE_ROWS = 1000
_HEADER_COLUMNS = ('turn_id,parent_turn_id,status,current_attempt_id,projection_revision,'
                   'model_context_display_offset,ordinal')
_CURSOR_OUTSIDE = 'context page cursor is outside the selected suffix'


def _header_bytes(row):
    revision = int(row['projection_revision']) - int(_optional_row_value(row, 'model_context_display_offset') or 0)
    if revision < 0:
        raise StorageError('database_integrity', 'context display offset exceeds revision')
    return (json.dumps([
        row['turn_id'], row['parent_turn_id'], row['status'],
        row['current_attempt_id'], revision,
    ], ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def _main_lane_headers(session, conv, owner, through_ordinal, *, after_ordinal=None):
    """Main-lane headers in lane order through ``through_ordinal``, one page per statement."""
    while True:
        if after_ordinal is None:
            rows = session.fetch_all(
                f'SELECT {_HEADER_COLUMNS} FROM storage_conversation_turns '
                'WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal<=? '
                'ORDER BY ordinal,turn_id LIMIT ?',
                (conv, owner, 'main', through_ordinal, _HEADER_PAGE_ROWS))
        else:
            rows = session.fetch_all(
                f'SELECT {_HEADER_COLUMNS} FROM storage_conversation_turns '
                'WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal>? '
                'AND ordinal<=? ORDER BY ordinal,turn_id LIMIT ?',
                (conv, owner, 'main', after_ordinal, through_ordinal, _HEADER_PAGE_ROWS))
        yield from rows
        if len(rows) < _HEADER_PAGE_ROWS:
            return
        # (conversation_id, lane_id, ordinal) is unique: the ordinal is the key.
        after_ordinal = rows[-1]['ordinal']


class _Page:
    """One bounded response page of the selected suffix, after the caller's cursor."""

    def __init__(self, session, conv, owner, after_turn_id, continuation):
        self.session, self.conv, self.owner = session, conv, owner
        self.after = after_turn_id
        continuation_bytes = len(json.dumps(continuation, ensure_ascii=False,
                                            separators=(',', ':')).encode('utf-8')) if continuation else 0
        self.limit = min(67_104_768 - 8 * 1024 * 1024 - 128 * 1024,
                         67_104_768 - continuation_bytes - 128 * 1024)
        self.turns = []
        self.bytes = 0
        self.full = False

    def offer(self, header, *, target):
        if self.after is not None:
            if header['turn_id'] == self.after:
                if target:
                    raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
                self.after = None
            return
        if self.full:
            return
        row = self.session.fetch_one(
            'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
            'AND user_id=? AND turn_id=?', (self.conv, self.owner, header['turn_id']))
        if row is None:
            raise StorageError('database_internal', 'Model context Turn disappeared')
        turn = _turn_public(self.session, row)
        size = len(json.dumps(turn, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if self.turns and self.bytes + size > self.limit:
            self.full = True
            return
        self.bytes += size
        self.turns.append(turn)


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
    target_ordinal = int(target['ordinal'])
    cached = continuation_get(session, {
        'conversation_id': conv, 'user_id': owner, 'lane_id': 'main'}) if replay else None
    proof = (cached or {}).get('historyProof')
    proof = proof if isinstance(proof, dict) else {}
    count = proof.get('count')
    version2 = type(proof.get('version')) is int and proof.get('version') == 2
    terminal = (cached or {}).get('turns') or []
    # Lane index of the checkpoint terminal; it can cover a prefix only when
    # the walk reaches it before the target.
    anchor = (count if version2 and type(count) is int and count >= 0 and len(terminal) == 1
              else None)
    if not replay:
        continuation_status = 'replay_disabled'
    elif not cached:
        continuation_status = 'checkpoint_missing'
    elif 'historyProof' not in cached:
        continuation_status = 'legacy_checkpoint'
    elif proof.get('version') != 2:
        continuation_status = 'legacy_proof'
    else:
        continuation_status = 'checkpoint_not_before_target'
    legacy = bool(cached) and 'historyProof' not in cached
    # Uncovered history is paged as it streams by. A possible anchor defers
    # the page until the walk proves or refutes it.
    page = (_Page(session, conv, owner, after_turn_id, cached if legacy else None)
            if anchor is None else None)
    digest = hashlib.sha256()
    index = 0
    covered = 0
    cursor_ordinal = None
    reached_target = False
    for header in _main_lane_headers(session, conv, owner, target_ordinal):
        is_target = header['turn_id'] == target_id
        if is_target and int(header['ordinal']) != target_ordinal:
            raise StorageError('database_internal', 'Model context target order changed')
        if page is not None:
            page.offer(header, target=is_target)
        elif after_turn_id is not None and header['turn_id'] == after_turn_id:
            if is_target:
                raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
            cursor_ordinal = int(header['ordinal'])
        if is_target:
            reached_target = True
            break
        if index == anchor:
            continuation_status = 'history_changed'
            if digest.hexdigest() == proof.get('digest'):
                continuation_status = 'terminal_changed'
                final_id = terminal[0][0]
                if header['turn_id'] == final_id:
                    row = session.fetch_one(
                        'SELECT * FROM storage_conversation_turns WHERE conversation_id=? '
                        'AND user_id=? AND turn_id=?', (conv, owner, final_id))
                    if row is not None and settled_witness(_turn_public(session, row)) == terminal[0]:
                        covered = index + 1
                        continuation_status = 'available'
            if covered:
                if cursor_ordinal is not None:
                    raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
                page = _Page(session, conv, owner, after_turn_id, cached)
        digest.update(_header_bytes(header))
        index += 1
    if not reached_target:
        raise StorageError('database_internal', 'Model context target order changed')
    if page is None:
        # The anchor covered nothing: walk only the page again, from the lane
        # head or after the cursor. A proof-bearing checkpoint is never legacy.
        if after_turn_id is not None and cursor_ordinal is None:
            raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
        page = _Page(session, conv, owner, None, None)
        for header in _main_lane_headers(session, conv, owner, target_ordinal,
                                         after_ordinal=cursor_ordinal):
            page.offer(header, target=header['turn_id'] == target_id)
            if page.full:
                break
    if page.after is not None:
        raise StorageError('database_protocol_error', _CURSOR_OUTSIDE)
    continuation = cached if covered or legacy else None
    turns = page.turns
    return {
        'supported': True, 'covered': covered, 'turns': turns,
        'nextTurnId': turns[-1]['turnId'] if page.full else None,
        'continuation': continuation, 'continuationStatus': continuation_status,
        'historyProof': {'version': 2, 'count': index, 'digest': digest.hexdigest()},
        'proofEligible': all(not (row.get('projection') or {}).get('attachments') for row in turns),
    }
