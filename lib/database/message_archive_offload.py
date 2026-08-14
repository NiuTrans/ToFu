"""Batch-safe physical offload for frozen conversation transcript archives.

After normalized message rows become authoritative, ``conversations.messages``
is forensic history rather than live truth. SQLite nevertheless stores it in
the same record as hot metadata; changing only ``rev`` can therefore rewrite
all of the archive's overflow pages. This reviewed maintenance operation moves
that frozen value to an immutable side table and leaves ``[]`` on the hot row.

Nothing runs automatically at startup. Operators call the bounded function in
small batches, observe latency/WAL, and can stop after any committed row.
"""

from __future__ import annotations

import json
import time

from lib.database import json_dumps_pg, write_transaction
from lib.database._access_policy import allow_transcript_archive_access
from lib.database.conversation_repository import conversation_rows_authoritative
from lib.database.sqlite_owner import maintenance_write_authority


def _json_value(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8', 'replace')
    if isinstance(value, str):
        return json.loads(value)
    return value


def _text_size(value) -> int:
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode('utf-8'))
    return len(json_dumps_pg(value).encode('utf-8'))


def offload_frozen_message_archives(
    db,
    *,
    limit: int = 1,
    progress=None,
) -> dict:
    """Move at most ``limit`` retired parent blobs into the cold side table.

    Each conversation is copied, semantically verified, and cleared in one
    transaction. The canonical child revision/count marker is checked before
    and after the move, and restored after the legacy ``messages`` trigger
    observes the deliberate clear. A failed verification rolls back both the
    archive insert and parent mutation.
    """
    if not conversation_rows_authoritative():
        raise RuntimeError(
            'frozen archive offload requires message-row authority')
    try:
        bounded = max(1, min(int(limit), 100))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('limit must be an integer') from exc

    started = time.monotonic()
    archived = cleared = bytes_released = 0
    with maintenance_write_authority('offload frozen conversation archives'):
        candidates = db.execute(
            'SELECT c.id, c.user_id FROM conversations c '
            'LEFT JOIN conversation_message_archives a '
            'ON a.conv_id=c.id AND a.user_id=c.user_id '
            'WHERE a.conv_id IS NULL ORDER BY c.updated_at, c.id LIMIT ?',
            (bounded,),
        ).fetchall()
        for candidate in candidates:
            conv_id = candidate['id']
            user_id = int(candidate['user_id'])
            with write_transaction(db, label='offload one frozen transcript'):
                with allow_transcript_archive_access():
                    source = db.execute(
                        'SELECT messages, rev, messages_rows_rev, msg_count '
                        'FROM conversations WHERE id=? AND user_id=?',
                        (conv_id, user_id),
                    ).fetchone()
                if source is None:
                    continue
                rev = int(source['rev'] or 0)
                marker = source['messages_rows_rev']
                msg_count = int(source['msg_count'] or 0)
                if marker is None or int(marker) != rev:
                    raise RuntimeError(
                        f'canonical row marker is stale for {conv_id}')
                raw_messages = source['messages']
                parsed = _json_value(raw_messages)
                if not isinstance(parsed, list):
                    raise RuntimeError(
                        f'frozen transcript is not a list for {conv_id}')
                serialized = (raw_messages if isinstance(raw_messages, str)
                              else json_dumps_pg(parsed))
                db.execute(
                    'INSERT INTO conversation_message_archives '
                    '(conv_id,user_id,messages,source_rev,msg_count,archived_at) '
                    'VALUES (?,?,?,?,?,?)',
                    (conv_id, user_id, serialized, rev, len(parsed),
                     int(time.time() * 1000)),
                )
                stored = db.execute(
                    'SELECT messages FROM conversation_message_archives '
                    'WHERE conv_id=? AND user_id=?',
                    (conv_id, user_id),
                ).fetchone()
                if stored is None or _json_value(stored['messages']) != parsed:
                    raise RuntimeError(
                        f'frozen transcript archive verification failed for {conv_id}')

                released = _text_size(raw_messages) if parsed else 0
                if parsed:
                    # Updating this column fires the legacy rev/msg_count
                    # trigger. Restore the canonical header immediately in the
                    # same transaction; normalized rows did not change.
                    with allow_transcript_archive_access():
                        cursor = db.execute(
                            'UPDATE conversations SET messages=? '
                            'WHERE id=? AND user_id=? AND rev=? '
                            'AND messages_rows_rev=? AND msg_count=?',
                            (json_dumps_pg([]), conv_id, user_id, rev, rev,
                             msg_count),
                        )
                    if getattr(cursor, 'rowcount', None) != 1:
                        raise RuntimeError(
                            f'frozen transcript CAS failed for {conv_id}')
                    db.execute(
                        'UPDATE conversations SET rev=?, msg_count=?, '
                        'messages_rows_rev=? WHERE id=? AND user_id=?',
                        (rev, msg_count, rev, conv_id, user_id),
                    )
                    cleared += 1
                    bytes_released += released

                header = db.execute(
                    'SELECT rev, messages_rows_rev, msg_count '
                    'FROM conversations WHERE id=? AND user_id=?',
                    (conv_id, user_id),
                ).fetchone()
                if (int(header['rev']) != rev
                        or int(header['messages_rows_rev']) != rev
                        or int(header['msg_count']) != msg_count):
                    raise RuntimeError(
                        f'canonical header changed during offload for {conv_id}')
            archived += 1
            if progress:
                progress(archived, len(candidates), conv_id, released)

    return {
        'candidates': len(candidates),
        'archived': archived,
        'cleared': cleared,
        'bytes_released': bytes_released,
        'elapsed_s': time.monotonic() - started,
    }


__all__ = ['offload_frozen_message_archives']
