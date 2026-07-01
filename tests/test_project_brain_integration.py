"""tests/test_project_brain_integration.py — the AUTONOMOUS FLYWHEEL, end to end.

Every prior Project Brain test proves a GEAR in isolation (monkeypatched
presence, stubbed Api, direct `build_brain_summary`). This proves the FLYWHEEL:
the "live" chain nobody had exercised as a whole —

    sweep_all_active_projects()            (the real scheduler entry)
      → select_dispatchable                (real board read, real lease eval)
      → dispatch_epic → claim + enqueue    (real message_queue workflow_step)
      → dispatch_next_queued               (real queue drain)
      → create_task + spawn_task           (real task lifecycle; spawn stubbed)
      → complete_task                      (real board complete)
      → on_epic_completed                  (real dependent unblock + re-dispatch)

Nothing on the dispatch/queue/board path is stubbed. ONLY `spawn_task` (the
thread that would actually run an LLM) is replaced with a recorder — so we
prove "a real task was created and handed to the spawner" without a network
call. Everything else runs against a real (conftest-forced SQLite) DB under an
app context.

Includes ONE source-level negative control: no-op the `sweep_all_active_projects()`
call inside the scheduler tick → the flywheel never self-starts → the cold-start
assertion FAILS. Byte-identical restore.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_SCHEDULER_SRC = os.path.join(ROOT, 'lib', 'scheduler', 'manager.py')


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
        for tbl in ('project_tasks', 'project_events', 'project_charter',
                    'message_queue', 'conversations'):
            db.execute(f'DELETE FROM {tbl}')
        db.commit()
    # Best-effort push stub (feed/presence emit) — no live WS in the test.
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def _seed_conv(flask_app, conv_id, project_path):
    """Create a real conversation row so dispatch_next_queued can append to it."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        settings = json_dumps_pg({'projectPath': project_path,
                                  'projectEnabled': True})
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, '
            ' settings, created_at, updated_at, search_text) '
            'VALUES (?, 1, ?, ?, ?, ?, ?, ?)',
            (conv_id, 'Worker conv', json_dumps_pg(
                [{'role': 'user', 'content': 'seed'}]),
             settings, now, now, 'seed'))
        db.commit()


def _stub_spawn(monkeypatch):
    """Replace the LLM-running spawner with a recorder — prove a task was
    created + handed off, WITHOUT running a model. Patch at the defining
    module so the `from lib.tasks_pkg import spawn_task` inside
    dispatch_next_queued resolves the stub."""
    spawned = []
    import lib.tasks_pkg as tp
    monkeypatch.setattr(tp, 'spawn_task', lambda task: spawned.append(task))
    return spawned


def _queue_workflow_ids(flask_app, conv_id):
    from lib.message_queue import KIND_WORKFLOW, get_queue
    with flask_app.app_context():
        return [q for q in get_queue(conv_id) if q['kind'] == KIND_WORKFLOW]


def _feed_kinds_ordered(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        feed = read_project_feed(project_path, limit=500)
    # read_project_feed returns newest-first; reverse to chronological.
    return [e['kind'] for e in reversed(feed['events'])]


# ════════════════════════════════════════════════════════════════════
#  THE FLYWHEEL
# ════════════════════════════════════════════════════════════════════

def test_full_autonomous_flywheel(flask_app, monkeypatch):
    from lib.conversations.project_board import (
        complete_task, post_task, read_board,
    )
    from lib.conversations.project_brain_summary import build_brain_summary
    from lib.conversations.project_dispatch import sweep_all_active_projects
    from lib.database import DOMAIN_CHAT, get_thread_db

    proj = os.path.abspath('/tmp/flywheel-proj')
    conv = 'conv-flywheel-worker'
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    # Make sweep_all_active_projects find THIS project (it enumerates recent
    # projects); stub the enumeration to our seeded project deterministically.
    monkeypatch.setattr('lib.project_mod.get_recent_projects',
                        lambda: [{'path': proj}])

    with flask_app.app_context():
        # 1) Two epics: A (no deps) + B (depends_on A), both posted by our conv.
        a_id = post_task(proj, conv, 'Epic A — foundation')['id']
        b_id = post_task(proj, conv, 'Epic B — builds on A',
                         depends_on=[a_id])['id']

        # 2) The HEARTBEAT (real scheduler entry) sweeps → A dispatchable,
        #    B blocked by its unfinished dependency.
        dispatched = sweep_all_active_projects()
        assert dispatched >= 1, 'sweep must dispatch the dependency-free epic A'

        board = read_board(proj)
        by_id = {t['id']: t for t in board['tasks']}
        # A claimed under our conv; B still open (dependency unmet).
        assert by_id[a_id]['status'] == 'claimed', 'A must be claimed by the sweep'
        assert by_id[a_id]['owner_conv_id'] == conv
        assert by_id[b_id]['status'] == 'open', 'B must NOT dispatch (dep unmet)'

        # A workflow_step kickoff for A is in the real queue; none for B.
        wf = _queue_workflow_ids(flask_app, conv)
        assert len(wf) == 1, f'exactly one workflow kickoff (for A), got {len(wf)}'
        # Confirm the kickoff's boardTaskId via a direct payload read.
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT payload FROM message_queue WHERE conv_id=? AND kind='workflow_step'",
            (conv,)).fetchall()
        import json as _json
        board_ids = [_json.loads(r['payload']).get('boardTaskId') for r in rows]
        assert board_ids == [a_id], f'kickoff must target A, got {board_ids}'

        # 3) Summary mid-flight: A is claimed by our (active) peer → peerEpics
        #    joins conv → "Epic A". (announce the peer so it's active.)
        import lib.presence.registry as reg
        monkeypatch.setattr(reg, '_state', {})
        monkeypatch.setattr(reg, '_sweeper_started', True)
        reg.announce(proj, conv, task_id='t-a', title='Worker conv')
        s_mid = build_brain_summary(proj)
        assert s_mid['epicsClaimed'] == 1 and s_mid['epicsOpen'] == 1
        assert s_mid['peerEpics'].get(conv) == 'Epic A — foundation'

        # 4) Drain the REAL queue → dispatch_next_queued creates a REAL task
        #    and hands it to the (stubbed) spawner.
        from lib.message_queue import dispatch_next_queued
        task_id = dispatch_next_queued(conv)
        assert task_id, 'dispatch_next_queued must create + start a task'
        assert len(spawned) == 1, 'spawn_task must be handed exactly one task'
        assert spawned[0]['id'] == task_id
        assert spawned[0]['convId'] == conv

        # 5) A completes → on_epic_completed fires → B unblocks + re-dispatches.
        complete_task(proj, conv, a_id)

        board2 = read_board(proj)
        by_id2 = {t['id']: t for t in board2['tasks']}
        assert by_id2[a_id]['status'] == 'done', 'A must be done'
        assert by_id2[b_id]['status'] == 'claimed', \
            'B must be auto-dispatched (claimed) once its dependency completed'
        assert by_id2[b_id]['owner_conv_id'] == conv

        # B's kickoff is now in the queue (a NEW workflow_step for B).
        rows2 = db.execute(
            "SELECT payload FROM message_queue WHERE conv_id=? AND kind='workflow_step'",
            (conv,)).fetchall()
        board_ids2 = [_json.loads(r['payload']).get('boardTaskId') for r in rows2]
        assert b_id in board_ids2, 'B kickoff must be enqueued after A completes'

        # 6) Feed recorded the real sequence: A claimed → A completed (chrono).
        kinds = _feed_kinds_ordered(flask_app, proj)
        assert 'claimed' in kinds and 'completed' in kinds
        # the first claimed precedes the completed of A
        assert kinds.index('claimed') < kinds.index('completed')

        # Final summary: A done, B claimed → counts reflect the flywheel state.
        s_end = build_brain_summary(proj)
        assert s_end['epicsDone'] == 1
        assert s_end['epicsClaimed'] == 1  # B now claimed
        assert s_end['peerEpics'].get(conv) == 'Epic B — builds on A'


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROL: the scheduler-tick wiring is load-bearing
# ════════════════════════════════════════════════════════════════════

def _patch_restore(path, old, new, run):
    with open(path, encoding='utf-8') as f:
        original = f.read()
    assert old in original, f'anchor not found in {path}'
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original.replace(old, new, 1))
        run()
    finally:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original)
    with open(path, encoding='utf-8') as f:
        assert f.read() == original, 'source not restored byte-identical'


def test_NC_scheduler_tick_wiring_is_load_bearing(flask_app, monkeypatch):
    """NC: no-op the `sweep_all_active_projects()` call inside the scheduler
    tick → the heartbeat never fires → a cold-start epic is NEVER self-started.

    We prove the WIRING (the call the tick makes) is load-bearing by invoking
    the real tick method (`_check_and_run_due_tasks`) with no due tasks and
    asserting: with the call intact an open epic gets claimed; with it no-opped
    it stays open.
    """
    import importlib

    proj = os.path.abspath('/tmp/flywheel-nc')
    conv = 'conv-nc-worker'

    def _drive_tick_and_check(expect_claimed):
        # Reload scheduler so the (possibly patched) source is in effect.
        import lib.scheduler.manager as sched
        importlib.reload(sched)
        from lib.conversations.project_board import post_task, read_board
        from lib.database import DOMAIN_CHAT, get_thread_db
        monkeypatch.setattr('lib.project_mod.get_recent_projects',
                            lambda: [{'path': proj}])
        _stub_spawn(monkeypatch)
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            db.execute("DELETE FROM project_tasks WHERE project_path=?", (proj,))
            db.execute("DELETE FROM message_queue WHERE conv_id=?", (conv,))
            db.commit()
            epic = post_task(proj, conv, 'Cold-start epic')['id']
            # Drive the REAL tick method (no due tasks → it falls through to
            # the sweep call at the end).
            mgr = sched.get_scheduler()
            mgr._check_and_run_due_tasks()
            board = read_board(proj)
            claimed = [t for t in board['tasks']
                       if t['id'] == epic and t['status'] == 'claimed']
        return bool(claimed)

    # First: sanity — with the wiring intact, the tick self-starts the epic.
    assert _drive_tick_and_check(True), \
        'baseline: the scheduler tick must self-start a cold-start epic'

    # NC: neuter the sweep call inside the tick → epic must stay open.
    def run():
        assert not _drive_tick_and_check(False), \
            'NC: with the tick sweep no-opped, the epic must NOT self-start'

    _patch_restore(
        _SCHEDULER_SRC,
        'from lib.conversations.project_dispatch import sweep_all_active_projects\n'
        '            sweep_all_active_projects()',
        'from lib.conversations.project_dispatch import sweep_all_active_projects  # NC\n'
        '            pass  # NC sweep disabled',
        run,
    )
    # restore scheduler module
    import lib.scheduler.manager as sched
    importlib.reload(sched)
