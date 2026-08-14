#!/usr/bin/env python3
"""tests/test_settings_store.py — the serialized conversations.settings
read-merge-write helper (lib/conversations/settings_store.py).

WHY
---
~13 call sites used to do a bare read-modify-write of the WHOLE settings blob
(``SELECT settings`` → mutate one key → ``UPDATE conversations SET settings=?``).
Because each rewrites the ENTIRE blob, two concurrent settings writers silently
clobber each other's keys (last writer wins). Real collisions on a single box:
a tool-toggle PATCH dropping a just-stamped ``activeTaskId``; an autopilot
summary store wiping the ``projectSummary`` cache. The messages column had a
CAS helper; the settings column had NONE. The fix is a per-conv SERIALIZED
read-merge-write (``update_conversation_settings`` / ``set_conversation_settings``)
so every writer merges its key onto the FRESHEST blob.

Tests (drive the REAL helper against a real DB):
  1. ``test_set_merges_without_clobber`` — two SEPARATE set calls each keep the
     other's key (basic merge).
  2. ``test_missing_conv_returns_none`` — absent row → None (skipped semantics).
  3. ``test_mutate_false_skips_write`` — a mutate returning False writes nothing.
  4. ``test_concurrent_writer_not_clobbered`` — ★ the clobber-prevention proof.
     A concurrent settings write (stamping key B) sneaks in DURING our mutate
     callback (which sets key A). Because the helper RE-READS under the lock,
     the merge sees the fresh blob and BOTH keys survive.
     Double-neuter: make the helper read settings ONCE before the lock (a bare
     RMW) → key B is clobbered → this FAILS.

Env note (see project memory): run DIRECTLY
(``python tests/test_settings_store.py``) — bare pytest may lack the schema.
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed(db, conv_id, settings):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'settings-test',
        'messages': '[]', 'msg_count': 0,
        'created_at': now, 'updated_at': now, 'search_text': '',
        'settings': json_dumps_pg(settings),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text', 'settings'],
       retry=True)
    db.commit()


def _read_settings(db, conv_id):
    row = db.execute('SELECT settings, updated_at FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row:
        return None, None
    s = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    return s, row[1]


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def test_set_merges_without_clobber():
    from lib.conversations import set_conversation_settings
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-set-merge'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, {'model': 'x'})
    try:
        set_conversation_settings(conv_id, {'activeTaskId': 't1'}, db=db)
        set_conversation_settings(conv_id, {'autopilotEnabled': True}, db=db)
        s, _ = _read_settings(db, conv_id)
        assert s.get('model') == 'x', s
        assert s.get('activeTaskId') == 't1', s
        assert s.get('autopilotEnabled') is True, s
    finally:
        _cleanup(db, conv_id)
    _ok('sequential set calls merge (no key clobbered)')


def test_missing_conv_returns_none():
    from lib.conversations import set_conversation_settings, update_conversation_settings
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    assert set_conversation_settings('cv-does-not-exist', {'a': 1}, db=db) is None
    assert update_conversation_settings('cv-does-not-exist', lambda s: s.update({'a': 1}), db=db) is None
    _ok('absent conversation row → None (skipped)')


def test_mutate_false_skips_write():
    from lib.conversations import update_conversation_settings
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-skip-write'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, {'model': 'x'})
    try:
        _, before = _read_settings(db, conv_id)

        def _noop(settings):
            settings['sideEffect'] = 'should-not-persist'
            return False  # signal: nothing changed

        res = update_conversation_settings(conv_id, _noop, db=db)
        assert res is not None, 'row exists → should not be None'
        s, after = _read_settings(db, conv_id)
        assert 'sideEffect' not in s, f'False mutate must not persist: {s}'
        assert after == before, 'updated_at must be untouched on a skipped write'
    finally:
        _cleanup(db, conv_id)
    _ok('mutate returning False writes nothing (skip)')


def test_concurrent_writer_not_clobbered():
    """★ Clobber-prevention proof — a classic lost-update stress test.

    Two threads each call the helper N times to INCREMENT a shared counter key
    (``read n → write n+1``). If the read+write is serialized per conv (the real
    helper re-reads UNDER the per-conv lock), no increment is lost → final
    counter == 2N. If the read happened OUTSIDE the lock (a bare RMW — the
    pre-fix behaviour), two threads read the same n and both write n+1 → lost
    updates → final counter < 2N.

    Each thread uses its OWN thread-local DB connection (``get_thread_db``), so
    this is a realistic two-writer race on the same row. This is the
    double-neuter target: move the SELECT before ``with _lock_for(...)`` and
    this FAILS (final < 2N).
    """
    import threading

    from lib.conversations import update_conversation_settings
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-lost-update'
    main_db = get_thread_db(DOMAIN_CHAT)
    _seed(main_db, conv_id, {'counter': 0})

    N = 60
    errors = []

    def _worker():
        try:
            # Fresh thread-local connection for this thread.
            get_thread_db(DOMAIN_CHAT)
            for _ in range(N):
                def _inc(settings):
                    settings['counter'] = int(settings.get('counter', 0)) + 1
                update_conversation_settings(conv_id, _inc)
        except Exception as e:  # pragma: no cover - surfaced via errors list
            errors.append(e)

    t1 = threading.Thread(target=_worker, name='sset-A')
    t2 = threading.Thread(target=_worker, name='sset-B')
    t1.start(); t2.start()
    t1.join(); t2.join()

    try:
        assert not errors, f'worker raised: {errors}'
        s, _ = _read_settings(main_db, conv_id)
        assert s.get('counter') == 2 * N, (
            f'lost update — expected {2 * N}, got {s.get("counter")}: '
            f'read+write not serialized under the per-conv lock')
    finally:
        _cleanup(main_db, conv_id)
    _ok(f'{2 * N} concurrent increments all land (no lost update / clobber)')


def main():
    print()
    print(_color('═══ conversations.settings store tests ═══', '36'))
    print()
    tests = [
        test_set_merges_without_clobber,
        test_missing_conv_returns_none,
        test_mutate_false_skips_write,
        test_concurrent_writer_not_clobbered,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} SETTINGS-STORE TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_settings_store.__main__')
    main()
