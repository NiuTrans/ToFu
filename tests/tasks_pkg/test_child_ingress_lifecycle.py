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


def test_child_event_backlog_is_delivered_in_order_without_queue_capacity_failure(monkeypatch):
    monkeypatch.setattr(guard, '_DELIVERY_QUEUE_MAX', 2)
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
