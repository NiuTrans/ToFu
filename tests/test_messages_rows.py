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

import pytest

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


pytestmark = pytest.mark.unit


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
    # Pytest stays default-off so deployment state and incidental mirror writes
    # cannot leak into isolated fixtures. The personal server default is ON.
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


def test_personal_server_defaults_rows_on_but_keeps_env_kill_switch(monkeypatch):
    monkeypatch.delenv('TOFU_MESSAGES_ROWS', raising=False)
    monkeypatch.delenv('TOFU_MESSAGES_ROWS_READ', raising=False)
    monkeypatch.setattr(mr, '_default_rows_enabled', lambda: True)
    assert mr.rows_write_enabled() is True
    assert mr.rows_read_enabled() is True
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '0')
    assert mr.rows_write_enabled() is True
    assert mr.rows_read_enabled() is False
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '0')
    assert mr.rows_write_enabled() is False
    assert mr.rows_read_enabled() is False


def test_activity_projection_candidates_use_partial_index_and_page_bounds(
        tmp_path):
    from lib.database import _core as core

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'projection-page.db'))
    try:
        core.init_db()
        db = core.get_thread_db(core.DOMAIN_CHAT)
        plan = db.execute(
            'EXPLAIN QUERY PLAN SELECT DISTINCT conv_id '
            'FROM conversation_messages '
            'INDEXED BY idx_conv_msgs_incomplete_projection '
            'WHERE (message_ts IS NULL OR billing_meta IS NULL) '
            'AND conv_id > ? ORDER BY conv_id LIMIT ?', ('', 100)).fetchall()
        assert 'idx_conv_msgs_incomplete_projection' in ' '.join(
            str(row['detail']) for row in plan)

        for conv_id in ('page-a', 'page-b', 'page-c'):
            row = mr.message_to_row(
                conv_id, 0, {'role': 'user', 'content': conv_id})
            row['message_ts'] = None
            row['billing_meta'] = None
            columns = tuple(row)
            db.execute(
                'INSERT INTO conversation_messages (' + ','.join(columns) +
                ') VALUES (' + ','.join(['?'] * len(columns)) + ')',
                tuple(row[column] for column in columns))
        db.commit()

        assert mr._activity_projection_candidates(db, '', 2) == [
            'page-a', 'page-b']
        assert mr._activity_projection_candidates(db, 'page-b', 2) == [
            'page-c']
    finally:
        core.restore_db_state(snapshot)


def test_activity_projection_backfill_starts_once_and_is_kill_switchable(
        monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._alive = False

        def start(self):
            self._alive = True
            started.append(self)

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(mr, '_activity_backfill_thread', None)
    monkeypatch.setattr(mr, 'rows_read_enabled', lambda: True)
    monkeypatch.setattr(mr.threading, 'Thread', _FakeThread)
    monkeypatch.setenv('TOFU_MESSAGES_ACTIVITY_BACKFILL', '1')
    assert mr.start_activity_projection_backfill() is True
    assert mr.start_activity_projection_backfill() is False
    assert len(started) == 1
    assert started[0].kwargs['daemon'] is True
    assert started[0].kwargs['name'] == 'message-activity-backfill'

    monkeypatch.setattr(mr, '_activity_backfill_thread', None)
    monkeypatch.setenv('TOFU_MESSAGES_ACTIVITY_BACKFILL', '0')
    assert mr.start_activity_projection_backfill() is False


def test_row_roundtrip_is_lossless():
    for i, msg in enumerate(SAMPLE):
        row = mr.message_to_row('cv', i, msg)
        back = mr.row_to_message(row)
        # meta is the authoritative copy → exact reconstruction, including a
        # malformed historical scalar entry rather than silently coercing it.
        assert back == msg, f'idx {i}: {back!r} != {msg!r}'


def test_hoisted_columns_match_search_fields():
    # content (str) hoisted to `content`; list hoisted to `content_json`.
    r0 = mr.message_to_row('cv', 0, SAMPLE[0])
    assert r0['content'] == 'hello world'
    assert r0['content_json'] == '[]'
    assert r0['msg_id'] == 'm0'
    assert r0['message_ts'] == 1
    assert json.loads(r0['billing_meta']) == {'timestamp': 1}
    r2 = mr.message_to_row('cv', 2, SAMPLE[2])
    assert r2['content'] == ''
    assert json.loads(r2['content_json'])[0]['text'] == 'look at this'
    r1 = mr.message_to_row('cv', 1, SAMPLE[1])
    assert r1['thinking'] == 'let me think'
    assert r1['message_ts'] == 0
    r3 = mr.message_to_row('cv', 3, SAMPLE[3])
    assert r3['translated_content'] == '翻译后的回复'


def test_billing_projection_canonicalizes_usage_and_drops_wire_payloads():
    row = mr.message_to_row('cv', 0, {
        'role': 'assistant', 'timestamp': 123,
        'usage': {
            'input_tokens': 100,
            'output_tokens': 25,
            'cache_creation_input_tokens': 8,
            'prompt_tokens_details': {'cached_tokens': 7},
            '_wire_fp': 'x' * 100_000,
        },
        'model': 'model-x', 'providerId': 'provider-y',
    })
    billing = json.loads(row['billing_meta'])
    assert billing == {
        'timestamp': 123,
        'model': 'model-x',
        'providerId': 'provider-y',
        'usage': {
            'prompt_tokens': 100,
            'completion_tokens': 25,
            'cache_write_tokens': 8,
            'cache_read_tokens': 7,
            'reasoning_tokens': 0,
        },
    }
    assert '_wire_fp' not in row['billing_meta']
    assert len(row['billing_meta']) < 300


def test_light_projection_drops_wire_payloads_without_losing_cost_or_authority():
    """The row-window projection must not read old transport diagnostics."""
    msg = {
        'role': 'assistant', 'content': 'answer', '_msgId': 'a1',
        'usage': {'completion_tokens': 4, '_wire_markers': ['x'] * 1000},
        'apiRounds': [{
            'round': 1, 'cost': {'costCny': 0.2},
            'usage': {
                'prompt_tokens': 12,
                '_dispatch': {'provider': 'p'},
                '_wire_bytes': list(range(4000)),
                '_wire_field_bytes': {'messages': 'x' * 200_000},
            },
        }],
        '_continueApiRounds': [{
            'round': 2,
            'usage': {'completion_tokens': 3, 'trace_id': 'continue',
                      '_wire_bytes': ['x'] * 1000},
        }],
        '_liveLastRoundUsage': {
            'tokensIn': 12,
            'usage': {'prompt_tokens': 12, '_wire_fp': ['x'] * 1000},
        },
    }
    row = mr.message_to_row('cv', 0, msg)
    authoritative = json.loads(row['meta'])
    projected = json.loads(row['meta_light'])

    assert authoritative == msg
    assert '_wire_bytes' in msg['apiRounds'][0]['usage'], 'input was mutated'
    got = projected['apiRounds'][0]
    assert got['cost'] == {'costCny': 0.2}
    assert got['usage']['prompt_tokens'] == 12
    assert got['usage']['_dispatch'] == {'provider': 'p'}
    assert not any(k.startswith('_wire_') for k in got['usage'])
    assert projected['usage'] == {'completion_tokens': 4}
    assert projected['_continueApiRounds'][0]['usage'] == {
        'completion_tokens': 3, 'trace_id': 'continue',
    }
    assert projected['_liveLastRoundUsage'] == {
        'tokensIn': 12, 'usage': {'prompt_tokens': 12},
    }
    assert projected['_trimmed'] is True
    assert len(row['meta_light']) < 1000


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
        assert verdict['content_ok'] is True
        assert verdict['search_text_ok'] is True
        assert verdict['jsonb_len'] == verdict['rows_len']
        assert verdict['light_ready'] is True
        assert verdict['activity_ready'] is True
        assert verdict['billing_ready'] is True

        # Rolling-upgrade safety: an old process writes NULL meta_light. The
        # gate must refuse it, then the online SQL-only backfill converges
        # without touching the lossless meta value.
        original_meta = db.execute(
            'SELECT meta FROM conversation_messages WHERE conv_id=? AND seq=1',
            (conv_id,)).fetchone()['meta']
        db.execute(
            'UPDATE conversation_messages SET meta_light=NULL '
            'WHERE conv_id=? AND seq=1', (conv_id,))
        db.commit()
        assert mr.mirror_is_current(db, conv_id, expected_count=len(SAMPLE)) is False
        assert mr.backfill_light_projection(db, conv_id) == 1
        projected = db.execute(
            'SELECT meta, meta_light FROM conversation_messages '
            'WHERE conv_id=? AND seq=1', (conv_id,)).fetchone()
        assert projected['meta'] == original_meta
        light = json.loads(projected['meta_light']) if isinstance(
            projected['meta_light'], str) else projected['meta_light']
        assert 'toolRounds' not in light
        assert light['_trimmedToolRoundCount'] == 1
        assert mr.mirror_is_current(db, conv_id, expected_count=len(SAMPLE)) is True

        # The fixed-width activity timestamp has an independent rolling
        # backfill. A real missing timestamp materializes as 0; NULL means the
        # projection has not run yet.
        db.execute(
            'UPDATE conversation_messages SET message_ts=NULL, billing_meta=NULL '
            'WHERE conv_id=? AND seq IN (0,1)', (conv_id,))
        db.commit()
        assert mr.backfill_activity_projection(db, conv_id) == 2
        activity = db.execute(
            'SELECT seq, message_ts FROM conversation_messages '
            'WHERE conv_id=? AND seq IN (0,1) ORDER BY seq',
            (conv_id,)).fetchall()
        assert [(r['seq'], r['message_ts']) for r in activity] == [(0, 1), (1, 0)]
        billing = db.execute(
            'SELECT seq, billing_meta FROM conversation_messages '
            'WHERE conv_id=? AND seq IN (0,1) ORDER BY seq',
            (conv_id,)).fetchall()
        assert all(r['billing_meta'] is not None for r in billing)

        # Corrupt a field that build_search_text deliberately ignores. The old
        # search-only gate said OK; the read-cutover gate must fail closed.
        changed = dict(SAMPLE[0], timestamp=999)
        db.execute(
            'UPDATE conversation_messages SET meta=? WHERE conv_id=? AND seq=0',
            (json.dumps(changed, ensure_ascii=False), conv_id))
        hidden_loss = mr.verify_conv_parity(db, conv_id)
        assert hidden_loss['search_text_ok'] is True
        assert hidden_loss['content_ok'] is False
        assert hidden_loss['ok'] is False

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
        marker = db.execute(
            'SELECT rev, messages_rows_rev FROM conversations '
            'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
        assert marker['messages_rows_rev'] == marker['rev'], (
            'successful mirror did not atomically mark the authoritative rev')
    finally:
        os.environ.pop('TOFU_MESSAGES_ROWS', None)
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


def test_persist_rolls_back_blob_when_strong_row_write_fails(monkeypatch):
    """The transitional blob and canonical rows advance in one transaction."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.chat.persistence import persist_conv_messages

    conv_id = 'cv-rows-atomic-failure'
    _ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    os.environ['TOFU_MESSAGES_ROWS'] = '1'
    try:
        original = [
            {'role': 'user', 'content': 'before', '_msgId': 'atomic-m0'},
            {'role': 'assistant', 'content': 'stable', '_msgId': 'atomic-m1'},
        ]
        persist_conv_messages(
            db, conv_id, [dict(m) for m in original], 'atomic-before')

        def _fail_rows(*args, **kwargs):
            raise RuntimeError('injected canonical-row failure')

        monkeypatch.setattr(mr, '_mirror_conv_rows', _fail_rows)
        edited = [dict(m) for m in original]
        edited[1]['content'] = 'must-not-partially-land'
        with pytest.raises(RuntimeError, match='canonical-row failure'):
            persist_conv_messages(db, conv_id, edited, 'atomic-after')

        blob_row = db.execute(
            'SELECT title, messages FROM conversations '
            'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
        assert blob_row['title'] == 'atomic-before'
        assert json.loads(blob_row['messages']) == original
        rows = db.execute(
            'SELECT * FROM conversation_messages WHERE conv_id=? ORDER BY seq',
            (conv_id,)).fetchall()
        assert mr.rows_to_messages(rows) == original
        assert mr.verify_conv_parity(db, conv_id)['ok'] is True
    finally:
        os.environ.pop('TOFU_MESSAGES_ROWS', None)
        db_execute_with_retry(
            db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db_execute_with_retry(
            db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print('ok', name)
    print('ALL PASSED')
