#!/usr/bin/env python3
"""tests/test_settings_cache_invalidation.py — the structural guarantee that
EVERY conversations.settings write through the shared gate invalidates the
sidebar meta cache (lib/conversations/settings_store.py →
lib/conversations/meta_cache.py).

WHY
---
The sidebar ``?meta=1`` cache (``meta_cache._meta_cache_by_user``) stores the
WHOLE ``settings`` blob per user with a 120s safety-net TTL. Historically the
shared settings-write gate (``update_conversation_settings`` /
``set_conversation_settings``) did the serialized read-merge-write but performed
ZERO cache invalidation — invalidation was left to each caller to remember.
Several UI-visible writers forgot, most notably ``routes/chat_tool_state.py``
(the frequently-hit tool-toggle PATCH), so a sibling tab / other device saw a
stale toggle for up to the TTL. That is a structural defect: "every caller must
remember to invalidate" is a convention no one can enforce.

THE FIX (settings_store.py)
---------------------------
Invalidation is now BUILT INTO the gate: on any write that actually lands
(``mutate`` did not return False AND the row exists), the gate calls
``_invalidate_after_settings_write(conv_id, user_id, notify)`` which ALWAYS
clears the local (+ cross-replica) meta cache entry, and — when ``notify`` is
True (the default, for UI-visible writes) — ALSO emits the cross-device
``conv_changed`` push via ``notify_conv_changed`` (which itself invalidates
FIRST, so the local clear happens-before any refresh the push triggers). A
``notify=False`` caller (pure-prompt ``projectSummary``; callers that emit their
own notify to avoid a double push) STILL invalidates the local cache.

Tests (drive the REAL gate + the previously-broken call sites against a real DB;
observe the REAL meta cache entry):
  1. tool-state write            → cache invalidated (ts→0) + push emitted.
  2. autopilot arm / disarm      → cache invalidated + push emitted.
  3. run-record store (sidecar)  → cache invalidated + push emitted.
  4. notify=False path           → cache STILL invalidated, but NO push
     (the "do not skip invalidation for any path" requirement).
  5. mutate→False (no-op write)  → cache NOT touched (no over-invalidation).
Each mutating path carries a NEUTER control that stubs the gate's
``_invalidate_after_settings_write`` to a no-op and proves the cache stays warm
— i.e. the GATE-LEVEL invalidation is load-bearing, not a caller side-effect.

Env note (see project memory): run DIRECTLY
(``python tests/test_settings_cache_invalidation.py``) — bare pytest may lack
the schema. Uses PYTEST_DISABLE_PLUGIN_AUTOLOAD-friendly isolated imports.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_settings_cache_invalidation.__main__', init_schema=False)

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_UID = 1


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _schema():
    from lib.database import init_db
    init_db()


@pytest.fixture
def captured(monkeypatch):
    """Capture every push_event(channel, task_id, payload) the seam emits.

    Patched at the DEFINITION module so the lazy
    ``from lib.agent_core.push import push_event`` inside notify_conv_changed
    picks up the fake regardless of which caller triggered it."""
    frames = []
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(
        push_mod, 'push_event',
        lambda channel, task_id, payload: frames.append(
            {'channel': channel, 'taskId': task_id, 'payload': payload}))
    return frames


def _notify_frames(frames):
    return [f for f in frames if f['channel'] == 'notify']


def _db():
    from lib.database import DOMAIN_CHAT, get_thread_db
    return get_thread_db(DOMAIN_CHAT)


def _seed(conv_id, settings=None, messages=None):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(_db(), CONVERSATIONS, {
        'id': conv_id, 'user_id': _UID, 'title': 'settings-cache-test',
        'messages': json_dumps_pg(messages or []),
        'msg_count': len(messages or []),
        'created_at': now, 'updated_at': now, 'search_text': '',
        'settings': json_dumps_pg(settings or {}),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text', 'settings'],
       retry=True)
    _db().commit()


def _cleanup(conv_id):
    from lib.database import db_execute_with_retry
    try:
        db_execute_with_retry(_db(),
                              'DELETE FROM conversations WHERE id=? AND user_id=?',
                              (conv_id, _UID))
        _db().commit()
    except Exception:
        pass


def _warm_cache(uid=_UID):
    """Force the user's meta cache entry to a KNOWN-WARM state (ts>0).

    We set the entry directly (no DB round-trip) so "warm" is deterministic and
    independent of query timing: ``ts>0`` means "cached & fresh". The gate's
    invalidation resets ``ts`` to 0 (``_local_invalidate``), which is exactly
    what we assert on."""
    import lib.conversations.meta_cache as mc
    with mc._meta_cache_lock:
        e = mc._entry(uid)
        e['data'] = b'[]'
        e['etag'] = 'warm'
        e['ts'] = time.monotonic()  # > 0 → warm
    return e


def _cache_ts(uid=_UID):
    import lib.conversations.meta_cache as mc
    e = mc._meta_cache_by_user.get(uid)
    return None if e is None else e['ts']


def _is_invalidated(uid=_UID):
    """True iff the warm entry was cleared (ts reset to 0)."""
    return _cache_ts(uid) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  1. Tool-state write (routes/chat_tool_state.py's PATCH → set_conversation_settings)
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_state_write_invalidates_cache(captured):
    """A tool-toggle settings write clears the sidebar cache AND pushes."""
    from lib.conversations import set_conversation_settings
    conv_id = 'cv-cache-tool-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {'fetchEnabled': False})
    try:
        _warm_cache()
        assert not _is_invalidated(), 'precondition: cache warm before write'
        # Exactly what chat_tool_state.py's _write() does.
        res = set_conversation_settings(conv_id, {'fetchEnabled': True}, db=_db())
        assert res is not None, 'row exists → write should land'
        assert _is_invalidated(), 'tool-state write must invalidate the meta cache'
        # UI-visible → default notify=True → a cross-device push frame fires.
        frames = _notify_frames(captured)
        assert len(frames) == 1, f'expected 1 notify push, got {frames}'
        assert frames[0]['payload']['convId'] == conv_id
        assert frames[0]['payload']['type'] == 'conv_changed'
    finally:
        _cleanup(conv_id)


def test_tool_state_write_NEUTER_cache_stays_warm(captured, monkeypatch):
    """NEUTER: stub the gate's invalidation → the write still lands in the DB
    but the cache stays WARM (ts unchanged). Proves the GATE-LEVEL invalidation
    (not a caller side-effect) is what clears the cache."""
    import lib.conversations.settings_store as ss
    from lib.conversations import set_conversation_settings
    conv_id = 'cv-cache-toolN-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {'fetchEnabled': False})
    try:
        e = _warm_cache()
        warm_ts = e['ts']
        monkeypatch.setattr(ss, '_invalidate_after_settings_write',
                            lambda *a, **k: None)
        res = set_conversation_settings(conv_id, {'fetchEnabled': True}, db=_db())
        assert res is not None and res.get('fetchEnabled') is True
        # DB write happened; cache was NOT invalidated (the load-bearing proof).
        assert _cache_ts() == warm_ts, 'neutered gate must leave the cache warm'
        assert _notify_frames(captured) == [], 'neutered gate emits no push'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  2. Autopilot arm / disarm (chat_queue.py → set_conversation_settings)
# ─────────────────────────────────────────────────────────────────────────────

def test_autopilot_arm_disarm_invalidates_cache(captured):
    from lib.conversations import set_conversation_settings
    conv_id = 'cv-cache-ap-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {})
    try:
        # ARM
        _warm_cache()
        set_conversation_settings(conv_id, {'autopilotEnabled': True}, db=_db())
        assert _is_invalidated(), 'autopilot arm must invalidate the meta cache'
        # DISARM
        _warm_cache()
        set_conversation_settings(conv_id, {'autopilotEnabled': False}, db=_db())
        assert _is_invalidated(), 'autopilot disarm must invalidate the meta cache'
        # Two UI-visible writes → two pushes.
        assert len(_notify_frames(captured)) == 2, _notify_frames(captured)
    finally:
        _cleanup(conv_id)


def test_autopilot_arm_NEUTER_cache_stays_warm(monkeypatch):
    import lib.conversations.settings_store as ss
    from lib.conversations import set_conversation_settings
    conv_id = 'cv-cache-apN-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {})
    try:
        e = _warm_cache()
        warm_ts = e['ts']
        monkeypatch.setattr(ss, '_invalidate_after_settings_write',
                            lambda *a, **k: None)
        set_conversation_settings(conv_id, {'autopilotEnabled': True}, db=_db())
        assert _cache_ts() == warm_ts, 'neutered gate must leave the cache warm'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  3. Autopilot run-record store (sidecar — UI-visible summary)
# ─────────────────────────────────────────────────────────────────────────────

def test_run_record_store_invalidates_cache(captured):
    from lib.tasks_pkg.autopilot import _store_run_record
    conv_id = 'cv-cache-rr-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {}, messages=[
        {'role': 'user', 'content': 'go', '_msgId': 'm-u'},
        {'role': 'assistant', 'content': 'done', '_msgId': 'm-a',
         '_autopilotRunId': 'ar-run1'},
    ])
    try:
        _warm_cache()
        rec = _store_run_record(conv_id, 'ar-run1', reason='task_done',
                                text='All objectives met.')
        assert rec is not None and rec['status'] == 'concluded'
        assert _is_invalidated(), 'run-record store must invalidate the meta cache'
        # UI-visible summary sidecar → default notify=True → push.
        assert len(_notify_frames(captured)) == 1, _notify_frames(captured)
    finally:
        _cleanup(conv_id)


def test_run_record_store_NEUTER_cache_stays_warm(monkeypatch):
    import lib.conversations.settings_store as ss
    from lib.tasks_pkg.autopilot import _store_run_record
    conv_id = 'cv-cache-rrN-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {}, messages=[
        {'role': 'user', 'content': 'go', '_msgId': 'm-u'},
        {'role': 'assistant', 'content': 'done', '_msgId': 'm-a',
         '_autopilotRunId': 'ar-run1'},
    ])
    try:
        e = _warm_cache()
        warm_ts = e['ts']
        monkeypatch.setattr(ss, '_invalidate_after_settings_write',
                            lambda *a, **k: None)
        rec = _store_run_record(conv_id, 'ar-run1', reason='task_done',
                                text='All objectives met.')
        assert rec is not None, 'record should still persist (DB write happens)'
        assert _cache_ts() == warm_ts, 'neutered gate must leave the cache warm'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  4. notify=False path — STILL invalidates the local cache, but NO push.
#     (the "do not skip invalidation for any path" requirement — e.g.
#     projectSummary, or a caller that emits its own notify to avoid a double.)
# ─────────────────────────────────────────────────────────────────────────────

def test_notify_false_still_invalidates_but_no_push(captured):
    from lib.conversations import set_conversation_settings
    conv_id = 'cv-cache-nf-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {})
    try:
        _warm_cache()
        res = set_conversation_settings(
            conv_id, {'activeTaskId': 'task-xyz'}, db=_db(), notify=False)
        assert res is not None
        # Local cache MUST be cleared even with notify=False.
        assert _is_invalidated(), 'notify=False must STILL invalidate the local cache'
        # ...but no cross-device push (the caller either pushes its own or this
        # is pure-internal state).
        assert _notify_frames(captured) == [], 'notify=False must emit no push'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  5. A no-op write (mutate → False) must NOT invalidate — no over-invalidation.
# ─────────────────────────────────────────────────────────────────────────────

def test_noop_write_does_not_invalidate(captured):
    from lib.conversations import update_conversation_settings
    conv_id = 'cv-cache-noop-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, {'model': 'x'})
    try:
        e = _warm_cache()
        warm_ts = e['ts']

        def _skip(settings):
            return False  # nothing changed → no write

        res = update_conversation_settings(conv_id, _skip, db=_db())
        assert res is not None, 'row exists → returns settings, not None'
        assert _cache_ts() == warm_ts, 'a skipped write must NOT invalidate the cache'
        assert _notify_frames(captured) == [], 'a skipped write emits no push'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  6. Absent row → None, and the cache is untouched (nothing was written).
# ─────────────────────────────────────────────────────────────────────────────

def test_absent_row_does_not_invalidate(captured):
    from lib.conversations import set_conversation_settings
    try:
        e = _warm_cache()
        warm_ts = e['ts']
        assert set_conversation_settings('cv-absent-xyz', {'a': 1}, db=_db()) is None
        assert _cache_ts() == warm_ts, 'no row → no write → no invalidation'
        assert _notify_frames(captured) == []
    finally:
        pass


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
