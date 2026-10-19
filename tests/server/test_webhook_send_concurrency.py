"""Independent HTTP progress with ordered, owner-bound lanes and joined stop."""
import threading
from queue import Queue

import pytest
from routes.api_v1 import webhooks as wh
from tofu_workers.json_frames import JsonFrameStore

pytestmark = pytest.mark.unit


def _item(owner, subscription, n=0):
    return {'sub':{'id':subscription,'owner_user_id':str(owner)},'n':n,'attempt':0}


@pytest.fixture
def running(monkeypatch):
    queue, stop = Queue(), threading.Event()
    store = JsonFrameStore(segment_bytes=128,reuse_workers=4,thread_name_prefix='webhook-http-test')
    monkeypatch.setattr(wh, '_QUEUE', queue)
    monkeypatch.setattr(wh, '_WORKER_STOP', stop)
    monkeypatch.setattr(wh, '_FRAME_STORE', store)
    monkeypatch.setattr(wh, '_LISTENER_REGISTERED', False)
    monkeypatch.setattr(wh, '_subscription_is_active', lambda sub: True)
    worker = threading.Thread(target=wh._worker_loop)
    monkeypatch.setattr(wh, '_WORKER_THREAD', worker)
    try:
        yield queue, stop, store, worker
    finally:
        stop.set()
        queue.put(None)
        if worker.ident is not None:worker.join(3)
        store.close()
        assert not worker.is_alive()


def test_thirty_two_blocked_endpoints_do_not_delay_healthy_thirty_third(running, monkeypatch):
    queue, stop, store, worker = running
    condition, release, healthy = threading.Condition(), threading.Event(), threading.Event()
    entered = set()
    def deliver(item):
        if item['sub']['id'] == 'healthy':
            healthy.set()
            return True
        with condition:
            entered.add(item['sub']['id'])
            condition.notify_all()
        assert release.wait(10)
        return True
    monkeypatch.setattr(wh, '_deliver', deliver)
    worker.start()
    try:
        for n in range(32):queue.put(_item(1,f'blocked-{n}'))
        with condition:assert condition.wait_for(lambda:len(entered)==32,3)
        queue.put(_item(1,'healthy'))
        assert healthy.wait(2)
        assert not release.is_set()
        assert not wh.stop_webhook_worker(0.02)
        assert wh._FRAME_STORE is store
        release.set()
        assert wh.stop_webhook_worker(3)
        assert not store._executor.worker_threads_snapshot()
    finally:release.set()


def test_same_subscription_stays_ordered_while_another_owner_progresses(running, monkeypatch):
    queue, stop, store, worker = running
    first, release, third, other = (threading.Event() for _ in range(4))
    seen = []
    def deliver(item):
        if item['sub']['owner_user_id']=='2':
            other.set()
            return True
        seen.append(item['n'])
        if item['n']==0:
            first.set()
            assert release.wait(10)
        if item['n']==2:third.set()
        return True
    monkeypatch.setattr(wh, '_deliver', deliver)
    worker.start()
    try:
        for n in range(3):queue.put(_item(1,'same-id',n))
        assert first.wait(2)
        queue.put(_item(2,'same-id'))
        assert other.wait(2)
        assert seen == [0]
        release.set()
        assert third.wait(2)
        assert seen == [0,1,2]
    finally:release.set()


def test_revocation_is_rechecked_at_physical_entry(running, monkeypatch):
    queue, stop, store, worker = running
    entered, release, healthy = threading.Event(), threading.Event(), threading.Event()
    active = {'target':True,'healthy':True}
    seen=[]
    monkeypatch.setattr(wh, '_subscription_is_active', lambda sub:active[sub['id']])
    def deliver(item):
        if item['sub']['id']=='healthy':healthy.set();return True
        seen.append(item['n'])
        entered.set()
        assert release.wait(10)
        return True
    monkeypatch.setattr(wh, '_deliver', deliver)
    worker.start()
    try:
        for n in range(3):queue.put(_item(1,'target',n))
        assert entered.wait(2)
        active['target']=False
        release.set()
        queue.put(_item(1,'healthy'))
        assert healthy.wait(2)
        queue.join()
        assert seen==[0]
    finally:release.set()


def test_real_start_stop_registers_and_withdraws_listener(monkeypatch):
    from lib.agent_core.push import hub
    monkeypatch.setattr(wh, '_QUEUE', Queue())
    monkeypatch.setattr(wh, '_WORKER_THREAD', None)
    monkeypatch.setattr(wh, '_WORKER_STARTED', False)
    monkeypatch.setattr(wh, '_FRAME_STORE', None)
    monkeypatch.setattr(wh, '_LISTENER_REGISTERED', False)
    monkeypatch.setattr(wh, '_WORKER_STOP', threading.Event())
    try:
        wh._ensure_worker_started()
        assert wh._WORKER_THREAD.is_alive()
        assert wh._on_push_event in hub._listeners
        assert wh.stop_webhook_worker(3)
        assert wh._on_push_event not in hub._listeners
    finally:
        wh.stop_webhook_worker(3)


@pytest.mark.parametrize('partial', [False, True])
def test_worker_start_failure_cannot_dispatch_before_activation(monkeypatch, partial):
    queue = Queue()
    queue.put(_item(1,'pending'))
    monkeypatch.setattr(wh, '_QUEUE', queue)
    monkeypatch.setattr(wh, '_WORKER_THREAD', None)
    monkeypatch.setattr(wh, '_WORKER_STARTED', False)
    monkeypatch.setattr(wh, '_FRAME_STORE', None)
    monkeypatch.setattr(wh, '_LISTENER_REGISTERED', False)
    monkeypatch.setattr(wh, '_WORKER_STOP', threading.Event())
    calls = []
    monkeypatch.setattr(wh, '_deliver', lambda item:calls.append(item) or True)
    start = threading.Thread.start
    def fail(thread):
        if thread.name == 'webhook-worker':
            if partial:start(thread)
            raise RuntimeError('injected activation failure')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail)
    with pytest.raises(RuntimeError, match='activation'):
        wh._ensure_worker_started()
    if wh._WORKER_THREAD.ident is not None:wh._WORKER_THREAD.join(2)
    assert calls == []
    assert not wh._WORKER_STARTED
    assert wh.stop_webhook_worker(2)


def test_retries_preserve_head_order_and_do_not_discard_after_five_attempts(running, monkeypatch):
    queue, stop, store, worker = running
    complete = threading.Event()
    calls = []
    def deliver(item):
        calls.append(item['n'])
        if item['n']==0 and len(calls)<=10:
            item['_last_status']=503
            return False
        if item['n']==1:complete.set()
        return True
    monkeypatch.setattr(wh, '_deliver', deliver)
    monkeypatch.setattr(wh, '_schedule_retry', lambda delayed,item,now=None,**kwargs:delayed.schedule(item,now=now,ready_at=now))
    worker.start()
    queue.put(_item(1,'ordered',0))
    queue.put(_item(1,'ordered',1))
    assert complete.wait(3)
    assert calls==[0]*11+[1]


def test_live_subscription_identity_includes_owner(monkeypatch):
    monkeypatch.setattr(wh, '_load', lambda:[{'id':'same','owner_user_id':'2','disabled':False}])
    assert not wh._subscription_is_active({'id':'same','owner_user_id':'1'})
    assert wh._subscription_is_active({'id':'same','owner_user_id':'2'})
