"""tests/test_autopilot_kick.py — Kick autopilot on a FINISHED conversation.

Covers ``lib.tasks_pkg.autopilot.kick_autopilot`` and ``_run_autopilot_kick``:
the "push it forward" gesture (empty-Enter on a conversation whose reply has
already finished, autopilot ON).  Unlike ``arm_autopilot`` (which only flips a
LIVE task mid-stream), the kick spawns a thin carrier task that runs the
virtual-user hook directly — no AI worker turn.

These tests monkeypatch the spawn / message-build / VU hook so no live LLM or
orchestrator runs; they assert the guard logic, the ``_autopilot_kick`` flag,
and the carrier's done-event baton.
"""

import pytest


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    added = []

    def _put(task):
        task.setdefault('_userId', 1)
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def _running_task(tid, conv_id, **over):
    t = {'id': tid, 'convId': conv_id, 'status': 'running',
         '_userId': 1,
         'config': {'model': 'm', 'autopilot': False}}
    t.update(over)
    return t


# ── kick_autopilot guard + spawn ───────────────────────────────────────

def test_kick_requires_conv_id():
    from lib.tasks_pkg.autopilot import kick_autopilot
    r = kick_autopilot('', {}, user_id=1)
    assert r['taskId'] is None
    assert r['error'] == 'conv_id is required'


def test_kick_refuses_when_task_running(put_task):
    """A live (non-VU) task means the user should ARM, not kick."""
    from lib.tasks_pkg.autopilot import kick_autopilot
    put_task(_running_task('t-live', 'conv-K1'))
    r = kick_autopilot('conv-K1', {}, user_id=1)
    assert r['taskId'] is None
    assert r['error'] == 'task_already_running'


def test_kick_allows_when_only_vu_subtask_running(put_task, monkeypatch):
    """A running VU sub-task must NOT block a kick (it's part of the loop)."""
    import lib.tasks_pkg.autopilot as ap
    put_task(_running_task('t-vu', 'conv-K2', _vu_subtask=True))

    monkeypatch.setattr(ap, 'build_api_messages_from_db',
                        lambda cid, cfg, *, user_id: [{'role': 'user', 'content': 'hi'}],
                        raising=False)
    # build_api_messages_from_db is imported lazily inside kick_autopilot;
    # patch at the source module too.
    import lib.tasks_pkg.conv_message_builder.api as cmb
    monkeypatch.setattr(cmb, 'build_api_messages_from_db',
                        lambda cid, cfg, *, user_id: [{'role': 'user', 'content': 'hi'}])

    spawned = {}
    import lib.tasks_pkg.spawn as pkg
    monkeypatch.setattr(pkg, 'spawn_task', lambda t: spawned.update(t=t))

    r = kick = ap.kick_autopilot('conv-K2', {'model': 'm'}, user_id=1)
    assert r['taskId']
    assert spawned['t']['_autopilot_kick'] is True
    assert spawned['t']['config']['autopilot'] is True


def test_kick_conversation_not_found(monkeypatch):
    import lib.tasks_pkg.conv_message_builder.api as cmb
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setattr(cmb, 'build_api_messages_from_db', lambda cid, cfg, *, user_id: None)
    r = ap.kick_autopilot('conv-missing', {}, user_id=1)
    assert r['taskId'] is None
    assert r['error'] == 'conversation_not_found'


def test_kick_conversation_empty(monkeypatch):
    import lib.tasks_pkg.conv_message_builder.api as cmb
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setattr(cmb, 'build_api_messages_from_db', lambda cid, cfg, *, user_id: [])
    r = ap.kick_autopilot('conv-empty', {}, user_id=1)
    assert r['taskId'] is None
    assert r['error'] == 'conversation_empty'


def test_kick_sets_flag_and_strips_checkpoints(monkeypatch):
    import lib.tasks_pkg.conv_message_builder.api as cmb
    import lib.tasks_pkg.spawn as pkg
    import lib.tasks_pkg.autopilot as ap

    monkeypatch.setattr(cmb, 'build_api_messages_from_db',
                        lambda cid, cfg, *, user_id: [{'role': 'user', 'content': 'hi'}])
    spawned = {}
    monkeypatch.setattr(pkg, 'spawn_task', lambda t: spawned.update(t=t))

    r = ap.kick_autopilot('conv-K3', {
        'model': 'm', 'excludeLast': True, 'checkpointUsage': {'x': 1},
    }, user_id=1)
    assert r['taskId']
    t = spawned['t']
    assert t['_autopilot_kick'] is True
    assert t['config']['autopilot'] is True
    assert 'excludeLast' not in t['config']
    assert 'checkpointUsage' not in t['config']


# ── _run_autopilot_kick carrier ────────────────────────────────────────

def test_run_kick_emits_no_retired_baton_on_done(monkeypatch):
    """Post-pt_8dc03017 contract (updated 2026-07-27): the kick carrier's
    done carries NO withheld autopilot baton and the task keeps NO
    ``_autopilot_followup`` stash — that discovery mechanism was
    deliberately retired (aa6f7ea6).  The follow-up is discovered via
    ``latestLiveTaskId`` on the terminal tick (pinned by
    tests/test_vu_carrier_stream_contract.py) or /api/chat/active.
    This test pins the RETIREMENT so the dual mechanism cannot silently
    come back."""
    import lib.tasks_pkg.autopilot as ap
    from lib.tasks_pkg.manager import create_task

    task = create_task('conv-K4', [{'role': 'user', 'content': 'hi'}],
                       {'model': 'm', 'autopilot': True}, user_id=1)
    task['_autopilot_kick'] = True

    monkeypatch.setattr(ap, 'maybe_run_autopilot',
                        lambda t: {'next_task_id': 'next-123',
                                   'vu_msg': {'role': 'user', 'content': 'go on'}})
    appended = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda t, ev: appended.append(ev))
    monkeypatch.setattr('lib.tasks_pkg.manager.persist_task_result',
                        lambda t: None)

    ap._run_autopilot_kick(task)

    assert task['status'] == 'done'
    assert '_autopilot_followup' not in task, (
        'the retired withheld-baton stash must NOT come back')
    done = [e for e in appended if e.get('type') == 'done']
    assert len(done) == 1
    assert 'autopilotNextTaskId' not in done[0], (
        'the retired baton fields must NOT ride the kick done — discovery '
        'is via latestLiveTaskId (terminal tick) or /api/chat/active')
    assert 'autopilotVuMessage' not in done[0]


def test_run_kick_done_without_followup(monkeypatch):
    """VU declined (TASK_DONE) → done event carries no baton, no crash."""
    import lib.tasks_pkg.autopilot as ap
    from lib.tasks_pkg.manager import create_task

    task = create_task('conv-K5', [{'role': 'user', 'content': 'hi'}],
                       {'model': 'm', 'autopilot': True}, user_id=1)
    task['_autopilot_kick'] = True

    monkeypatch.setattr(ap, 'maybe_run_autopilot', lambda t: None)
    appended = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda t, ev: appended.append(ev))
    monkeypatch.setattr('lib.tasks_pkg.manager.persist_task_result',
                        lambda t: None)

    ap._run_autopilot_kick(task)

    done = [e for e in appended if e.get('type') == 'done']
    assert len(done) == 1
    assert 'autopilotNextTaskId' not in done[0]
    assert '_autopilot_followup' not in task


# ── HTTP route: POST /api/v1/chat/autopilot/kick ───────────────────────

@pytest.mark.api
def test_kick_endpoint_requires_conv_id(flask_client):
    resp = flask_client.post('/api/v1/chat/autopilot/kick', json={})
    assert resp.status_code == 400


@pytest.mark.api
def test_kick_endpoint_conflict_when_running(flask_client, put_task):
    """A running task for the conv → 409 (arm instead)."""
    put_task(_running_task('t-http-live', 'conv-http-kick'))
    resp = flask_client.post('/api/v1/chat/autopilot/kick',
                             json={'convId': 'conv-http-kick'})
    assert resp.status_code == 409


@pytest.mark.api
def test_kick_endpoint_conflict_when_no_conversation(flask_client):
    """No conversation row → kick_autopilot returns error → 409."""
    resp = flask_client.post('/api/v1/chat/autopilot/kick',
                             json={'convId': 'conv-does-not-exist-xyz'})
    assert resp.status_code == 409
