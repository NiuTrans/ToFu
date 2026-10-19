"""A durable actor frontier requires the unchanged registered child birth."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.agent_core.native_process import CheckpointRequest
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy, checkpoint_lifetime_matches
from tests.agent_core.test_actor_checkpoint_account import child
from tests.agent_core.test_shared_account_checkpoint import fresh
from tests.agent_core.test_native_checkpoint_policy import SelectedStorage
from tests.agent_core.test_native_task_terminal import principal

pytestmark = pytest.mark.unit


@pytest.fixture
def bound_actor():
    root, account = fresh()
    task = child(root, 'actor')
    task.update(_transientRuntime=True, _invocationStorageFree=False,
                convId='conversation', _parentTaskId=root['id'], _archiveScope='invocation:7:actor')
    identity = dict(user_id=7, conversation_id='conversation', parent_task_id=root['id'],
                    child_task_id='actor', archive_scope=task['_archiveScope'])
    storage = SelectedStorage()
    record = {'value': dict(child_invocation=identity, task_id='actor', user_id=7,
                            conv_id='conversation')}
    storage.query = lambda operation, payload: deepcopy(record) if operation == 'record.get' else {'id': 'conversation'}
    with ThreadPoolExecutor(max_workers=1) as executor:
        arguments = dict(principal=principal(), task=task, storage=storage, executor=executor,
            epoch='epoch', binding='binding', token='lease', header_bytes=4096,
            snapshot_bytes=65536, identity_bytes=1024, io_timeout_ms=5000,
            restore=AsyncMock(), registered_child=True)
        yield task, storage, record, arguments
    account.close(task)


@pytest.mark.parametrize('damage', ['storage_free', 'headless', 'wrong_birth', 'completed', 'recover', 'handoff'])
def test_actor_acquisition_refuses_ineligible_lineage_before_journal_write(bound_actor, damage):
    task, storage, record, arguments = bound_actor
    if damage == 'storage_free': task['_invocationStorageFree'] = True
    elif damage == 'headless': task['convId'] = ''
    elif damage == 'wrong_birth': record['value']['child_invocation']['parent_task_id'] = 'another-parent'
    elif damage == 'completed': record['value']['flow_invocation_completion'] = {}
    elif damage == 'recover': arguments['recover_abandoned'] = True
    elif damage == 'handoff': arguments['handoff_from_accepted'] = True
    with pytest.raises((ValueError, LookupError)):
        NativeCheckpointPolicy(**arguments)
    assert storage.calls == []


@pytest.mark.parametrize('damage', ['parent', 'conversation', 'scope', 'stored_birth', 'completion'])
def test_actor_callback_rechecks_birth_and_refuses_new_frontier(bound_actor, damage):
    task, storage, record, arguments = bound_actor
    policy = NativeCheckpointPolicy(**arguments)
    request = CheckpointRequest(principal(), task['id'], 'load', 0, 'epoch', 'binding', threading.Event())
    async def run():
        assert await policy.load(request) is None
        calls = len(storage.calls)
        if damage == 'parent': task['_parentTaskId'] = 'changed'
        elif damage == 'conversation': task['convId'] = 'changed'
        elif damage == 'scope': task['_archiveScope'] = 'changed'
        elif damage == 'stored_birth': record['value']['child_invocation']['parent_task_id'] = 'changed'
        else: record['value']['flow_invocation_completion'] = {}
        with pytest.raises(ValueError):
            await policy.begin(replace(request, action='begin'))
        assert len(storage.calls) == calls
    asyncio.run(run())


def test_registered_actor_configuration_cannot_enable_headless_or_root_checkpointing(bound_actor):
    task, _, _, arguments = bound_actor
    policy = NativeCheckpointPolicy(**arguments)
    profile = {'checkpoint_actor_account': True, 'checkpoint_shared_account': True}
    assert checkpoint_lifetime_matches(task, policy, profile, True)
    assert not checkpoint_lifetime_matches(dict(task), policy, profile, True)
    assert not checkpoint_lifetime_matches(task, None, profile, True)
    assert not checkpoint_lifetime_matches(task, policy, profile, False)
    assert not checkpoint_lifetime_matches(task, policy, {'checkpoint_shared_account': True}, True)
    assert checkpoint_lifetime_matches(task, None, {}, True)


@pytest.mark.parametrize('damage', ['implicit', 'wrong_actor', 'root_alias', 'missing_root', 'tree_usage'])
def test_sdk_actor_identity_cannot_be_reinterpreted_as_root_authority(damage):
    from lib.agent_core.native_process import _CheckpointControl, NativeProcessError
    control = _CheckpointControl(SimpleNamespace(task_id='actor'), SimpleNamespace(binding='binding'), 'epoch')
    control.actor_usage = True
    saved = {'actor_task_id': 'actor', 'root_task_id': 'shared-root'}
    control.account_identity(saved)
    if damage == 'implicit': control.actor_usage = False
    elif damage == 'wrong_actor': saved['actor_task_id'] = 'sibling'
    elif damage == 'root_alias': saved['root_task_id'] = 'actor'
    elif damage == 'missing_root': del saved['root_task_id']
    else: saved['root_tokens'] = 0
    with pytest.raises(NativeProcessError):
        control.account_identity(saved)
