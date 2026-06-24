"""tests/test_autopilot_arm.py — Runtime arming of autopilot mid-stream.

Covers ``lib.tasks_pkg.autopilot.arm_autopilot`` / ``disarm_autopilot``: the
"take over from here" gesture.  Arming has TWO effects (unified turn-source
queue model):
  1. flips ``config['autopilot']=True`` on any ALREADY-RUNNING task so the VU
     takes over at its natural stop without the user re-sending, AND
  2. enqueues a persistent autopilot armed-marker sentinel
     (``lib.message_queue``, priority 90) so the arm survives a page reload,
     shows in the queue bar (cancellable), and keeps autopilot armed even when
     no task is live.  ``armed`` is True whenever autopilot is now armed for
     the conv (live flip OR marker present).

Endpoint mode is mutually exclusive — arming is refused outright when an
endpoint task is live (no marker created).

These tests inject synthetic tasks straight into the in-memory ``tasks``
registry (no live LLM / orchestrator) and assert the mutation + return shape.
Each test clears the conv's marker first so DB state doesn't leak between runs.
"""

import pytest

from lib.tasks_pkg.autopilot import (
    arm_autopilot, disarm_autopilot, is_autopilot_enabled,
)
from lib.message_queue import clear_autopilot_marker, has_autopilot_marker


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from lib.tasks_pkg import tasks, tasks_lock
    added = []
    convs = set()

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        if task.get('convId'):
            convs.add(task['convId'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)
    # Clean up any markers these tests created so DB state doesn't leak.
    for cid in convs:
        try:
            clear_autopilot_marker(cid)
        except Exception:
            pass


def _running_task(tid, conv_id, **cfg_over):
    cfg = {'model': 'm', 'autopilot': False}
    cfg.update(cfg_over)
    return {'id': tid, 'convId': conv_id, 'status': 'running', 'config': cfg}


def test_arm_flips_live_task_config(put_task):
    """A running task for the conv gets config.autopilot flipped + marker set."""
    clear_autopilot_marker('conv-A')
    put_task(_running_task('t-arm-1', 'conv-A'))
    result = arm_autopilot('conv-A')
    assert result['armed'] is True
    assert 't-arm-1' in result['taskIds']
    assert result['markerAdded'] is True
    assert has_autopilot_marker('conv-A') is True
    # The mutation makes is_autopilot_enabled return True so the end-of-turn
    # hook (which re-reads it at finalize) will now fire.
    from lib.tasks_pkg import tasks
    assert tasks['t-arm-1']['config']['autopilot'] is True
    assert is_autopilot_enabled(tasks['t-arm-1']) is True


def test_arm_marker_when_no_live_task(put_task):
    """No live task → no config flip, but the persistent marker arms autopilot.

    New contract: the marker survives reload and governs the loop even when the
    reply already finished, so ``armed`` is True with an empty ``taskIds``.
    """
    clear_autopilot_marker('conv-B')
    put_task({'id': 't-done-1', 'convId': 'conv-B', 'status': 'done',
              'config': {'autopilot': False}})
    result = arm_autopilot('conv-B')
    assert result['armed'] is True
    assert result['taskIds'] == []
    assert result['markerAdded'] is True
    assert has_autopilot_marker('conv-B') is True


def test_arm_refused_when_endpoint_live(put_task):
    """A live endpoint task refuses the arm outright — no flip, no marker."""
    clear_autopilot_marker('conv-C')
    put_task(_running_task('t-ep-1', 'conv-C', endpointMode=True))
    result = arm_autopilot('conv-C')
    assert result['armed'] is False
    assert result['markerAdded'] is False
    assert has_autopilot_marker('conv-C') is False
    from lib.tasks_pkg import tasks
    # config untouched
    assert tasks['t-ep-1']['config']['autopilot'] is False


def test_arm_skips_vu_subtask(put_task):
    """The VU sub-task itself must never be config-flipped (would recurse).

    No live dispatchable task → no taskIds, but the persistent marker still
    arms the conv (and the VU sub-task config is left untouched).
    """
    clear_autopilot_marker('conv-D')
    t = _running_task('t-vu-1', 'conv-D')
    t['_vu_subtask'] = True
    put_task(t)
    result = arm_autopilot('conv-D')
    assert result['taskIds'] == []
    from lib.tasks_pkg import tasks
    assert tasks['t-vu-1']['config']['autopilot'] is False


def test_arm_idempotent_marker(put_task):
    """Arming twice creates at most one marker; second call markerAdded=False."""
    clear_autopilot_marker('conv-E')
    put_task(_running_task('t-on-1', 'conv-E', autopilot=True))
    first = arm_autopilot('conv-E')
    assert first['markerAdded'] is True
    second = arm_autopilot('conv-E')
    # Already armed → no NEW marker, but still reported armed.
    assert second['markerAdded'] is False
    assert second['armed'] is True
    assert second['taskIds'] == []


def test_disarm_clears_marker_and_config(put_task):
    """disarm_autopilot removes the marker AND flips live config off."""
    clear_autopilot_marker('conv-dis')
    put_task(_running_task('t-dis-1', 'conv-dis', autopilot=True))
    arm_autopilot('conv-dis')
    assert has_autopilot_marker('conv-dis') is True
    result = disarm_autopilot('conv-dis')
    assert result['markerCleared'] is True
    assert 't-dis-1' in result['taskIds']
    assert has_autopilot_marker('conv-dis') is False
    from lib.tasks_pkg import tasks
    assert tasks['t-dis-1']['config']['autopilot'] is False


def test_arm_only_targets_matching_conv(put_task):
    """Arming conv-X must not touch a running task for conv-Y."""
    clear_autopilot_marker('conv-X')
    clear_autopilot_marker('conv-Y')
    put_task(_running_task('t-x', 'conv-X'))
    put_task(_running_task('t-y', 'conv-Y'))
    result = arm_autopilot('conv-X')
    assert result['taskIds'] == ['t-x']
    from lib.tasks_pkg import tasks
    assert tasks['t-y']['config']['autopilot'] is False
    assert has_autopilot_marker('conv-Y') is False


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
    """No live task → armed=True via the persistent marker (new contract)."""
    clear_autopilot_marker('conv-nonexistent-xyz')
    resp = flask_client.post('/api/v1/chat/autopilot/arm',
                             json={'convId': 'conv-nonexistent-xyz'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['armed'] is True
    assert body['taskIds'] == []
    clear_autopilot_marker('conv-nonexistent-xyz')


@pytest.mark.api
def test_disarm_endpoint(flask_client):
    """POST /autopilot/disarm clears the marker and reports disarmed."""
    from lib.message_queue import arm_autopilot_marker
    arm_autopilot_marker('conv-dis-http', {})
    resp = flask_client.post('/api/v1/chat/autopilot/disarm',
                             json={'convId': 'conv-dis-http'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['markerCleared'] is True
    assert has_autopilot_marker('conv-dis-http') is False
