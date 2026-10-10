"""Production projection commits reject disposed children, not only test callbacks."""
import threading
import time
from types import SimpleNamespace

import pytest

from lib.tasks_pkg import child_invocation as children
from lib.tasks_pkg.manager import _provider_ingress_guard as guard
from lib.orchestration.chat_event_sink import OrchestrationChatTaskEventSink
from lib.orchestration.chat_flow_adapter import FlowEventAdapter
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.runtime_event_sink import FlowEventSink
from lib.agent_core.task_runtime import TaskRuntime

pytestmark = pytest.mark.unit


class BlockingLock:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        self.entered.set()
        assert self.release.wait(3)

    def __exit__(self, *args):
        pass


@pytest.mark.parametrize('boundary', ['goal_delta', 'goal_tool', 'runtime'])
def test_disposed_child_is_denied_at_real_commit(boundary, monkeypatch):
    fence = children.ChildPublicationFence()
    lock = BlockingLock()
    events, durable, failures = [], [], []
    parent = {'content': 'next phase', 'thinking': '', 'toolRounds': [],
              'content_lock': lock, 'events_lock': lock, 'events': []}
    if boundary == 'runtime':
        runtime = TaskRuntime(kind='ingress-commit-test')
        runtime.get = lambda task_id: parent
        sink = FlowEventSink(lambda event: runtime.append_event('parent', event),
                             durable_project=lambda *args: durable.append(args),
                             persist_deltas=True)
        event = {'type': 'step_delta', 'chunk': 'stale'}
    else:
        chat = OrchestrationChatTaskEventSink(parent, lambda *args: events.append(args))
        adapter = FlowEventAdapter(on_stream=chat)
        executor = SimpleNamespace(_on_event=adapter.on_event, _journal=None)
        from lib.orchestration.agent_runner import (
            OrchestrationInvocationRunner, OrchestrationAgentRunnerConfig)
        monkeypatch.setattr('lib.tasks_pkg.manager.task_user_id', lambda task: 771)
        monkeypatch.setattr('lib.swarm.registry.scope_tools_for_role', lambda *args: [])
        monkeypatch.setattr('lib.swarm.registry.get_role_system_suffix', lambda role: '')
        def create(parent, *, event_sink, **kwargs):
            return {'sink': lambda event: fence.deliver(event_sink, event)}
        monkeypatch.setattr(children, 'create_child_invocation', create)
        event = ({'type': 'delta', 'content': 'stale'} if boundary == 'goal_delta'
                 else {'type': 'tool_start', 'toolCallId': 'late', 'roundNum': 1})
        monkeypatch.setattr(children, 'run_child_invocation',
                            lambda child: child['sink'](event))
        runner = OrchestrationInvocationRunner(
            OrchestrationAgentRunnerConfig(parent_task={'id': 'parent'},
                                           model='fixture', model_routing_policy='selected'),
            emit=lambda event: FlowExecutor._emit(executor, event),
            abort_check=lambda: False)
        sink = lambda event: runner({'id': 'worker', 'role': 'worker'}, '', 1)

    def deliver():
        try:
            fence.deliver(sink, event)
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=deliver)
    worker.start()
    try:
        assert lock.entered.wait(3)
        fence.close()  # Must not wait for the parent's lock or observer.
        parent['content'] = 'new phase owns parent'
    finally:
        lock.release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert failures and 'revoked' in str(failures[0])
    assert parent['content'] == 'new phase owns parent'
    assert parent['toolRounds'] == []
    assert parent['events'] == []
    assert not events and not durable


def test_normal_child_drain_tolerates_sidecar_latency():
    task = {'id': 'normal-child-budget', '_invocationPublicationFence': children.ChildPublicationFence()}
    token = guard.begin_provider_ingress(task, span_id='normal')
    delivered = []
    guard.enqueue_ingress_delivery(task, token=token, sequence=0, event_type='delta',
                                  deliver=lambda: (time.sleep(.3), delivered.append(1)))
    receipt = guard.end_provider_ingress(task, token=token)
    assert delivered == [1]
    assert not receipt['flushIncomplete']
    children.close_child_ingress(task)


def test_deferred_parent_delivery_keeps_original_authority():
    fence = children.ChildPublicationFence()
    task = {'id': 'deferred-parent'}
    entered, release = threading.Event(), threading.Event()
    events = []
    token = guard.begin_provider_ingress(task, span_id='parent')
    def blocked():
        entered.set()
        assert release.wait(3)
    guard.enqueue_ingress_delivery(task, token=token, sequence=0, event_type='delta', deliver=blocked)
    assert entered.wait(3)
    fence.deliver(lambda event: guard.enqueue_ingress_delivery(
        task, token=token, sequence=1, event_type='delta',
        deliver=lambda: children.commit_child_publication(lambda: events.append(event))), {})
    fence.close()
    release.set()
    receipt = guard.end_provider_ingress(task, token=token)
    assert not events
    assert receipt['deliveryFailures'] == 1


@pytest.mark.parametrize('closed', [False, True])
def test_graph_bus_preserves_revocation_after_observer_admission(closed):
    """The real runtime sink, not a cooperative fake commit, owns replay seq."""
    from lib.agent_core.publication import ChildPublicationRevoked
    runtime = TaskRuntime(kind='child-publication-graph-race')
    parent = {'events_lock': threading.Lock(), 'events': [], 'status': 'running',
              '_eventNextSeq': 0, 'usage': {'cost': 7}}
    runtime.get = lambda task_id: parent
    durable, failures = [], []
    sink = FlowEventSink(lambda event: runtime.append_event('parent', event),
                         durable_project=lambda *args: durable.append(args),
                         persist_deltas=True)
    entered, release = threading.Event(), threading.Event()
    fence = children.ChildPublicationFence()

    def observer(event):
        entered.set()
        assert release.wait(3)
        sink(event)

    executor = SimpleNamespace(_on_event=observer, _journal=None)
    def deliver():
        try:
            fence.deliver(lambda event: FlowExecutor._emit(executor, event),
                          {'type': 'step_delta', 'chunk': 'valid or stale'})
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=deliver)
    worker.start()
    try:
        assert entered.wait(3)
        if closed:
            children.close_child_ingress({'id': 'graph-race-child',
                                         '_invocationPublicationFence': fence})
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    if closed:
        assert len(failures) == 1 and isinstance(failures[0], ChildPublicationRevoked)
        assert parent['events'] == []
        assert 'updated_at' not in parent
        assert parent['_eventNextSeq'] == 0
        assert parent['usage'] == {'cost': 7}
        assert durable == []
        with pytest.raises(ChildPublicationRevoked):
            fence.deliver(sink, {'type': 'round_usage', 'cost': 999})
        assert parent['_eventNextSeq'] == 0
        assert parent['usage'] == {'cost': 7}
    else:
        assert not failures
        assert len(parent['events']) == len(durable) == 1
        assert parent['_eventNextSeq'] == 1
    # Closing a child never closes the parent/root publication lane.
    sink({'type': 'step_delta', 'chunk': 'root'})
    assert len(parent['events']) == len(durable) == (1 if closed else 2)
