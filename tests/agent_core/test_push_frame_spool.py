"""Push backlog delivery, temporary ownership and physical failure boundaries."""
import asyncio
import gc
import threading

import pytest

from lib.agent_core.push import PushClient, PushHub
from lib.agent_core.push_frames import PushFrameStore

pytestmark = pytest.mark.unit


def test_shared_large_publication_is_encoded_once_and_all_files_retire(monkeypatch):
    hub = PushHub()
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    clients = [PushClient(user_id=1) for _ in range(33)]
    body = '译' * (3 * 1024 * 1024)
    store = hub._frame_store
    try:
        for client in clients:
            hub.register(client)
            hub.subscribe(client, 'chat', 'task')
        hub._deliver_frame({'channel': 'chat', 'taskId': 'task', '_ownerUserId': '1', 'body': body})
        async def consume():
            for client in clients:
                assert (await client.drain())['body'] == body
                assert client._connected
        asyncio.run(consume())
        gc.collect()
        assert store.snapshot() == {'event_spool_files': 0, 'event_spool_bytes': 0}
    finally:
        hub.stop()
        for client in clients:
            hub.unregister(client)


def test_blocked_encoding_preserves_pongs_fifo_and_unrelated_work(monkeypatch):
    store = PushFrameStore(segment_bytes=128)
    entered, release = threading.Event(), threading.Event()
    encode = store._encode
    def blocked(frame):
        if frame.get('n') == 1:
            entered.set()
            assert release.wait(10)
        return encode(frame)
    monkeypatch.setattr(store, '_encode', blocked)
    client = PushClient(user_id=1)
    client._frame_store = store
    async def scenario():
        try:
            client.enqueue({'n': 1})
            assert await asyncio.to_thread(entered.wait, 2)
            client.enqueue({'n': 2})
            healthy = store.prepare({'n': 3})
            assert await asyncio.wrap_future(healthy)
            draining = asyncio.create_task(client.drain())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            client.enqueue_control({'type': 'pong', 't': 1})
            assert await asyncio.wait_for(draining, 2) == {'type': 'pong', 't': 1}
            release.set()
            assert await client.drain() == {'n': 1}
            assert await client.drain() == {'n': 2}
        finally:
            release.set()
            await client.aclose()
    asyncio.run(scenario())
    gc.collect()
    assert store.snapshot()['event_spool_files'] == 0


def test_partial_write_failure_preserves_prior_and_later_owned_ranges(monkeypatch):
    import lib.agent_core.push_frames as frames
    if not hasattr(frames.os, 'pwrite'):
        pytest.skip('independent cursor I/O unavailable')
    store = PushFrameStore(segment_bytes=1024)
    original = frames.os.pwrite
    failed = False
    def partial(fd, data, offset):
        nonlocal failed
        if bytes(data).startswith(b'{"bad"'):
            failed = True
            original(fd, data[:2], offset)
            raise OSError('injected partial write')
        return original(fd, data, offset)
    try:
        prior = store.prepare({'n': 1}).result(timeout=2)
        monkeypatch.setattr(frames.os, 'pwrite', partial)
        with pytest.raises(OSError, match='injected'):
            store.prepare({'bad': 1}).result(timeout=2)
        later = store.prepare({'n': 2}).result(timeout=2)
        assert failed
        assert prior.decode() == {'n': 1}
        assert later.decode() == {'n': 2}
    finally:
        store.close()


@pytest.mark.parametrize('unencodable', [object(), float('nan'), float('inf')],
                         ids=['object', 'nan', 'infinity'])
def test_encoding_error_disconnects_only_affected_client(unencodable):
    async def scenario():
        broken, healthy = PushClient(user_id=1), PushClient(user_id=1)
        try:
            broken.enqueue({'unencodable': unencodable})
            healthy.enqueue({'n': 1})
            assert await broken.drain() is None
            assert not broken._connected
            assert await healthy.drain() == {'n': 1}
            assert healthy._connected
        finally:
            await broken.aclose()
            await healthy.aclose()
    asyncio.run(scenario())


def test_cancelled_sender_keeps_owned_frame_for_another_drain(monkeypatch):
    client = PushClient(user_id=1)
    store = PushFrameStore()
    client._frame_store = store
    entered, release = threading.Event(), threading.Event()
    encode = store._encode
    def blocked(frame):
        entered.set()
        assert release.wait(10)
        return encode(frame)
    monkeypatch.setattr(store, '_encode', blocked)
    async def scenario():
        try:
            client.enqueue({'n': 1})
            assert await asyncio.to_thread(entered.wait, 2)
            waiting = asyncio.create_task(client.drain())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            release.set()
            assert await client.drain() == {'n': 1}
        finally:
            release.set()
            await client.aclose()
    asyncio.run(scenario())


def test_disconnect_joins_running_encoding_and_releases_pending_files(monkeypatch):
    client = PushClient(user_id=1)
    store = PushFrameStore()
    client._frame_store = store
    entered, release = threading.Event(), threading.Event()
    encode = store._encode
    def blocked(frame):
        entered.set()
        assert release.wait(10)
        return encode(frame)
    monkeypatch.setattr(store, '_encode', blocked)
    async def scenario():
        client.enqueue({'n': 1})
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        assert await client.drain() is None
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        store.close()
    gc.collect()
    assert store.snapshot()['event_spool_files'] == 0


def test_repeated_close_cancellation_still_joins_physical_encoder(monkeypatch):
    client = PushClient(user_id=1)
    store = PushFrameStore()
    client._frame_store = store
    entered, release = threading.Event(), threading.Event()
    encode = store._encode
    def blocked(frame):
        entered.set()
        assert release.wait(10)
        return encode(frame)
    monkeypatch.setattr(store, '_encode', blocked)
    async def scenario():
        client.enqueue({'n': 1})
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not store._executor.worker_threads_snapshot()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        store.close()


def test_publication_freezes_nested_values_before_background_encoding():
    store = PushFrameStore()
    original = {'nested': {'values': [1, 2]}}
    try:
        prepared = store.prepare(original)
        original['nested']['values'].append(3)
        assert prepared.result(timeout=2).decode() == {'nested': {'values': [1, 2]}}
    finally:
        store.close()


def test_portable_cursor_fallback_retains_exact_frames(monkeypatch):
    import lib.agent_core.push_frames as frames
    monkeypatch.delattr(frames.os, 'pread', raising=False)
    monkeypatch.delattr(frames.os, 'pwrite', raising=False)
    store = PushFrameStore(segment_bytes=16)
    try:
        first = store.prepare({'n': 1}).result(timeout=2)
        second = store.prepare({'n': 2}).result(timeout=2)
        assert first.decode() == {'n': 1}
        assert second.decode() == {'n': 2}
    finally:
        store.close()


def test_thirty_two_blocked_encoders_do_not_delay_healthy_thirty_third(monkeypatch):
    store = PushFrameStore()
    condition, release = threading.Condition(), threading.Event()
    entered = 0
    encode = store._encode
    def blocked(frame):
        nonlocal entered
        if frame.get('blocked'):
            with condition:
                entered += 1
                condition.notify_all()
            assert release.wait(10)
        return encode(frame)
    monkeypatch.setattr(store, '_encode', blocked)
    try:
        pending = [store.prepare({'blocked': True, 'n': n}) for n in range(32)]
        with condition:
            assert condition.wait_for(lambda: entered == 32, timeout=3)
        assert store.prepare({'healthy': True}).result(timeout=2).decode() == {'healthy': True}
        release.set()
        assert [frame.result(timeout=3).decode()['n'] for frame in pending] == list(range(32))
    finally:
        release.set()
        store.close()
    assert not store._executor.worker_threads_snapshot()


def test_worker_start_failure_is_explicit_and_leaves_no_physical_carrier(monkeypatch):
    start = threading.Thread.start
    def failed(thread):
        if thread.name.startswith('tofu-push-frame'):
            raise RuntimeError('injected worker start failure')
        return start(thread)
    async def scenario():
        client = PushClient(user_id=1)
        try:
            monkeypatch.setattr(threading.Thread, 'start', failed)
            client.enqueue({'n': 1})
            monkeypatch.setattr(threading.Thread, 'start', start)
            assert await client.drain() is None
            assert not client._connected
            assert not client._frame_store._executor.worker_threads_snapshot()
            assert client._frame_store.snapshot()['event_spool_files'] == 0
        finally:
            monkeypatch.setattr(threading.Thread, 'start', start)
            await client.aclose()
    asyncio.run(scenario())


def test_read_failure_closes_transport_instead_of_emitting_partial_frame():
    async def scenario():
        client = PushClient(user_id=1)
        try:
            client.enqueue({'n': 1})
            event = client._queue._queue[0]
            stored = await asyncio.wrap_future(event.prepared)
            stored.owner.file.truncate(0)
            assert await client.drain() is None
            assert not client._connected
            assert client._queue.empty()
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_short_reads_continue_until_the_complete_owned_range(monkeypatch):
    import lib.agent_core.push_frames as frames
    if not hasattr(frames.os, 'pread'):
        pytest.skip('independent cursor I/O unavailable')
    store = PushFrameStore()
    try:
        frame = store.prepare({'body': '完整数据', 'n': 1}).result(timeout=2)
        read = frames.os.pread
        monkeypatch.setattr(frames.os, 'pread', lambda fd, size, offset: read(fd, min(size, 3), offset))
        assert frame.decode() == {'body': '完整数据', 'n': 1}
    finally:
        store.close()


def test_snapshot_failure_does_not_republish_or_duplicate_delivery_observers(monkeypatch):
    class BrokenSnapshot:
        def __deepcopy__(self, memo):
            raise ValueError('injected snapshot failure')
    hub, client = PushHub(), PushClient(user_id=1)
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    observed = []
    hub.add_delivery_listener(lambda frame: observed.append(frame['taskId']))
    try:
        hub.register(client)
        hub.subscribe(client, 'chat', 'task')
        hub.push_event('chat', 'task', {'body': BrokenSnapshot()}, user_id=1)
        assert asyncio.run(client.drain()) is None
        assert observed == ['task']
    finally:
        hub.stop()
        hub.unregister(client)
