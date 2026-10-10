"""Child birth authority is registered before use, with storage-free exclusion."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


def _parent():
    key = uuid4().hex
    return {'id': 'registration-parent-' + key, 'convId': 'registration-conv-' + key,
            '_userId': 731, 'config': {'memoryEnabled': False}}


def _create(parent):
    from lib.tasks_pkg.child_invocation import create_child_invocation
    return create_child_invocation(parent, model='offline-test', tools=[],
                                   objective='isolated goal', event_sink=lambda event: None,
                                   abort_check=lambda: False)


def test_birth_registers_explicit_owner_parent_conversation_before_return(monkeypatch):
    import lib.storage as storage
    from lib.tasks_pkg.manager import discard_task
    parent = _parent()
    observed = []

    def command(operation, payload, *args, **kwargs):
        observed.append((operation, deepcopy(payload)))
        return {'archive_scope': f'invocation:{payload["user_id"]}:{payload["child_task_id"]}'}

    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kwargs: SimpleNamespace(command=command))
    child = _create(parent)
    try:
        assert observed == [('invocation.register_child', {
            'user_id': 731, 'conversation_id': parent['convId'],
            'parent_task_id': parent['id'], 'child_task_id': child['id']})]
        assert child['_archiveScope'] == f'invocation:731:{child["id"]}'
        assert child['convId'] == parent['convId']
        assert not parent.get('usage')
    finally:
        discard_task(child['id'])


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_registration_failure_releases_allocated_child_and_preserves_parent(monkeypatch, failure):
    import lib.storage as storage
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    parent = _parent()
    seen = []

    def command(operation, payload, *args, **kwargs):
        seen.append(payload['child_task_id'])
        raise failure('registration denied')

    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kwargs: SimpleNamespace(command=command))
    with pytest.raises(failure, match='registration denied'):
        _create(parent)
    assert len(seen) == 1
    assert chat_task_runtime.get(seen[0]) is None
    assert not parent.get('aborted')
    assert not parent.get('usage')


@pytest.mark.parametrize('flag', ['_transientRuntime', '_invocationStorageFree'])
def test_storage_free_registration_never_acquires_storage(monkeypatch, flag):
    import lib.storage as storage
    from lib.tasks_pkg.invocation_registration import register_child_invocation
    parent = {**_parent(), flag: True}
    child = {'id': 'registration-child-' + uuid4().hex}
    before = deepcopy(child)
    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kwargs: pytest.fail('storage-free child opened storage'))
    register_child_invocation(child, parent)
    assert child == before


def test_durable_headless_parent_borrows_budget_without_conversation_registration(monkeypatch):
    import lib.storage as storage
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.tasks_pkg.manager import discard_task
    parent = {**_parent(), 'convId': ''}
    parent['config']['executionLimits'] = {'max_tokens': 10000, 'max_tool_calls': 12,
                                          'deadline': None}
    budget = ExecutionBudget(parent)
    parent['_executionBudget'] = budget
    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kwargs: pytest.fail('headless child opened conversation storage'))
    child = _create(parent)
    try:
        assert child['convId'] == ''
        assert child['_invocationStorageFree'] is True
        assert child['_executionBudget'] is budget
        assert '_archiveScope' not in child
        assert not parent.get('_transientRuntime')
        assert not parent.get('_invocationStorageFree')
        assert parent['_executionBudget'] is budget
        assert not parent.get('aborted')
    finally:
        discard_task(child['id'])


def test_missing_conversation_identity_is_not_a_headless_storage_bypass(monkeypatch):
    import lib.storage as storage
    from lib.tasks_pkg.invocation_registration import register_child_invocation
    parent = _parent()
    del parent['convId']
    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kwargs: SimpleNamespace(command=lambda *args: pytest.fail('invalid parent registered')))
    with pytest.raises(KeyError, match='convId'):
        register_child_invocation({'id': 'invalid-child-' + uuid4().hex}, parent)
