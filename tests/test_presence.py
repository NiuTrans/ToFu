"""Owner isolation and lifecycle contracts for process-local presence."""

from __future__ import annotations

import os
import threading
import time

import pytest

import lib.presence.registry as registry

pytestmark = pytest.mark.unit

OWNER_A = 17
OWNER_B = 23


@pytest.fixture
def captured_frames(monkeypatch):
    frames: list[dict] = []

    def capture(channel, task_id, payload, *, user_id=None):
        frames.append({
            'channel': channel,
            'taskId': task_id,
            'userId': user_id,
            **payload,
        })

    import lib.agent_core.push as push

    monkeypatch.setattr(push, 'push_event', capture)
    return frames


@pytest.fixture
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry, '_state', {})
    monkeypatch.setattr(registry, '_sweeper_started', True)
    return registry


@pytest.fixture
def project_root(tmp_path):
    return str(tmp_path / 'project')


def _scope(root: str, owner: int = OWNER_A):
    return owner, os.path.abspath(root)


def test_announce_is_owner_scoped_and_never_writes_project_files(
    fresh_registry, captured_frames, project_root
):
    registry.announce(
        project_root,
        'conv-a',
        user_id=OWNER_A,
        task_id='task-a',
        title='Fix parser',
        phase='working',
    )

    peer = registry.snapshot(project_root, user_id=OWNER_A)['peers'][0]
    assert peer['convId'] == 'conv-a'
    assert peer['status'] == 'active'
    assert peer['statusLabel'] == 'working'
    assert registry.snapshot(project_root, user_id=OWNER_B)['peers'] == []
    assert not os.path.exists(os.path.join(project_root, '.tofu', 'presence'))
    assert captured_frames[-1]['userId'] == OWNER_A


def test_same_project_and_conversation_are_independent_between_owners(
    fresh_registry, captured_frames, project_root
):
    registry.announce(
        project_root, 'same-conv', user_id=OWNER_A, title='Owner A')
    registry.announce(
        project_root, 'same-conv', user_id=OWNER_B, title='Owner B')

    registry.mark_idle(project_root, 'same-conv', user_id=OWNER_A)

    assert registry.snapshot(project_root, user_id=OWNER_A)['peers'] == []
    peers_b = registry.snapshot(project_root, user_id=OWNER_B)['peers']
    assert [peer['title'] for peer in peers_b] == ['Owner B']


def test_reannounce_preserves_files_and_started_timestamp(
    fresh_registry, captured_frames, project_root
):
    registry.announce(project_root, 'conv-a', user_id=OWNER_A, task_id='t1')
    registry.record_files(
        project_root,
        'conv-a',
        [{'path': 'a.py', 'action': 'written'}],
        user_id=OWNER_A,
    )
    started = registry._state[_scope(project_root)]['conv-a']['startedTs']

    registry.announce(project_root, 'conv-a', user_id=OWNER_A, task_id='t2')
    peer = registry._state[_scope(project_root)]['conv-a']

    assert peer['files'] == ['a.py']
    assert peer['startedTs'] == started
    assert peer['taskId'] == 't2'


def test_record_files_unions_paths_and_forms_backend_label(
    fresh_registry, captured_frames, project_root
):
    registry.announce(project_root, 'conv-a', user_id=OWNER_A)
    registry.record_files(
        project_root,
        'conv-a',
        [{'path': 'a.py'}, {'path': 'b.py'}],
        user_id=OWNER_A,
    )
    registry.record_files(
        project_root,
        'conv-a',
        [{'path': 'b.py'}, {'path': 'c.py'}],
        user_id=OWNER_A,
    )

    peer = registry.snapshot(project_root, user_id=OWNER_A)['peers'][0]
    assert peer['files'] == ['a.py', 'b.py', 'c.py']
    assert peer['currentFile'] == 'c.py'
    assert peer['statusLabel'] == 'editing c.py'


def test_conflicts_are_detected_only_inside_one_owner_scope(
    fresh_registry, captured_frames, project_root
):
    registry.announce(
        project_root, 'conv-a', user_id=OWNER_A, title='Alpha')
    registry.announce(
        project_root, 'conv-b', user_id=OWNER_A, title='Beta')
    registry.announce(
        project_root, 'conv-c', user_id=OWNER_B, title='Other owner')
    registry.record_files(
        project_root, 'conv-a', [{'path': 'shared.py'}], user_id=OWNER_A)
    registry.record_files(
        project_root, 'conv-c', [{'path': 'shared.py'}], user_id=OWNER_B)
    assert not [frame for frame in captured_frames if frame.get('kind') == 'conflict']

    registry.record_files(
        project_root, 'conv-b', [{'path': 'shared.py'}], user_id=OWNER_A)
    conflicts = [
        frame for frame in captured_frames if frame.get('kind') == 'conflict'
    ]
    assert len(conflicts) == 1
    assert conflicts[0]['userId'] == OWNER_A
    assert set(conflicts[0]['conflict']['peers']) == {'conv-a', 'conv-b'}


def test_subagents_have_distinct_composite_identities(
    fresh_registry, captured_frames, project_root
):
    for agent_id in ('agent-1', 'agent-2'):
        registry.announce(
            project_root,
            'conv-a',
            user_id=OWNER_A,
            agent_id=agent_id,
            title=agent_id,
        )
        registry.record_files(
            project_root,
            'conv-a',
            [{'path': 'shared.py'}],
            user_id=OWNER_A,
            agent_id=agent_id,
        )

    conflict = [
        frame for frame in captured_frames if frame.get('kind') == 'conflict'
    ][-1]
    assert set(conflict['conflict']['peers']) == {
        'conv-a#agent-1',
        'conv-a#agent-2',
    }


def test_mark_idle_then_depart_targets_one_owner_and_peer(
    fresh_registry, captured_frames, project_root
):
    registry.announce(project_root, 'conv-a', user_id=OWNER_A)
    registry.announce(
        project_root, 'conv-a', user_id=OWNER_A, agent_id='agent-1')

    registry.mark_idle(
        project_root, 'conv-a', user_id=OWNER_A, agent_id='agent-1')
    assert len(registry.snapshot(project_root, user_id=OWNER_A)['peers']) == 1

    registry.depart(project_root, 'conv-a', user_id=OWNER_A)
    assert registry.snapshot(project_root, user_id=OWNER_A)['peers'] == []


def test_sweep_transitions_once_then_reaps_with_owner_filtered_frames(
    fresh_registry, captured_frames, project_root
):
    registry.announce(project_root, 'conv-a', user_id=OWNER_A)
    peer = registry._state[_scope(project_root)]['conv-a']
    peer['lastBeatTs'] = int(
        (time.time() - registry.ACTIVE_TTL_SEC - 2) * 1000)
    captured_frames.clear()

    assert registry.sweep() == 0
    assert registry.sweep() == 0
    idle = [
        frame
        for frame in captured_frames
        if frame.get('kind') == 'update'
        and frame.get('peer', {}).get('status') == 'idle'
    ]
    assert len(idle) == 1
    assert idle[0]['userId'] == OWNER_A

    peer['lastBeatTs'] = int(
        (time.time() - registry.IDLE_TTL_SEC - 2) * 1000)
    assert registry.sweep() == 1
    assert _scope(project_root) not in registry._state
    assert captured_frames[-1]['kind'] == 'depart'
    assert captured_frames[-1]['userId'] == OWNER_A


def test_push_delivery_occurs_outside_registry_lock(
    fresh_registry, monkeypatch, project_root
):
    observed_while_locked: list[str] = []

    def probe(_channel, _task_id, payload, *, user_id=None):
        acquired = {'value': False}

        def attempt():
            acquired['value'] = registry._lock.acquire(blocking=False)
            if acquired['value']:
                registry._lock.release()

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join()
        if not acquired['value']:
            observed_while_locked.append(payload.get('kind', '?'))

    import lib.agent_core.push as push

    monkeypatch.setattr(push, 'push_event', probe)
    registry.announce(project_root, 'conv-a', user_id=OWNER_A)
    registry.heartbeat(
        project_root, 'conv-a', user_id=OWNER_A, phase='generating')
    registry.record_files(
        project_root, 'conv-a', [{'path': 'a.py'}], user_id=OWNER_A)
    registry.mark_idle(project_root, 'conv-a', user_id=OWNER_A)
    registry.depart(project_root, 'conv-a', user_id=OWNER_A)

    assert observed_while_locked == []


def test_empty_registry_owns_no_sweeper_thread(monkeypatch):
    monkeypatch.setattr(registry, '_state', {})
    monkeypatch.setattr(registry, '_sweeper_started', False)
    monkeypatch.setattr(registry, '_sweeper_thread', None)

    assert registry.start_sweeper() is False
    assert registry._sweeper_thread is None
    assert registry._sweeper_started is False


def test_sweeper_retires_empty_batch_and_later_announce_restarts(
        monkeypatch, project_root):
    now = time.time()
    stale_peer = {
        'convId': 'stale-conv',
        'agentId': '',
        'lastBeatTs': int((now - registry.IDLE_TTL_SEC - 1) * 1000),
    }
    monkeypatch.setattr(
        registry, '_state', {_scope(project_root): {'stale-conv': stale_peer}})
    monkeypatch.setattr(registry, '_sweeper_started', True)
    monkeypatch.setattr(registry, '_sweeper_stop', threading.Event())
    monkeypatch.setattr(registry, '_broadcast', lambda *_args, **_kwargs: None)
    current_owner = threading.current_thread()
    monkeypatch.setattr(registry, '_sweeper_thread', current_owner)

    registry._sweep_loop(0)

    assert registry._state == {}
    assert registry._sweeper_thread is None
    assert registry._sweeper_started is False

    registry.announce(project_root, 'new-conv', user_id=OWNER_A)
    replacement = registry._sweeper_thread
    assert replacement is not None
    assert replacement is not current_owner
    assert replacement.is_alive()
    assert registry.stop_sweeper(timeout=1.0) is True


def test_empty_retirement_cannot_detach_newer_owner(monkeypatch):
    old_owner = threading.current_thread()
    newer_owner = object()
    monkeypatch.setattr(registry, '_state', {})
    monkeypatch.setattr(registry, '_sweeper_started', True)
    monkeypatch.setattr(registry, '_sweeper_thread', newer_owner)

    assert registry._retire_sweeper_if_empty(old_owner) is False
    assert registry._sweeper_thread is newer_owner
    assert registry._sweeper_started is True


def test_sweep_interval_is_bounded_against_busy_loops():
    assert registry._bounded_sweep_interval(0) == 10
    assert registry._bounded_sweep_interval(float('nan')) == 10
    assert registry._bounded_sweep_interval(0.001) == 0.1
    assert registry._bounded_sweep_interval(999) == registry.ACTIVE_TTL_SEC


def test_sweeper_start_failure_releases_unstarted_owner(
        monkeypatch, project_root):
    class BrokenThread:
        @staticmethod
        def start():
            raise RuntimeError('injected start failure')

    monkeypatch.setattr(
        registry, '_state', {_scope(project_root): {'peer': {}}})
    monkeypatch.setattr(registry, '_sweeper_started', False)
    monkeypatch.setattr(registry, '_sweeper_thread', None)
    monkeypatch.setattr(
        registry.threading, 'Thread', lambda **_kwargs: BrokenThread())

    with pytest.raises(RuntimeError, match='injected start failure'):
        registry.start_sweeper()

    assert registry._sweeper_thread is None
    assert registry._sweeper_started is False


def test_ephemeral_announce_survives_sweeper_start_failure(
        monkeypatch, project_root):
    class BrokenThread:
        @staticmethod
        def start():
            raise RuntimeError('injected start failure')

    monkeypatch.setattr(registry, '_state', {})
    monkeypatch.setattr(registry, '_sweeper_started', False)
    monkeypatch.setattr(registry, '_sweeper_thread', None)
    monkeypatch.setattr(registry, '_broadcast', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        registry.threading, 'Thread', lambda **_kwargs: BrokenThread())

    registry.announce(project_root, 'conv-a', user_id=OWNER_A)

    assert [peer['convId'] for peer in registry.snapshot(
        project_root, user_id=OWNER_A)['peers']] == ['conv-a']
    assert registry._sweeper_thread is None
    assert registry._sweeper_started is False
