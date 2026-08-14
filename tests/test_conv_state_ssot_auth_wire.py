#!/usr/bin/env python3
"""pt_conv_state_ssot P7 (pt_ab42421158214591) — WebSocket auth wire.

Closes the latent multi-tenant leak that pt_ab42421158214591 filed:

Before this commit
  * routes/push.py::push_ws() called PushClient() with NO user identity.
  * _handle_client_frame calls build_conv_state_snapshot(user_id=1) with
    a literal constant, ignoring any auth on the WS handshake.
  * snapshot_running_by_conv() returns the ENTIRE registry regardless of
    task ownership.
  * The reducer's cross-user gate is inert because window._currentUserId
    is never set.

The three server-side leaks were latent under single-user default
(DEFAULT_USER_ID=1 everywhere lines up). When auth lands and users see
different ``AuthContext.user_id`` values, tab B would receive a
snapshot built from user A's registry, sidebar-lighting siblings that
belong to another tenant.

Owner design decision (this dispatch): resolve auth ONCE at WebSocket
handshake (Quart's before_request does NOT fire on WS routes, so we
resolve inline from ``websocket.cookies`` + ``websocket.headers``).
Stash user_id on PushClient. Downstream builder + registry read
consult it. The frontend _currentUserId initializer is a separate
concern (needs a boot-sequence design decision), deliberately left for
a follow-up ticket.

Coverage (failing-first, six faces):

  1. PushClient carries a user_id field (stashed at connect time).
  2. build_conv_state_snapshot(user_id=X) returns userId=X in payload
     (identity — no more hardcoded 1).
  3. snapshot_running_by_conv(user_id='u42') filters tasks by
     task['_userId'], excluding tasks owned by different users.
  4. snapshot_running_by_conv(user_id='') = back-compat all-registry
     (write-path callers with no user context still work).
  5. create_task stashes task['_userId'] from current_auth()
     (mirroring the existing _profileScope resolve pattern).
  6. build_conv_state_snapshot passes user_id through to
     snapshot_running_by_conv (the whole point: per-user projection).
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


@pytest.fixture
def clean_registry():
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    with _tl:
        _tasks.clear()
    yield
    with _tl:
        _tasks.clear()


def _seed_task(tid, conv_id, user_id, **extra):
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    t = {
        'id': tid, 'convId': conv_id, 'status': 'running',
        'aborted': False, 'created_at': time.time(),
        '_t_last_event': time.time(), '_dispatch_heartbeat': time.time(),
        '_userId': user_id,
    }
    t.update(extra)
    with _tl:
        _tasks[tid] = t
    return t


# ────────────────────────────────────────────────────────────────────
# Face 1: PushClient carries user_id (connection-scoped identity stash)
# ────────────────────────────────────────────────────────────────────
def test_pushclient_carries_user_id():
    """Owner mandate: 'plumb authenticated user_id from WebSocket session'.
    PushClient must accept a user_id at construction so the WS handshake
    can stash it once and downstream code reads it uniformly."""
    from lib.agent_core.push import PushClient
    c = PushClient(user_id='u42')
    assert c.user_id == 'u42'
    # Empty is the pre-auth default (single-user, personal-install).
    c2 = PushClient()
    assert c2.user_id == ''


# ────────────────────────────────────────────────────────────────────
# Face 2: build_conv_state_snapshot payload carries the passed user_id
# ────────────────────────────────────────────────────────────────────
def test_snapshot_payload_userid_reflects_argument(clean_registry):
    from lib.agent_core.push import build_conv_state_snapshot
    p = build_conv_state_snapshot(user_id='u42')
    assert p['userId'] == 'u42', (
        'build_conv_state_snapshot must NOT hardcode 1 — it must reflect '
        'the passed user_id verbatim, got %r' % p['userId'])
    p2 = build_conv_state_snapshot(user_id='')
    assert p2['userId'] == ''


# ────────────────────────────────────────────────────────────────────
# Face 3: snapshot_running_by_conv filters by user when specified
# ────────────────────────────────────────────────────────────────────
def test_snapshot_running_by_conv_filters_by_user(clean_registry):
    from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
    _seed_task('tid-alice', 'conv-alice', user_id='alice')
    _seed_task('tid-bob',   'conv-bob',   user_id='bob')
    alice_view = snapshot_running_by_conv(user_id='alice')
    bob_view = snapshot_running_by_conv(user_id='bob')
    assert 'conv-alice' in alice_view and alice_view['conv-alice'] == ['tid-alice']
    assert 'conv-bob' not in alice_view, (
        "Alice's snapshot leaked Bob's task — SSOT scoping FAILED")
    assert 'conv-bob' in bob_view and bob_view['conv-bob'] == ['tid-bob']
    assert 'conv-alice' not in bob_view


# ────────────────────────────────────────────────────────────────────
# Face 4: back-compat — no user_id (or '') returns all-registry
# ────────────────────────────────────────────────────────────────────
def test_snapshot_running_by_conv_no_user_returns_all(clean_registry):
    """The write-path callers of notify_conv_changed today pass
    DEFAULT_USER_ID=1 (int). Pre-auth, no per-task user_id is stashed
    (default ''). snapshot_running_by_conv(user_id='') must therefore
    still see every task — otherwise the current single-user
    deployment breaks the moment we land the API change."""
    from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
    _seed_task('tid-a', 'conv-a', user_id='')
    _seed_task('tid-b', 'conv-b', user_id='alice')
    view = snapshot_running_by_conv()  # no arg → all
    assert 'conv-a' in view
    assert 'conv-b' in view
    view2 = snapshot_running_by_conv(user_id='')
    assert view == view2  # explicit '' == default


# ────────────────────────────────────────────────────────────────────
# Face 5: create_task stashes _userId from current_auth()
# ────────────────────────────────────────────────────────────────────
def test_create_task_stashes_user_id(clean_registry, monkeypatch):
    """Mirrors the existing _profileScope resolve pattern at create_task
    time. When current_auth() returns an AuthContext with user_id='u42',
    the freshly-created task carries task['_userId']='u42'."""
    from lib.api_keys._context import AuthContext

    class _FakeCtx:
        user_id = 'u42'

    monkeypatch.setattr('routes.api_v1.auth.current_auth',
                        lambda: AuthContext(key_id='k', user_id='u42'))
    # Also stub the profile scope resolver so it doesn't fail on missing
    # request context (irrelevant to this test; but its own exception
    # path would otherwise mask the _userId stash).
    monkeypatch.setattr('lib.memory.user_profile.resolve_profile_scope',
                        lambda ctx: 'u42')
    from lib.tasks_pkg.manager._registry import create_task
    t = create_task('conv-X', messages=[{'role': 'user', 'content': 'hi'}],
                    config={}, supersede=False)
    try:
        assert t.get('_userId') == 'u42', (
            'create_task must stash current_auth().user_id onto '
            "task['_userId'] so downstream snapshot_running_by_conv "
            'can filter by it. got %r' % t.get('_userId'))
    finally:
        # Registry cleanup handled by clean_registry fixture, but make
        # sure the aborted-floor DB write is not triggered here (we
        # didn't seed a full task shape).
        from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
        with _tl:
            _tasks.pop(t['id'], None)


# ────────────────────────────────────────────────────────────────────
# Face 6: build_conv_state_snapshot passes user_id into registry filter
# ────────────────────────────────────────────────────────────────────
def test_snapshot_full_pipeline_scopes_by_user(clean_registry):
    """End-to-end: build_conv_state_snapshot(user_id='alice') must
    return a convs dict containing only alice's convs — proving the
    param is not just decorative but actually reaches the filter."""
    from lib.agent_core.push import build_conv_state_snapshot
    _seed_task('tid-alice', 'conv-alice', user_id='alice')
    _seed_task('tid-bob',   'conv-bob',   user_id='bob')
    alice = build_conv_state_snapshot(user_id='alice')
    assert 'conv-alice' in alice['convs']
    assert 'conv-bob' not in alice['convs'], (
        "snapshot payload for user 'alice' leaked user 'bob' conv — "
        'the user_id argument is not being threaded into the registry '
        'filter')


# ────────────────────────────────────────────────────────────────────
# Face 7: notify_conv_changed threads user_id into projection
# ────────────────────────────────────────────────────────────────────
def test_notify_conv_changed_scopes_projection_by_user(clean_registry,
                                                       monkeypatch):
    """notify_conv_changed(user_id=X) must produce a runningTaskIds
    payload projected THROUGH user_id=X, not the whole registry.
    Otherwise the write-side leak persists."""
    frames = []
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event',
                        lambda ch, tid, p: frames.append(p))

    _seed_task('tid-alice', 'conv-shared', user_id='alice')
    _seed_task('tid-bob',   'conv-shared', user_id='bob')

    from lib.conversations.meta_cache import notify_conv_changed
    # user_id passed through the seam MUST reach snapshot_running_by_conv.
    # We pass a specific user_id (alice's stringy form) so the
    # projection is scoped. int→str mapping is the seam's responsibility.
    notify_conv_changed('conv-shared', rev=7, user_id='alice')
    assert frames, 'expected a notify frame'
    p = frames[-1]
    tids = p.get('runningTaskIds', [])
    assert 'tid-alice' in tids
    assert 'tid-bob' not in tids, (
        'notify_conv_changed(user_id=alice) leaked tid-bob into the '
        "projection — snapshot_running_by_conv wasn't scoped. tids=%r"
        % tids)


# ────────────────────────────────────────────────────────────────────
# Face 8: reducer normalizes int↔str userId (auth-lands compat)
# ────────────────────────────────────────────────────────────────────
def test_reducer_normalizes_int_str_userid():
    """When auth lands, window._currentUserId is a str
    (AuthContext.user_id). Legacy notify_conv_changed callers still pass
    DEFAULT_USER_ID=1 (int). Without normalization, a single-user tab
    with _currentUserId='1' would silently reject its OWN server's
    userId=1 frames and the sidebar would go dark.

    Drive the shipped conv_state_reducer.js in Node with mock convs and
    assert that both directions of int↔str pass the gate."""
    import os as _os
    import shutil as _shutil
    import subprocess as _sp
    if not _shutil.which('node'):
        pytest.skip('node not installed')
    HERE = _os.path.dirname(_os.path.abspath(__file__))
    ROOT = _os.path.normpath(_os.path.join(HERE, '..'))
    from tests._runtime_sections import runtime_section_path
    reducer = runtime_section_path('core/conv_state_reducer.js')

    script = r"""
const fs = require('fs');
global.window = global;
global.debugLog = () => {};
const src = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Case A: _currentUserId='1' (str), frame.userId=1 (int) → must accept
{
  window._currentUserId = '1';
  const convs = [{ id: 'c1' }];
  applyRunningTaskIdsFrame(convs, {
    convId: 'c1', runningTaskIds: ['t1'],
    runningTaskIdsRev: [10, 'r0'], userId: 1,
  });
  check('str1_accepts_int1',
        convs[0]._authoritativeActiveTaskIds &&
        convs[0]._authoritativeActiveTaskIds.has('t1'));
}
// Case B: _currentUserId=1 (int), frame.userId='1' (str) → must accept
{
  window._currentUserId = 1;
  const convs = [{ id: 'c1' }];
  applyRunningTaskIdsFrame(convs, {
    convId: 'c1', runningTaskIds: ['t2'],
    runningTaskIdsRev: [20, 'r0'], userId: '1',
  });
  check('int1_accepts_str1',
        convs[0]._authoritativeActiveTaskIds &&
        convs[0]._authoritativeActiveTaskIds.has('t2'));
}
// Case C: different real users → REJECT
{
  window._currentUserId = 'alice';
  const convs = [{ id: 'c1' }];
  applyRunningTaskIdsFrame(convs, {
    convId: 'c1', runningTaskIds: ['t3'],
    runningTaskIdsRev: [30, 'r0'], userId: 'bob',
  });
  check('cross_user_rejected',
        !convs[0]._authoritativeActiveTaskIds ||
        !convs[0]._authoritativeActiveTaskIds.has('t3'));
}
// Case D: empty-string identity on either side = unscoped, accept
{
  window._currentUserId = null;
  const convs = [{ id: 'c1' }];
  applyRunningTaskIdsFrame(convs, {
    convId: 'c1', runningTaskIds: ['t4'],
    runningTaskIdsRev: [40, 'r0'], userId: 42,
  });
  check('no_local_id_accepts',
        convs[0]._authoritativeActiveTaskIds &&
        convs[0]._authoritativeActiveTaskIds.has('t4'));
}
console.log(out.join('\n'));
"""
    driver = _os.path.join(HERE, '_ssot_auth_reducer_driver.js')
    with open(driver, 'w') as f:
        f.write(script)
    try:
        proc = _sp.run(['node', driver, reducer], capture_output=True,
                       text=True, timeout=15)
    finally:
        try:
            _os.remove(driver)
        except OSError:
            pass
    assert proc.returncode == 0, f'node driver failed: {proc.stderr}'
    fails = [ln for ln in proc.stdout.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reducer normalization failed:\n' + proc.stdout


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
