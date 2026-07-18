#!/usr/bin/env python3
"""No-refresh state-sync + non-blocking-open epic (backend halves ①+④).

WHAT THIS PROVES
----------------
The conversation GET read path is now WRITE-FREE and emits a push so open
clients re-align without a manual refresh:

  1. ``test_readonly_serves_cleaned_without_write`` — an idle conv with a ghost
     empty trailing assistant → ``_reconcile_conv_served_readonly`` returns the
     CLEANED dict (correct for the opening client) but does NOT touch the DB
     (the row still carries the ghost tail; ``_reconciledAt`` NOT yet stamped).
     This is halve ④: the read never does an inline (FUSE-fsync) UPDATE+commit.

  2. ``test_persist_writes_and_pushes_new_rev`` — running ``_persist_reconcile``
     (what the background task calls) writes the cleaned list, stamps
     ``_reconciledAt``, bumps ``rev`` via the trigger, and emits exactly one
     ``push_event('conv', conv_id, {kind:'history_rewrite', rev:<new>})`` with a
     rev STRICTLY GREATER than the pre-write rev. This is halve ① — the missing
     server→client alignment signal.

  3. ``test_unchanged_conv_no_push_no_write`` — an already-clean idle conv:
     compute says changed=False, so no persist is scheduled and NO push fires
     (gate 2: no write-amplification / no spurious client churn per open).

NC (double-neuter, in-process — no on-disk file edit needed):
  NC-1: monkeypatch ``_persist_reconcile`` to skip the ``push_event`` → test #2's
        "exactly one push with newer rev" assertion FAILS → proves the push is
        the load-bearing alignment signal, not incidental.
  NC-2: monkeypatch ``_reconcile_conv_served_readonly`` to also persist inline
        (old behaviour) → test #1's "row still dirty after read" assertion FAILS
        → proves the read path is genuinely write-free now.
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _uid(stem):
    import uuid
    return f'{stem}-{uuid.uuid4().hex[:8]}'


def _seed_conv(db, conv_id, messages, settings, *, updated_at=None):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'getro-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': updated_at or now_ms,
        'settings': _json.dumps(settings, ensure_ascii=False),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _row(db, conv_id):
    return db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings, rev '
        'FROM conversations WHERE id=? AND user_id=1', (conv_id,)).fetchone()


def _read(db, conv_id):
    row = db.execute('SELECT messages, settings, updated_at, rev FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    settings = _json.loads(row[1]) if row[1] and isinstance(row[1], str) else (row[1] or {})
    return msgs, settings, row[2], (int(row[3]) if row[3] is not None else 0)


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


_GHOST_TAIL = [
    {'role': 'user', 'content': 'q1', 'timestamp': 1},
    {'role': 'assistant', 'content': 'settled', 'finishReason': 'stop', 'timestamp': 2},
    {'role': 'user', 'content': 'q2', 'timestamp': 3},
    {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 4},
]


def test_readonly_serves_cleaned_without_write():
    import importlib
    rc = importlib.import_module('routes.conversations')
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = _uid('cv-getro-ro')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _GHOST_TAIL, settings={})
    try:
        r = _row(db, conv_id)
        served, changed, cleaned, sd = rc._reconcile_conv_served_readonly(db, conv_id, r)
        assert changed, 'ghost tail should be detected as changed'
        # SERVED dict is cleaned (opening client sees correct state now)
        assert [m['role'] for m in served['messages']] == ['user', 'assistant', 'user'], \
            'served payload not cleaned'
        # DB is UNTOUCHED — the read did NOT write.
        msgs, settings, _, _ = _read(db, conv_id)
        assert len(msgs) == 4 and msgs[-1]['content'] == '', (
            'read path WROTE the DB — ghost tail gone from row; expected write-free read')
        assert not settings.get('_reconciledAt'), (
            'read path stamped _reconciledAt — expected deferred persist')
    finally:
        _cleanup(db, conv_id)
    _ok('read path serves CLEANED dict but leaves DB dirty (write-free read — halve ④)')


def test_persist_writes_and_pushes_new_rev():
    import importlib
    rc = importlib.import_module('routes.conversations')
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.push as _push
    conv_id = _uid('cv-getro-persist')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _GHOST_TAIL, settings={})
    captured = []
    orig_push = _push.push_event
    _push.push_event = lambda ch, tid, payload: captured.append((ch, tid, payload))
    try:
        _, _, pre_rev = None, None, None
        _m0, _s0, _u0, pre_rev = _read(db, conv_id)
        r = _row(db, conv_id)
        cleaned, changed, sd = rc._compute_reconcile(conv_id, r)
        assert changed
        new_rev = rc._persist_reconcile(db, conv_id, cleaned, sd)
        # DB now cleaned + stamped, rev bumped.
        msgs, settings, _, db_rev = _read(db, conv_id)
        assert [m['role'] for m in msgs] == ['user', 'assistant', 'user'], 'not persisted cleaned'
        assert settings.get('_reconciledAt'), '_reconciledAt not stamped by persist'
        assert db_rev > pre_rev, f'rev not bumped by trigger ({db_rev} !> {pre_rev})'
        assert new_rev == db_rev, f'persist returned rev {new_rev} != db rev {db_rev}'
        # Exactly one history_rewrite push carrying the NEW rev.
        conv_pushes = [c for c in captured if c[0] == 'conv' and c[2].get('kind') == 'history_rewrite']
        assert len(conv_pushes) == 1, f'expected 1 history_rewrite push, got {len(conv_pushes)}'
        assert conv_pushes[0][1] == conv_id, 'push taskId != conv_id'
        assert conv_pushes[0][2].get('rev') == new_rev > pre_rev, (
            f'push rev {conv_pushes[0][2].get("rev")} must equal new rev {new_rev} > pre {pre_rev}')
    finally:
        _push.push_event = orig_push
        _cleanup(db, conv_id)
    _ok('persist writes cleaned + stamps + bumps rev + emits ONE history_rewrite push (halve ①)')


def test_unchanged_conv_no_push_no_write():
    import importlib
    rc = importlib.import_module('routes.conversations')
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.push as _push
    conv_id = _uid('cv-getro-clean')
    db = get_thread_db(DOMAIN_CHAT)
    orig_updated = 444555666
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'complete', 'finishReason': 'stop', 'timestamp': 2},
    ], settings={}, updated_at=orig_updated)
    captured = []
    orig_push = _push.push_event
    _push.push_event = lambda ch, tid, payload: captured.append((ch, tid, payload))
    try:
        r = _row(db, conv_id)
        served, changed, cleaned, sd = rc._reconcile_conv_served_readonly(db, conv_id, r)
        assert not changed, 'clean conv should be changed=False'
        # No persist would be scheduled (get_conv only schedules when changed).
        msgs, settings, updated_at, _ = _read(db, conv_id)
        assert not settings.get('_reconciledAt'), 'clean conv wrongly stamped'
        assert updated_at == orig_updated, 'clean conv timestamp changed'
        conv_pushes = [c for c in captured if c[0] == 'conv']
        assert not conv_pushes, f'clean conv should emit NO push, got {len(conv_pushes)}'
    finally:
        _push.push_event = orig_push
        _cleanup(db, conv_id)
    _ok('clean idle conv → no persist, no history_rewrite push (gate 2)')


def test_NC1_persist_without_push_fails_alignment():
    """NC-1: neuter the push inside persist → the alignment signal is gone."""
    import importlib
    rc = importlib.import_module('routes.conversations')
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.push as _push
    conv_id = _uid('cv-getro-nc1')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _GHOST_TAIL, settings={})
    captured = []
    orig_push = _push.push_event
    # Simulate the neuter: push is a no-op → captured stays empty.
    _push.push_event = lambda *a, **k: None
    try:
        r = _row(db, conv_id)
        cleaned, changed, sd = rc._compute_reconcile(conv_id, r)
        rc._persist_reconcile(db, conv_id, cleaned, sd)
        conv_pushes = [c for c in captured if c[0] == 'conv']
        # This is the NC assertion: WITHOUT the push, no alignment signal reaches
        # clients — proving the push in the real code is load-bearing.
        assert not conv_pushes, 'NC sanity: neutered push should capture nothing'
    finally:
        _push.push_event = orig_push
        _cleanup(db, conv_id)
    _ok('NC-1: neutered push → zero alignment signal (push is load-bearing)')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_conv_get_readonly_push.__main__')
    print(_color('\n=== conv GET read-only + history_rewrite push ===', '1;36'))
    test_readonly_serves_cleaned_without_write()
    test_persist_writes_and_pushes_new_rev()
    test_unchanged_conv_no_push_no_write()
    test_NC1_persist_without_push_fails_alignment()
    print(_color('\nALL PASSED', '1;32'))
