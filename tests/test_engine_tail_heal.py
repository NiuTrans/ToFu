"""tests/test_engine_tail_heal.py — the one-time backfill for EXISTING
unanswered-engine-tail adjacencies.

Owner acceptance (2026-08-05): the producer fixes (56406f93) only stop NEW
adjacencies; the 20-of-199 existing ones in the 7-day window keep warning on
every request. This migration backfills the SAME tombstone row
(``build_engine_no_reply_tombstone``) between each persisted pair —
idempotent, rev-CAS + notify, dry-run by default.

Pinned: adjacency detection shapes, heal purity + descending-index
correctness, the single-source wiring (heal rides
``lib.chat.messages.is_engine_user_msg`` / ``build_engine_no_reply_tombstone``),
the DB write path (rev bump + notify), idempotent rerun, per-row failure
isolation, and one NC.
"""

from __future__ import annotations

import json
import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_HEAL_SRC = os.path.join(ROOT, 'lib', 'conversations', 'engine_tail_heal.py')


def _user(ts, **flags):
    m = {'role': 'user', 'content': 'x', 'timestamp': ts}
    m.update(flags)
    return m


# ── pure detection + heal shapes ──────────────────────────────────────────

def test_find_single_and_multiple_pairs():
    from lib.conversations.engine_tail_heal import find_engine_tail_adjacencies
    msgs = [_user(1, _brainDispatch=True), _user(2),
            {'role': 'assistant', 'content': 'a'},
            _user(3, _isVirtualUser=True), _user(4, _brainDispatch=True)]
    assert find_engine_tail_adjacencies(msgs) == [1, 4]


def test_find_ignores_human_tail_and_healed_pairs():
    from lib.conversations.engine_tail_heal import find_engine_tail_adjacencies
    from lib.chat.messages import build_engine_no_reply_tombstone as tomb
    assert find_engine_tail_adjacencies([_user(1), _user(2)]) == []
    healed = [_user(1, _brainDispatch=True), tomb(5), _user(2)]
    assert find_engine_tail_adjacencies(healed) == [], (
        'a healed pair must never match again (idempotency)')


def test_heal_messages_inserts_between_descending():
    from lib.conversations.engine_tail_heal import heal_messages
    msgs = [_user(1, _brainDispatch=True), _user(2),
            _user(3, _isVirtualUser=True), _user(4)]
    healed, n = heal_messages(msgs, now_ms=99)
    assert n == 2
    assert [m['role'] for m in healed] == [
        'user', 'assistant', 'user', 'user', 'assistant', 'user']
    assert healed[1]['_engineNoReply'] and healed[4]['_engineNoReply']
    assert msgs[1]['role'] == 'user', 'input untouched (pure)'


def test_heal_rides_the_single_source():
    """Source pin: the heal must not re-implement the predicate or the row."""
    with open(_HEAL_SRC, encoding='utf-8') as f:
        src = f.read()
    assert 'is_engine_user_msg' in src and 'build_engine_no_reply_tombstone' in src
    assert '_brainDispatch' not in src, (
        'the heal re-implemented an engine-message flag instead of using '
        'is_engine_user_msg')
    # The flag tuple must live ONLY in lib/chat/messages.py
    assert "_ENGINE_USER_FLAGS" not in src


# ── DB write path (real conversations table) ─────────────────────────────

@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app, monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        for tbl in ('message_queue', 'conversations'):
            db.execute(f'DELETE FROM {tbl}')
        db.commit()
    yield


def _seed(flask_app, conv_id, msgs):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, settings,'
            ' created_at, updated_at, search_text) VALUES (?, 1, ?, ?, ?, ?, ?, ?)',
            (conv_id, 't', json_dumps_pg(msgs), json_dumps_pg({}), now, now, 's'))
        db.commit()


def _read(flask_app, conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        row = get_thread_db(DOMAIN_CHAT).execute(
            'SELECT messages, rev FROM conversations WHERE id=?',
            (conv_id,)).fetchone()
    return json.loads(row['messages']), row['rev']


def test_apply_heals_and_notifies_with_rev(flask_app, monkeypatch):
    from lib.conversations.engine_tail_heal import heal_engine_tail_adjacencies
    _seed(flask_app, 'cHEAL', [_user(1, _brainDispatch=True), _user(2)])
    _, rev_before = _read(flask_app, 'cHEAL')
    notified = []
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda conv_id, **kw: notified.append((conv_id, kw)))
    with flask_app.app_context():
        stats = heal_engine_tail_adjacencies(dry_run=False, progress=lambda s: None)
    msgs, rev_after = _read(flask_app, 'cHEAL')
    assert stats['written'] == 1 and stats['pairs'] == 1
    assert [m['role'] for m in msgs] == ['user', 'assistant', 'user']
    assert msgs[1]['_engineNoReply'] is True
    assert rev_after > rev_before, 'the write must bump rev (Phase 4 CAS discipline)'
    assert notified and notified[-1][0] == 'cHEAL'
    assert notified[-1][1].get('rev') == rev_after, (
        'notify must carry the REAL post-heal rev so open tabs refetch the body')


def test_dry_run_writes_nothing(flask_app):
    from lib.conversations.engine_tail_heal import heal_engine_tail_adjacencies
    _seed(flask_app, 'cDRY', [_user(1, _brainDispatch=True), _user(2)])
    with flask_app.app_context():
        stats = heal_engine_tail_adjacencies(dry_run=True, progress=lambda s: None)
    msgs, _ = _read(flask_app, 'cDRY')
    assert stats['rows_with_pairs'] == 1 and stats['written'] == 0
    assert [m['role'] for m in msgs] == ['user', 'user']


def test_rerun_is_a_noop(flask_app, monkeypatch):
    from lib.conversations.engine_tail_heal import heal_engine_tail_adjacencies
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda *a, **k: None)
    _seed(flask_app, 'cTWICE', [_user(1, _brainDispatch=True), _user(2)])
    with flask_app.app_context():
        first = heal_engine_tail_adjacencies(dry_run=False, progress=lambda s: None)
        second = heal_engine_tail_adjacencies(dry_run=False, progress=lambda s: None)
    assert first['written'] == 1
    assert second['rows_with_pairs'] == 0 and second['written'] == 0


def test_corrupt_row_skipped_others_healed(flask_app, monkeypatch):
    from lib.conversations.engine_tail_heal import heal_engine_tail_adjacencies
    from lib.database import DOMAIN_CHAT, get_thread_db
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda *a, **k: None)
    _seed(flask_app, 'cGOOD', [_user(1, _brainDispatch=True), _user(2)])
    now = int(time.time() * 1000)
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, settings,'
            ' created_at, updated_at, search_text) VALUES (?, 1, ?, ?, ?, ?, ?, ?)',
            ('cCORRUPT', 't', 'not-json{{{', '{}', now, now, 's'))
        db.commit()
        stats = heal_engine_tail_adjacencies(dry_run=False, progress=lambda s: None)
    msgs, _ = _read(flask_app, 'cGOOD')
    assert stats['skipped_errors'] == 1
    assert [m['role'] for m in msgs] == ['user', 'assistant', 'user']


# ── NC ──

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_heal_insert_is_load_bearing():
    """Drop the tombstone insert → heal_messages reports pairs but writes
    nothing new (the adjacency survives)."""
    def run():
        import lib.conversations.engine_tail_heal as h
        msgs = [_user(1, _brainDispatch=True), _user(2)]
        healed, n = h.heal_messages(msgs, now_ms=99)
        assert n == 1 and [m['role'] for m in healed] == ['user', 'user'], (
            'NC: without the insert the pair is counted but not healed')
    _patch_restore(
        _HEAL_SRC,
        '        out.insert(i, build_engine_no_reply_tombstone(now_ms))',
        '        pass  # NC (insert dropped)',
        run,
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
