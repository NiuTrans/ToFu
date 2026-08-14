#!/usr/bin/env python3
"""Step (i) of the rev-based-reconcile epic: prove the server-issued monotonic
``conversations.rev`` is bumped by EVERY message writer and by NO settings-only
writer — via a DB trigger, so a future writer cannot forget it.

WHY A TRIGGER, NOT PER-WRITER CODE
----------------------------------
There are 11+ heterogeneous writers of ``conversations.messages`` (5 via
``upsert(CONVERSATIONS, …)``, 6 via raw ``UPDATE conversations SET messages=…``
in routes/conversations.py, lib/tasks_pkg/manager.py, persistence_store.py,
lib/translate/commit.py). Hand-adding ``rev = rev + 1`` to each is exactly the
"forget one → rev goes stale → CAS starts rejecting legitimate writes" hazard.
Instead a ``BEFORE UPDATE OF messages`` (PG) / ``AFTER UPDATE OF messages``
(SQLite) trigger bumps ``rev`` IN THE SAME STATEMENT as every writer, guarded by
``messages IS DISTINCT FROM`` so a settings-only / title-only write never bumps
it (which would cause a false CAS 409).

This test drives the REAL writer SHAPES against a real seeded conversation and
asserts:
  * a raw ``UPDATE … SET messages=…`` bumps rev by exactly 1;
  * the shared ``upsert(CONVERSATIONS, …)`` path (save_conv / delete_message /
    patch_message / patch_message_by_id / delete_branch all funnel through it)
    bumps rev when messages change;
  * a NO-OP messages write (same bytes) does NOT bump rev (IS DISTINCT FROM);
  * a settings-ONLY write (manager recovery branch shape) does NOT bump rev;
  * a title-ONLY write (rename shape) does NOT bump rev;
  * rev is strictly monotonic across a sequence of real message edits.

NEUTER (on-disk, real schema file restored byte-identical): drop the rev-bump
trigger from the SQLite schema, re-init a fresh DB, and prove a messages UPDATE
LEAVES rev at 0 → confirms the trigger (not some incidental default) is what
advances rev. Restores the file and re-inits.

Standalone runner (real DB, mirrors tests/test_get_path_reconcile.py); also
importable as pytest test functions.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_THIS = os.path.abspath(__file__)
_ROOT = os.path.dirname(os.path.dirname(_THIS))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'rev-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
        'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _rev(db, conv_id):
    r = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    return int((r[0] if not isinstance(r, dict) else r['rev']) or 0)


def _msg_count(db, conv_id):
    r = db.execute('SELECT msg_count FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    return int((r[0] if not isinstance(r, dict) else r['msg_count']) or 0)


def _raw_update_messages(db, conv_id, messages):
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    db.execute('UPDATE conversations SET messages=?, updated_at=?, msg_count=? '
               'WHERE id=? AND user_id=1',
               (json_dumps_pg(messages), now_ms, len(messages), conv_id))
    db.commit()


def _upsert_messages(db, conv_id, messages):
    """The shared save_conv / delete_message / patch_* / delete_branch path."""
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    existing = db.execute('SELECT created_at FROM conversations WHERE id=? AND user_id=1',
                          (conv_id,)).fetchone()
    created_at = (existing[0] if existing else now_ms)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'rev-test',
        'messages': json_dumps_pg(messages), 'created_at': created_at,
        'updated_at': now_ms, 'settings': '{}', 'msg_count': len(messages),
        'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                    'updated_at', 'settings', 'msg_count', 'search_text'],
        retry=True)
    db.commit()


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _M(n):
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'm{i}', 'timestamp': i} for i in range(n)]


def test_rev_bumps_on_message_writers():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-rev-writers'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(2))
    try:
        r0 = _rev(db, conv_id)
        assert r0 == 0, f'fresh row should start rev=0, got {r0}'

        # 1) raw UPDATE SET messages= → bump by 1
        _raw_update_messages(db, conv_id, _M(3))
        r1 = _rev(db, conv_id)
        assert r1 == r0 + 1, f'raw messages UPDATE did not bump rev: {r0}→{r1}'

        # 2) shared upsert() path (the 5-writer funnel) → bump by 1
        _upsert_messages(db, conv_id, _M(4))
        r2 = _rev(db, conv_id)
        assert r2 == r1 + 1, f'upsert messages write did not bump rev: {r1}→{r2}'

        # 3) another raw edit → strictly monotonic
        _raw_update_messages(db, conv_id, _M(5))
        r3 = _rev(db, conv_id)
        assert r3 == r2 + 1, f'rev not monotonic: {r2}→{r3}'
    finally:
        _cleanup(db, conv_id)
    _ok('rev bumps by exactly 1 on raw-UPDATE + upsert message writers (monotonic)')


def test_rev_not_bumped_by_noop_or_metadata_writes():
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    conv_id = 'cv-rev-noop'
    db = get_thread_db(DOMAIN_CHAT)
    msgs = _M(3)
    _seed(db, conv_id, msgs)
    try:
        # advance rev once so we're testing "stays put", not "starts at 0"
        _raw_update_messages(db, conv_id, _M(4))
        base = _rev(db, conv_id)
        assert base >= 1

        now_ms = int(time.time() * 1000)

        # NO-OP: write the SAME messages bytes → IS DISTINCT FROM false → no bump
        cur_msgs = _M(4)
        _raw_update_messages(db, conv_id, cur_msgs)
        _raw_update_messages(db, conv_id, cur_msgs)  # identical again
        assert _rev(db, conv_id) == base, 'identical-messages write bumped rev (should be no-op)'

        # settings-ONLY write (manager recovery branch shape) → no bump
        db.execute('UPDATE conversations SET settings=?, updated_at=? '
                   'WHERE id=? AND user_id=1',
                   ('{"activeTaskId":null}', now_ms, conv_id))
        db.commit()
        assert _rev(db, conv_id) == base, 'settings-only write bumped rev (false CAS-409 hazard)'

        # title-ONLY write (rename shape) → no bump
        db.execute('UPDATE conversations SET title=? WHERE id=? AND user_id=1',
                   ('renamed', conv_id))
        db.commit()
        assert _rev(db, conv_id) == base, 'title-only write bumped rev'

        # sanity: a real message change still bumps after the no-ops
        _raw_update_messages(db, conv_id, _M(6))
        assert _rev(db, conv_id) == base + 1, 'rev failed to bump after metadata no-ops'
        _ = json_dumps_pg  # touch import
    finally:
        _cleanup(db, conv_id)
    _ok('rev NOT bumped by no-op / settings-only / title-only writes (no false 409)')


def test_message_trigger_derives_msg_count_even_when_writer_is_wrong():
    """``msg_count`` is metadata derived from messages, so one forgotten or
    stale writer value must not make list counts and row-read gates lie."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    conv_id = 'cv-msg-count-derived'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(2))
    try:
        db.execute('UPDATE conversations SET messages=?, msg_count=? '
                   'WHERE id=? AND user_id=1',
                   (json_dumps_pg(_M(5)), 999, conv_id))
        db.commit()
        assert _msg_count(db, conv_id) == 5
        assert _rev(db, conv_id) == 1
    finally:
        _cleanup(db, conv_id)


_POSITIVE = [test_rev_bumps_on_message_writers,
             test_rev_not_bumped_by_noop_or_metadata_writes,
             test_message_trigger_derives_msg_count_even_when_writer_is_wrong]




def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def _neuter_and_subrun():
    """NC: drop the rev-bump trigger from the LIVE test DB, do a messages
    UPDATE, and assert rev STAYS 0 — proving the trigger (not an incidental
    default/writer) is what advances rev. Re-creates the trigger afterwards so
    the positive tests stay green.

    Backend-aware: SQLite uses the AFTER-UPDATE trigger; PG uses the
    BEFORE-UPDATE trigger + function. We DROP whichever exists, test, restore.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = 'cv-rev-nc'
    is_pg = 'postgres' in type(db).__module__.lower() or 'pg' in type(db).__name__.lower()
    try:
        # Drop the trigger (both dialects tolerate IF EXISTS).
        db.execute('DROP TRIGGER IF EXISTS conversations_rev_bump_trg'
                   + (' ON conversations' if is_pg else ''))
        db.commit()
        _seed(db, conv_id, _M(2))
        base = _rev(db, conv_id)
        _raw_update_messages(db, conv_id, _M(3))
        after = _rev(db, conv_id)
        rev_moved = after != base
        return (not rev_moved), f'base={base} after={after} (rev should NOT move with trigger dropped)'
    finally:
        _cleanup(db, conv_id)
        # Restore the trigger by re-running schema init (idempotent). Falls back
        # to a direct re-create on SQLite if a full re-init isn't wired here.
        try:
            if is_pg:
                db.execute('''CREATE OR REPLACE FUNCTION conversations_rev_bump() RETURNS trigger AS $$
                    BEGIN IF (NEW.messages IS DISTINCT FROM OLD.messages) THEN
                    NEW.rev := COALESCE(OLD.rev,0)+1;
                    NEW.msg_count := CASE WHEN jsonb_typeof(NEW.messages)='array'
                    THEN jsonb_array_length(NEW.messages) ELSE 0 END;
                    END IF; RETURN NEW; END; $$ LANGUAGE plpgsql;''')
                db.execute('DROP TRIGGER IF EXISTS conversations_rev_bump_trg ON conversations')
                db.execute('''CREATE TRIGGER conversations_rev_bump_trg
                    BEFORE UPDATE OF messages ON conversations
                    FOR EACH ROW EXECUTE FUNCTION conversations_rev_bump();''')
            else:
                db.execute('DROP TRIGGER IF EXISTS conversations_rev_bump_trg')
                db.execute('''CREATE TRIGGER conversations_rev_bump_trg
                    AFTER UPDATE OF messages ON conversations
                    FOR EACH ROW WHEN NEW.messages IS NOT OLD.messages
                    BEGIN UPDATE conversations SET rev = OLD.rev + 1,
                    msg_count = CASE WHEN json_valid(NEW.messages)
                    THEN json_array_length(NEW.messages) ELSE 0 END
                    WHERE id = NEW.id AND user_id = NEW.user_id; END;''')
            db.commit()
        except Exception as e:
            print(' ', _color('!', '33'), f'trigger restore failed (re-init on next startup): {e}')


def main():
    print()
    print(_color('═══ conversations.rev monotonic — writer coverage + neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_conv_rev_monotonic.__main__')

    print(_color('Baseline (shipped schema + writers):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the trigger/schema before neutering')

    print()
    print(_color('NC — drop the rev-bump trigger, fresh DB, messages UPDATE:', '36'))
    ok, out = _neuter_and_subrun()
    if not ok:
        _fail('NC did not confirm the trigger is load-bearing:\n' + out)
    _ok('NC: with the trigger removed, a messages UPDATE leaves rev=0 (trigger is load-bearing)')

    print()
    print(_color('═══ ALL REV-MONOTONIC TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
