"""Durable derived model context, never transcript authority.

ModelContinuationStore.get/put mirror ModelReplayCache's keyword API. A single
latest checkpoint per owner/conversation/lane survives process and cache loss.
Turn settlement carries prepared candidates atomically. Standalone put remains
for explicit settled imports; restore validates the full prefix and policy.
"""
from __future__ import annotations


import hashlib
import json
import uuid


from lib.storage.errors import StorageError
from lib.agent_core.model_context_limits import (
    MAX_ENTRY_BYTES, MAX_MESSAGES, MAX_TURN_WITNESSES,
)
from lib.storage.generated_model_context_projection import (
    DISPLAY_ONLY_FIELDS as _DISPLAY_ONLY_FIELDS,
    ROUND_DISPLAY_ONLY_FIELDS as _ROUND_DISPLAY_ONLY_FIELDS,
    SEGMENT_DISPLAY_ONLY_FIELDS, TRANSLATED_SEGMENT_TYPES,
)


def _model_round(round_entry):
    if not isinstance(round_entry, dict):
        return round_entry
    return {key: value for key, value in round_entry.items()
            if key not in _ROUND_DISPLAY_ONLY_FIELDS}


def model_projection_for_context(projection):
    """Normalize only producer-owned display locations, never arbitrary JSON.

    Containers are detached only where needed; potentially large evidence
    strings are shared read-only. Unknown fields and all real tool evidence
    remain in the witness. The source projection is never mutated.
    """
    result = {key: value for key, value in projection.items()
              if key not in _DISPLAY_ONLY_FIELDS}
    if isinstance(result.get('toolRounds'), list):
        result['toolRounds'] = [_model_round(row) for row in result['toolRounds']]
    if isinstance(result.get('segments'), list):
        segments = []
        for segment in result['segments']:
            if not isinstance(segment, dict):
                segments.append(segment)
                continue
            excluded = (SEGMENT_DISPLAY_ONLY_FIELDS
                        if isinstance(segment.get('type'), str)
                        and segment['type'] in TRANSLATED_SEGMENT_TYPES else ())
            projected = {key: value for key, value in segment.items()
                         if key not in excluded}
            if '_round' in projected:
                projected['_round'] = _model_round(projected['_round'])
            segments.append(projected)
        result['segments'] = segments
    return result


def _same_json(left, right):
    """Type-aware JSON equality: true/1 and false/0 are different evidence."""
    if type(left) is not type(right):
        return False
    if left is right:
        return True
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right))
    return left == right


def model_projections_equal(left, right):
    return _same_json(model_projection_for_context(left),
                      model_projection_for_context(right))


def settled_witness(row):
    """Model-visible Turn witness; declared display-only locations are excluded."""
    source = {key: row.get(key) for key in ('actor', 'laneId', 'parentTurnId', 'status', 'currentAttemptId')}

    if 'modelContextRevision' in row:
        source['modelContextRevision'] = row['modelContextRevision']
    elif 'projectionRevision' in row:
        source['modelContextRevision'] = int(row['projectionRevision']) - int(
            row.get('_modelContextDisplayOffset') or 0)
    source['projection'] = model_projection_for_context(row.get('projection') or {})
    digest = hashlib.sha256()
    for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).iterencode(source):
        digest.update(part.encode('utf-8'))
    return [row['turnId'], digest.hexdigest()]


def encode_projection(projection):
    """Return bounded JSON or None; malformed/oversized input never replaces state."""
    if not isinstance(projection, dict):
        return None
    messages, turns = projection.get('messages'), projection.get('turns')
    if (not isinstance(messages, list) or not 0 < len(messages) <= MAX_MESSAGES
            or not all(isinstance(message, dict) for message in messages)
            or messages[-1].get('role') != 'assistant'
            or not isinstance(turns, list) or not 0 < len(turns) <= MAX_TURN_WITNESSES):
        return None
    if 'historyProof' in projection:
        proof = projection['historyProof']
        if (not isinstance(proof, dict) or set(proof) != {'version', 'count', 'digest'}
                or type(proof.get('version')) is not int or proof['version'] not in (1, 2)
                or type(proof.get('count')) is not int or not 0 <= proof['count'] < 10000
                or not isinstance(proof.get('digest'), str) or len(proof['digest']) != 64
                or any(char not in '0123456789abcdef' for char in proof['digest'])
                or len(turns) != 1):
            return None
    for witness in turns:
        if (not isinstance(witness, list) or len(witness) != 2
                or not isinstance(witness[0], str) or not 0 < len(witness[0]) <= 256
                or not isinstance(witness[1], str) or len(witness[1]) != 64
                or any(char not in '0123456789abcdef' for char in witness[1])):
            return None
    encoded = bytearray()
    try:
        for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).iterencode(projection):
            if len(part) + len(encoded) > MAX_ENTRY_BYTES:
                return None
            chunk = part.encode('utf-8')
            if len(chunk) + len(encoded) > MAX_ENTRY_BYTES:
                return None
            encoded.extend(chunk)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None
    return encoded.decode('utf-8')


_MAX_CONTEXT_READ_ATTEMPTS = 3
_CONTEXT_CURSOR_OUTSIDE_SUFFIX = 'context page cursor is outside the selected suffix'


class _ContextPagingChanged(StorageError):
    """Only locally detected snapshot invalidation permits a complete restart."""

    def __init__(self):
        super().__init__('database_conflict', 'model context changed during paging')


class ModelContinuationStore:
    """Owner-scoped semantic storage port; selected-authority errors propagate."""

    def __init__(self, *, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is not None:
            return self._client
        from lib.storage import get_storage_client
        return get_storage_client()

    def read_context(self, *, user_id, conversation_id, turn_id, replay=True):
        """Restart invalidated page assemblies; never mix different history versions."""
        for attempt in range(_MAX_CONTEXT_READ_ATTEMPTS):
            try:
                return self._read_context_once(
                    user_id=user_id, conversation_id=conversation_id,
                    turn_id=turn_id, replay=replay)
            except _ContextPagingChanged:
                if attempt + 1 == _MAX_CONTEXT_READ_ATTEMPTS:
                    raise

    def _read_context_once(self, *, user_id, conversation_id, turn_id, replay):
        """One optimistic assembly, fenced by the authority's prefix and cache proofs."""
        payload = {'user_id': user_id, 'conversation_id': conversation_id,
                   'turn_id': turn_id, 'replay': replay}
        first = self.client.query('turn.context.read', payload, deadline=5.0)
        if not isinstance(first, dict) or not first.get('supported') or not first.get('nextTurnId'):
            return first
        turns = list(first['turns'])
        proof = first['historyProof']
        covered = first['covered']
        cursor = first['nextTurnId']
        seen = set()
        while cursor:
            if cursor in seen or cursor != turns[-1]['turnId'] or len(turns) >= 10000:
                raise StorageError('database_protocol_error', 'model context cursor did not advance')
            seen.add(cursor)
            try:
                page = self.client.query('turn.context.read',
                                         payload | {'after_turn_id': cursor}, deadline=5.0)
            except StorageError as exc:
                # A newly published continuation can move the selected suffix
                # past our previously valid cursor on either storage authority.
                if (exc.code == 'database_protocol_error'
                        and str(exc) == _CONTEXT_CURSOR_OUTSIDE_SUFFIX):
                    raise _ContextPagingChanged() from exc
                raise
            if (not isinstance(page, dict) or not page.get('supported')
                    or page.get('historyProof') != proof or page.get('covered') != covered
                    or page.get('continuation') != first.get('continuation')
                    or not page.get('turns')
                    or any(row['turnId'] in seen for row in page['turns'])):
                raise _ContextPagingChanged()
            turns.extend(page['turns'])
            first['proofEligible'] = first['proofEligible'] and page['proofEligible']
            cursor = page.get('nextTurnId')
        first['turns'] = turns
        first['nextTurnId'] = None
        return first

    def get(self, *, user_id, conversation_id, lane_id):
        return self.client.query('model_continuation.get', {
            'user_id': user_id, 'conversation_id': conversation_id,
            'lane_id': lane_id}, deadline=5.0)

    def put(self, *, user_id, conversation_id, lane_id, projection):
        if encode_projection(projection) is None:
            return False
        return bool(self.client.command('model_continuation.put', {
            'user_id': user_id, 'conversation_id': conversation_id,
            'lane_id': lane_id, 'projection': projection},
            command_id='model-continuation:' + uuid.uuid4().hex, deadline=5.0))

    def delete(self, *, user_id, conversation_id, lane_id):
        return bool(self.client.command('model_continuation.delete', {
            'user_id': user_id, 'conversation_id': conversation_id,
            'lane_id': lane_id}, command_id='model-continuation-delete:' + uuid.uuid4().hex,
            deadline=5.0))
