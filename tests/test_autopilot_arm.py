"""tests/test_autopilot_arm.py — Runtime arming of autopilot mid-stream.

Covers ``lib.tasks_pkg.autopilot.arm_autopilot``: the "take over from here"
gesture that flips ``config['autopilot']=True`` on an ALREADY-RUNNING task so
the virtual user takes over at the next natural stop — without the user
re-sending. This is option (A): arming only affects a still-running task; a
reply that already finished is NOT auto-spawned (the persisted setting kicks
the loop off on the next manual send instead).

These tests inject synthetic tasks straight into the in-memory ``tasks``
registry (no live LLM / orchestrator) and assert the mutation + return shape.
"""

import pytest

from lib.tasks_pkg.autopilot import arm_autopilot, is_autopilot_enabled


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from lib.tasks_pkg import tasks, tasks_lock
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def _running_task(tid, conv_id, **cfg_over):
    cfg = {'model': 'm', 'autopilot': False}
    cfg.update(cfg_over)
    return {'id': tid, 'convId': conv_id, 'status': 'running', 'config': cfg}


def test_arm_flips_live_task_config(put_task):
    """A running task for the conv gets config.autopilot flipped to True."""
    put_task(_running_task('t-arm-1', 'conv-A'))
    result = arm_autopilot('conv-A')
    assert result['armed'] is True
    assert 't-arm-1' in result['taskIds']
    # The mutation makes is_autopilot_enabled return True so the end-of-turn
    # hook (which re-reads it at finalize) will now fire.
    from lib.tasks_pkg import tasks
    assert tasks['t-arm-1']['config']['autopilot'] is True
    assert is_autopilot_enabled(tasks['t-arm-1']) is True


def test_arm_noop_when_no_live_task(put_task):
    """A done task is NOT armed — armed=False, caller relies on persisted setting."""
    put_task({'id': 't-done-1', 'convId': 'conv-B', 'status': 'done',
              'config': {'autopilot': False}})
    result = arm_autopilot('conv-B')
    assert result['armed'] is False
    assert result['taskIds'] == []


def test_arm_skips_endpoint_task(put_task):
    """Endpoint-mode tasks are mutually exclusive — never armed."""
    put_task(_running_task('t-ep-1', 'conv-C', endpointMode=True))
    result = arm_autopilot('conv-C')
    assert result['armed'] is False
    from lib.tasks_pkg import tasks
    # config untouched
    assert tasks['t-ep-1']['config']['autopilot'] is False


def test_arm_skips_vu_subtask(put_task):
    """The VU sub-task itself must never be armed (would recurse)."""
    t = _running_task('t-vu-1', 'conv-D')
    t['_vu_subtask'] = True
    put_task(t)
    result = arm_autopilot('conv-D')
    assert result['armed'] is False


def test_arm_idempotent_when_already_on(put_task):
    """A task already running with autopilot on is not re-counted as armed."""
    put_task(_running_task('t-on-1', 'conv-E', autopilot=True))
    result = arm_autopilot('conv-E')
    # Already on → nothing flipped → armed False (no NEW arming happened).
    assert result['armed'] is False
    assert result['taskIds'] == []


def test_arm_only_targets_matching_conv(put_task):
    """Arming conv-X must not touch a running task for conv-Y."""
    put_task(_running_task('t-x', 'conv-X'))
    put_task(_running_task('t-y', 'conv-Y'))
    result = arm_autopilot('conv-X')
    assert result['taskIds'] == ['t-x']
    from lib.tasks_pkg import tasks
    assert tasks['t-y']['config']['autopilot'] is False


# ── HTTP route: POST /api/v1/chat/autopilot/arm ────────────────────────

@pytest.mark.api
def test_arm_endpoint_flips_live_task(flask_client, put_task):
    """The arm endpoint flips the live task's config and returns armed=True."""
    put_task(_running_task('t-http-1', 'conv-http-1'))
    resp = flask_client.post('/api/v1/chat/autopilot/arm',
                             json={'convId': 'conv-http-1'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['armed'] is True
    assert 't-http-1' in body['taskIds']
    from lib.tasks_pkg import tasks
    assert tasks['t-http-1']['config']['autopilot'] is True


@pytest.mark.api
def test_arm_endpoint_requires_conv_id(flask_client):
    """Missing convId → 400."""
    resp = flask_client.post('/api/v1/chat/autopilot/arm', json={})
    assert resp.status_code == 400


@pytest.mark.api
def test_arm_endpoint_no_live_task(flask_client):
    """No live task for the conv → armed=False (caller relies on persisted setting)."""
    resp = flask_client.post('/api/v1/chat/autopilot/arm',
                             json={'convId': 'conv-nonexistent-xyz'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['armed'] is False
    assert body['taskIds'] == []
