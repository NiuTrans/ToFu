"""Daily-report activity reads use exact row mirrors without blob transfer."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from lib.daily_report import conversations

pytestmark = pytest.mark.unit


def _ms(day: int) -> int:
    return int(dt.datetime(2026, 8, day, 12).timestamp() * 1000)


def _range():
    start = int(dt.datetime(2026, 8, 1).timestamp() * 1000)
    end = int(dt.datetime(2026, 9, 1).timestamp() * 1000)
    return start, end


def _db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE conversations (
            id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            messages TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            settings TEXT NOT NULL DEFAULT '{}',
            rev INTEGER NOT NULL,
            messages_rows_rev INTEGER NOT NULL,
            msg_count INTEGER NOT NULL,
            PRIMARY KEY (id, user_id)
        );
        CREATE TABLE conversation_messages (
            conv_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL,
            meta_light TEXT,
            message_ts INTEGER,
            billing_meta TEXT,
            PRIMARY KEY (conv_id, seq)
        );
    ''')
    return db


def _wire(monkeypatch, db, *, row_reads=True):
    import lib.database as database
    import lib.database.messages_rows as messages_rows

    monkeypatch.setattr(database, 'get_thread_db', lambda _domain: db)
    monkeypatch.setattr(database, '_BACKEND', 'sqlite')
    monkeypatch.setattr(messages_rows, 'rows_read_enabled',
                        lambda: row_reads)


def _insert_conv(db, cid, messages, *, rev=1, mirror_rev=1,
                 created=None, updated=None):
    created = _ms(1) if created is None else created
    updated = _ms(3) if updated is None else updated
    db.execute(
        'INSERT INTO conversations '
        '(id,user_id,title,messages,created_at,updated_at,rev,'
        'messages_rows_rev,msg_count) VALUES (?,?,?,?,?,?,?,?,?)',
        (cid, 1, 'Title ' + cid, json.dumps(messages), created, updated,
         rev, mirror_rev, len(messages)))


def _insert_rows(db, cid, messages):
    for seq, msg in enumerate(messages):
        raw = json.dumps(msg)
        billing_keys = ('usage', 'timestamp', 'model', 'preset', 'effort',
                        'provider_id', 'providerId')
        billing = json.dumps({k: msg[k] for k in billing_keys if k in msg})
        content = msg.get('content', '')
        content = content if isinstance(content, str) else ''
        db.execute('INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)',
                   (cid, seq, content, raw, raw, msg.get('timestamp', 0), billing))
    db.commit()


def test_verified_row_path_transfers_only_timestamps_and_counts_every_day(
        monkeypatch):
    db = _db()
    # The authority is deliberately not valid JSON. A blob read would produce
    # no activity; the exact current mirror is therefore the only path that can
    # make this assertion pass.
    rows = [
        {'role': 'user', 'timestamp': _ms(1), 'content': 'a'},
        {'role': 'assistant', 'timestamp': _ms(2), 'content': 'b'},
        {'role': 'user', 'timestamp': _ms(2), 'content': 'c'},
    ]
    _insert_conv(db, 'current', [], rev=7, mirror_rev=7)
    db.execute("UPDATE conversations SET messages='not-json', msg_count=3 "
               "WHERE id='current'")
    _insert_rows(db, 'current', rows)
    _wire(monkeypatch, db)

    statements = []
    db.set_trace_callback(statements.append)
    start, end = _range()
    got = conversations._activity_counts_for_range(start, end)

    assert got == {1: 1, 2: 1}, (
        'one conversation active on two days must count once on EACH day')
    sql = '\n'.join(statements).lower()
    assert 'cast(message_ts as text)' in sql
    assert 'select id, messages, created_at' not in sql


def test_stale_mirror_falls_back_to_authoritative_blob(monkeypatch):
    db = _db()
    authority = [{'role': 'user', 'timestamp': _ms(3), 'content': 'truth'}]
    stale = [{'role': 'user', 'timestamp': _ms(1), 'content': 'stale'}]
    _insert_conv(db, 'stale', authority, rev=9, mirror_rev=8)
    _insert_rows(db, 'stale', stale)
    _wire(monkeypatch, db)

    statements = []
    db.set_trace_callback(statements.append)
    start, end = _range()
    got = conversations._activity_counts_for_range(start, end)

    assert got == {3: 1}
    assert 'as _legacy_messages' in '\n'.join(statements).lower()


def test_missing_message_timestamp_uses_conversation_fallback(monkeypatch):
    db = _db()
    rows = [{'role': 'user', 'content': 'legacy'}]
    _insert_conv(db, 'legacy', rows, created=_ms(1), updated=_ms(4))
    _insert_rows(db, 'legacy', rows)
    _wire(monkeypatch, db)

    start, end = _range()
    assert conversations._activity_counts_for_range(start, end) == {4: 1}


def test_upgraded_null_scalar_falls_back_to_light_projection(monkeypatch):
    db = _db()
    rows = [{'role': 'user', 'timestamp': _ms(6), 'content': 'upgrade'}]
    _insert_conv(db, 'upgrade', rows)
    _insert_rows(db, 'upgrade', rows)
    db.execute("UPDATE conversation_messages SET message_ts=NULL "
               "WHERE conv_id='upgrade'")
    db.commit()
    _wire(monkeypatch, db)

    start, end = _range()
    assert conversations._activity_counts_for_range(start, end) == {6: 1}


def test_row_read_kill_switch_preserves_legacy_blob_behavior(monkeypatch):
    db = _db()
    authority = [{'role': 'user', 'timestamp': _ms(5), 'content': 'legacy'}]
    _insert_conv(db, 'disabled', authority)
    _insert_rows(db, 'disabled', authority)
    _wire(monkeypatch, db, row_reads=False)

    statements = []
    db.set_trace_callback(statements.append)
    start, end = _range()
    assert conversations._activity_counts_for_range(start, end) == {5: 1}
    sql = '\n'.join(statements).lower()
    assert 'select messages,' in sql and 'from conversations' in sql


def test_report_extraction_fetches_only_active_row_meta(monkeypatch):
    db = _db()
    rows = [
        {'role': 'user', 'timestamp': _ms(1), 'content': 'old'},
        {'role': 'user', 'timestamp': _ms(2), 'content': 'today question'},
        {'role': 'assistant', 'timestamp': _ms(2), 'content': 'today answer',
         'toolRounds': [{'calls': [{'name': 'read_file'}]}]},
        {'role': 'assistant', 'timestamp': _ms(3), 'content': 'future'},
    ]
    _insert_conv(db, 'report', [])
    db.execute("UPDATE conversations SET messages='not-json', msg_count=4 "
               "WHERE id='report'")
    _insert_rows(db, 'report', rows)
    _wire(monkeypatch, db)

    statements = []
    db.set_trace_callback(statements.append)
    got = conversations._extract_convs_for_date('2026-08-02')

    assert len(got) == 1
    assert got[0]['id'] == 'report'
    assert got[0]['rounds'] == 1
    assert got[0]['toolsUsed'] == ['read_file']
    assert 'today question' in got[0]['transcript']
    assert 'today answer' in got[0]['transcript']
    assert 'old' not in got[0]['transcript']
    assert 'future' not in got[0]['transcript']
    sql = '\n'.join(statements).lower()
    assert 'select id, title, messages' not in sql
    assert 'select conv_id, seq, meta from conversation_messages' in sql


def test_cost_scan_uses_verified_light_rows_without_authority_blob(
        monkeypatch):
    from lib.daily_report import cost

    db = _db()
    rows = [
        {'role': 'user', 'timestamp': _ms(1), 'content': 'question'},
        {'role': 'assistant', 'timestamp': _ms(2), 'content': 'answer',
         'usage': {'prompt_tokens': 100, 'completion_tokens': 25},
         'model': 'model-x', 'providerId': 'provider-y'},
    ]
    _insert_conv(db, 'cost-current', [], rev=4, mirror_rev=4)
    db.execute("UPDATE conversations SET messages='not-json', msg_count=2, "
               "settings=? WHERE id='cost-current'",
               (json.dumps({'model': 'fallback-model'}),))
    _insert_rows(db, 'cost-current', rows)
    _wire(monkeypatch, db)
    monkeypatch.setattr(cost, '_calc_msg_cost_cny', lambda *a, **k: 1.25)

    statements = []
    db.set_trace_callback(statements.append)
    start, end = _range()
    got = cost._scan_costs_in_range(start, end, 2026, 8)

    assert got[2]['cost'] == 1.25
    assert got[2]['conversations']['cost-current'] == {
        'name': 'Title cost-current', 'cost': 1.25, 'tokens': 125,
    }
    sql = '\n'.join(statements).lower()
    assert "json_extract(billing_meta,'$.usage')" in sql
    assert 'json_each(case when json_valid(c.messages)' not in sql


def test_cost_scan_stale_mirror_falls_back_per_conversation(monkeypatch):
    from lib.daily_report import cost

    db = _db()
    authority = [{
        'role': 'assistant', 'timestamp': _ms(3), 'content': 'truth',
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    }]
    stale = [{
        'role': 'assistant', 'timestamp': _ms(1), 'content': 'stale',
        'usage': {'prompt_tokens': 999, 'completion_tokens': 999},
    }]
    _insert_conv(db, 'cost-stale', authority, rev=8, mirror_rev=7)
    _insert_rows(db, 'cost-stale', stale)
    _wire(monkeypatch, db)
    monkeypatch.setattr(cost, '_calc_msg_cost_cny', lambda *a, **k: 2.0)

    statements = []
    db.set_trace_callback(statements.append)
    start, end = _range()
    got = cost._scan_costs_in_range(start, end, 2026, 8)

    assert set(got) == {3}
    assert got[3]['cost'] == 2.0
    sql = '\n'.join(statements).lower()
    assert 'json_each(case when json_valid(c.messages)' not in sql
    assert 'as _legacy_messages' in sql
