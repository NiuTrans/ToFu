"""Latest-value push frames: a newer value replaces an unsent older value.

Cumulative frames (each value contains everything before it) may name a
latest-value key. Per socket and (channel, taskId) stream, a newer value of the
same key takes the queued position of an unsent older one; any other frame of
that stream fences the position, and a value the sender already dequeued is
never replaced. Superseded values are never copied, encoded or sent.
"""
import asyncio
import json

import pytest

from lib.agent_core.push import PushClient, PushHub
from tofu_workers import json_frames
from tofu_workers.json_frames import JsonFrameStore

pytestmark = pytest.mark.unit

KEY = 'text:block-1'


@pytest.fixture
def hub(monkeypatch):
    hub = PushHub()
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    try:
        yield hub
    finally:
        hub.stop()


def _client(hub, *subscriptions, user_id=1):
    client = PushClient(user_id=user_id)
    assert hub.register(client)
    for channel, task_id in subscriptions:
        hub.subscribe(client, channel, task_id)
    return client


def _drain_ready(client, *, encoded=False):
    async def drain():
        frames = []
        while not client._queue.empty():
            frame = await client.drain(encoded=encoded)
            frames.append(json.loads(frame) if encoded else frame)
        return frames
    return asyncio.run(drain())


def _preview(hub, value, *, key=KEY, task='task-1', user_id=1):
    hub.push_event('translate', task, {
        'type': 'running', 'status': 'running', 'partialByRound': {key: value},
    }, user_id=user_id, latest_value_key=key)


def _values(frames):
    return [
        next(iter(frame['partialByRound'].values()))
        if 'partialByRound' in frame else frame['type']
        for frame in frames
    ]


def test_newer_values_replace_an_unsent_older_value(hub):
    client = _client(hub, ('translate', '*'))
    for size in range(1, 51):
        _preview(hub, '译' * size)
    frames = _drain_ready(client)
    assert _values(frames) == ['译' * 50]
    assert frames[0]['channel'] == 'translate' and frames[0]['taskId'] == 'task-1'
    assert '_latestValueKey' not in frames[0] and '_ownerUserId' not in frames[0]


def test_superseded_values_are_never_copied_or_encoded(hub, monkeypatch):
    calls = {'deepcopy': 0, 'prepare': 0, 'submit': 0}
    deepcopy, prepare, submit = (
        json_frames.copy.deepcopy, JsonFrameStore.prepare, JsonFrameStore._submit)

    def counted(name, function):
        def call(*args, **kwargs):
            calls[name] += 1
            return function(*args, **kwargs)
        return call

    monkeypatch.setattr(json_frames.copy, 'deepcopy', counted('deepcopy', deepcopy))
    monkeypatch.setattr(JsonFrameStore, 'prepare', counted('prepare', prepare))
    monkeypatch.setattr(JsonFrameStore, '_submit', counted('submit', submit))
    client = _client(hub, ('translate', '*'))
    for size in range(1, 201):
        _preview(hub, 'x' * size)
    assert calls == {'deepcopy': 0, 'prepare': 0, 'submit': 0}
    assert _values(_drain_ready(client, encoded=True)) == ['x' * 200]
    assert calls == {'deepcopy': 0, 'prepare': 0, 'submit': 1}


def test_a_value_the_sender_dequeued_is_never_replaced(hub):
    client = _client(hub, ('translate', '*'))

    async def scenario():
        _preview(hub, 'a')
        first = await client.drain()
        _preview(hub, 'ab')
        _preview(hub, 'abc')
        second = await client.drain()
        return [first, second]

    assert _values(asyncio.run(scenario())) == ['a', 'abc']
    assert _drain_ready(client) == []


def test_a_later_frame_of_the_same_stream_fences_the_position(hub):
    client = _client(hub, ('translate', '*'))
    _preview(hub, 'a')
    hub.push_event('translate', 'task-1', {'type': 'status'}, user_id=1)
    _preview(hub, 'ab')
    _preview(hub, 'abc')
    assert _values(_drain_ready(client)) == ['a', 'status', 'abc']


def test_other_streams_neither_fence_nor_lose_their_frames(hub):
    client = _client(hub, ('translate', '*'), ('notify', '*'))
    _preview(hub, 'a')
    hub.push_event('notify', 'conversation', {'type': 'invalidated'}, user_id=1)
    _preview(hub, 'ab', task='task-2')
    _preview(hub, 'abc')
    frames = _drain_ready(client)
    assert [(frame['taskId'], value) for frame, value in zip(frames, _values(frames))] == [
        ('task-1', 'abc'), ('conversation', 'invalidated'), ('task-2', 'ab')]


def test_distinct_keys_and_owners_never_supersede_each_other(hub):
    owner = _client(hub, ('translate', '*'))
    other_owner = _client(hub, ('translate', '*'), user_id=2)
    _preview(hub, 'one', key='text:block-1')
    _preview(hub, 'two', key='text:block-2')
    _preview(hub, 'one more', key='text:block-1')
    _preview(hub, 'foreign', user_id=2)
    assert _values(_drain_ready(owner)) == ['one', 'two', 'one more']
    assert _values(_drain_ready(other_owner)) == ['foreign']


def test_each_socket_receives_its_own_newest_value(hub):
    early = _client(hub, ('translate', '*'))
    _preview(hub, 'a')
    late = _client(hub, ('translate', '*'))
    _preview(hub, 'ab')

    async def claim_early():
        return await early.drain()

    assert _values([asyncio.run(claim_early())]) == ['ab']
    _preview(hub, 'abc')
    assert _values(_drain_ready(early)) == ['abc']
    assert _values(_drain_ready(late)) == ['abc']


def test_sockets_sending_the_same_value_share_one_encoding(hub, monkeypatch):
    encodes = []
    encode = JsonFrameStore.encode

    def counted(self, frame, **kwargs):
        encodes.append(kwargs)
        return encode(self, frame, **kwargs)

    monkeypatch.setattr(JsonFrameStore, 'encode', counted)
    clients = [_client(hub, ('translate', '*')) for _ in range(3)]
    _preview(hub, 'shared')
    texts = [asyncio.run(client.drain(encoded=True)) for client in clients]
    assert len(set(texts)) == 1 and json.loads(texts[0])['partialByRound'] == {KEY: 'shared'}
    assert len(encodes) == 1


def test_frame_only_clients_and_listeners_observe_every_value(hub):
    class FrameClient:
        user_id = '1'

        def __init__(self):
            self.frames = []

        def enqueue(self, frame):
            self.frames.append(frame)

    plugin = FrameClient()
    hub.subscribe(plugin, 'translate', '*')
    observed = []
    hub.add_listener(lambda channel, task_id, payload: observed.append(payload))
    for value in ('a', 'ab', 'abc'):
        _preview(hub, value)
    assert _values(plugin.frames) == ['a', 'ab', 'abc']
    assert _values(observed) == ['a', 'ab', 'abc']
    assert not any('_latestValueKey' in frame for frame in plugin.frames + observed)


def test_latest_value_identity_crosses_the_fanout_bus(hub):
    published = []

    class JsonBus:
        def start(self):
            pass

        def stop(self):
            pass

        def publish(self, frame):
            wire = json.dumps(frame)
            published.append(json.loads(wire))
            hub._deliver_frame(json.loads(wire))

    hub._bus = JsonBus()
    client = _client(hub, ('translate', '*'))
    _preview(hub, 'a')
    _preview(hub, 'ab')
    assert [frame['_latestValueKey'] for frame in published] == [KEY, KEY]
    frames = _drain_ready(client)
    assert _values(frames) == ['ab'] and '_latestValueKey' not in frames[0]


def test_disconnect_releases_queued_values(hub):
    client = _client(hub, ('translate', '*'))
    _preview(hub, 'a')
    client.disconnect()
    _preview(hub, 'ab')
    assert client._queue.empty()
    assert asyncio.run(client.drain()) is None
