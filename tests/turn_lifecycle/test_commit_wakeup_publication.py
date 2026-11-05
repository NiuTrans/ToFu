"""Commit wakeups leave the per-task projection critical section, in commit order.

A task's durable Turn writes hold its projection lock across the storage
command. Post-commit wake publication (broker → Push Hub → frame encoding) is
shared with every conversation, so it must run after that lock is released
without reordering one task's notices.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lib.identity import PrincipalContext
from lib.storage import StorageRuntime, StorageSupervisor
from lib.storage.service import install_runtime_for_test

pytestmark = pytest.mark.unit

_PROJECTION_LOCK_KEY = '_turnProjectionStateLock'


@pytest.fixture
def storage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    runtime = StorageRuntime(supervisor, auto_restart=False)
    install_runtime_for_test(runtime)
    try:
        runtime.start()
        yield supervisor
    finally:
        install_runtime_for_test(None)


@pytest.fixture
def notify_client(monkeypatch):
    import lib.conversation_sync.broker  # noqa: F401 - subscribes post-commit wakeups
    from lib.agent_core.push import PushClient, hub

    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    client = PushClient(user_id=1)
    assert hub.register(client)
    hub.subscribe(client, 'notify', '*')
    try:
        yield client
    finally:
        hub.unregister(client)
        client.disconnect()


class _ObservedProjectionLock:
    """Re-entrant lock that reports ownership and can run one release hook."""

    def __init__(self):
        self._lock = threading.RLock()
        self.after_first_release = None

    def acquire(self, *args, **kwargs):
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()
        if not self._lock._is_owned():
            hook, self.after_first_release = self.after_first_release, None
            if hook is not None:
                hook()

    def owned(self) -> bool:
        return self._lock._is_owned()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc_info):
        self.release()


def _running_task(conv_id, task_id):
    from lib import turn_lifecycle

    created = turn_lifecycle.create_turn_pair(
        conv_id, command_id=f'{task_id}-pair',
        input_projection={'content': 'q'}, config={'model': 'gpt-4o'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'wake order', 'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    turn_lifecycle.bind_task(attempt_id, task_id, user_id=1)
    lock = _ObservedProjectionLock()
    task = {
        '_attemptId': attempt_id, 'id': task_id, 'status': 'running',
        '_userId': 1,
        '_principalContext': PrincipalContext.user(
            subject_id='test-user:1', owner_user_id=1,
        ).to_payload(),
        'content': 'seed', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
        _PROJECTION_LOCK_KEY: lock,
    }
    return task, lock


def _drain_notify_sequences(client, conv_id):
    from lib.conversation_sync.cursor import decode_cursor

    async def drain_ready():
        frames = []
        while not client._queue.empty():
            frames.append(await client.drain())
        return frames

    return [
        decode_cursor(conv_id, 1, frame['cursorHint'])
        for frame in asyncio.run(drain_ready())
        if frame.get('type') == 'conversation.invalidated'
        and frame.get('conversationId') == conv_id
    ]


def test_commit_wakeups_are_pushed_after_the_projection_lock_is_released(
        storage, notify_client, monkeypatch):
    from lib import turn_lifecycle
    from lib.agent_core.push import PushHub
    from tofu_workers.json_frames import JsonFrameStore

    task, lock = _running_task('wake-lock-conv', 'wake-lock-task')
    _drain_notify_sequences(notify_client, 'wake-lock-conv')
    observed: list[tuple[str, bool]] = []

    def instrument(owner, name):
        original = getattr(owner, name)

        def observed_call(self, *args, **kwargs):
            observed.append((name, lock.owned()))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(owner, name, observed_call)

    instrument(PushHub, 'push_event')
    instrument(JsonFrameStore, 'prepare')
    instrument(JsonFrameStore, '_submit')

    task['content'] = 'streamed text'
    assert turn_lifecycle.record_task_event(task, {'type': 'delta'})
    task['status'] = 'completed'
    assert turn_lifecycle.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'})

    names = {name for name, _owned in observed}
    assert {'push_event', 'prepare', '_submit'} <= names
    assert [entry for entry in observed if entry[1]] == []
    sequences = _drain_notify_sequences(notify_client, 'wake-lock-conv')
    assert len(sequences) == 2
    assert sequences == sorted(sequences)


def test_a_concurrent_recorder_cannot_publish_its_wakeup_first(
        storage, notify_client):
    from lib import turn_lifecycle
    from lib.storage.commit_events import subscribe_committed_events

    task, lock = _running_task('wake-order-conv', 'wake-order-task')
    _drain_notify_sequences(notify_client, 'wake-order-conv')
    notices: list[int] = []
    unsubscribe = subscribe_committed_events(lambda batch: notices.extend(
        int(item['event']['syncSeq']) for item in batch
        if item.get('event', {}).get('conversationId') == 'wake-order-conv'))
    recorded = []

    def second_recorder():
        task['content'] = 'second write'
        recorded.append(turn_lifecycle.record_task_event(task, {'type': 'delta'}))

    def interleave_second_commit():
        # The first writer has committed and released the projection lock,
        # but has not yet published. A second writer commits and publishes.
        thread = threading.Thread(target=second_recorder)
        thread.start()
        thread.join(30)

    try:
        task['content'] = 'first write'
        lock.after_first_release = interleave_second_commit
        recorded.append(turn_lifecycle.record_task_event(task, {'type': 'delta'}))
    finally:
        unsubscribe()

    assert recorded == [True, True]
    assert len(notices) == 2
    assert notices == sorted(notices)
    assert _drain_notify_sequences(notify_client, 'wake-order-conv') == notices
