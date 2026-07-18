"""tests/test_timer_orphan_retire.py — root-cause fixes for the "abandoned
conversations suddenly pop up" bug (timer subsystem).

Two timer execution paths share ONE ``status='active'`` marker:

  * INLINE (``executor/_timer.py::_execute_timer_create``) — the only path
    ``timer_create`` uses today. It BLOCKS its parent task and polls inline;
    when the condition is met it returns the result INTO the still-running
    parent LLM loop (no new turn injected). Its life is bound to the parent
    task, which is in-memory → it dies with the process on restart.
  * BACKGROUND (``timer/_loop.py::_execute_continuation``) — a self-driving
    injector that, when ready, INJECTS a brand-new user+assistant turn into
    the target conversation and calls ``notify_conv_changed`` (bumping rev →
    the conv jumps to the top of the sidebar).

The bug: ``resume_active_timers`` re-spawned EVERY active row as a background
injector. A parent-blocking inline timer whose parent died on restart is an
ORPHAN — re-spawning it makes it inject a follow-up turn into a conversation
the user has long left, floating abandoned conversations back to the top.

These tests pin the root-cause fix:
  1. A resumed orphan INLINE timer is RETIRED (status → 'orphaned'), never
     re-spawned, never injects, never bumps rev.
  2. A BACKGROUND timer is still resumed (we don't over-retire).
  3. Deleting a conversation cascade-cancels its timer rows, so a deleted
     conv's timer can never be resurrected on the next restart.

Uses the session SQLite DB from conftest (TOFU_DB_PATH). Pattern-gated cleanup
so no synthetic ``active`` row leaks into a shared/production DB.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import lib.scheduler.timer as timer_mod
from lib.database import DOMAIN_SYSTEM, get_thread_db

pytestmark = pytest.mark.unit

_CONV = 'conv-timer-orphan-retire'


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    try:
        db = get_thread_db(DOMAIN_SYSTEM)
        db.execute('DELETE FROM timer_watchers WHERE conv_id=?', [_CONV])
        db.commit()
    except Exception:
        pass


def _new_timer(origin='inline', poll_interval=10, max_polls=120):
    t = timer_mod.create_timer(
        conv_id=_CONV,
        check_instruction='Is the run finished?',
        continuation_message='Summarize the results.',
        poll_interval=poll_interval,
        max_polls=max_polls,
        check_command='',
        tools_config={},
        source_task_id='task-dead-on-restart',
        origin=origin,
    )
    return t['id']


def _set_created_at(timer_id, dt):
    db = get_thread_db(DOMAIN_SYSTEM)
    db.execute('UPDATE timer_watchers SET created_at=? WHERE id=?',
               [dt.isoformat(), timer_id])
    db.commit()


def _status(timer_id):
    row = timer_mod._get_timer_row(timer_id)
    return row['status'] if row else None


# ── Requirement 1: create_timer stamps origin='inline' by default ───────────

def test_create_timer_defaults_origin_inline():
    tid = _new_timer()  # no origin override
    row = timer_mod._get_timer_row(tid)
    assert row is not None
    assert row['origin'] == 'inline', (
        'timer_create is inherently inline (parent-blocking) — every timer '
        'created today must be stamped origin=inline')


# ── Requirement 2: resumed orphan inline timer is retired, never injects ────

def test_resume_retires_orphan_inline_timer_without_injecting(monkeypatch):
    """A fresh, active, origin='inline' timer at resume time is an ORPHAN
    (its parent task died with the process). Resume must RETIRE it
    (status → 'orphaned'), NOT re-spawn it as a background injector — so it
    never injects a follow-up turn and never bumps the conv's rev."""
    tid = _new_timer(origin='inline')
    _set_created_at(tid, datetime.now())  # fresh → survives the age-sweep

    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda t: spawned.append(t))

    # inject_and_run_task is the ONLY seam that appends a new turn + bumps rev
    # (via notify_conv_changed). It must NEVER be invoked for a resumed orphan.
    injected: list[str] = []
    import lib.scheduler._shared as _shared
    monkeypatch.setattr(_shared, 'inject_and_run_task',
                        lambda **kw: injected.append(kw.get('conv_id')) or 'task-x')

    timer_mod.resume_active_timers()

    assert _status(tid) == 'orphaned', (
        'a resumed orphan inline timer must be retired to status=orphaned')
    assert tid not in spawned, 'an orphan inline timer must NOT be re-spawned'
    assert injected == [], (
        'a resumed orphan inline timer must NOT inject a follow-up turn '
        '(no rev bump → no sidebar resurrection)')


def test_resume_skips_triggered_inline_timer(monkeypatch):
    """An inline timer that already fired in-process is written to a terminal
    status ('triggered'). resume_active_timers only selects status='active', so
    such a row must be skipped entirely — neither re-spawned nor mark_orphaned.
    This keeps the orphan definition strict: ONLY status='active' inline rows
    are orphans; a settled 'triggered' row is done, not dirty."""
    tid = _new_timer(origin='inline')
    _set_created_at(tid, datetime.now())
    # Simulate a normal in-process trigger writing the terminal state.
    db = get_thread_db(DOMAIN_SYSTEM)
    db.execute("UPDATE timer_watchers SET status='triggered' WHERE id=?", [tid])
    db.commit()

    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda t: spawned.append(t))
    orphaned_calls: list[str] = []
    monkeypatch.setattr(timer_mod, '_mark_orphaned',
                        lambda t: orphaned_calls.append(t))

    timer_mod.resume_active_timers()

    assert _status(tid) == 'triggered', 'a triggered row stays triggered on resume'
    assert tid not in spawned, 'a triggered timer must NOT be re-spawned'
    assert tid not in orphaned_calls, 'a terminal timer must NOT be re-orphaned'


def test_resume_respawns_background_timer(monkeypatch):
    """A genuine background injector timer is still resumed (don't over-retire
    everything — the long-running 'notify me when X finishes' value survives)."""
    tid = _new_timer(origin='background')
    _set_created_at(tid, datetime.now())

    spawned: list[str] = []
    monkeypatch.setattr(timer_mod, 'start_timer_loop',
                        lambda t: spawned.append(t))

    timer_mod.resume_active_timers()

    assert _status(tid) == 'active', 'a background timer stays active on resume'
    assert tid in spawned, 'a background timer must be re-spawned on resume'


# ── Requirement 3: delete_conv cascade-cancels its timers ───────────────────

def test_delete_conv_cascade_cancels_timer(flask_app):
    """Deleting a conversation must cancel its timer_watchers rows so a deleted
    conv's timer can never be resurrected by resume_active_timers()."""
    import time as _time

    from lib.database import DOMAIN_CHAT, get_thread_db as _get_db
    from routes.conversations import _delete_conv_blocking

    with flask_app.app_context():
        chatdb = _get_db(DOMAIN_CHAT)
        now_ms = int(_time.time() * 1000)
        chatdb.execute(
            'INSERT INTO conversations (id, user_id, title, messages, settings, '
            'created_at, updated_at) VALUES (?,1,?,?,?,?,?)',
            (_CONV, 'orphan-cascade', '[]', '{}', now_ms, now_ms))
        chatdb.commit()

        tid = _new_timer(origin='inline')
        assert _status(tid) == 'active'

        _delete_conv_blocking(chatdb, _CONV)

        assert _status(tid) == 'cancelled', (
            'deleting a conversation must cascade-cancel its active timers')
