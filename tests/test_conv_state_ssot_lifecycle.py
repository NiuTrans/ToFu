#!/usr/bin/env python3
"""pt_conv_state_ssot — P3: task lifecycle 4 hooks broadcast full snapshot.

Owner's diagnosis (2026-07-24) — "P2 只覆盖了 send 侧点亮,完成侧熔灯要靠 P3
task lifecycle stop hook 才通":

Audit of existing paths (kept green as regression fixtures below):

  * create_task    — Already covered by the existing _notify_conv_changed
                     calls in every entry point (chat_send / regen /
                     continue / queue-dispatch / autopilot / dispatch).
                     After P1 landed, those frames carry runningTaskIds
                     with the new tid.
  * happy-path stop — persist_task_result() → _sync_result_to_conversation()
                     already emits notify_conv_changed(rev=...); the
                     payload now (post-P1) reads snapshot_running_by_conv
                     AFTER the task's status flipped to 'done', so the
                     tid is filtered out. Frame received by clients,
                     dot extinguishes.
  * reap_stuck     — _finalize_reaped_stuck_task() runs the SAME
                     _sync_result_to_conversation as happy-path. Same
                     result: frame emitted, tid absent.
  * supersede abort — GAP. abort_running_tasks_for_conv() flips
                     t['aborted']=True and writes the aborted terminal
                     floor via _write_aborted_terminal_floor (a pure DB
                     upsert). It does NOT call notify_conv_changed.
                     So a sibling device holding the busy dot for the
                     superseded task sees it stay lit until its next
                     poll (25/90s later) or until the NEW task
                     eventually emits its own notify. This is the
                     "completion-side extinguish" gap owner flagged.

This suite is failing-first for the ONE new bit P3 must add:

  1. abort_running_tasks_for_conv MUST emit a notify_conv_changed frame
     for the conv AFTER the aborted floor is written, so clients get
     the fresh runningTaskIds projection (which no longer includes the
     aborted tid — snapshot_running_by_conv filters
     status='running' && !aborted).
  2. The frame's payload must carry the CURRENT full projection (owner
     hard constraint #3 — "read FULL current registry snapshot for the
     conv, never compute incrementally"), NOT just "removed tid=X".
  3. If NO tasks were aborted (nothing to do), NO frame is emitted —
     otherwise every no-op super sweep would spam the channel.
  4. Idempotent under multi-conv abort: aborting all tasks of conv-A
     emits ONE frame for conv-A, not one per aborted tid.

Regression coverage (already-green paths, guarded here as fixtures so a
future refactor cannot silently regress them):

  5. persist_task_result -> _sync_result_to_conversation continues to
     emit a notify frame (existing behaviour, guards owner's happy-path
     completion-side extinguish claim).
  6. reap-stuck's _finalize_reaped_stuck_task continues to emit a
     notify frame via the same seam.

We monkeypatch push_event (the outbound seam) — same pattern as
test_conv_changed_notify.py + test_conv_state_ssot_payload.py — so we
inspect the payload directly without ASGI wiring.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

# Route handlers import ``flask`` — the project runs them on the Quart shim.
import quart as _quart
sys.modules.setdefault('flask', _quart)


@pytest.fixture
def captured(monkeypatch):
    frames = []

    def _fake_push_event(channel, task_id, payload):
        frames.append({'channel': channel, 'taskId': task_id, 'payload': payload})

    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake_push_event)
    return frames


@pytest.fixture
def isolated_registry(monkeypatch):
    """Wipe the shared task registry so a test that seeds two fake tasks
    can't be polluted by leftovers from previous tests. Also stub out the
    aborted-floor DB write so we don't need a full DB. """
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    with _tl:
        _tasks.clear()
    # No-op the DB floor writer — we're testing the notify seam, not persist.
    import lib.tasks_pkg.manager._registry as reg_mod
    monkeypatch.setattr(reg_mod, '_write_aborted_terminal_floor', lambda t: None)
    yield reg_mod
    with _tl:
        _tasks.clear()


def _seed_task(tid, conv_id, **extra):
    """Insert a running task fake into the registry."""
    from lib.tasks_pkg.manager._state import tasks as _tasks, tasks_lock as _tl
    t = {
        'id': tid, 'convId': conv_id, 'status': 'running',
        'aborted': False, 'created_at': time.time(),
        '_t_last_event': time.time(), '_dispatch_heartbeat': time.time(),
    }
    t.update(extra)
    with _tl:
        _tasks[tid] = t
    return t


# ─────────────────────────────────────────────────────────────────────
# Face 1: supersede abort emits a notify_conv_changed frame
# ─────────────────────────────────────────────────────────────────────
def test_supersede_abort_emits_notify_frame(captured, isolated_registry):
    """When abort_running_tasks_for_conv aborts a stale task, it must
    push a notify frame so sibling devices extinguish the dot without
    waiting for the 25/90s poll fallback."""
    from lib.tasks_pkg.manager._registry import abort_running_tasks_for_conv
    _seed_task('tid-stale', 'conv-X')
    _seed_task('tid-new', 'conv-X')
    n = abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-new')
    assert n == 1, 'exactly one stale task should have been aborted'
    notify_frames = [f for f in captured
                     if f['channel'] == 'notify' and f['taskId'] == 'conv-X']
    assert len(notify_frames) >= 1, (
        'supersede abort must emit at least one notify frame; got %r' %
        [f['payload'].get('type') for f in captured])


# ─────────────────────────────────────────────────────────────────────
# Face 2: the frame carries the current FULL projection (not "removed tid=X")
# ─────────────────────────────────────────────────────────────────────
def test_supersede_notify_carries_current_projection(captured, isolated_registry):
    """runningTaskIds must be the CURRENT projection — 'tid-new' still
    live in the registry, 'tid-stale' now marked aborted so filtered
    out. Owner constraint #3: read FULL current registry snapshot."""
    from lib.tasks_pkg.manager._registry import abort_running_tasks_for_conv
    _seed_task('tid-stale', 'conv-X')
    _seed_task('tid-new', 'conv-X')
    abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-new')
    notify_frames = [f for f in captured if f['channel'] == 'notify']
    assert notify_frames
    p = notify_frames[-1]['payload']
    assert 'runningTaskIds' in p
    assert 'tid-new' in p['runningTaskIds'], (
        'the surviving task must still appear in the projection: %r' %
        p['runningTaskIds'])
    assert 'tid-stale' not in p['runningTaskIds'], (
        'the just-aborted task must NOT appear in the projection: %r' %
        p['runningTaskIds'])


# ─────────────────────────────────────────────────────────────────────
# Face 3: no aborts → no frame (don't spam the channel on every no-op sweep)
# ─────────────────────────────────────────────────────────────────────
def test_supersede_noop_emits_no_frame(captured, isolated_registry):
    """If the abort sweep found nothing to abort (fresh create with no
    prior task), it must NOT emit a notify frame. Every create_task
    calls abort_running_tasks_for_conv unconditionally so a spurious
    frame per create would double the notify load fleet-wide."""
    from lib.tasks_pkg.manager._registry import abort_running_tasks_for_conv
    _seed_task('tid-only', 'conv-X')
    # No other task to abort — the sweep is a no-op.
    n = abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-only')
    assert n == 0
    notify_frames = [f for f in captured if f['channel'] == 'notify']
    assert not notify_frames, (
        'a no-op abort sweep must emit no notify frame; got %r' %
        [f['payload'] for f in notify_frames])


# ─────────────────────────────────────────────────────────────────────
# Face 4: aborting N tasks on same conv emits ONE frame (not N)
# ─────────────────────────────────────────────────────────────────────
def test_supersede_multi_abort_emits_single_frame(captured, isolated_registry):
    """Aborting multiple tasks in one sweep must consolidate to ONE
    notify frame for the conv — otherwise a big supersede floods the
    channel with N-1 stale frames per conv."""
    from lib.tasks_pkg.manager._registry import abort_running_tasks_for_conv
    for i in range(4):
        _seed_task(f'tid-old-{i}', 'conv-X')
    _seed_task('tid-new', 'conv-X')
    n = abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-new')
    assert n == 4
    notify_frames = [f for f in captured
                     if f['channel'] == 'notify' and f['taskId'] == 'conv-X']
    assert len(notify_frames) == 1, (
        'multi-abort must emit exactly one notify frame; got %d' %
        len(notify_frames))


# ─────────────────────────────────────────────────────────────────────
# Face 5: regression — abort still writes DB floor even after new hook
# ─────────────────────────────────────────────────────────────────────
def test_abort_still_writes_floor(captured, isolated_registry, monkeypatch):
    """The new notify emit must NOT displace the existing DB terminal
    floor write. Order matters: floor first (durable), notify second
    (best-effort real-time). If notify raises, floor MUST still land."""
    import lib.tasks_pkg.manager._registry as reg_mod
    floor_calls = []
    monkeypatch.setattr(reg_mod, '_write_aborted_terminal_floor',
                        lambda t: floor_calls.append(t.get('id')))
    _seed_task('tid-stale', 'conv-X')
    _seed_task('tid-new', 'conv-X')
    reg_mod.abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-new')
    assert 'tid-stale' in floor_calls, (
        'the DB terminal floor write must still fire — got %r' % floor_calls)


# ─────────────────────────────────────────────────────────────────────
# Face 6: notify failure never breaks the abort path (fail-open)
# ─────────────────────────────────────────────────────────────────────
def test_notify_failure_does_not_break_abort(monkeypatch, isolated_registry):
    """If push_event raises, the abort sweep must still complete —
    otherwise a broken push transport takes down every supersede path
    (send / regen / continue / queue-dispatch / autopilot)."""
    import lib.agent_core.push as push_mod

    def _boom(*a, **k):
        raise RuntimeError('push transport down')
    monkeypatch.setattr(push_mod, 'push_event', _boom)
    from lib.tasks_pkg.manager._registry import abort_running_tasks_for_conv
    _seed_task('tid-stale', 'conv-X')
    _seed_task('tid-new', 'conv-X')
    n = abort_running_tasks_for_conv('conv-X', exclude_task_id='tid-new')
    assert n == 1, 'abort must still complete even when notify raises'


# ─────────────────────────────────────────────────────────────────────
# Faces 7-10: the USER-STOP route (chat_abort) must broadcast too
#
# The three existing emit sites — create/entry-point frames, the supersede
# sweep (Face 1), and the terminal seam (notify_terminal_busy_state) — all
# miss ONE path: the composer's own Stop button (POST
# /api/v1/chat/abort/<task_id> → routes.chat_poll_abort.chat_abort).
# chat_abort flips task['aborted']=True (so the projection excludes the tid
# from that instant — conv_has_work_in_flight: "aborted always wins") but
# never EMITS a frame, so the originating tab's
# conv._authoritativeActiveTaskIds keeps the tid after finishStream cleared
# the local handles (activeStreams + conv.activeTaskId). convIsBusy then
# keeps the composer in Stop shape while Priority-3 of the stop cascade has
# no handle left — every further click is a silent no-op until the task
# fully unwinds and the TERMINAL frame lands (up to a whole tool call
# later). User report: "暂停按钮要点击多次才生效".
# ─────────────────────────────────────────────────────────────────────
def _make_abort_app():
    """Minimal Quart app for test_request_context (PROVIDE_AUTOMATIC_OPTIONS
    patch replicated from tests/test_api_response.py — bare Quart() raises
    KeyError on the installed sansio without it)."""
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


def _call_chat_abort(tid):
    """Drive the REAL route handler (decorators included) inside a request
    context with an authenticated admin ctx — the same shape the cookie-
    authenticated UI arrives with."""
    from quart import g
    from lib.api_keys import AuthContext
    app = _make_abort_app()

    async def _t():
        async with app.test_request_context(
                f'/api/v1/chat/abort/{tid}', method='POST'):
            g.auth_ctx = AuthContext(key_id='k-test', name='t',
                                     scopes=frozenset({'admin'}))
            from routes.chat_poll_abort import chat_abort
            resp = chat_abort(tid)
            # api_ok() → (Response, 200); read the status off the tuple.
            return resp[1] if isinstance(resp, tuple) else 200
    return asyncio.run(_t())


# ─────────────────────────────────────────────────────────────────────
# Face 7: user-Stop emits a notify frame for the task's conv
# ─────────────────────────────────────────────────────────────────────
def test_user_stop_route_emits_notify_frame(captured, isolated_registry):
    """POST /api/v1/chat/abort/<tid> must push a notify frame for the
    aborted task's conv — the same completion-side extinguish the
    supersede sweep got in P3. Without it the composer's Stop button
    stays lit (authoritative busy Set never cleared) until the task
    finishes unwinding."""
    _seed_task('tid-stop', 'conv-Y')
    status = _call_chat_abort('tid-stop')
    assert status == 200
    notify_frames = [f for f in captured
                     if f['channel'] == 'notify' and f['taskId'] == 'conv-Y']
    assert len(notify_frames) >= 1, (
        'user-Stop (chat_abort) must emit at least one notify frame for '
        'conv-Y; got %r' % [f['payload'].get('type') for f in captured])


# ─────────────────────────────────────────────────────────────────────
# Face 8: the frame carries the CURRENT projection (aborted tid excluded)
# ─────────────────────────────────────────────────────────────────────
def test_user_stop_notify_carries_current_projection(captured, isolated_registry):
    """runningTaskIds on the user-Stop frame must be the fresh registry
    projection: the just-aborted tid filtered out (conv_has_work_in_flight
    → aborted wins), a sibling live task still present."""
    _seed_task('tid-stop', 'conv-Y')
    _seed_task('tid-live', 'conv-Y')
    _call_chat_abort('tid-stop')
    notify_frames = [f for f in captured if f['channel'] == 'notify']
    assert notify_frames
    p = notify_frames[-1]['payload']
    assert 'runningTaskIds' in p, (
        'user-Stop frame must carry runningTaskIds: %r' % p)
    assert 'tid-stop' not in p['runningTaskIds'], (
        'the just-aborted task must NOT appear in the projection: %r' %
        p['runningTaskIds'])
    assert 'tid-live' in p['runningTaskIds'], (
        'the surviving sibling task must still appear: %r' %
        p['runningTaskIds'])


# ─────────────────────────────────────────────────────────────────────
# Face 9: a DUPLICATE abort re-emits the frame (corrective re-broadcast)
# ─────────────────────────────────────────────────────────────────────
def test_user_stop_duplicate_abort_reemits(captured, isolated_registry):
    """A second click on Stop (task already aborted) must STILL emit the
    frame: the client clicking again is precisely the one whose local busy
    Set never cleared (it missed the first frame), so the duplicate abort
    doubles as a corrective re-broadcast of the idle projection."""
    _seed_task('tid-stop', 'conv-Y')
    _call_chat_abort('tid-stop')
    first = len([f for f in captured
                 if f['channel'] == 'notify' and f['taskId'] == 'conv-Y'])
    assert first >= 1
    _call_chat_abort('tid-stop')  # duplicate — was_already_aborted branch
    second = len([f for f in captured
                  if f['channel'] == 'notify' and f['taskId'] == 'conv-Y'])
    assert second > first, (
        'a duplicate user-Stop must re-emit the busy projection '
        '(got %d frame(s) before, %d after)' % (first, second))


# ─────────────────────────────────────────────────────────────────────
# Face 10: notify failure never breaks the user-Stop route (fail-open)
# ─────────────────────────────────────────────────────────────────────
def test_user_stop_notify_failure_does_not_break_abort(monkeypatch, captured,
                                                       isolated_registry):
    """A push-transport failure must never turn the user's Stop click into
    a 500 — the abort flag + subprocess kill are the load-bearing halves;
    the broadcast is best-effort (notify_conv_changed itself is fail-open
    too, this guards the route's own import/call seam)."""
    import lib.agent_core.push as push_mod

    def _boom(*a, **k):
        raise RuntimeError('push transport down')
    monkeypatch.setattr(push_mod, 'push_event', _boom)
    t = _seed_task('tid-stop', 'conv-Y')
    status = _call_chat_abort('tid-stop')
    assert status == 200, 'abort route must still complete when notify raises'
    assert t.get('aborted') is True, 'the abort flag must still be set'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
