"""Application-owned transient results share the required terminal boundary."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.agent_core.terminal_delivery import bind_terminal_delivery, deliver_terminal

pytestmark = pytest.mark.unit


def task():
    return {'id': 'child', '_userId': 19, '_transientRuntime': True,
            'status': 'done', 'content': 'result', 'config': {}}


def test_concurrent_duplicates_acknowledge_once():
    value = task()
    received = []
    bind_terminal_delivery(value, lambda completed: received.append(completed['id']))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: deliver_terminal(value), range(32)))
    assert received == ['child']


def test_failed_delivery_keeps_result_for_maintenance_retry(monkeypatch):
    from lib.tasks_pkg.manager._persist import persist_task_result
    value = task()
    calls = []

    def receive(completed):
        calls.append(completed['content'])
        if len(calls) == 1:
            raise RuntimeError('lost acknowledgement')

    bind_terminal_delivery(value, receive)
    monkeypatch.setattr('lib.tasks_pkg.manager._persist._release_heavy_task_state',
                        lambda _: None)
    assert persist_task_result(value) is False
    assert value['_terminalPersistencePending'] is True
    assert value['_terminalPersistenceRetryReady'] is True
    assert persist_task_result(value) is True
    assert '_terminalPersistencePending' not in value
    assert calls == ['result', 'result']


def test_plain_transient_completion_never_notifies_durable_observers(monkeypatch):
    from lib.tasks_pkg.manager import _terminal
    received = []
    monkeypatch.setattr(_terminal, '_TERMINAL_LISTENERS', [received.append])
    _terminal.notify_terminal_listeners(task())
    assert received == []


def test_handoff_rejects_foreign_identity_and_nonterminal_delivery():
    value = task()
    received = []
    bind_terminal_delivery(value, received.append)
    value['status'] = 'running'
    deliver_terminal(value)
    assert not received
    value.update(status='done', _userId=20)
    with pytest.raises(ValueError, match='identity'):
        deliver_terminal(value)


def test_failed_acknowledgement_and_recursive_delivery_are_retryable():
    value = task()
    bind_terminal_delivery(value, lambda _: False)
    with pytest.raises(RuntimeError, match='acknowledge'):
        deliver_terminal(value)
    nested = task()
    bind_terminal_delivery(nested, deliver_terminal)
    with pytest.raises(RuntimeError, match='recursive'):
        deliver_terminal(nested)


def test_preparation_retries_then_freezes_payload_and_checks_identity():
    from lib.agent_core.terminal_delivery import prepare_terminal_persistence
    value = task()
    calls = []
    def prepare(current, content, epoch):
        calls.append((content, epoch))
        if len(calls) == 1:
            raise RuntimeError('preparation retry')
        return {'source': content, 'epoch': epoch}
    bind_terminal_delivery(value, lambda _: None, persistence_preparer=prepare)
    with pytest.raises(RuntimeError, match='retry'):
        prepare_terminal_persistence(value, content='result', content_epoch=1)
    first = prepare_terminal_persistence(value, content='result', content_epoch=1)
    first['source'] = 'caller changed copy'
    assert prepare_terminal_persistence(value, content='result', content_epoch=1)['source'] == 'result'
    assert len(calls) == 2
    with pytest.raises(ValueError, match='input changed'):
        prepare_terminal_persistence(value, content='changed', content_epoch=1)
    value['_userId'] = 20
    with pytest.raises(ValueError, match='identity'):
        prepare_terminal_persistence(value, content='result', content_epoch=1)


@pytest.mark.parametrize('payload', [[], {'number': float('nan')}, {'number': float('inf')}, {'unsupported': object()}])
def test_preparation_rejects_non_object_and_non_json_payload(payload):
    from lib.agent_core.terminal_delivery import prepare_terminal_persistence
    value = task()
    bind_terminal_delivery(value, lambda _: None, persistence_preparer=lambda *args: payload)
    with pytest.raises((ValueError, TypeError)):
        prepare_terminal_persistence(value, content='result', content_epoch=0)


def test_recursive_preparation_is_retryable():
    from lib.agent_core.terminal_delivery import prepare_terminal_persistence
    value = task()
    bind_terminal_delivery(value, lambda _: None, persistence_preparer=lambda *args:
        prepare_terminal_persistence(value, content='result', content_epoch=0))
    with pytest.raises(RuntimeError, match='recursive'):
        prepare_terminal_persistence(value, content='result', content_epoch=0)


def test_complete_large_terminal_payload_is_frozen_and_isolated_from_caller_mutation():
    from lib.agent_core.terminal_delivery import prepare_terminal_persistence
    value = task()
    original = {'frozen': '完整终态输入 🧪' * 70000}
    calls = []
    def prepare(*args):
        calls.append(args)
        return original
    bind_terminal_delivery(value, lambda _: None, persistence_preparer=prepare)
    result = prepare_terminal_persistence(value, content='result', content_epoch=1)
    assert result == original
    result['frozen'] = 'caller mutation'
    assert prepare_terminal_persistence(value, content='result', content_epoch=1) == original
    assert len(calls) == 1
    with pytest.raises(ValueError, match='input changed'):
        prepare_terminal_persistence(value, content='changed', content_epoch=1)
