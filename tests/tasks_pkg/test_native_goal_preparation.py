"""Whole-set readiness frees callback workers and joins cancellation before close."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
import threading
from unittest.mock import AsyncMock

import pytest

from lib.agent_core.abort_signal import AbortSignal
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
from tests.agent_core.test_native_goal_handoff import Storage, request as checkpoint_request
from tests.agent_core.test_recovered_actor_prefix import root, child, source
from tests.model_routing.test_native_route import principal
from tests.tasks_pkg.test_native_invocation import binding
from tests.tasks_pkg.test_native_prepared import prepared

pytestmark = pytest.mark.unit
AUTHORIZATION = {'claim_request': {'invoice': {'task_id': 'source'}}}


@contextmanager
def group(*, suffixes=('a', 'b'), frontier=None):
    task, account = root('target')
    task['abort_event'] = AbortSignal()
    account.restore_pending_frontier(task, source() if frontier is None else frontier, owner=7)
    storage = Storage()
    handoff = NativeGoalHandoff(principal=principal(), task=task, storage=storage,
        evidence_bytes=1048576, io_timeout_ms=5000)
    account.bind_account_frontier_observer(lambda cut: None,
        actor_rebind_observer=handoff.commit_frontiers, actor_rebind_bytes=1048576)
    with ThreadPoolExecutor(max_workers=1) as executor:
        requests, ports, policies = [], [], []
        for suffix in suffixes:
            actor = child(task, 'target-'+suffix)
            actor.update(_transientRuntime=True, _invocationStorageFree=False,
                convId='conversation', _archiveScope='invocation:7:'+actor['id'],
                abort_event=AbortSignal())
            identity = dict(user_id=7, conversation_id='conversation', parent_task_id='target',
                child_task_id=actor['id'], archive_scope=actor['_archiveScope'])
            storage.records[actor['id']] = {'value': dict(child_invocation=identity,
                task_id=actor['id'], user_id=7, conv_id='conversation')}
            request = prepared()
            request.task = actor
            request.tid = actor['id']
            request.cfg = actor['config']
            requests.append(request)
            port = binding(asyncio.get_running_loop(), executor)
            port._transient = True
            port._profile = json.dumps(dict(json.loads(port._profile), epoch=actor['id']))

            def factory(request, owner, callback_executor, profile):
                assert callback_executor is executor
                policy = NativeCheckpointPolicy(principal=owner, task=request.task,
                    storage=storage, executor=executor, epoch=profile['epoch'], binding='binding',
                    token='token-'+request.task['id'], header_bytes=4096, snapshot_bytes=65536,
                    identity_bytes=1024, io_timeout_ms=5000, restore=AsyncMock(), registered_child=True)
                policies.append(policy)
                return policy

            port.checkpoint_factory = factory
            ports.append(port)
        yield handoff, storage, executor, requests, ports, policies


def bindings(requests):
    return [(request.task, 'source-'+request.task['id'].removeprefix('target-')) for request in requests]


def assert_released(handoff, requests, ports, policies):
    assert all(policy.closed and policy.storage is None for policy in policies)
    assert all(not port.host._clients for port in ports)
    assert handoff.task['abort_event']._observers is None
    assert all(request.task['abort_event']._observers is None for request in requests)


@pytest.mark.parametrize('durable_admission', [False, True])
def test_single_callback_worker_prepares_every_recipient_before_one_handoff(monkeypatch, durable_admission):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            admitted = []
            async def native(client, repo, selection, configuration, **kwargs):
                assert len(policies) == 2 and all(p.capability is not None for p in policies)
                policy = kwargs['checkpoint_policy']
                if durable_admission:
                    assert policy.task_id in admitted
                assert policy.task is kwargs['request'].task
                assert await policy.load(replace(checkpoint_request(policy), epoch=policy.epoch)) is None
                return policy.task_id
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                 executor=executor) as prepare:
                for port in ports:
                    port.checkpoint_preparation = prepare
                def execute(port, request):
                    if not durable_admission:
                        return port.run(request)
                    request.task['_nativePreparedInvocation'] = port
                    def admit():
                        assert len(policies) == 2 and all(p.capability is not None for p in policies)
                        assert not port.host._clients
                        admitted.append(request.task['id'])
                    with port.admission_scope(request.task, admit):
                        return port.run(request)
                result = await asyncio.wait_for(asyncio.gather(*(
                    asyncio.to_thread(execute, port, request) for port, request in zip(ports, requests))), 5)
                assert result == ['target-a', 'target-b']
            assert [name for name, _ in storage.calls].count('goal_handoff') == 1
            assert [name for name, _ in storage.calls].count('load') == 2
            assert handoff.budget.consumption() == (36, 5)
            assert_released(handoff, requests, ports, policies)
            assert all(port._dispatch_admission is None for port in ports)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


def test_durable_admission_failure_after_handoff_never_enters_native_execution(monkeypatch):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            native = AsyncMock()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            failure = OSError('journal admission unavailable')
            admitted = []
            async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                 executor=executor) as prepare:
                def execute(port, request):
                    port.checkpoint_preparation = prepare
                    request.task['_nativePreparedInvocation'] = port
                    def refuse():
                        assert all(policy.capability is not None for policy in policies)
                        admitted.append(request.task['id'])
                        raise failure
                    with port.admission_scope(request.task, refuse):
                        return port.run(request)
                outcomes = await asyncio.wait_for(asyncio.gather(*(
                    asyncio.to_thread(execute, port, request) for port, request in zip(ports, requests)),
                    return_exceptions=True), 5)
                assert all(outcome is failure for outcome in outcomes)
            assert len(admitted) == 2
            native.assert_not_awaited()
            assert_released(handoff, requests, ports, policies)
            assert all(port._dispatch_admission is None for port in ports)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('index', [0, 1])
def test_factory_failure_wakes_all_recipients_without_native_entry(monkeypatch, index):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            failure = ValueError('factory failed')
            def refuse(*args):
                raise failure
            ports[index].checkpoint_factory = refuse
            native = AsyncMock()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            with pytest.raises(ValueError, match='factory failed'):
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor) as prepare:
                    for port in ports:
                        port.checkpoint_preparation = prepare
                    outcomes = await asyncio.wait_for(asyncio.gather(*(
                        port._run(request) for port, request in zip(ports, requests)),
                        return_exceptions=True), 5)
                    assert any(outcome is failure for outcome in outcomes)
            native.assert_not_awaited()
            assert not storage.calls
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('abort_root', [False, True])
def test_stop_joins_busy_factory_and_releases_every_wait(monkeypatch, abort_root):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            entered, release = threading.Event(), threading.Event()
            factory = ports[1].checkpoint_factory
            def slow(*args):
                entered.set()
                assert release.wait(5)
                return factory(*args)
            ports[1].checkpoint_factory = slow
            native = AsyncMock()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            with pytest.raises(asyncio.CancelledError):
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor) as prepare:
                    for port in ports:
                        port.checkpoint_preparation = prepare
                    tasks = [asyncio.create_task(port._run(request)) for port, request in zip(ports, requests)]
                    try:
                        assert await asyncio.to_thread(entered.wait, 5)
                        (handoff.task if abort_root else requests[0].task)['abort_event'].set()
                        await asyncio.sleep(0)
                        assert not tasks[1].done()
                    finally:
                        release.set()
                    outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
            native.assert_not_awaited()
            assert not storage.calls
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('lost_ack', [False, True])
def test_cancelled_handoff_joins_ack_before_policy_cleanup(monkeypatch, lost_ack):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            entered, release = threading.Event(), threading.Event()
            control = storage.native_checkpoint_control
            def slow(operation, *args, **kwargs):
                if operation == 'goal_handoff':
                    entered.set()
                    assert release.wait(5)
                    if lost_ack:
                        storage.damage = 'unknown'
                return control(operation, *args, **kwargs)
            storage.native_checkpoint_control = slow
            native = AsyncMock()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            with pytest.raises(asyncio.CancelledError):
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor) as prepare:
                    for port in ports:
                        port.checkpoint_preparation = prepare
                    tasks = [asyncio.create_task(port._run(request)) for port, request in zip(ports, requests)]
                    try:
                        assert await asyncio.to_thread(entered.wait, 5)
                        tasks[0].cancel()
                        await asyncio.sleep(0)
                        assert all(not policy.closed for policy in policies)
                        assert all(not task.done() for task in tasks)
                    finally:
                        release.set()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            native.assert_not_awaited()
            assert [name for name, _ in storage.calls].count('goal_handoff') == 1
            assert [name for name, _ in storage.calls].count('forget') == (0 if lost_ack else 2)
            if lost_ack:
                assert handoff.budget.reason() == 'execution_account_frontier_unavailable'
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'foreign', 'source'])
def test_invalid_inventory_is_rejected_before_preparation(damage):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            rows = bindings(requests)
            if damage == 'missing':
                rows.pop()
            elif damage == 'duplicate':
                rows[1] = rows[0]
            elif damage == 'foreign':
                rows[1] = (dict(rows[1][0]), rows[1][1])
            else:
                rows[1] = (rows[1][0], 'other-source')
            with pytest.raises(ValueError):
                async with handoff.prepare_recipients(rows, authorization=AUTHORIZATION, executor=executor):
                    pytest.fail('invalid group entered')
            assert not storage.calls and not policies
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


def test_scope_failure_and_interrupted_close_join_a_factory_already_running(monkeypatch):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            entered, release = threading.Event(), threading.Event()
            factory = ports[1].checkpoint_factory
            failure = RuntimeError('caller stopped group preparation')
            raising = asyncio.Event()
            def slow(*args):
                entered.set()
                assert release.wait(5)
                return factory(*args)
            ports[1].checkpoint_factory = slow
            native = AsyncMock()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            async def owner():
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor) as prepare:
                    for port in ports:
                        port.checkpoint_preparation = prepare
                    tasks = [asyncio.create_task(port._run(request)) for port, request in zip(ports, requests)]
                    assert len(tasks) == 2
                    assert await asyncio.to_thread(entered.wait, 5)
                    raising.set()
                    raise failure
            outer = asyncio.create_task(owner())
            try:
                await asyncio.wait_for(raising.wait(), 5)
                outer.cancel()
                await asyncio.sleep(0)
                assert not outer.done()
            finally:
                release.set()
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(outer, 5)
            assert caught.value is failure
            native.assert_not_awaited()
            assert not storage.calls
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


def test_missing_policy_fails_group_and_closed_scope_cannot_run_again(monkeypatch):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            ports[0].checkpoint_factory = lambda *args: None
            native = AsyncMock()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            with pytest.raises(ValueError, match='no policy'):
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor) as prepare:
                    for port in ports:
                        port.checkpoint_preparation = prepare
                    await asyncio.gather(*(port._run(request) for port, request in zip(ports, requests)))
            with pytest.raises(ValueError, match='closed'):
                await ports[0]._run(requests[0])
            with pytest.raises(ValueError, match='one complete'):
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor):
                    pytest.fail('reused one-shot preparation')
            native.assert_not_awaited()
            assert not storage.calls
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())


def test_early_scope_exit_surfaces_failure_after_all_native_invocations_join(monkeypatch):
    async def run():
        with group() as (handoff, storage, executor, requests, ports, policies):
            entered = []
            both_entered = asyncio.Event()
            failure = OSError('native terminal failed while closing')
            async def native(*args, **kwargs):
                task = kwargs['request'].task
                listener = task['abort_event'].subscribe_async()
                entered.append(task['id'])
                if len(entered) == 2:
                    both_entered.set()
                try:
                    await listener.future
                    if task is requests[0].task:
                        raise failure
                    raise asyncio.CancelledError()
                finally:
                    listener.close()
            monkeypatch.setattr('lib.tasks_pkg.native_invocation.run_native_prepared_task', native)
            with pytest.raises(ExceptionGroup) as caught:
                async with handoff.prepare_recipients(bindings(requests), authorization=AUTHORIZATION,
                                                     executor=executor) as prepare:
                    for port in ports:
                        port.checkpoint_preparation = prepare
                    tasks = [asyncio.create_task(port._run(request)) for port, request in zip(ports, requests)]
                    await asyncio.wait_for(both_entered.wait(), 5)
            assert caught.value.exceptions == (failure,)
            assert all(task.done() for task in tasks)
            assert_released(handoff, requests, ports, policies)
            for port in ports:
                await port.host.close()
    asyncio.run(run())
