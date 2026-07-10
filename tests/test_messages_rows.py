#!/usr/bin/env python3
"""Phase 5 "messages-as-rows" migrator tests (lib/database/messages_rows.py).

The whole point of the migrator-first approach: PROVE the row representation
reconstructs ``build_search_text`` byte-for-byte BEFORE any read cutover. These
tests are the gate.

  1. message_to_row → row_to_message is lossless (field-for-field).
  2. build_search_text(reconstructed) == build_search_text(original) on the
     tricky shapes: plain str content, multipart list content, thinking,
     translatedContent, system/tool roles (skipped by search), junk entries.
  3. Flags default OFF and are decoupled (read requires write).
  4. End-to-end: backfill a real SQLite conversation into rows, then
     verify_conv_parity reports ok with matching search blobs.
"""

import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite's import expectations).
import quart as _quart
sys.modules.setdefault('flask', _quart)

# DATA-LOSS GUARD: this module imports the DB layer AT MODULE TOP (below), which
# freezes _core._BACKEND. A bare `python tests/test_messages_rows.py` skips
# conftest, so force sqlite + assert the DB is a test DB BEFORE that import.
# (Only fires under __main__; pytest sets TOFU_DB_PATH so this is a no-op there.)
if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_messages_rows.__main__')

from lib.conversations.search_index import build_search_text
from lib.database import messages_rows as mr


def _ensure_table():
    """Idempotently create conversation_messages on the ACTIVE backend.

    The schema-version cache can short-circuit init_db so a long-lived test DB
    (PG or SQLite) may not yet carry a freshly-added table. This mirrors the
    bootstrap's own create_if_absent call, so DB-backed tests are hermetic
    regardless of the ambient DB's recorded schema version.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database import _core
    from lib.database._core_schema import CONVERSATION_MESSAGES, create_if_absent
    backend = getattr(_core, '_BACKEND', 'sqlite')
    if backend == 'pg':
        from lib.database._schema_pg import _table_exists
    else:
        from lib.database._schema_sqlite import _table_exists
    db = get_thread_db(DOMAIN_CHAT)
    create_if_absent(db, CONVERSATION_MESSAGES, table_exists=_table_exists)
    db.execute('CREATE INDEX IF NOT EXISTS idx_conv_msgs_conv ON conversation_messages(conv_id, seq)')
    db.execute('DROP INDEX IF EXISTS idx_conv_msgs_msgid')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_msgs_msgid ON conversation_messages(conv_id, msg_id) WHERE msg_id <> ''")
    db.commit()


# A deliberately gnarly conversation exercising every build_search_text branch.
SAMPLE = [
    {'role': 'user', 'content': 'hello world', '_msgId': 'm0', 'timestamp': 1},
    {'role': 'assistant', 'content': 'hi there', 'thinking': 'let me think',
     'finishReason': 'stop', 'usage': {'in': 10, 'out': 5},
     'toolRounds': [{'toolName': 'grep', 'toolContent': 'x'}], '_msgId': 'm1'},
    {'role': 'user', 'content': [
        {'type': 'text', 'text': 'look at this'},
        {'type': 'image_url', 'image_url': 'data:image/png;base64,zzz'},
        'a bare string part',
    ], '_msgId': 'm2'},
    {'role': 'assistant', 'content': 'translated reply',
     'translatedContent': '翻译后的回复', '_msgId': 'm3'},
    # roles that build_search_text skips entirely:
    {'role': 'system', 'content': 'you are a bot'},
    {'role': 'tool', 'content': 'tool output'},
    # junk the flattener must tolerate:
    'not a dict',
    {'role': 'assistant'},  # no content/thinking
]


def test_flags_default_off_and_decoupled():
    # Ensure a clean env for this assertion.
    for k in ('TOFU_MESSAGES_ROWS', 'TOFU_MESSAGES_ROWS_READ'):
        os.environ.pop(k, None)
    assert mr.rows_write_enabled() is False
    assert mr.rows_read_enabled() is False
    # Read requires write even when read flag is set.
    os.environ['TOFU_MESSAGES_ROWS_READ'] = '1'
    assert mr.rows_read_enabled() is False, 'read must require write flag too'
    os.environ['TOFU_MESSAGES_ROWS'] = '1'
    assert mr.rows_write_enabled() is True
    assert mr.rows_read_enabled() is True
    for k in ('TOFU_MESSAGES_ROWS', 'TOFU_MESSAGES_ROWS_READ'):
        os.environ.pop(k, None)


def test_row_roundtrip_is_lossless():
    for i, msg in enumerate(SAMPLE):
        row = mr.message_to_row('cv', i, msg)
        back = mr.row_to_message(row)
        # meta is the authoritative copy → exact reconstruction of the dict form.
        expected = msg if isinstance(msg, dict) else {}
        assert back == expected, f'idx {i}: {back!r} != {expected!r}'


def test_hoisted_columns_match_search_fields():
    # content (str) hoisted to `content`; list hoisted to `content_json`.
    r0 = mr.message_to_row('cv', 0, SAMPLE[0])
    assert r0['content'] == 'hello world'
    assert r0['content_json'] == '[]'
    assert r0['msg_id'] == 'm0'
    r2 = mr.message_to_row('cv', 2, SAMPLE[2])
    assert r2['content'] == ''
    assert json.loads(r2['content_json'])[0]['text'] == 'look at this'
    r1 = mr.message_to_row('cv', 1, SAMPLE[1])
    assert r1['thinking'] == 'let me think'
    r3 = mr.message_to_row('cv', 3, SAMPLE[3])
    assert r3['translated_content'] == '翻译后的回复'


def test_search_text_byte_identical_after_roundtrip():
    assert mr.verify_search_text_parity(SAMPLE) is True
    # And explicitly, the blobs are equal:
    expected = build_search_text(SAMPLE)
    rows = [mr.message_to_row('cv', i, m) for i, m in enumerate(SAMPLE)]
    got = build_search_text(mr.rows_to_messages(rows))
    assert got == expected
    # The translated text + multipart text + thinking are all present.
    assert '翻译后的回复' in got
    assert 'look at this' in got
    assert 'a bare string part' in got
    assert 'let me think' in got
    # System/tool content must NOT leak in (search skips those roles).
    assert 'you are a bot' not in got
    assert 'tool output' not in got


def test_search_text_parity_on_string_input():
    # build_search_text accepts a JSON string; verify the gate does too.
    assert mr.verify_search_text_parity(json.dumps(SAMPLE, ensure_ascii=False)) is True


def test_end_to_end_backfill_and_verify_sqlite():
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert

    conv_id = 'cv-rows-e2e'
    _ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'rows-e2e',
        'messages': json_dumps_pg(SAMPLE), 'msg_count': len(SAMPLE),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()
    try:
        n = mr.backfill_conv(db, conv_id, SAMPLE, now_ms=now_ms)
        assert n == len(SAMPLE)
        # Rows landed in order.
        rows = db.execute(
            'SELECT seq, msg_id FROM conversation_messages WHERE conv_id=? ORDER BY seq',
            (conv_id,)
        ).fetchall()
        assert [r['seq'] for r in rows] == list(range(len(SAMPLE)))
        # The gate: search blobs byte-identical from JSONB vs rows.
        verdict = mr.verify_conv_parity(db, conv_id)
        assert verdict['ok'] is True, f'parity mismatch: {verdict}'
        assert verdict['jsonb_len'] == verdict['rows_len']

        # Idempotent: re-running backfill converges (no duplicate rows).
        n2 = mr.backfill_conv(db, conv_id, SAMPLE, now_ms=now_ms)
        assert n2 == len(SAMPLE)
        cnt = db.execute(
            'SELECT COUNT(*) AS c FROM conversation_messages WHERE conv_id=?',
            (conv_id,)
        ).fetchone()['c']
        assert cnt == len(SAMPLE), f'backfill not idempotent: {cnt} rows'
    finally:
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


def test_dual_write_noop_when_flag_off():
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    conv_id = 'cv-rows-noop'
    _ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    os.environ.pop('TOFU_MESSAGES_ROWS', None)
    try:
        mr.dual_write_conv(db, conv_id, SAMPLE)  # flag off → no-op
        cnt = db.execute(
            'SELECT COUNT(*) AS c FROM conversation_messages WHERE conv_id=?',
            (conv_id,)
        ).fetchone()['c']
        assert cnt == 0, 'dual_write must be a no-op when flag is off'
    finally:
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db.commit()


def test_dual_write_through_persist_conv_messages_when_on():
    """Flag ON: persist_conv_messages must mirror into rows AND the row
    reconstruction must reproduce build_search_text byte-for-byte."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.chat.persistence import persist_conv_messages

    conv_id = 'cv-rows-dualwrite'
    _ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    os.environ['TOFU_MESSAGES_ROWS'] = '1'
    try:
        # persist_conv_messages assigns _msgId in place; pass a fresh copy.
        msgs = [dict(m) if isinstance(m, dict) else m for m in SAMPLE]
        persist_conv_messages(db, conv_id, msgs, 'dualwrite')
        db.commit()
        cnt = db.execute(
            'SELECT COUNT(*) AS c FROM conversation_messages WHERE conv_id=?',
            (conv_id,)
        ).fetchone()['c']
        assert cnt == len(SAMPLE), f'expected {len(SAMPLE)} mirrored rows, got {cnt}'
        verdict = mr.verify_conv_parity(db, conv_id)
        assert verdict['ok'] is True, f'parity mismatch after dual-write: {verdict}'
    finally:
        os.environ.pop('TOFU_MESSAGES_ROWS', None)
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print('ok', name)
    print('ALL PASSED')
