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

Flow execution is mutually exclusive — arming is refused while a Flow-managed
task is live (no marker created).

These tests inject synthetic tasks straight into the in-memory ``tasks``
registry (no live LLM / orchestrator) and assert the mutation + return shape.
Each test clears the conv's marker first so DB state doesn't leak between runs.
"""

import sys

import pytest
import lib.message_queue as message_queue

from lib.tasks_pkg.autopilot import (
    arm_autopilot, disarm_autopilot, is_autopilot_enabled,
)
from lib.message_queue import clear_autopilot_marker, has_autopilot_marker


@pytest.fixture(autouse=True)
def marker_authority(monkeypatch):
    """Keep arm/disarm tests focused on policy, not Sidecar integration.

    The real queue persistence contract is covered by ``test_queue_lease``.
    This owner-keyed fake also makes cross-owner leakage impossible in these
    task-registry tests without creating unrelated conversation rows.
    """
    markers: dict[tuple[int, str], dict] = {}

    def arm(conversation_id, config, *, user_id):
        key = (int(user_id), conversation_id)
        if key in markers:
            return {'armed': False, 'queueId': markers[key]['queueId']}
        record = {'queueId': f'marker:{user_id}:{conversation_id}',
                  'config': dict(config or {})}
        markers[key] = record
        return {'armed': True, **record}

    def clear(conversation_id, *, user_id):
        return markers.pop((int(user_id), conversation_id), None) is not None

    def has(conversation_id, *, user_id):
        return (int(user_id), conversation_id) in markers

    monkeypatch.setattr(message_queue, 'arm_autopilot_marker', arm)
    monkeypatch.setattr(message_queue, 'clear_autopilot_marker', clear)
    monkeypatch.setattr(message_queue, 'has_autopilot_marker', has)
    monkeypatch.setattr(
        'lib.tasks_pkg.autopilot_markers.conclude_run',
        lambda conversation_id, *, user_id, reason='stopped', run_id='': None,
    )
    monkeypatch.setattr(sys.modules[__name__], 'clear_autopilot_marker', clear)
    monkeypatch.setattr(sys.modules[__name__], 'has_autopilot_marker', has)
    return markers


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    added = []
    convs = set()

    def _put(task):
        task.setdefault('_userId', 1)
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
            clear_autopilot_marker(cid, user_id=1)
        except Exception:
            pass


def _running_task(tid, conv_id, **cfg_over):
    cfg = {'model': 'm', 'autopilot': False}
    cfg.update(cfg_over)
    return {
        'id': tid,
        'convId': conv_id,
        '_userId': 1,
        'status': 'running',
        'config': cfg,
    }


def test_arm_flips_live_task_config(put_task):
    """A running task for the conv gets config.autopilot flipped + marker set."""
    clear_autopilot_marker('conv-A', user_id=1)
    put_task(_running_task('t-arm-1', 'conv-A'))
    result = arm_autopilot('conv-A', user_id=1)
    assert result['armed'] is True
    assert 't-arm-1' in result['taskIds']
    assert result['markerAdded'] is True
    assert has_autopilot_marker('conv-A', user_id=1) is True
    # The mutation makes is_autopilot_enabled return True so the end-of-turn
    # hook (which re-reads it at finalize) will now fire.
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-arm-1']['config']['autopilot'] is True
    assert is_autopilot_enabled(tasks['t-arm-1']) is True


def test_arm_marker_when_no_live_task(put_task):
    """No live task → no config flip, but the persistent marker arms autopilot.

    New contract: the marker survives reload and governs the loop even when the
    reply already finished, so ``armed`` is True with an empty ``taskIds``.
    """
    clear_autopilot_marker('conv-B', user_id=1)
    put_task({'id': 't-done-1', 'convId': 'conv-B', 'status': 'done',
              'config': {'autopilot': False}})
    result = arm_autopilot('conv-B', user_id=1)
    assert result['armed'] is True
    assert result['taskIds'] == []
    assert result['markerAdded'] is True
    assert has_autopilot_marker('conv-B', user_id=1) is True


def test_arm_skips_vu_subtask(put_task):
    """The VU sub-task itself must never be config-flipped (would recurse).

    No live dispatchable task → no taskIds, but the persistent marker still
    arms the conv (and the VU sub-task config is left untouched).
    """
    clear_autopilot_marker('conv-D', user_id=1)
    t = _running_task('t-vu-1', 'conv-D')
    t['_vu_subtask'] = True
    put_task(t)
    result = arm_autopilot('conv-D', user_id=1)
    assert result['taskIds'] == []
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-vu-1']['config']['autopilot'] is False


def test_arm_idempotent_marker(put_task):
    """Arming twice creates at most one marker; second call markerAdded=False."""
    clear_autopilot_marker('conv-E', user_id=1)
    put_task(_running_task('t-on-1', 'conv-E', autopilot=True))
    first = arm_autopilot('conv-E', user_id=1)
    assert first['markerAdded'] is True
    second = arm_autopilot('conv-E', user_id=1)
    # Already armed → no NEW marker, but still reported armed.
    assert second['markerAdded'] is False
    assert second['armed'] is True
    assert second['taskIds'] == []


def test_disarm_clears_marker_and_config(put_task):
    """disarm_autopilot removes the marker AND flips live config off."""
    clear_autopilot_marker('conv-dis', user_id=1)
    put_task(_running_task('t-dis-1', 'conv-dis', autopilot=True))
    arm_autopilot('conv-dis', user_id=1)
    assert has_autopilot_marker('conv-dis', user_id=1) is True
    result = disarm_autopilot('conv-dis', user_id=1)
    assert result['markerCleared'] is True
    assert 't-dis-1' in result['taskIds']
    assert has_autopilot_marker('conv-dis', user_id=1) is False
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-dis-1']['config']['autopilot'] is False


def test_arm_only_targets_matching_conv(put_task):
    """Arming conv-X must not touch a running task for conv-Y."""
    clear_autopilot_marker('conv-X', user_id=1)
    clear_autopilot_marker('conv-Y', user_id=1)
    put_task(_running_task('t-x', 'conv-X'))
    put_task(_running_task('t-y', 'conv-Y'))
    result = arm_autopilot('conv-X', user_id=1)
    assert result['taskIds'] == ['t-x']
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-y']['config']['autopilot'] is False
    assert has_autopilot_marker('conv-Y', user_id=1) is False


# ── HTTP route: POST /api/v1/chat/autopilot/arm ────────────────────────

@pytest.mark.api
def test_arm_endpoint_defers_without_mutating_live_standard_task(
    flask_client, put_task,
):
    """A running standard turn cannot change interpreter at settlement."""
    put_task(_running_task('t-http-1', 'conv-http-1'))
    resp = flask_client.post('/api/v1/chat/autopilot/arm',
                             json={'convId': 'conv-http-1'})
    assert resp.status_code == 404
    body = resp.get_json()
    assert body['error'] == 'conversation_not_found'
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-http-1']['config']['autopilot'] is False


@pytest.mark.api
def test_arm_endpoint_requires_conv_id(flask_client):
    """Missing convId → 400."""
    resp = flask_client.post('/api/v1/chat/autopilot/arm', json={})
    assert resp.status_code == 400


@pytest.mark.api
def test_arm_endpoint_no_live_task(flask_client):
    """No live task defers to the next turn without a classic marker."""
    from tests._seed import seed_conversation

    conversation_id = 'conv-idle-goal-mode'
    seed_conversation(
        conversation_id,
        user_id=1,
        title='Idle Goal Mode',
        messages=[],
    )
    clear_autopilot_marker(conversation_id, user_id=1)
    resp = flask_client.post('/api/v1/chat/autopilot/arm',
                             json={'convId': conversation_id})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['armed'] is True
    assert body['taskIds'] == []
    assert body['deferred'] is True
    assert body['markerAdded'] is False
    assert has_autopilot_marker('conv-nonexistent-xyz', user_id=1) is False
    assert body['settingPersisted'] is True
    assert body['modeEnabled'] is True
    clear_autopilot_marker(conversation_id, user_id=1)


@pytest.mark.api
def test_arm_endpoint_does_not_claim_missing_conversation(flask_client):
    resp = flask_client.post(
        '/api/v1/chat/autopilot/arm',
        json={'convId': 'conv-goal-missing'},
    )
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'conversation_not_found'


@pytest.mark.api
def test_disarm_endpoint(flask_client):
    """POST /autopilot/disarm clears the marker and reports disarmed."""
    from lib.message_queue import arm_autopilot_marker
    arm_autopilot_marker('conv-dis-http', {}, user_id=1)
    resp = flask_client.post('/api/v1/chat/autopilot/disarm',
                             json={'convId': 'conv-dis-http'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['markerCleared'] is True
    assert has_autopilot_marker('conv-dis-http', user_id=1) is False
