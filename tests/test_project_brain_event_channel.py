"""tests/test_project_brain_event_channel.py — the EVENT CHANNEL: the three
producer-side trigger seams that removed the common 30 s heartbeat waits
(owner, 2026-07-27 — "implement the event channel").

Before this, every self-starting flow waited for the scheduler's 30 s tick
(``lib/scheduler/manager.py`` → ``sweep_all_active_projects`` /
``drain_idle_peer_messages``). The sweep stays — as the crash / lease / strand
SAFETY NET — but the common flows now dispatch AT THE EVENT:

  1. ``on_epic_posted`` — ``post_task`` fires an immediate dispatch when the
     epic can genuinely start (open, deps done, routing-target conv EXISTS and
     is IDLE). A busy poster / unmet deps / dead target fall back to the old
     machinery unchanged (no claim-strand into dead convs).
  2. ``on_conv_idle`` — a task completing with an EMPTY queue nudges the board:
     an open epic routed to this conv starts NOW (chained one per completion —
     the same chain shape the queue drain uses).
  3. send-time peer drain — a peer message into an IDLE conv drains at SEND
     time via the same ``dispatch_next_queued`` seam (no 30 s wait); a LIVE
     target keeps the fast-path twin + completion-hook delivery.

Load-bearing negative controls (NC×3): reverting each seam to a no-op must
strand the work back onto the 30 s heartbeat (immediacy assertions fail).
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')
_SYNC_SRC = os.path.join(ROOT, 'lib', 'tasks_pkg', 'manager', '_sync.py')
_PEER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_peer.py')

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


# ────────────────────────────────────────────────────────────────────
#  Fixtures / helpers (mirrors tests/test_project_brain_integration.py)
# ────────────────────────────────────────────────────────────────────

def _clear_task_registry():
    """Wipe the in-proc task registry so a stubbed-spawn task (which never
    completes, so it lingers as 'running') can't make a later conv look busy
    via _conv_has_live_task. Best-effort."""
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            tasks.clear()
    except Exception:
        pass


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
    # Fresh peer rate-limit window per test.
    try:
        import lib.conversations.project_peer as pp
        with pp._rate_lock:
            pp._peer_msg_history.clear()
    except Exception:
        pass
    yield


def _seed_conv(flask_app, conv_id, project_path='', settings=None):
    """Create a real conversation row so dispatch_next_queued can append to it."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        st = {'projectPath': project_path, 'projectEnabled': True} \
            if project_path else (settings or {})
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, '
            ' settings, created_at, updated_at, search_text) '
            'VALUES (?, 1, ?, ?, ?, ?, ?, ?)',
            (conv_id, 'Worker conv', json_dumps_pg(
                [{'role': 'user', 'content': 'seed'}]),
             json_dumps_pg(st), now, now, 'seed'))
        db.commit()


def _stub_spawn(monkeypatch):
    """Replace the LLM-running spawner with a recorder (patched at the package
    so the ``from lib.tasks_pkg import spawn_task`` inside dispatch_next_queued
    resolves the stub)."""
    spawned = []
    import lib.tasks_pkg as tp
    monkeypatch.setattr(tp, 'spawn_task', lambda task: spawned.append(task))
    return spawned


def _mark_busy(conv_id, task_id='busytask0000001'):
    """Register a fake LIVE task so the conv reads busy (the production-real
    shape of an agent posting mid-turn). Clear with _clear_task_registry()."""
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks[task_id] = {'id': task_id, 'convId': conv_id, 'status': 'running',
                          'aborted': False, 'config': {}, 'toolRounds': []}
    return task_id


def _board_row(flask_app, project_path, task_id):
    from lib.conversations.project_board import read_board
    with flask_app.app_context():
        board = read_board(project_path)
    return next((t for t in board['tasks'] if t['id'] == task_id), None)


def _queue_rows(flask_app, conv_id):
    from lib.message_queue import get_queue
    with flask_app.app_context():
        return get_queue(conv_id)


def _persisted_last_user(flask_app, conv_id):
    import json as _json
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        msgs = _json.loads(row['messages'] or '[]') if row else []
    users = [m for m in msgs if m.get('role') == 'user']
    return users[-1] if users else {}


def _terminal_task(conv_id, project_path):
    """A terminal task dict in the shape _dispatch_queued_message consumes."""
    return {'id': 'task-terminal-001', 'convId': conv_id, 'status': 'done',
            'aborted': False,
            'config': {'projectPath': project_path, 'model': 'm'},
            'toolRounds': []}


# ════════════════════════════════════════════════════════════════════
#  SEAM 1 — on_epic_posted: post-time immediate dispatch
# ════════════════════════════════════════════════════════════════════

def test_post_starts_immediately_when_target_idle(flask_app, monkeypatch):
    """THE cold-start kill: posting an epic whose routing target is an IDLE,
    EXISTING conv starts it synchronously inside post_task — claimed, kickoff
    enqueued AND drained, a real task spawned — with ZERO sweep/tick calls."""
    from lib.conversations.project_board import post_task

    proj = os.path.abspath('/tmp/ec-post-idle')
    conv = 'conv-ec-post-idle'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        epic = post_task(proj, conv, 'Start me now')['id']

    row = _board_row(flask_app, proj, epic)
    assert row['status'] == 'claimed', \
        'the epic must be claimed at POST time (no 30 s heartbeat wait)'
    assert row['owner_conv_id'] == conv
    assert len(spawned) == 1, \
        'post_task must drain the kickoff immediately → exactly one task spawned'
    assert spawned[0]['convId'] == conv
    assert (spawned[0]['config'] or {}).get('projectPath') == proj
    assert _queue_rows(flask_app, conv) == [], 'the kickoff must be drained, not queued'
    last_user = _persisted_last_user(flask_app, conv)
    assert last_user.get('_brainDispatch') is True
    assert last_user.get('_boardTaskId') == epic
    _clear_task_registry()


def test_post_with_unmet_deps_waits_for_completion_trigger(flask_app, monkeypatch):
    """A dependent epic is NOT started at post time (its dep is unfinished);
    completing the dependency starts it IMMEDIATELY via on_epic_completed."""
    from lib.conversations.project_board import complete_task, post_task

    proj = os.path.abspath('/tmp/ec-post-deps')
    conv = 'conv-ec-post-deps'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        # Busy poster (the production-real shape: an agent posts mid-turn) so
        # A itself stays open for this test's drive.
        _mark_busy(conv)
        a_id = post_task(proj, conv, 'Epic A — foundation')['id']
        b_id = post_task(proj, conv, 'Epic B — depends on A',
                         depends_on=[a_id])['id']
        _clear_task_registry()

    assert _board_row(flask_app, proj, a_id)['status'] == 'open'
    assert _board_row(flask_app, proj, b_id)['status'] == 'open', \
        'B must NOT dispatch at post time (dependency unmet)'
    assert spawned == []

    with flask_app.app_context():
        complete_task(proj, conv, a_id)

    row_b = _board_row(flask_app, proj, b_id)
    assert row_b['status'] == 'claimed', \
        'completing the dependency must dispatch B IMMEDIATELY (event, not tick)'
    assert len(spawned) == 1
    assert _persisted_last_user(flask_app, conv).get('_boardTaskId') == b_id
    _clear_task_registry()


def test_post_with_busy_target_waits_for_nudge(flask_app, monkeypatch):
    """A BUSY routing target is deliberately NOT claimed/enqueued at post time
    (the completion nudge or the sweep owns it) — no claim-stacking."""
    from lib.conversations.project_board import post_task

    proj = os.path.abspath('/tmp/ec-post-busy')
    conv = 'conv-ec-post-busy'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        _mark_busy(conv)
        epic = post_task(proj, conv, 'Posted mid-turn')['id']

    row = _board_row(flask_app, proj, epic)
    assert row['status'] == 'open', \
        'a busy target must NOT be claimed at post time (nudge/sweep owns it)'
    assert _queue_rows(flask_app, conv) == [], 'no kickoff stacked into a busy conv'
    assert spawned == []
    _clear_task_registry()


def test_post_with_missing_target_conv_keeps_heartbeat_behaviour(flask_app,
                                                                 monkeypatch):
    """A routing target with NO conversation row is NOT claimed at post time —
    dispatch_epic claims FIRST, so claiming into a dead conv would strand the
    epic until lease expiry (worse than the ≤30 s sweep it replaces). The
    sweep's claim/migration path owns that shape; the epic stays open."""
    from lib.conversations.project_board import post_task

    proj = os.path.abspath('/tmp/ec-post-dead')
    spawned = _stub_spawn(monkeypatch)
    with flask_app.app_context():
        epic = post_task(proj, 'conv-does-not-exist', 'Dead target')['id']

    row = _board_row(flask_app, proj, epic)
    assert row['status'] == 'open', \
        'a dead target must NOT be claim-stranded at post time'
    assert spawned == []
    _clear_task_registry()


def test_reopen_starts_immediately_when_target_idle(flask_app, monkeypatch):
    """The human's revive lever rides the SAME event seam (owner, 2026-07-27):
    an epic reopened (done → open) whose routing target EXISTS and is IDLE
    starts AT reopen time — the operator never watches a dead 30 s gap. The
    seam's own guards (busy/dead target) are already pinned by the post_task
    negative tests above."""
    from lib.conversations.project_board import (
        complete_task, post_task, reopen_task,
    )

    proj = os.path.abspath('/tmp/ec-reopen')
    conv = 'conv-ec-reopen'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        _mark_busy(conv)                       # production-real: posted mid-turn
        epic = post_task(proj, conv, 'Revive me later')['id']
        _clear_task_registry()
        complete_task(proj, conv, epic)        # done … the human changes course
        assert _board_row(flask_app, proj, epic)['status'] == 'done'
        assert spawned == [], 'nothing runs for the done epic'

        res = reopen_task(proj, 'conv-operator', epic)

    assert res.get('ok'), res
    row = _board_row(flask_app, proj, epic)
    assert row['status'] == 'claimed', \
        'a reopened epic with an idle existing target must start AT reopen time'
    assert row['owner_conv_id'] == conv, 'routed back to its creator'
    assert len(spawned) == 1
    assert _persisted_last_user(flask_app, conv).get('_boardTaskId') == epic
    _clear_task_registry()


# ════════════════════════════════════════════════════════════════════
#  SEAM 2 — on_conv_idle: the completion nudge
# ════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════
#  SEAM 2 — on_conv_idle: the completion nudge
# ════════════════════════════════════════════════════════════════════

def test_completion_nudge_starts_open_epic_on_empty_queue(flask_app, monkeypatch):
    """THE common 30 s kill: an epic posted while the conv was BUSY starts the
    moment the current task completes with an empty queue — not ≤30 s later at
    the next sweep."""
    from lib.conversations.project_board import post_task
    from lib.tasks_pkg.manager._sync import _dispatch_queued_message

    proj = os.path.abspath('/tmp/ec-nudge')
    conv = 'conv-ec-nudge'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        _mark_busy(conv)
        epic = post_task(proj, conv, 'Queued behind the current turn')['id']
        _clear_task_registry()  # the task now "completes"
        assert _board_row(flask_app, proj, epic)['status'] == 'open'

        # The REAL post-task completion hook, queue empty → nudge fires.
        _dispatch_queued_message(_terminal_task(conv, proj))

    row = _board_row(flask_app, proj, epic)
    assert row['status'] == 'claimed', \
        'the completion nudge must dispatch the open epic immediately'
    assert len(spawned) == 1, 'the nudge must drain the kickoff → one task spawned'
    assert spawned[0]['convId'] == conv
    assert _persisted_last_user(flask_app, conv).get('_boardTaskId') == epic
    _clear_task_registry()


def test_completion_nudge_no_project_noop(flask_app, monkeypatch):
    """A completing task WITHOUT a projectPath never nudges (non-project convs
    are untouched by the brain)."""
    from lib.conversations.project_board import post_task
    from lib.tasks_pkg.manager._sync import _dispatch_queued_message

    proj = os.path.abspath('/tmp/ec-nudge-noproj')
    conv = 'conv-ec-nudge-noproj'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        _mark_busy(conv)
        epic = post_task(proj, conv, 'Orphan epic')['id']
        _clear_task_registry()
        task = _terminal_task(conv, '')          # no projectPath on the task
        _dispatch_queued_message(task)

    assert _board_row(flask_app, proj, epic)['status'] == 'open'
    assert spawned == []
    _clear_task_registry()


def test_completion_nudge_skips_when_queue_nonempty(flask_app, monkeypatch):
    """A non-empty queue drains NORMALLY (the queued user turn first); the
    nudge only fires when the queue is EMPTY — queued human turns never get
    pre-empted by board work."""
    from lib.conversations.project_board import post_task
    from lib.message_queue import enqueue_message
    from lib.tasks_pkg.manager._sync import _dispatch_queued_message

    proj = os.path.abspath('/tmp/ec-nudge-queue')
    conv = 'conv-ec-nudge-queue'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        _mark_busy(conv)
        epic = post_task(proj, conv, 'Board work waits its turn')['id']
        _clear_task_registry()
        enqueue_message(conv, {'text': 'a real human follow-up'}, {})

        _dispatch_queued_message(_terminal_task(conv, proj))

    assert len(spawned) == 1, 'the queued human turn must dispatch first'
    assert _persisted_last_user(flask_app, conv).get('content') == \
        'a real human follow-up'
    assert _board_row(flask_app, proj, epic)['status'] == 'open', \
        'the epic must NOT pre-empt a non-empty queue'
    _clear_task_registry()


def test_completion_nudge_ignores_epics_routed_elsewhere(flask_app, monkeypatch):
    """The nudge is conv-scoped: an open epic routed to ANOTHER conv is not
    this completion's business (its own completion hook / the sweep owns it)."""
    from lib.conversations.project_board import post_task
    from lib.tasks_pkg.manager._sync import _dispatch_queued_message

    proj = os.path.abspath('/tmp/ec-nudge-other')
    conv_a = 'conv-ec-nudge-a'
    conv_b = 'conv-ec-nudge-b'
    _clear_task_registry()
    _seed_conv(flask_app, conv_a, proj)
    _seed_conv(flask_app, conv_b, proj)
    spawned = _stub_spawn(monkeypatch)

    with flask_app.app_context():
        _mark_busy(conv_b)
        epic = post_task(proj, conv_b, 'B owns this one')['id']
        _clear_task_registry()

        # A's task completes with an empty queue — must NOT touch B's epic.
        _dispatch_queued_message(_terminal_task(conv_a, proj))

    assert _board_row(flask_app, proj, epic)['status'] == 'open'
    assert spawned == []
    _clear_task_registry()


# ════════════════════════════════════════════════════════════════════
#  SEAM 3 — peer message send-time idle drain
# ════════════════════════════════════════════════════════════════════

def _seed_peer_convs(flask_app, sender, target):
    _seed_conv(flask_app, sender)
    _seed_conv(flask_app, target)
    from lib import agent_inbox
    agent_inbox.reset_for_test(sender)
    agent_inbox.reset_for_test(target)


def test_peer_message_to_idle_conv_delivers_at_send_time(flask_app, monkeypatch):
    """THE peer-latency kill: a peer note into an IDLE conv is drained
    synchronously inside send_peer_message — appended as a fresh _peerMessage
    turn, a task spawned, the durable row consumed — with NO 30 s wait and NO
    inbox twin (the twin is the live-target fast path)."""
    from lib import agent_inbox
    from lib.conversations.project_peer import send_peer_message

    sender = 'conv-ec-peer-send'
    target = 'conv-ec-peer-tgt'
    _clear_task_registry()
    _seed_peer_convs(flask_app, sender, target)
    spawned = _stub_spawn(monkeypatch)

    res = send_peer_message('/proj', sender, target,
                            'confirm you are not touching lib/parser/')
    assert res.get('ok'), res

    assert _queue_rows(flask_app, target) == [], \
        'the durable row must be consumed at send time (no heartbeat wait)'
    assert len(spawned) == 1 and spawned[0]['convId'] == target
    last_user = _persisted_last_user(flask_app, target)
    assert last_user.get('_peerMessage') is True
    assert last_user.get('_fromConv') == sender
    assert 'lib/parser/' in last_user.get('content', '')
    assert agent_inbox.peek(target) == 0, 'an idle target gets NO inbox twin'
    _clear_task_registry()


def test_peer_message_to_live_conv_keeps_twin_path(flask_app, monkeypatch):
    """Regression: a LIVE, drain-eligible target keeps the EXISTING two-lane
    delivery (durable row + inbox twin); the send-time drain must NOT fire
    (that would double-deliver / race the live turn)."""
    from lib import agent_inbox
    from lib.conversations.project_peer import send_peer_message

    sender = 'conv-ec-peer-send2'
    target = 'conv-ec-peer-tgt2'
    _clear_task_registry()
    _seed_peer_convs(flask_app, sender, target)
    spawned = _stub_spawn(monkeypatch)

    _mark_busy(target)  # a live, drain-eligible task
    res = send_peer_message('/proj', sender, target, 'boundary check?')
    assert res.get('ok'), res

    assert len(_queue_rows(flask_app, target)) == 1, \
        'the durable row stays for the live turn / completion hook'
    assert agent_inbox.peek(target) == 1, 'the fast-path twin is offered'
    assert spawned == [], 'no new task is spawned into a live conv'
    _clear_task_registry()


def test_peer_heartbeat_still_catches_when_send_time_drain_fails(flask_app,
                                                                 monkeypatch):
    """The 30 s pass is the NET, not the starter: if the send-time drain
    explodes, the message is NOT lost — drain_idle_peer_messages delivers it."""
    import lib.message_queue as mq
    from lib.conversations.project_peer import send_peer_message

    sender = 'conv-ec-peer-send3'
    target = 'conv-ec-peer-tgt3'
    _clear_task_registry()
    _seed_peer_convs(flask_app, sender, target)

    real_dispatch = mq.dispatch_next_queued

    def _boom(conv_id, **kw):
        raise RuntimeError('simulated send-time drain failure')

    monkeypatch.setattr(mq, 'dispatch_next_queued', _boom)
    res = send_peer_message('/proj', sender, target, 'net test')
    assert res.get('ok'), f'a drain failure must not fail the SEND: {res}'
    assert len(_queue_rows(flask_app, target)) == 1, \
        'the durable row survives a send-time drain failure (never a loss)'

    # The heartbeat pass (real dispatch restored) delivers the stranded row.
    monkeypatch.setattr(mq, 'dispatch_next_queued', real_dispatch)
    spawned = _stub_spawn(monkeypatch)
    with flask_app.app_context():
        mq.drain_idle_peer_messages()
    assert len(spawned) == 1 and spawned[0]['convId'] == target
    assert _persisted_last_user(flask_app, target).get('_peerMessage') is True
    assert _queue_rows(flask_app, target) == []
    _clear_task_registry()


# ════════════════════════════════════════════════════════════════════
#  NEGATIVE CONTROLS — each seam is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_post_trigger_is_load_bearing(flask_app, monkeypatch):
    """NC: revert the post_task → on_epic_posted trigger → a post into an IDLE
    conv no longer starts (the epic waits for the 30 s heartbeat — the
    pre-channel shape)."""
    proj = os.path.abspath('/tmp/ec-nc-post')
    conv = 'conv-ec-nc-post'

    def _post_and_observe():
        from lib.conversations.project_board import post_task
        from lib.database import DOMAIN_CHAT, get_thread_db
        _clear_task_registry()
        spawned = _stub_spawn(monkeypatch)
        _seed_conv(flask_app, conv, proj)
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path=?', (proj,))
            db.execute('DELETE FROM message_queue WHERE conv_id=?', (conv,))
            db.commit()
            epic = post_task(proj, conv, 'NC epic')['id']
        row = _board_row(flask_app, proj, epic)
        _clear_task_registry()
        return len(spawned), row['status']

    n, status = _post_and_observe()
    assert n == 1 and status == 'claimed', \
        'baseline: the post trigger starts an idle-target epic immediately'

    def _wipe_conv():
        from lib.database import DOMAIN_CHAT, get_thread_db
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM conversations WHERE id=?', (conv,))
            db.commit()
    _wipe_conv()

    def run():
        n2, status2 = _post_and_observe()
        assert n2 == 0 and status2 == 'open', \
            'NC: without the post trigger the epic waits for the heartbeat'

    _patch_restore(
        _BOARD_SRC,
        '        from lib.conversations.project_dispatch import on_epic_posted\n'
        '        on_epic_posted(project_path, task_id)',
        '        pass  # NC: post-time dispatch trigger removed',
        run,
    )
    _wipe_conv()


def test_NC_completion_nudge_is_load_bearing(flask_app, monkeypatch):
    """NC: revert the _dispatch_queued_message → _nudge_brain_dispatch call →
    an epic posted-behind-a-busy-turn stays OPEN at completion (heartbeat-only
    fallback — the 30 s wait returns)."""
    proj = os.path.abspath('/tmp/ec-nc-nudge')
    conv = 'conv-ec-nc-nudge'

    def _drive_hook_and_observe():
        import lib.tasks_pkg.manager._sync as S
        from lib.conversations.project_board import post_task
        from lib.database import DOMAIN_CHAT, get_thread_db
        _clear_task_registry()
        spawned = _stub_spawn(monkeypatch)
        _seed_conv(flask_app, conv, proj)
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path=?', (proj,))
            db.execute('DELETE FROM message_queue WHERE conv_id=?', (conv,))
            db.commit()
            _mark_busy(conv)
            epic = post_task(proj, conv, 'NC nudge epic')['id']
            _clear_task_registry()
            S._dispatch_queued_message(_terminal_task(conv, proj))
        row = _board_row(flask_app, proj, epic)
        _clear_task_registry()
        return len(spawned), row['status']

    n, status = _drive_hook_and_observe()
    assert n == 1 and status == 'claimed', \
        'baseline: the completion nudge starts the open epic immediately'

    def _wipe_conv():
        from lib.database import DOMAIN_CHAT, get_thread_db
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM conversations WHERE id=?', (conv,))
            db.commit()
    _wipe_conv()

    def run():
        n2, status2 = _drive_hook_and_observe()
        assert n2 == 0 and status2 == 'open', \
            'NC: without the completion nudge the epic waits for the heartbeat'

    _patch_restore(
        _SYNC_SRC,
        '            _nudge_brain_dispatch(task, conv_id)\n'
        '            return',
        '            pass  # NC: completion nudge removed\n'
        '            return',
        run,
    )
    _wipe_conv()


def test_NC_send_time_drain_is_load_bearing(flask_app, monkeypatch):
    """NC: revert the send_peer_message idle-drain → a peer note into an IDLE
    conv stays a stranded queue row (Symptom A returns — the 30 s wait)."""
    sender = 'conv-ec-nc-send'
    target = 'conv-ec-nc-tgt'

    def _send_and_observe():
        import lib.conversations.project_peer as pp
        _clear_task_registry()
        spawned = _stub_spawn(monkeypatch)
        _seed_peer_convs(flask_app, sender, target)
        with pp._rate_lock:
            pp._peer_msg_history.clear()
        res = pp.send_peer_message('/proj', sender, target, 'NC peer note')
        assert res.get('ok'), res
        q_len = len(_queue_rows(flask_app, target))
        _clear_task_registry()
        return len(spawned), q_len

    n, q = _send_and_observe()
    assert n == 1 and q == 0, \
        'baseline: the send-time drain delivers an idle-target peer note at once'

    def _wipe_convs():
        from lib.database import DOMAIN_CHAT, get_thread_db
        with flask_app.app_context():
            db = get_thread_db(DOMAIN_CHAT)
            for cid in (sender, target):
                db.execute('DELETE FROM conversations WHERE id=?', (cid,))
                db.execute('DELETE FROM message_queue WHERE conv_id=?', (cid,))
            db.commit()
    _wipe_convs()

    def run():
        n2, q2 = _send_and_observe()
        assert n2 == 0 and q2 == 1, \
            'NC: without the send-time drain the peer row is stranded (30 s wait)'

    _patch_restore(
        _PEER_SRC,
        '            if not _live_drain_eligible_task(to_conv_id):\n'
        '                from lib.message_queue import dispatch_next_queued\n'
        '                _tid = dispatch_next_queued(to_conv_id)',
        '            if False:  # NC: send-time idle drain removed\n'
        '                from lib.message_queue import dispatch_next_queued\n'
        '                _tid = dispatch_next_queued(to_conv_id)',
        run,
    )
    _wipe_convs()
