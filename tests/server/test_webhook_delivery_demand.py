"""Webhook temporary residency, unlimited retry retention and physical close."""
import gc
import json
import threading
from queue import Empty
from types import SimpleNamespace

import pytest
from routes.api_v1 import webhooks as wh
from tofu_workers.json_frames import JsonFrameStore

pytestmark = pytest.mark.unit


@pytest.fixture
def delivery(monkeypatch):
    wh._WORKER_STOP.clear()
    store = JsonFrameStore(segment_bytes=1024 * 1024, reuse_workers=4,
                          thread_name_prefix='webhook-demand-test')
    queue = wh._DeliveryQueue()
    subscriptions = [{'id':f'wh_{n}', 'owner_user_id':'1', 'url':'https://example.com/callback',
                      'secret':'fixture-secret', 'channel':'chat', 'task_id':'*',
                      'event_types':[], 'disabled':False} for n in range(33)]
    monkeypatch.setattr(wh, '_FRAME_STORE', store)
    monkeypatch.setattr(wh, '_QUEUE', queue)
    monkeypatch.setattr(wh, '_subscription_snapshot', lambda: tuple(subscriptions))
    monkeypatch.setattr(wh, '_validate_webhook_url', lambda *args, **kwargs: None)
    monkeypatch.setattr(wh, '_DROP_COUNTS', {})
    try:
        yield store, queue, subscriptions
    finally:
        store.close()
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except Empty:
                break


def test_large_backlog_shares_each_body_and_retains_all_retries(delivery):
    store, queue, subscriptions = delivery
    body = '译' * 1000
    for n in range(129):
        wh._on_push_event('chat', 'task', {'_ownerUserId':'1', 'n':n, 'body':body})
    assert queue.qsize() == 4257
    delayed = wh._RetryHeap()
    for n in range(129):
        first = None
        for sub in subscriptions:
            item = queue.get_nowait()
            if first is None:
                first = item['_event_frame']
            assert item['_event_frame'] is first
            assert json.loads(first.result(timeout=3).text()) == {'n':n, 'body':body}
            assert item['sub']['id'] == sub['id']
            assert 'event_json' not in item
            assert wh._schedule_retry(delayed, item, now=10)
            queue.task_done()
    assert len(delayed) == 4257
    for _ in range(4257):
        item = delayed.pop_ready(12)
        assert item is not None
    del item, first, delayed
    gc.collect()
    assert store.snapshot()['event_spool_files'] == 0
    assert not wh._DROP_COUNTS


def test_transient_retries_are_not_discarded_after_old_attempt_ceiling():
    delayed = wh._RetryHeap()
    item = {'attempt':0, '_retained_bytes':1024}
    now = 0
    for _ in range(25):
        assert wh._schedule_retry(delayed, item, now=now)
        ready = delayed.next_ready_at()
        assert ready - now <= 60
        assert delayed.pop_ready(ready) is item
        now = ready
    assert item['attempt'] == 25


def test_large_signed_http_body_is_complete_and_owner_marker_is_private(delivery, monkeypatch):
    store, queue, subscriptions = delivery
    subscriptions[:] = subscriptions[:1]
    body = '译' * (3 * 1024 * 1024)
    wh._on_push_event('chat', 'task', {'_ownerUserId':'1', 'body':body})
    item = queue.get_nowait()
    captured = {}
    def post(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(wh, 'http_post', post)
    assert wh._deliver(item)
    envelope = json.loads(captured['data'])
    assert envelope['event'] == {'body':body}
    assert captured['allow_redirects'] is False
    headers = captured['headers']
    assert headers['X-Tofu-Signature'] == 'v1=' + wh._sign(
        item['sub']['secret'], captured['data'].decode('utf-8'), headers['X-Tofu-Timestamp'])
    queue.task_done()


def test_stop_retains_live_encoder_owner_until_actual_join(delivery, monkeypatch):
    store, queue, subscriptions = delivery
    entered, release = threading.Event(), threading.Event()
    encode = store._encode
    def blocked(frame):
        entered.set()
        assert release.wait(10)
        return encode(frame)
    monkeypatch.setattr(store, '_encode', blocked)
    monkeypatch.setattr(wh, '_WORKER_THREAD', None)
    monkeypatch.setattr(wh, '_LISTENER_REGISTERED', False)
    try:
        wh._on_push_event('chat', 'task', {'_ownerUserId':'1', 'body':'owned'})
        assert entered.wait(3)
        assert wh.stop_webhook_worker(timeout=0.02) is False
        assert wh._FRAME_STORE is store
        assert store._close_thread.is_alive()
        release.set()
        assert wh.stop_webhook_worker(timeout=3) is True
        assert not store._close_thread.is_alive()
        assert not store._executor.worker_threads_snapshot()
        assert wh._FRAME_STORE is None
    finally:
        release.set()
        store.close()


def test_bounded_close_keeps_shutdown_callbacks_off_the_observer_thread(monkeypatch):
    store = JsonFrameStore(segment_bytes=128, reuse_workers=1)
    store.prepare({'body':'owned'}).result(timeout=2)
    entered, release = threading.Event(), threading.Event()
    shutdown = store._executor.shutdown
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return shutdown(*args, **kwargs)
    monkeypatch.setattr(store._executor, 'shutdown', blocked)
    try:
        assert store.close_with_timeout(0.02) is False
        assert entered.is_set()
        assert store._close_thread.is_alive()
        release.set()
        assert store.close_with_timeout(2)
    finally:
        release.set()
        store.close()
