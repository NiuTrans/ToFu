"""Child delivery closes publication, not arbitrary blocked Python callbacks."""
import threading

import pytest

from lib.agent_core.publication import ChildPublicationFence, commit_child_publication
from lib.tasks_pkg.manager import _provider_ingress_guard as guard

pytestmark = pytest.mark.unit


def _carrier(name):
    return {'id': name, '_invocationPublicationFence': ChildPublicationFence(),
            'abort_event': threading.Event()}


def _enqueue(task, token, callback, sequence=0):
    return guard.enqueue_ingress_delivery(
        task, token=token, sequence=sequence, event_type='delta', deliver=callback)


def test_child_return_revokes_commit_inside_already_entered_sink(monkeypatch):
    from lib.tasks_pkg import child_invocation as owner
    from lib.tasks_pkg.invocation import deliver_invocation_event
    entered, release = threading.Event(), threading.Event()
    mutations = []
    child = _carrier('lifecycle-return-race')
    child['_invocationPolicy'] = object()
    child['_invocationAbortCheck'] = lambda: False

    def sink(event):
        entered.set()
        assert release.wait(5)
        commit_child_publication(lambda: mutations.append(event))

    child['_invocationEventSink'] = lambda event: child['_invocationPublicationFence'].deliver(sink, event)
    monkeypatch.setattr(guard, '_CHILD_FLUSH_TIMEOUT_SEC', 0.01)

    def execute(task, **kwargs):
        token = guard.begin_provider_ingress(task, span_id='blocked')
        _enqueue(task, token, lambda: deliver_invocation_event(task, {'type': 'delta'}))
        assert entered.wait(5)
        for sequence in range(1, 10):
            _enqueue(task, token, lambda: mutations.append('queued'), sequence)

    monkeypatch.setattr(owner, 'execute_invocation', execute)
    try:
        with pytest.raises(RuntimeError, match='evidence delivery failed'):
            owner.run_child_invocation(child)
        state = child['_providerIngressIsolation']
        assert state['worker'].is_alive()
        assert state['queue'].qsize() <= 1  # only wake sentinel survives
        assert state['flushIncomplete']
        assert '_invocationEventSink' not in child
        assert mutations == []
    finally:
        release.set()
        child['_providerIngressIsolation']['worker'].join(5)
    assert not state['worker'].is_alive()
    assert mutations == []


def test_wedged_child_delivery_cannot_refuse_another_child(monkeypatch):
    monkeypatch.setattr(guard, '_CHILD_FLUSH_TIMEOUT_SEC', 0.01)
    release, entered = threading.Event(), threading.Event()
    blocked = _carrier('blocked-delivery')
    token = guard.begin_provider_ingress(blocked, span_id='blocked')
    def block():
        entered.set()
        assert release.wait(5)
    _enqueue(blocked, token, block)
    try:
        assert entered.wait(1)
        receipt = guard.end_provider_ingress(blocked, token=token, disposing=True)
        assert receipt['flushIncomplete']
        assert blocked['_providerIngressIsolation']['worker'].is_alive()
        for index in range(24):
            delivered = threading.Event()
            child = _carrier(f'independent-{index}')
            child_token = guard.begin_provider_ingress(child, span_id='normal')
            assert _enqueue(child, child_token, delivered.set)
            assert delivered.wait(1)
            receipt = guard.end_provider_ingress(child, token=child_token)
            assert not receipt['flushIncomplete']
            assert not child.get('_invocationDeliveryError')
            child['_providerIngressIsolation']['worker'].join(1)
    finally:
        release.set()
        blocked['_providerIngressIsolation']['worker'].join(5)
    assert not blocked['_providerIngressIsolation']['worker'].is_alive()


def test_child_event_backlog_is_delivered_in_order_without_queue_capacity_failure():
    child = _carrier('delivery-backlog')
    entered, release = threading.Event(), threading.Event()
    observed = []
    def block():
        entered.set()
        assert release.wait(5)
    token = guard.begin_provider_ingress(child, span_id='backlog')
    try:
        assert _enqueue(child, token, block)
        assert entered.wait(1)
        for sequence in range(1, 100):
            assert _enqueue(child, token, lambda index=sequence: observed.append(index), sequence)
        assert not child.get('_invocationDeliveryError')
        release.set()
        receipt = guard.end_provider_ingress(child, token=token)
        assert not receipt['flushIncomplete']
        assert receipt['droppedEvents'] == 0
        assert observed == list(range(1, 100))
    finally:
        release.set()
        child['_providerIngressIsolation']['worker'].join(5)


def test_normal_child_completion_waits_for_slow_evidence_in_fifo_order(monkeypatch):
    from lib.tasks_pkg import child_invocation as owner
    from lib.tasks_pkg.invocation import deliver_invocation_event
    from lib.tasks_pkg.invocation import InvocationPolicy

    monkeypatch.setattr(guard, '_FLUSH_JOIN_TIMEOUT_SEC', 0.01)
    entered, release, joining, finished = (threading.Event() for _ in range(4))
    observed, results, failures = [], [], []
    child = _carrier('slow-required-evidence')
    child['_invocationPolicy'] = InvocationPolicy.child('parent')
    child['_invocationAbortCheck'] = lambda: False

    def sink(event):
        if event['seq'] == 0:
            entered.set()
            assert release.wait(5)
        commit_child_publication(lambda: observed.append(event['seq']))

    child['_invocationEventSink'] = lambda event: child['_invocationPublicationFence'].deliver(sink, event)

    def execute(task, **kwargs):
        token = guard.begin_provider_ingress(task, span_id='slow-storage')
        for sequence in range(3):
            event = {'type': 'delta', 'seq': sequence}
            _enqueue(task, token, lambda event=event: deliver_invocation_event(task, event), sequence)
        assert entered.wait(2)
        joining.set()
        receipt = guard.end_provider_ingress(task, token=token)
        assert not receipt['flushIncomplete']
        assert receipt['deliveredEvents'] == 3
        # The first post-stream event must follow every queued event.
        deliver_invocation_event(task, {'type': 'complete', 'seq': 3})

    monkeypatch.setattr(owner, 'execute_invocation', execute)

    def run():
        try:
            results.append(owner.run_child_invocation(child))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert joining.wait(2)
        # Outlast the observer timeout while the required sink is still live.
        assert not finished.wait(0.1)
        assert not child.get('_invocationDeliveryError')
        release.set()
        assert finished.wait(3)
        assert failures == []
        assert results == [child]
        assert observed == [0, 1, 2, 3]
        assert not child['_providerIngressIsolation']['worker'].is_alive()
    finally:
        release.set()
        runner.join(5)
        state = child.get('_providerIngressIsolation') or {}
        if state.get('worker') is not None:
            state['worker'].join(5)


@pytest.mark.parametrize('stop', ['owner', 'deadline', 'stalled'])
def test_required_evidence_wait_observes_invocation_control(monkeypatch, stop):
    from lib.swarm.liveness import ProgressBeacon

    monkeypatch.setattr(guard, '_CHILD_FLUSH_TIMEOUT_SEC', 0.01)
    entered, release, stopping, finished = (threading.Event() for _ in range(4))
    child = _carrier('controlled-evidence-wait')
    child['_invocationAbortCheck'] = lambda: stop == 'owner' and stopping.is_set()
    beacon = ProgressBeacon()
    child['_invocationProgressBeacon'] = beacon
    monkeypatch.setattr(ProgressBeacon, 'is_making_progress',
                        lambda self, key: not (stop == 'stalled' and stopping.is_set()))
    observed, receipts, failures = [], [], []

    def sink():
        entered.set()
        assert release.wait(5)
        commit_child_publication(lambda: observed.append('late'))

    token = guard.begin_provider_ingress(child, span_id='controlled-storage')
    _enqueue(child, token, lambda: child['_invocationPublicationFence'].deliver(
        lambda event: sink(), {}))
    assert entered.wait(2)

    def flush():
        try:
            receipts.append(guard.end_provider_ingress(child, token=token))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    runner = threading.Thread(target=flush)
    runner.start()
    try:
        if stop == 'deadline':
            child['_invocationDeadline'] = 0
        stopping.set()
        assert finished.wait(2)
        assert failures == []
        assert child['aborted'] and child['abort_event'].is_set()
        if stop != 'owner':
            assert child['_invocationStopReason'] == stop
        assert receipts[0]['flushIncomplete']
        assert child['_invocationDeliveryError'] is not None
        assert child['_providerIngressIsolation']['worker'].is_alive()
    finally:
        release.set()
        runner.join(5)
        child['_providerIngressIsolation']['worker'].join(5)
    assert observed == []


@pytest.mark.parametrize('fails', [False, True])
def test_only_successful_evidence_delivery_refreshes_progress(monkeypatch, fails):
    from lib.swarm.liveness import ProgressBeacon

    child = _carrier('delivery-progress')
    beacon = ProgressBeacon()
    child['_invocationProgressBeacon'] = beacon
    child['_invocationProgressKey'] = 'worker-progress'
    observed = []
    monkeypatch.setattr(ProgressBeacon, 'touch',
                        lambda self, key, note='': observed.append((key, note)))

    def deliver():
        assert observed == []  # Entering a blocked sink is not completion.
        if fails:
            raise RuntimeError('required sink refused')

    token = guard.begin_provider_ingress(child, span_id='progress')
    _enqueue(child, token, deliver)
    receipt = guard.end_provider_ingress(child, token=token)
    if fails:
        assert receipt['deliveryFailures'] == 1
        assert child['_invocationDeliveryError'] is not None
        assert observed == []
    else:
        assert receipt['deliveredEvents'] == 1
        assert observed == [('worker-progress', 'evidence_delivered')]
