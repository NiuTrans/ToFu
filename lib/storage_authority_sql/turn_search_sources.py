"""Bounded owner-scoped search readers; SQL selects messages, never transcripts."""
from collections import Counter
from collections.abc import Mapping
import sqlite3

import orjson

from lib.storage_authority_sql.archived_message_codec import (
    ARCHIVED_MESSAGE_CODEC_KEY, decode_archived_message_sequence_from_storage,
)
from lib.storage_authority_sql.projection_codec import STORAGE_PROJECTION_CODEC_KEY
from lib.storage_authority_sql.turn_search_chunks import FIELDS, field_text
from lib.storage_authority_sql.turn_projection_head import projection_from_turn_row

MAX_BYTES = 2_000_000
MAX_MESSAGE_BYTES = 250_000


def _one(session, sql, params):
    try:
        return session.fetch_one(sql, params)
    except Exception as exc:
        # JSON syntax errors only: locks, cancellation and connection failures
        # retain their adapter/retry semantics.
        if (isinstance(exc, sqlite3.OperationalError) and 'malformed JSON' in str(exc)) or getattr(exc, 'sqlstate', None) == '22P02':
            raise ValueError('malformed_source') from exc
        raise


def _length(session, expression):
    return (f'octet_length(({expression})::text)' if session.backend == 'postgres'
            else f'length(CAST(({expression}) AS BLOB))')


def _json(session, expression, key):
    return (f"({expression})::jsonb->'{key}'" if session.backend == 'postgres'
            else f"json_extract({expression},'$.{key}')")


def _public_fields(projection):
    if (not isinstance(projection, Mapping) or STORAGE_PROJECTION_CODEC_KEY in projection
            or ARCHIVED_MESSAGE_CODEC_KEY in projection):
        raise ValueError('malformed_source')
    return {key: field_text(projection.get(key)) for key in FIELDS}


def _selected(session, expression):
    # JSON construction preserves array/string types on both backends.
    args = ','.join(f"'{key}',{_json(session, expression, key)}" for key in (*FIELDS, 'role'))
    fn = 'jsonb_build_object' if session.backend == 'postgres' else 'json_object'
    return f'{fn}({args})'


def turn_snapshot(session, user_id, turn_id):
    row = _one(session,
        'SELECT t.conversation_id,t.user_id,t.turn_id,t.lane_id,t.ordinal,t.actor,t.status,'
        't.projection_revision,t.updated_at,t.current_attempt_id,t.projection_checkpoint_revision,'
        't.projection_materialized_revision,t.projection_patch_count,t.projection_patch_bytes,'
        'c.title AS conversation_title,c.updated_at_ms AS conversation_updated_at_ms '
        'FROM storage_conversation_turns t JOIN storage_conversations c '
        'ON c.id=t.conversation_id AND c.user_id=t.user_id WHERE t.user_id=? AND t.turn_id=?',
        (user_id, turn_id))
    if row is None:
        return None
    row = dict(row)
    where = ' FROM storage_conversation_turns WHERE user_id=? AND turn_id=? AND projection_revision=?'
    params = (user_id, turn_id, row['projection_revision'])
    codec = _json(session, 'projection_json', STORAGE_PROJECTION_CODEC_KEY)
    if session.backend == 'sqlite':
        codec = f"json_type(projection_json,'$.{STORAGE_PROJECTION_CODEC_KEY}')"
    shape = ('jsonb_typeof(projection_json::jsonb)' if session.backend == 'postgres'
             else 'json_type(projection_json)')
    marker = _one(session, f'SELECT {codec} IS NOT NULL AS codec,{shape} AS shape' + where, params)
    if marker is None:
        raise ValueError('source_changed')
    if marker['shape'] != 'object':
        raise ValueError('malformed_source')
    full = (row['projection_checkpoint_revision'] is not None or
            row['projection_materialized_revision'] is not None or bool(marker['codec']))
    if full:
        base_where, base_params = where, params
        if row['projection_checkpoint_revision'] is not None:
            base_where = (' FROM storage_turn_projection_checkpoints WHERE user_id=? AND turn_id=? '
                          'AND conversation_id=? AND attempt_id=? AND projection_revision=?')
            base_params = (user_id, turn_id, row['conversation_id'], row['current_attempt_id'] or '',
                           row['projection_checkpoint_revision'])
        size = _one(session, 'SELECT ' + _length(session, 'projection_json') + ' AS n' + base_where, base_params)
        if size is None:
            raise ValueError('malformed_source')
        if (int(size['n'] or 0) + int(row['projection_patch_bytes'] or 0)) * 8 > MAX_BYTES:
            raise ValueError('source_budget')
        # Supply the admitted inline base explicitly. Checkpoints are loaded by
        # the public helper with owner/attempt/revision and byte-evidence fences.
        if row['projection_checkpoint_revision'] is None:
            base = _one(session, 'SELECT projection_json' + where, params)
            if base is None:
                raise ValueError('source_changed')
            row['projection_json'] = base['projection_json']
        projection = projection_from_turn_row(session, row)
    else:
        selected = _selected(session, 'projection_json')
        size = _one(session, 'SELECT ' + _length(session, selected) + ' AS n' + where, params)
        if size is None:
            raise ValueError('source_changed')
        if int(size['n'] or 0) * 8 > MAX_BYTES:
            raise ValueError('source_budget')
        selected_row = _one(session, f'SELECT {selected} AS value' + where, params)
        if selected_row is None:
            raise ValueError('source_changed')
        value = selected_row['value']
        projection = orjson.loads(value) if isinstance(value, (str, bytes)) else value
    row['projection_json'] = orjson.dumps(_public_fields(projection))
    return row


def history_page(session, identity, cursor):
    """Visit at most eight message positions under a shared 2 MB decode charge.

    Cursor encodes the owner, source kind/id and next message index. Large plain
    arrays stay in SQL; oversized individual messages/envelopes remain partial.
    """
    owner, conv = int(identity['user_id']), str(identity['entity_key'])
    kind, source, index = 0, '', 0
    if cursor:
        try:
            saved_owner, saved_conv, kind, source, index = orjson.loads(cursor)
            if (saved_owner != owner or saved_conv != conv or type(kind) is not int
                    or kind not in (0, 1) or not isinstance(source, str)
                    or type(index) is not int or index < 0):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('invalid_history_cursor') from None
    rows, reasons, used = [], Counter(), 0
    remaining = True
    for _ in range(8):
        if kind == 0:
            where = (' FROM storage_conversations c WHERE user_id=? AND id=? AND NOT EXISTS '
                     '(SELECT 1 FROM storage_conversation_turns t WHERE t.user_id=c.user_id AND t.conversation_id=c.id)')
            params = (owner, conv)
        else:
            where = ' FROM storage_compaction_archives WHERE user_id=? AND conversation_id=? AND archive_id=?'
            params = (owner, conv, source)
        count_expr = ("CASE WHEN jsonb_typeof(messages_json::jsonb)='array' THEN jsonb_array_length(messages_json::jsonb) ELSE -1 END" if session.backend == 'postgres'
                      else "CASE WHEN json_type(messages_json)='array' THEN json_array_length(messages_json) ELSE -1 END")
        try:
            count = _one(session, f'SELECT {count_expr} AS n' + where, params)
            n = int(count['n']) if count else 0
            if n < 0:
                raise ValueError('malformed_source')
        except ValueError:
            reasons['malformed_source'] += 1
            n = 0
        if index >= n:
            next_source = _one(session, 'SELECT archive_id FROM storage_compaction_archives '
                'WHERE user_id=? AND conversation_id=? AND archive_id>? ORDER BY archive_id LIMIT 1',
                (owner, conv, source))
            if next_source is None:
                remaining = False
                break
            kind, source, index = 1, str(next_source['archive_id']), 0
            continue
        message = (f'messages_json::jsonb->{index}' if session.backend == 'postgres'
                   else f"json_extract(messages_json,'$[{index}]')")
        try:
            size = _one(session, 'SELECT ' + _length(session, message) + ' AS n' + where, params)
            charge = int(size['n'] or 0) * 8
            if charge > MAX_BYTES:
                raise ValueError('source_budget')
            if used + charge > MAX_BYTES:
                break
            raw = _one(session, f'SELECT {message} AS value' + where, params)['value']
            value = orjson.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(value, dict):
                raise ValueError('malformed_source')
            if ARCHIVED_MESSAGE_CODEC_KEY in value:
                envelope = value[ARCHIVED_MESSAGE_CODEC_KEY]
                declared = envelope.get('decodedBytes') if isinstance(envelope, dict) else None
                if type(declared) is not int or declared < 1:
                    raise ValueError('malformed_source')
                if declared > MAX_MESSAGE_BYTES:
                    raise ValueError('source_budget')
                charge = max(charge, declared * 8)
                if used + charge > MAX_BYTES:
                    break
            used += charge
            try:
                value = decode_archived_message_sequence_from_storage([value])[0]
            except ValueError as exc:
                raise ValueError('malformed_source') from exc
            if value.get('role') in ('user', 'assistant'):
                rows.append({'user_id': owner, 'conversation_id': conv,
                    'turn_id': 'history:' + orjson.dumps([source, index]).decode(), 'ordinal': index,
                    'actor': value['role'], 'lane_id': 'main', 'status': 'settled',
                    'projection_json': orjson.dumps(_public_fields(value))})
        except ValueError as exc:
            reason = str(exc) if str(exc) == 'source_budget' else 'malformed_source'
            reasons[reason] += 1
        index += 1
    next_cursor = orjson.dumps([owner, conv, kind, source, index]).decode()
    return {'rows': rows, 'cursor': next_cursor, 'remaining': remaining,
            'skipped': sum(reasons.values()), 'skipped_reasons': dict(reasons)}
