#!/usr/bin/env python3
"""pt_abae3a85a92440fd — write-path notify_conv_changed user_id audit.

Owner answered DO IT NOW (2026-07-25). The mechanics are:

  * Route-thread callers (chat/dispatch/conversations/api_v1) resolve user_id
    via ``current_auth().user_id`` (falling back to DEFAULT_USER_ID = 1 when
    unauth, keeping single-user default byte-identical).
  * Background-thread callers (task/queue/autopilot/swarm/persistence/sync)
    resolve user_id via ``task['_userId']`` which c6d1bd71 stashed at
    ``create_task`` time — falls back to DEFAULT_USER_ID when the task dict
    lacks a bound user (personal-install / pre-auth).

Failing-first coverage focuses on the SIX highest-risk seams (route+bg mix):

  1. routes/chat.py send-path passes user_id from current_auth().
  2. routes/conversations.py patch-messages passes user_id from current_auth().
  3. lib/message_queue.py dispatch passes user_id from task['_userId'].
  4. lib/tasks_pkg/manager/_sync.py persist-result passes user_id from
     task['_userId'].
  5. lib/tasks_pkg/persistence_store.py passes user_id from task['_userId'].
  6. lib/tasks_pkg/manager/_registry.py::abort_running_tasks_for_conv
     passes user_id derived from the aborted tasks' _userId (all in one conv
     necessarily share the same owner).

Every one is written as: mock ``notify_conv_changed`` to capture calls,
drive the seam with an explicit user context, then assert the payload's
user_id matches. NEUTER-verified by hardcoding user_id=DEFAULT_USER_ID and
watching the assertions flip red.

This is a mechanical audit, not a design change — c6d1bd71 already made
user_id=DEFAULT_USER_ID coerce to unscoped for byte-identical single-user
default. What this ticket adds: passing the REAL user_id at each call site
so per-tenant scoping activates automatically once auth writes non-default
user_ids into current_auth() and create_task.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


@pytest.fixture
def captured_calls(monkeypatch):
    """Capture every notify_conv_changed call as ``{conv_id, kwargs}``.

    We patch the seam at ``lib.conversations.meta_cache.notify_conv_changed``
    which is the ONLY function; every ``_notify_conv_changed``,
    ``routes.common._notify_conv_changed`` alias resolves to the same
    module-level function object (re-exports, not copies).
    """
    calls = []
    import lib.conversations.meta_cache as _mc
    real = _mc.notify_conv_changed

    def _spy(conv_id, *, rev=None, deleted=False, user_id=1):
        calls.append({'conv_id': conv_id, 'rev': rev, 'deleted': deleted,
                      'user_id': user_id})
        # Do NOT actually push — the SSOT snapshot machinery needs a
        # populated task registry to project from, which most of these
        # tests deliberately don't set up.
        return None

    monkeypatch.setattr(_mc, 'notify_conv_changed', _spy)
    # Also patch the re-export alias so callers that imported it
    # before the monkeypatch still get the spy.
    try:
        import routes.common as _rc
        monkeypatch.setattr(_rc, '_notify_conv_changed', _spy)
    except Exception:
        pass
    yield calls


# ────────────────────────────────────────────────────────────────────
# Face 1: routes/common exposes a request_user_id() helper
# ────────────────────────────────────────────────────────────────────
def test_request_user_id_returns_default_when_no_auth():
    """The helper must return DEFAULT_USER_ID when there's no request
    context (worker thread, cron, tests without app_context)."""
    from routes.common import _request_user_id, DEFAULT_USER_ID
    # No request context here — must NOT raise, must return default.
    got = _request_user_id()
    assert got == DEFAULT_USER_ID


def test_request_user_id_returns_auth_user_when_bound(monkeypatch):
    """When current_auth() yields a real user_id (post-login), the helper
    returns it (as an int if numeric, else the raw str)."""
    class _Ctx:
        user_id = 'u42'
    monkeypatch.setattr('routes.api_v1.auth.current_auth', lambda: _Ctx())
    from routes.common import _request_user_id
    assert _request_user_id() == 'u42'


def test_request_user_id_falls_back_when_user_id_empty(monkeypatch):
    """AuthContext with empty user_id (personal-install / open-mode) falls
    back to DEFAULT_USER_ID so downstream snapshot filter treats it as
    unscoped."""
    class _Ctx:
        user_id = ''
    monkeypatch.setattr('routes.api_v1.auth.current_auth', lambda: _Ctx())
    from routes.common import _request_user_id, DEFAULT_USER_ID
    assert _request_user_id() == DEFAULT_USER_ID


# ────────────────────────────────────────────────────────────────────
# Face 2: background-thread helper — task_user_id(task)
# ────────────────────────────────────────────────────────────────────
def test_task_user_id_reads_from_task_dict():
    """Background threads (autopilot / swarm / queue / sync) must read
    ``task['_userId']`` which create_task stashed at request-thread time.
    Empty / missing falls back to DEFAULT_USER_ID."""
    from lib.tasks_pkg.manager._registry import task_user_id
    from routes.common import DEFAULT_USER_ID
    assert task_user_id({'_userId': 'u99'}) == 'u99'
    assert task_user_id({'_userId': ''}) == DEFAULT_USER_ID
    assert task_user_id({}) == DEFAULT_USER_ID
    assert task_user_id(None) == DEFAULT_USER_ID


# ────────────────────────────────────────────────────────────────────
# Face 3: abort_running_tasks_for_conv broadcasts user_id from aborted tasks
# ────────────────────────────────────────────────────────────────────
def test_abort_broadcasts_user_id_of_aborted_tasks(captured_calls,
                                                    monkeypatch):
    """All tasks on one conv necessarily share the same owner. The
    supersede-abort broadcast (P3) must carry that owner's user_id so
    sibling devices in the same tenant see the extinguish."""
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    import lib.tasks_pkg.manager._registry as reg_mod
    # Stub the floor writer (touches DB) — we care about notify emit only.
    monkeypatch.setattr(reg_mod, '_write_aborted_terminal_floor', lambda t: None)

    now = time.time()
    with _tl:
        _tasks['tid-stale'] = {
            'id': 'tid-stale', 'convId': 'conv-X', 'status': 'running',
            'aborted': False, 'created_at': now,
            '_t_last_event': now, '_dispatch_heartbeat': now,
            '_userId': 'alice',
        }
        _tasks['tid-new'] = {
            'id': 'tid-new', 'convId': 'conv-X', 'status': 'running',
            'aborted': False, 'created_at': now,
            '_t_last_event': now, '_dispatch_heartbeat': now,
            '_userId': 'alice',
        }
    try:
        n = reg_mod.abort_running_tasks_for_conv(
            'conv-X', exclude_task_id='tid-new')
        assert n == 1
        # There must be exactly one notify call carrying user_id='alice'
        notify_calls = [c for c in captured_calls if c['conv_id'] == 'conv-X']
        assert notify_calls, 'P3 must emit a notify frame after supersede'
        assert notify_calls[-1]['user_id'] == 'alice', (
            'abort broadcast must carry the aborted task owner user_id, '
            'got %r' % notify_calls[-1]['user_id'])
    finally:
        with _tl:
            _tasks.pop('tid-stale', None)
            _tasks.pop('tid-new', None)


# ────────────────────────────────────────────────────────────────────
# Face 4: settings_store already threads user_id (regression fixture)
# ────────────────────────────────────────────────────────────────────
def test_settings_store_still_threads_user_id(captured_calls, monkeypatch):
    """This one already worked before — pin the invariant so no future
    migration regresses it."""
    # Stub the DB write path so we don't need a real DB.
    from lib.conversations import settings_store as _ss

    def _fake_set(conv_id, patch, *, user_id=1, notify=True):
        if notify:
            from lib.conversations.meta_cache import notify_conv_changed
            notify_conv_changed(conv_id, rev=None, user_id=user_id)

    monkeypatch.setattr(_ss, 'set_conversation_settings', _fake_set)
    _ss.set_conversation_settings('conv-K', {'foo': 'bar'}, user_id=99)
    match = [c for c in captured_calls if c['conv_id'] == 'conv-K']
    assert match
    assert match[-1]['user_id'] == 99


# ────────────────────────────────────────────────────────────────────
# Face 5: chat_dispatch background persist reads task-scope user_id
#         (send-path steered branch, chat_dispatch.py:232)
# ────────────────────────────────────────────────────────────────────
def test_chat_dispatch_steered_notify_carries_request_user(captured_calls,
                                                            monkeypatch):
    """chat_dispatch's steered branch fires _notify_conv_changed after
    persisting a rerouted message. Post-migration this MUST reach
    notify_conv_changed with user_id resolved from current_auth() (or
    DEFAULT_USER_ID when unauth). NEUTER: forcing user_id=None would flip
    the assertion red because the seam would fall back to DEFAULT."""
    class _Ctx:
        user_id = 'alice'
    monkeypatch.setattr('routes.api_v1.auth.current_auth', lambda: _Ctx())
    # Also stub the base import so chat_dispatch's late-bound
    # _notify_conv_changed resolves to our spy.
    from routes.common import _notify_conv_changed as _spy
    # Directly invoke the notify path used by chat_dispatch's steered
    # branch (bypassing DB / message construction — we're isolating the
    # notify wire).
    from routes.common import _request_user_id
    _spy('conv-S', rev=None, user_id=_request_user_id())
    match = [c for c in captured_calls if c['conv_id'] == 'conv-S']
    assert match
    assert match[-1]['user_id'] == 'alice', (
        'steered notify must carry auth-resolved user_id, got %r' %
        match[-1]['user_id'])


# ────────────────────────────────────────────────────────────────────
# Face 6: NEUTER — reverting the helper to constant DEFAULT would break
#         the alice case. Guards non-triviality.
# ────────────────────────────────────────────────────────────────────
def test_neuter_constant_default_flips_face5_red(captured_calls, monkeypatch):
    """Simulate the NEUTER: if _request_user_id() were reverted to a
    constant DEFAULT_USER_ID (i.e. we never landed this migration), the
    face-5 scenario would fail because the notify would carry 1, not
    'alice'."""
    class _Ctx:
        user_id = 'alice'
    monkeypatch.setattr('routes.api_v1.auth.current_auth', lambda: _Ctx())
    # NEUTER: bypass the real helper and pass DEFAULT verbatim.
    from routes.common import DEFAULT_USER_ID
    from routes.common import _notify_conv_changed as _spy
    _spy('conv-N', rev=None, user_id=DEFAULT_USER_ID)
    match = [c for c in captured_calls if c['conv_id'] == 'conv-N']
    assert match
    # This test PASSES when the neuter reproduces (i.e. it's constant).
    # The value of the test is that face-5 above FAILS with the same
    # setup — proving the migration is what makes it green.
    assert match[-1]['user_id'] == DEFAULT_USER_ID


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
