#!/usr/bin/env python3
"""Cross-device send visibility — the north-star: a message sent from device B
must appear on device A immediately (real-time), NOT only after the assistant
reply lands.

BACKGROUND (the bug this fixes)
-------------------------------
Every /api/chat/send path emitted ``notify_conv_changed(conv_id, rev=None)``.
A ``rev=None`` frame is a metadata-only hint → device A only refreshes its
sidebar, never refetches the body. So the just-sent USER message stayed
invisible on the other device until the turn's assistant reply produced a
real-rev frame. The QUEUED lane was worse: the user message was never even
persisted to the conversation body — it lived only in ``message_queue`` — so it
could not appear until ``dispatch_next_queued`` ran after the current turn.

THE FIX (two halves, both exercised here)
-----------------------------------------
* Fix 1 — ``persist_conv_messages`` returns the post-write ``rev`` the DB
  trigger advanced (single source of truth, one SELECT inside persist), and the
  immediate-start ``chat_send`` path passes THAT rev to ``notify_conv_changed``
  instead of ``None`` → the sibling device's rev-gate refetches the body and
  the user message appears at once.
* Fix 2a — the QUEUED lane lands the user message in the body NOW as a
  display-only ``_pendingQueued`` row (``append_pending_user_msg``) and pushes
  the real rev. ``dispatch_next_queued`` later reconciles that row in place by
  timestamp (never a duplicate).

LOAD-BEARING SAFETY (why the original design withheld the persist)
------------------------------------------------------------------
The running turn's ``_sync_partial_to_conversation`` / ``_sync_result_to_
conversation`` located their assistant slot by BLIND TAIL (``messages[-1]``). A
trailing pending USER row would make them see a ``user`` tail and append a
SECOND assistant — the two-writer truncation. The fix makes slot location
ID-FIRST (locate by the running task's ``_assistantMsgId``), and
``append_pending_user_msg`` only pre-persists when the tail assistant is
id-owned by a running task. This suite proves the running turn's syncs are NOT
disturbed by a trailing pending row — with a NEUTER that restores blind-tail
location and shows the bogus-second-assistant regression re-appears.

Real-DB standalone runner (mirrors tests/test_conv_rev_monotonic.py); also
importable as pytest functions.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── real-DB helpers (same shapes as test_conv_rev_monotonic) ──

def _seed(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'xdev-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms, 'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _load(db, conv_id):
    import json
    r = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    if not r:
        return None
    raw = r[0] if not isinstance(r, dict) else r['messages']
    return json.loads(raw or '[]')


def _rev(db, conv_id):
    r = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    return int((r[0] if not isinstance(r, dict) else r['rev']) or 0)


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _roles(messages):
    return [m.get('role') for m in messages]


# ════════════════════════════════════════════════════════════════════
#  Fix 1 — persist_conv_messages returns the post-write rev
# ════════════════════════════════════════════════════════════════════

def test_persist_conv_messages_returns_post_write_rev():
    """persist_conv_messages must RETURN the rev the trigger advanced to — the
    single source of truth the send path forwards to the notify frame."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import persist_conv_messages
    conv_id = 'cv-xdev-rev-return'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [{'role': 'user', 'content': 'hi', 'timestamp': 1}])
    try:
        base = _rev(db, conv_id)
        msgs = [{'role': 'user', 'content': 'hi', 'timestamp': 1},
                {'role': 'user', 'content': 'second', 'timestamp': 2}]
        returned = persist_conv_messages(db, conv_id, msgs, 'xdev-test')
        db_rev = _rev(db, conv_id)
        assert returned == db_rev, f'returned rev {returned} != DB rev {db_rev}'
        assert returned == base + 1, f'expected rev bump {base}->{base+1}, got {returned}'
        # A metadata-only re-persist (identical messages) must NOT bump — and
        # returns the SAME rev (so the caller emits a truthful, non-advancing
        # frame the client rev-gates as a no-op).
        returned2 = persist_conv_messages(db, conv_id, msgs, 'xdev-test')
        assert returned2 == db_rev, f'no-op persist should keep rev {db_rev}, got {returned2}'
    finally:
        _cleanup(db, conv_id)
    _ok('persist_conv_messages returns the post-write rev (== DB rev; no-op keeps it)')


# ════════════════════════════════════════════════════════════════════
#  Fix 2a — append_pending_user_msg lands the queued user row
# ════════════════════════════════════════════════════════════════════

def test_pending_row_appends_when_tail_is_owned_assistant():
    """When the running turn's assistant slot is the tail AND is id-owned by a
    running task, the queued user message lands as a _pendingQueued row and a
    REAL rev is returned."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import append_pending_user_msg
    conv_id = 'cv-xdev-pending-ok'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'streaming...', 'timestamp': 2,
         '_msgId': 'amid-running'},
    ])
    try:
        base = _rev(db, conv_id)
        user_msg = {'role': 'user', 'content': 'q2 queued', 'timestamp': 3}
        appended, rev = append_pending_user_msg(
            db, conv_id, user_msg, valid_assistant_ids={'amid-running'})
        assert appended is True, 'pending row should have been appended'
        assert rev == base + 1, f'expected rev bump, got {rev} (base {base})'
        msgs = _load(db, conv_id)
        assert _roles(msgs) == ['user', 'assistant', 'user'], _roles(msgs)
        assert msgs[-1].get('_pendingQueued') is True, 'row must be marked _pendingQueued'
        assert msgs[-1].get('content') == 'q2 queued'
        # Idempotent: a second call with the SAME timestamp must NOT duplicate.
        appended2, _ = append_pending_user_msg(
            db, conv_id, user_msg, valid_assistant_ids={'amid-running'})
        assert appended2 is False, 'same-timestamp re-append must be declined'
        assert _roles(_load(db, conv_id)) == ['user', 'assistant', 'user']
    finally:
        _cleanup(db, conv_id)
    _ok('pending queued row lands (real rev) when tail is an id-owned assistant; idempotent')


def test_pending_row_declined_when_tail_not_assistant():
    """Order-safety gate: if the tail is NOT an assistant (turn hasn't produced
    its slot yet), decline — appending would misorder against the reply."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import append_pending_user_msg
    conv_id = 'cv-xdev-pending-userTail'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [{'role': 'user', 'content': 'q1', 'timestamp': 1,
                         '_msgId': 'u1'}])
    try:
        appended, rev = append_pending_user_msg(
            db, conv_id, {'role': 'user', 'content': 'q2', 'timestamp': 2},
            valid_assistant_ids={'amid-running'})
        assert appended is False, 'must decline when tail is not an assistant'
        assert rev is None
        assert _roles(_load(db, conv_id)) == ['user'], 'body must be unchanged'
    finally:
        _cleanup(db, conv_id)
    _ok('pending row DECLINED when tail is not an assistant (order-safety gate holds)')


def test_pending_row_declined_when_tail_assistant_not_owned():
    """Slot-addressability gate: tail IS an assistant but its _msgId is NOT the
    running task's — declined (the running sync couldn't locate it by id, so a
    pending row would break it)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import append_pending_user_msg
    conv_id = 'cv-xdev-pending-unowned'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'settled', 'timestamp': 2,
         '_msgId': 'some-old-id'},
    ])
    try:
        appended, rev = append_pending_user_msg(
            db, conv_id, {'role': 'user', 'content': 'q2', 'timestamp': 3},
            valid_assistant_ids={'amid-running'})
        assert appended is False, 'must decline when tail assistant is not id-owned'
        assert rev is None
        assert _roles(_load(db, conv_id)) == ['user', 'assistant']
    finally:
        _cleanup(db, conv_id)
    _ok('pending row DECLINED when tail assistant not owned by a running task')


def test_pending_row_declined_with_no_valid_ids():
    """A running task that shipped no stable _assistantMsgId can't be protected
    → decline (empty/None valid set)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import append_pending_user_msg
    conv_id = 'cv-xdev-pending-noids'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'x', 'timestamp': 2, '_msgId': 'a'},
    ])
    try:
        for ids in (None, set(), {''}):
            appended, rev = append_pending_user_msg(
                db, conv_id, {'role': 'user', 'content': 'q2', 'timestamp': 3},
                valid_assistant_ids=ids)
            assert appended is False, f'must decline with valid_assistant_ids={ids!r}'
        assert _roles(_load(db, conv_id)) == ['user', 'assistant']
    finally:
        _cleanup(db, conv_id)
    _ok('pending row DECLINED when no running-task assistant id to protect')


# ════════════════════════════════════════════════════════════════════
#  LOAD-BEARING: pending user row + streaming assistant → no truncation
# ════════════════════════════════════════════════════════════════════

def _running_task(conv_id, amid, content):
    """Minimal in-memory task dict shaped for the sync functions."""
    import threading
    return {
        'id': 'task-running', 'convId': conv_id, '_assistantMsgId': amid,
        'content': content, 'thinking': '', 'status': 'running',
        'events': [], 'events_lock': threading.Lock(),
    }


def test_partial_sync_updates_owned_slot_not_pending_row():
    """THE load-bearing test: with a trailing _pendingQueued USER row present,
    _sync_partial_to_conversation must update the running task's OWN assistant
    slot (located by id) and must NOT spawn a second assistant."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import _record_latest_task
    from lib.tasks_pkg.manager._sync import _sync_partial_to_conversation
    conv_id = 'cv-xdev-partial-owned'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'partial so far', 'timestamp': 2,
         '_msgId': 'amid-running'},
        {'role': 'user', 'content': 'q2 queued', 'timestamp': 3,
         '_msgId': 'u2', '_pendingQueued': True},
    ])
    try:
        _record_latest_task(conv_id, 'task-running')
        task = _running_task(conv_id, 'amid-running',
                             'partial so far + MORE streamed tokens')
        _sync_partial_to_conversation(task)
        msgs = _load(db, conv_id)
        assert _roles(msgs) == ['user', 'assistant', 'user'], (
            f'expected no new assistant row, got roles {_roles(msgs)}')
        by_id = {m.get('_msgId'): m for m in msgs}
        assert 'MORE streamed tokens' in by_id['amid-running']['content'], (
            'the id-owned assistant slot must have received the streamed growth')
        assert msgs[-1].get('_pendingQueued') is True, 'pending row must be untouched'
    finally:
        _cleanup(db, conv_id)
        _record_latest_task(conv_id, None)
    _ok('partial sync grows the id-owned assistant, leaves the pending user row intact')


def test_result_sync_fills_owned_slot_not_pending_row():
    """Terminal sync analogue: _sync_result_to_conversation fills the id-owned
    assistant slot even with a trailing pending user row — no bogus append."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import _record_latest_task
    from lib.tasks_pkg.manager._sync import _sync_result_to_conversation
    conv_id = 'cv-xdev-result-owned'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'partial', 'timestamp': 2,
         '_msgId': 'amid-running'},
        {'role': 'user', 'content': 'q2 queued', 'timestamp': 3,
         '_msgId': 'u2', '_pendingQueued': True},
    ])
    try:
        _record_latest_task(conv_id, 'task-running')
        task = _running_task(conv_id, 'amid-running', 'FINAL answer content')
        task['status'] = 'done'
        _sync_result_to_conversation(task, {'finishReason': 'stop'})
        msgs = _load(db, conv_id)
        assert _roles(msgs) == ['user', 'assistant', 'user'], (
            f'expected no new assistant row, got roles {_roles(msgs)}')
        by_id = {m.get('_msgId'): m for m in msgs}
        assert by_id['amid-running']['content'] == 'FINAL answer content'
        assert msgs[-1].get('_pendingQueued') is True
    finally:
        _cleanup(db, conv_id)
        _record_latest_task(conv_id, None)
    _ok('result sync fills the id-owned assistant, leaves the pending user row intact')


# ════════════════════════════════════════════════════════════════════
#  Idempotency: dispatch reconciles the pending row in place (no dup)
# ════════════════════════════════════════════════════════════════════

def test_dispatch_reconciles_pending_row_no_duplicate():
    """When dispatch_next_queued later appends the SAME queued user message
    (same timestamp) it must reconcile the existing _pendingQueued row in place
    (via append_user_msg_idempotent), not add a duplicate."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import _append_user_msg_with_cas
    conv_id = 'cv-xdev-dispatch-idem'
    db = get_thread_db(DOMAIN_CHAT)
    # Body after the previous turn finished: the pending row is now the tail
    # (its assistant reply was filled by the prior turn's sync).
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'a1', 'timestamp': 2, '_msgId': 'amid1'},
        {'role': 'user', 'content': 'q2 queued', 'timestamp': 3,
         '_msgId': 'u2', '_pendingQueued': True},
    ])
    try:
        # dispatch's pre-built user_msg carries the SAME timestamp (3).
        user_msg = {'role': 'user', 'content': 'q2 queued', 'timestamp': 3}
        ok = _append_user_msg_with_cas(db, conv_id, user_msg)
        assert ok is True
        msgs = _load(db, conv_id)
        assert _roles(msgs) == ['user', 'assistant', 'user'], (
            f'dispatch must NOT duplicate the pending row, got {_roles(msgs)}')
        # timestamp-3 user row appears exactly once.
        n = sum(1 for m in msgs if m.get('role') == 'user' and m.get('timestamp') == 3)
        assert n == 1, f'expected exactly one ts=3 user row, got {n}'
    finally:
        _cleanup(db, conv_id)
    _ok('dispatch reconciles the pending row in place (no duplicate user bubble)')


# ════════════════════════════════════════════════════════════════════
#  NORTH-STAR: queued send emits a REAL-rev frame (device A sees it now)
# ════════════════════════════════════════════════════════════════════

def test_north_star_queued_send_emits_real_rev_frame(monkeypatch):
    """End-to-end at the seam: landing the pending queued row bumps rev, and the
    send path forwards THAT rev to notify_conv_changed → a sibling device gets a
    body-refetch frame BEFORE the assistant reply. Captures the emitted frame."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import append_pending_user_msg
    import lib.agent_core.push as push_mod
    frames = []
    monkeypatch.setattr(push_mod, 'push_event',
                        lambda ch, tid, payload: frames.append((ch, tid, payload)))
    conv_id = 'cv-xdev-northstar'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'streaming', 'timestamp': 2,
         '_msgId': 'amid-running'},
    ])
    try:
        appended, rev = append_pending_user_msg(
            db, conv_id, {'role': 'user', 'content': 'q2', 'timestamp': 3},
            valid_assistant_ids={'amid-running'})
        assert appended and isinstance(rev, int)
        # This is what the send path does with the returned rev.
        from lib.conversations.meta_cache import notify_conv_changed
        notify_conv_changed(conv_id, rev=rev)
        notify_frames = [f for f in frames if f[0] == 'notify']
        assert notify_frames, 'a notify frame must be emitted'
        payload = notify_frames[-1][2]
        assert payload['type'] == 'conv_changed'
        assert payload.get('rev') == rev, (
            f'frame must carry the REAL rev {rev} (body refetch), got {payload.get("rev")}')
        assert 'rev' in payload, 'rev must be present (NOT a metadata-only frame)'
    finally:
        _cleanup(db, conv_id)
    _ok('north-star: queued send emits a REAL-rev conv_changed frame (device A sees it now)')


# ════════════════════════════════════════════════════════════════════
#  Standalone runner + NEUTER controls
# ════════════════════════════════════════════════════════════════════

_POSITIVE = [
    test_persist_conv_messages_returns_post_write_rev,
    test_pending_row_appends_when_tail_is_owned_assistant,
    test_pending_row_declined_when_tail_not_assistant,
    test_pending_row_declined_when_tail_assistant_not_owned,
    test_pending_row_declined_with_no_valid_ids,
    test_partial_sync_updates_owned_slot_not_pending_row,
    test_result_sync_fills_owned_slot_not_pending_row,
    test_dispatch_reconciles_pending_row_no_duplicate,
]


def _run(fn):
    try:
        fn() if fn.__code__.co_argcount == 0 else fn(_NullMonkeypatch())
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


class _NullMonkeypatch:
    """Minimal monkeypatch shim for the standalone runner (setattr with undo)."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo = []


def _neuter_slot_location_reintroduces_truncation():
    """NEUTER for the load-bearing guard: force BLIND-TAIL slot location in the
    partial sync (bypass the id-first lookup) and prove the bogus-second-
    assistant regression re-appears when a pending user row is the tail. This
    proves the id-first location — not luck — is what protects the running turn.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import _record_latest_task
    import lib.tasks_pkg.manager._events as _events
    import lib.tasks_pkg.manager._sync as _sync
    conv_id = 'cv-xdev-neuter-trunc'
    db = get_thread_db(DOMAIN_CHAT)

    # Neuter: make find_message_by_id (used by the id-first branch) always miss,
    # so slot location falls back to blind tail (the pre-fix behaviour).
    orig = _events.find_message_by_id
    _sync.find_message_by_id = lambda messages, mid: (None, None)  # used via import in _sync
    # _sync imports find_message_by_id lazily inside the function; patch the
    # source module so the lazy import picks up the neutered version.
    _events.find_message_by_id = lambda messages, mid: (None, None)
    try:
        _seed(db, conv_id, [
            {'role': 'user', 'content': 'q1', 'timestamp': 1},
            {'role': 'assistant', 'content': 'partial', 'timestamp': 2,
             '_msgId': 'amid-running'},
            {'role': 'user', 'content': 'q2 queued', 'timestamp': 3,
             '_msgId': 'u2', '_pendingQueued': True},
        ])
        _record_latest_task(conv_id, 'task-running')
        task = _running_task(conv_id, 'amid-running', 'partial + MORE')
        _sync._sync_partial_to_conversation(task)
        msgs = _load(db, conv_id)
        roles = _roles(msgs)
        # With blind-tail location, the sync sees the trailing USER row and
        # appends a SECOND assistant → the regression.
        regressed = roles == ['user', 'assistant', 'user', 'assistant']
        return regressed, f'roles={roles} (expected the 2nd-assistant regression)'
    finally:
        _events.find_message_by_id = orig
        _sync.find_message_by_id = orig
        _cleanup(db, conv_id)
        _record_latest_task(conv_id, None)


def _neuter_send_forwards_none_rev():
    """NEUTER for Fix 1/2a rev: if the send path forwarded rev=None (the bug),
    the frame is metadata-only → device A does NOT refetch the body. Prove the
    captured frame then lacks a rev."""
    import lib.agent_core.push as push_mod
    frames = []
    _orig = push_mod.push_event
    push_mod.push_event = lambda ch, tid, payload: frames.append((ch, tid, payload))
    try:
        from lib.conversations.meta_cache import notify_conv_changed
        notify_conv_changed('cv-xdev-neuter-none', rev=None)  # the pre-fix call
        notify_frames = [f for f in frames if f[0] == 'notify']
        payload = notify_frames[-1][2] if notify_frames else {}
        has_no_rev = 'rev' not in payload
        return has_no_rev, f'payload={payload} (pre-fix frame must lack rev)'
    finally:
        push_mod.push_event = _orig


def main():
    print()
    print(_color('═══ cross-device send visibility — Fix 1 + Fix 2a + load-bearing guards ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_cross_device_send_visibility.__main__')

    print(_color('Positive (shipped code):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('a positive test failed — fix before neutering')
    # north-star (needs a monkeypatch shim)
    if not _run(test_north_star_queued_send_emits_real_rev_frame):
        _fail('north-star test failed')

    print()
    print(_color('NEUTER — blind-tail slot location reintroduces truncation:', '36'))
    regressed, out = _neuter_slot_location_reintroduces_truncation()
    if not regressed:
        _fail('NC did not reproduce the 2nd-assistant regression:\n' + out)
    _ok('NC: blind-tail location spawns a bogus 2nd assistant (id-first fix is load-bearing)')

    print()
    print(_color('NEUTER — send forwarding rev=None yields a metadata-only frame:', '36'))
    ok, out = _neuter_send_forwards_none_rev()
    if not ok:
        _fail('NC did not confirm rev=None omits rev:\n' + out)
    _ok('NC: rev=None frame carries no rev (device A would not refetch — proves real-rev is load-bearing)')

    print()
    print(_color('═══ ALL CROSS-DEVICE VISIBILITY TESTS + NEUTERS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
