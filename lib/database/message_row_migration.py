"""Reviewed one-shot migration from transcript archive to normalized rows."""

from __future__ import annotations

import time

from lib.database._access_policy import allow_transcript_archive_access
from lib.database.conversation_repository import conversation_rows_authoritative
from lib.database import messages_rows
from lib.database.sqlite_owner import maintenance_write_authority
from lib.log import get_logger


logger = get_logger(__name__)


def backfill_message_rows(db, *, verify: bool = False, progress=None) -> dict:
    """Populate row transcripts only before the irreversible authority cutover.

    Once rows are authoritative, ``conversations.messages`` is a stale frozen
    archive and replaying it would resurrect lost data.  The migration therefore
    fails closed in that mode instead of offering a tempting repair shortcut.
    """
    if conversation_rows_authoritative():
        raise RuntimeError(
            'message-row backfill is retired after row-authority cutover; '
            'the frozen conversations.messages archive is not recoverable truth')
    with maintenance_write_authority('normalize legacy conversation messages'):
        with allow_transcript_archive_access():
            rows = db.execute(
                'SELECT id, messages FROM conversations ORDER BY id').fetchall()
        started = time.time()
        now_ms = int(started * 1000)
        done = total_messages = failures = parity_failures = 0
        for row in rows:
            conv_id = row['id']
            try:
                count = messages_rows.backfill_conv(
                    db, conv_id, row['messages'], now_ms=now_ms, commit=True)
            except Exception as exc:
                failures += 1
                try:
                    db.rollback()
                except Exception as rollback_exc:
                    logger.warning(
                        '[MessageRows] rollback failed after backfill error '
                        'conv=%s: %s', conv_id, rollback_exc)
                if progress:
                    progress('error', done, len(rows), conv_id, exc)
                continue
            done += 1
            total_messages += count
            if progress and done % 100 == 0:
                progress('progress', done, len(rows), conv_id, None)
        if verify:
            for row in rows:
                result = messages_rows.verify_conv_parity(db, row['id'])
                if not result['ok']:
                    parity_failures += 1
                    if progress:
                        progress(
                            'parity_error', done, len(rows), row['id'], result)
        return {
            'conversations': len(rows), 'done': done,
            'messages': total_messages, 'failures': failures,
            'parity_failures': parity_failures,
            'elapsed_s': time.time() - started,
        }


__all__ = ['backfill_message_rows']
