"""Regression tests for the normalized killed-tail startup scan.

The production failure shape was a boot-time ``LIKE`` across hundreds of MB
of conversation JSON.  These tests use the real SQLite schema and row mirror
to prove that the optimized lane keeps exact tail semantics, falls back to the
authoritative blob for a small stale residue, and abandons the lane while an
upgrade is still broadly incomplete.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.unit


def test_killed_tail_row_scan_is_exact_and_fail_closed(monkeypatch, tmp_path):
    from lib.database import (
        DOMAIN_CHAT,
        get_thread_db,
        json_dumps_pg,
        reset_sqlite_for_tests,
        restore_db_state,
    )
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import messages_rows as mr
    from lib.tasks_pkg import killed_recovery as kr

    snapshot = reset_sqlite_for_tests(str(tmp_path / 'killed-tail.db'))
    try:
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        killed = [
            {'role': 'user', 'content': 'question'},
            {'role': 'assistant', 'content': 'partial',
             'interruptedReason': 'killed'},
        ]
        settled = [
            {'role': 'assistant', 'content': 'old partial',
             'interruptedReason': 'killed'},
            {'role': 'user', 'content': 'new question'},
            {'role': 'assistant', 'content': 'done', 'finishReason': 'stop'},
        ]
        rows = [
            ('tail-killed', killed, now + 4),
            ('mid-killed', settled, now + 3),
            ('stale-killed', killed, now + 2),
            ('empty', [], now + 1),
        ]
        for cid, messages, updated_at in rows:
            upsert(db, CONVERSATIONS, {
                'id': cid, 'user_id': 1, 'title': cid,
                'messages': json_dumps_pg(messages),
                'msg_count': len(messages), 'created_at': now,
                'updated_at': updated_at,
            }, insert_cols=[
                'id', 'user_id', 'title', 'messages', 'msg_count',
                'created_at', 'updated_at',
            ], retry=True)
            db.commit()
            mr.backfill_conv(db, cid, messages, now_ms=updated_at)

        # One rolling-upgrade residue must be checked against the blob, never
        # silently treated as a non-killed tail.
        db.execute(
            'UPDATE conversation_messages SET meta_light=NULL '
            'WHERE conv_id=? AND seq=1', ('stale-killed',))
        db.commit()
        monkeypatch.setattr(mr, 'rows_read_enabled', lambda: True)

        found = kr._list_killed_turn_convs_from_rows(db, now - 1000, 500)
        assert found == ['tail-killed', 'stale-killed']
        assert kr._list_killed_turn_convs_from_rows(db, now - 1000, 1) == [
            'tail-killed']

        # Once more than 25% of candidates are stale, returning None is the
        # fail-closed signal for the caller to use the legacy authority scan.
        db.execute(
            'UPDATE conversation_messages SET meta_light=NULL '
            'WHERE conv_id=? AND seq=2', ('mid-killed',))
        db.commit()
        assert kr._list_killed_turn_convs_from_rows(
            db, now - 1000, 500) is None
    finally:
        restore_db_state(snapshot)
