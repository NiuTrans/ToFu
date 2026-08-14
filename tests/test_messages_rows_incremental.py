#!/usr/bin/env python3
"""tests/test_messages_rows_incremental.py — incremental dual-write mirror.

pt_59140ecd step ①. The old ``dual_write_conv`` delegated to
``backfill_conv``: DELETE-all + per-row re-upsert of the WHOLE history on
every JSONB write — a 1163-message conversation paid 1163 row round-trips per
appended message, strictly worse than the blob write it mirrors (measured in
docs/MESSAGES_ROWS_WRITE_FLIP_EVIDENCE.md §4.2).

The incremental mirror:
  * no hint  → one index-only COUNT, then re-write from the previous TIP
    onward (covers pure appends AND same-count tip mutation — the streaming
    finalize shape) + a truncation DELETE;
  * ``changed_seqs=[...]`` → the caller knows exactly which positions it
    edited in place (translate commit / patch-by-id); only those rows are
    re-mirrored, plus truncation repair.

failing-first: the upsert-cost tests are RED on the old shape (it re-writes
every row on every call). NEUTER: reverting the mirror to full rebuild trips
the cost pins; dropping the tip-refresh trips the same-count edit test.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_messages_rows_incremental.py -v
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_messages_rows_incremental.__main__')

from lib.database import messages_rows as mr

pytestmark = pytest.mark.unit


def _ensure_table():
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
    db.commit()
    return db


def _msgs(n, tag=''):
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'msg {i} {tag}', '_msgId': f'm{i}'}
            for i in range(n)]


def _row_contents(db, conv_id):
    rows = db.execute(
        'SELECT seq, meta FROM conversation_messages WHERE conv_id=? ORDER BY seq',
        (conv_id,)).fetchall()
    return {r['seq']: mr.row_to_message(r).get('content') for r in rows}


class _UpsertSpy:
    """Counts upsert calls into conversation_messages AFTER arming."""

    def __init__(self, monkeypatch):
        from lib.database import _core_schema
        self.calls = []
        real = _core_schema.upsert

        def _spy(db, table, row, **kw):
            if getattr(table, 'name', '') == 'conversation_messages':
                self.calls.append(row.get('seq'))
            return real(db, table, row, **kw)

        monkeypatch.setattr(_core_schema, 'upsert', _spy)


@pytest.fixture()
def conv_env(monkeypatch):
    """Fresh conv with flag ON; yields (db, conv_id); cleans up rows + env."""
    from lib.database import db_execute_with_retry
    db = _ensure_table()
    conv_id = 'cv-incr-' + str(int(time.time() * 1000)) + '-' + str(os.getpid())
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    yield db, conv_id
    try:
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db.commit()
    except Exception:
        db.rollback()


# ── 1. Append path: only the new row + the previous tip are re-written ────

def test_append_writes_only_tip_and_new_row(conv_env, monkeypatch):
    """RED on the old shape: full rebuild would upsert all 4 rows here.
    The incremental mirror must upsert exactly seqs {2, 3} (tip refresh + new)."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id, _msgs(3))
    spy = _UpsertSpy(monkeypatch)
    msgs = _msgs(4)
    msgs[2] = dict(msgs[2], content='msg 2 EDITED-TIP')  # streaming-finalize shape
    mr.dual_write_conv(db, conv_id, msgs)
    assert sorted(spy.calls) == [2, 3], (
        f'append path re-wrote seqs {sorted(spy.calls)} — expected exactly '
        '[2, 3] (tip + new). Full-rebuild regression re-writes all 4.')
    assert _row_contents(db, conv_id)[2] == 'msg 2 EDITED-TIP'
    assert len(_row_contents(db, conv_id)) == 4


def test_same_count_tip_edit_is_refreshed(conv_env, monkeypatch):
    """The dominant same-count mutation (a streaming task finalizing its LAST
    message) must be mirrored even though the row count did not change —
    this is why the tip row is always re-written. NEUTER: drop the tip
    refresh (start = old, not old-1) and this goes RED."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id, _msgs(3))
    spy = _UpsertSpy(monkeypatch)
    msgs = _msgs(3)
    msgs[2] = dict(msgs[2], content='final content after streaming')
    mr.dual_write_conv(db, conv_id, msgs)
    assert sorted(spy.calls) == [2]
    assert _row_contents(db, conv_id)[2] == 'final content after streaming'


def test_truncation_deletes_stale_tail(conv_env):
    """Branch-delete / regen shortens the blob — stale rows beyond the new
    tail must disappear."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id, _msgs(5))
    mr.dual_write_conv(db, conv_id, _msgs(3))
    contents = _row_contents(db, conv_id)
    assert sorted(contents.keys()) == [0, 1, 2]


def test_fresh_conv_inserts_all_rows(conv_env):
    """Never-mirrored conv (row count 0) gets a full insert on first write."""
    db, conv_id = conv_env
    mr.dual_write_conv(db, conv_id, _msgs(4))
    assert len(_row_contents(db, conv_id)) == 4


# ── 2. changed_seqs hint: edit-capable callers name their dirty positions ──

def test_changed_seqs_hint_mirrors_exactly_those(conv_env, monkeypatch):
    """A non-tip same-count edit (translate commit / patch-by-id) is invisible
    to the count heuristic — the caller MUST pass changed_seqs. With the hint
    only that row is re-written."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id, _msgs(4))
    spy = _UpsertSpy(monkeypatch)
    msgs = _msgs(4)
    msgs[1] = dict(msgs[1], content='msg 1 translated 翻译')
    mr.dual_write_conv(db, conv_id, msgs, changed_seqs=[1])
    assert sorted(spy.calls) == [1], (
        f'hint path re-wrote {sorted(spy.calls)} — expected exactly [1]')
    assert _row_contents(db, conv_id)[1] == 'msg 1 translated 翻译'


def test_non_tip_edit_without_hint_is_missed_by_design(conv_env):
    """Documents WHY the hint exists: a same-count edit NOT at the tip and
    NOT hinted stays stale in the rows. Edit-capable writers must pass
    changed_seqs (step ② fan-out); the fleet parity gate is the backstop."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id, _msgs(3))
    msgs = _msgs(3)
    msgs[0] = dict(msgs[0], content='msg 0 EDITED-EARLY')
    mr.dual_write_conv(db, conv_id, msgs)  # no hint
    assert _row_contents(db, conv_id)[0] == 'msg 0 ', (
        'design contract changed: unhinted non-tip same-count edits are '
        'expected to stay stale (heuristic covers tail only)')
    # … and the hint repairs exactly that:
    mr.dual_write_conv(db, conv_id, msgs, changed_seqs=[0])
    assert _row_contents(db, conv_id)[0] == 'msg 0 EDITED-EARLY'


def test_full_put_dirty_set_finds_non_tip_edits_and_shifted_rows():
    """The full-array PUT can edit any position, so count/tip inference is
    insufficient. Exact comparison stays sparse for an early in-place edit
    and expands across every shifted position for an insertion."""
    before = _msgs(4)
    edited = [dict(m) for m in before]
    edited[1]['content'] = 'edited early'
    assert mr.changed_message_seqs(before, edited) == [1]

    inserted = before[:1] + [
        {'role': 'assistant', 'content': 'inserted', '_msgId': 'new'}
    ] + before[1:]
    assert mr.changed_message_seqs(before, inserted) == [1, 2, 3, 4]

    # Tail removal needs no dirty upsert; _mirror_conv_rows' range DELETE owns
    # truncation repair.
    assert mr.changed_message_seqs(before, before[:2]) == []


def test_hint_out_of_range_and_truncation_repair(conv_env):
    """Hinted seqs beyond the blob are ignored; rows beyond the blob tail
    are still cleaned even on the hint path."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id, _msgs(5))
    mr.dual_write_conv(db, conv_id, _msgs(2), changed_seqs=[1, 9, -1])
    assert sorted(_row_contents(db, conv_id).keys()) == [0, 1]


# ── 3. End-to-end: a realistic write sequence stays parity-clean ───────────

def test_incremental_sequence_keeps_parity(conv_env):
    """append → append → tip edit → hint edit → truncate: after each step the
    row store must still reconstruct the blob byte-for-byte (search parity)."""
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg, db_execute_with_retry

    db, conv_id = conv_env
    now_ms = int(time.time() * 1000)

    def _write_blob(msgs):
        upsert(db, CONVERSATIONS, {
            'id': conv_id, 'user_id': 1, 'title': 'incr-e2e',
            'messages': json_dumps_pg(msgs), 'msg_count': len(msgs),
            'created_at': now_ms, 'updated_at': now_ms,
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                        'created_at', 'updated_at'], retry=True)

    msgs = _msgs(2)
    _write_blob(msgs)
    mr.dual_write_conv(db, conv_id, msgs)                       # fresh
    msgs = msgs + _msgs(1, 'b')
    msgs[2]['_msgId'] = 'm2'
    _write_blob(msgs)
    mr.dual_write_conv(db, conv_id, msgs)                       # append
    msgs[2] = dict(msgs[2], content='final')
    _write_blob(msgs)
    mr.dual_write_conv(db, conv_id, msgs)                       # tip edit
    msgs[0] = dict(msgs[0], translatedContent='翻译')
    _write_blob(msgs)
    mr.dual_write_conv(db, conv_id, msgs, changed_seqs=[0])     # hint edit
    msgs = msgs[:2]
    _write_blob(msgs)
    mr.dual_write_conv(db, conv_id, msgs)                       # truncate

    verdict = mr.verify_conv_parity(db, conv_id)
    assert verdict['ok'] is True, f'parity after incremental sequence: {verdict}'
    assert verdict['mirror_current'] is True
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def test_failed_multirow_mirror_rolls_back_the_whole_prefix(conv_env,
                                                            monkeypatch):
    """A failure on row N must not leave rows 0..N-1 pending for commit.

    The mirror is best-effort, but *partial* best-effort data is unsafe: a
    later commit can make an equal-count, mixed-version transcript durable.
    The savepoint makes one mirror attempt all-or-nothing without touching the
    authoritative conversation transaction.
    """
    from lib.database import _core_schema

    db, conv_id = conv_env
    original = _msgs(3)
    mr.backfill_conv(db, conv_id, original)
    real_upsert = _core_schema.upsert
    calls = 0

    def _fail_second(db_arg, table, row, **kwargs):
        nonlocal calls
        if getattr(table, 'name', '') == 'conversation_messages':
            calls += 1
            if calls == 2:
                raise RuntimeError('injected row-2 failure')
        return real_upsert(db_arg, table, row, **kwargs)

    monkeypatch.setattr(_core_schema, 'upsert', _fail_second)
    edited = [dict(m, content=f'edited-{i}') for i, m in enumerate(original)]
    assert mr.dual_write_conv(
        db, conv_id, edited, changed_seqs=[0, 1, 2]) is False
    db.commit()  # would make a leaked prefix durable in the pre-fix shape
    assert _row_contents(db, conv_id) == {
        0: 'msg 0 ', 1: 'msg 1 ', 2: 'msg 2 ',
    }


# ── 4. Duplicate _msgId robustness (the pt_97f32163 incident shape) ───────

def test_backfill_dedups_duplicate_msg_ids(conv_env):
    """Real blobs can carry two messages sharing one _msgId (prod conv
    ms1uojtuhk9fze). The partial unique index rejects the second — backfill
    must keep the id only on the FIRST occurrence and blank the later one,
    while meta preserves it verbatim (row_to_message stays lossless)."""
    db, conv_id = conv_env
    dup = [{'role': 'user', 'content': 'q', '_msgId': 'dup-1'},
           {'role': 'assistant', 'content': 'a1', '_msgId': 'dup-1'},
           {'role': 'assistant', 'content': 'a2', '_msgId': 'dup-1'}]
    n = mr.backfill_conv(db, conv_id, dup)
    assert n == 3  # no UniqueViolation, all three rows landed
    rows = db.execute(
        'SELECT seq, msg_id, meta FROM conversation_messages WHERE conv_id=? '
        'ORDER BY seq', (conv_id,)).fetchall()
    kept = [r['seq'] for r in rows if r['msg_id'] == 'dup-1']
    blanked = [r['seq'] for r in rows if r['msg_id'] == '']
    assert kept == [0], f'first occurrence must keep the id, got {kept}'
    assert blanked == [1, 2]
    # meta is lossless: the blanked rows still reconstruct the original id.
    assert mr.row_to_message(rows[1]).get('_msgId') == 'dup-1'


def test_incremental_mirror_avoids_msg_id_collision_across_seqs(conv_env):
    """A NEW message arriving with an _msgId that an EARLIER seq already owns
    (a later duplicate reply) must not violate the unique index — the mirror
    blanks the id on the new row (meta keeps it) instead of failing the whole
    write and leaving the conv permanently un-mirrored."""
    db, conv_id = conv_env
    mr.backfill_conv(db, conv_id,
                     [{'role': 'user', 'content': 'q', '_msgId': 'dup-2'}])
    msgs = [{'role': 'user', 'content': 'q', '_msgId': 'dup-2'},
            {'role': 'assistant', 'content': 'a', '_msgId': 'dup-2'}]
    mr.dual_write_conv(db, conv_id, msgs)  # append with a colliding id
    rows = db.execute(
        'SELECT seq, msg_id FROM conversation_messages WHERE conv_id=? '
        'ORDER BY seq', (conv_id,)).fetchall()
    assert [r['msg_id'] for r in rows] == ['dup-2', '']
    # … and re-mirroring the SAME seq with its own id is NOT a collision:
    mr.dual_write_conv(db, conv_id, msgs[:1])
    rows = db.execute(
        'SELECT seq, msg_id FROM conversation_messages WHERE conv_id=? '
        'ORDER BY seq', (conv_id,)).fetchall()
    assert rows[0]['msg_id'] == 'dup-2'


# ── 5. Persistent flag file (pt_59140ecd ④ — the owner-confirmed flip) ────

def test_flag_file_parsing_and_pytest_guard(tmp_path):
    """The flag file flips the write side only when it reads a truthy value;
    under pytest the DEPLOYMENT path is never consulted (default = off)."""
    p = tmp_path / 'messages_rows_write.flag'
    assert mr._flag_file_on(path=str(p)) is False          # absent → off
    p.write_text('1')
    assert mr._flag_file_on(path=str(p)) is True
    p.write_text('0')
    assert mr._flag_file_on(path=str(p)) is False
    p.write_text('true\n')
    assert mr._flag_file_on(path=str(p)) is True
    # Default (deployment) path must be inert under pytest even if a real
    # deployment file exists on this box.
    assert mr._flag_file_on() is False


def test_env_var_overrides_flag_file_both_ways(monkeypatch):
    """Env always wins: =0 is the kill switch even with the file present;
    =1 engages even with no file. Unset env + pytest → off."""
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '0')
    assert mr.rows_write_enabled() is False
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    assert mr.rows_write_enabled() is True
    monkeypatch.delenv('TOFU_MESSAGES_ROWS')
    assert mr.rows_write_enabled() is False  # pytest guard on default path


# ── 6. Null-escape robustness (the 2026-07-27 live mirror-failure class) ──

def test_message_to_row_strips_null_escapes_for_jsonb():
    """PG jsonb rejects the  escape ('unsupported Unicode escape
    sequence') — a mid-stream checkpoint carrying raw null bytes made every
    mirror of two live convs fail best-effort (60 swallowed failures). meta /
    content_json MUST be serialized with json_dumps_pg (the same serializer
    the blob path uses). A bare json.dumps regression reintroduces the
    escape → this test is RED."""
    msg = {'role': 'assistant',
           'content': 'terminal capture ' + chr(0) + ' tail',
           '_msgId': 'n1'}
    row = mr.message_to_row('cv', 0, msg)
    assert '\\u0000' not in row['meta'], (
        'meta carries a  escape — PG jsonb will reject the row write')
    row2 = mr.message_to_row('cv', 1, {'role': 'user',
                                       'content': [{'type': 'text',
                                                    'text': 'a' + chr(0) + 'b'}]})
    assert '\\u0000' not in row2['content_json']
    # Round-trip still reconstructs the message (meta stays parseable, and
    # the null byte is stripped EXACTLY as the blob writer would strip it).
    import json as _json
    back = _json.loads(row['meta'])
    assert back['role'] == 'assistant'
    assert back['content'] == 'terminal capture  tail'


if __name__ == '__main__':
    import pytest as _pt
    sys.exit(_pt.main([__file__, '-v']))
