"""Worker observation outlives primary settlement without changing its verdict."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading

import pytest

from lib.agent_core import task_runtime as runtime_module
from lib.agent_core.task_runtime import TaskRuntime
from lib.task_replay.task_replay import live_task_replay_response_schema

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime():
    value = TaskRuntime('postpass-spec', ttl=10, max_tasks=1, push_channel='')
    yield value
    for task_id in list(value._tasks):
        value.discard(task_id)


def submit(runtime, task, callback):
    entries = []
    runtime.submit_worker(task['id'], lambda _id, _owner, fn: entries.append(fn), callback)
    return entries[0]


def test_completed_artifact_retains_live_worker_then_starts_normal_ttl(runtime, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(runtime_module.time, 'time', lambda: clock[0])
    task = runtime.create(user_id=1)

    def work():
        assert runtime.finish(task['id'], result='body')
        assert task['events'][-1]['workerActive'] is True
        assert runtime.poll(task['id'])['workerActive'] is True
        assert not runtime.abort_owned(task['id'], user_id=1)
        clock[0] = 200.0
        assert runtime.cleanup_stale() == 0
        runtime.create(user_id=1)
        assert runtime.get(task['id']) is task  # capacity is eviction-only
        runtime.append_event(task['id'], {'type': 'report_meta', 'meta': {'cost': 2}})

    submit(runtime, task, work)()
    page = runtime.poll(task['id'])
    assert page['workerActive'] is False
    assert page['status'] == 'done' and page['result'] == 'body'
    assert page['finishedAt'] == 100_000  # primary completion remains stable
    assert page['events'][-1]['type'] == 'report_meta'
    assert runtime.cleanup_stale() == 0
    clock[0] = 211.0
    assert runtime.cleanup_stale() == 1
    assert runtime.get(task['id']) is None


@pytest.mark.parametrize('failure', [ValueError('worker fault'), KeyboardInterrupt()])
def test_worker_observation_retires_on_every_exit(runtime, failure):
    task = runtime.create(user_id=1)

    def work():
        raise failure

    entry = submit(runtime, task, work)
    if isinstance(failure, Exception):
        entry()
        assert task['status'] == 'error'
    else:
        with pytest.raises(KeyboardInterrupt):
            entry()
    assert runtime.poll(task['id'])['workerActive'] is False


def test_generic_callback_return_does_not_replace_finish_result(runtime):
    task = runtime.create(user_id=1)

    def run():
        runtime.finish(task['id'], result='published result')
        return 'callback implementation detail'

    assert submit(runtime, task, run)() is None
    assert runtime.poll(task['id'])['result'] == 'published result'


def test_overlapping_callbacks_and_replaced_registry_entry_keep_exact_identity(runtime):
    task = runtime.create(user_id=1)

    def inner():
        assert runtime.poll(task['id'])['workerActive'] is True

    def outer():
        submit(runtime, task, inner)()
        assert runtime.poll(task['id'])['workerActive'] is True
        runtime.discard(task['id'])
        runtime.create(user_id=1, task_id=task['id'])

    submit(runtime, task, outer)()
    replacement = runtime.get(task['id'])
    assert replacement is not task
    assert 'workerActive' not in runtime.poll(task['id'])


def test_crashed_retired_worker_cannot_settle_replacement_with_same_id(runtime):
    task = runtime.create(user_id=1)

    def work():
        runtime.discard(task['id'])
        runtime.create(user_id=1, task_id=task['id'])
        raise RuntimeError('retired worker failure')

    submit(runtime, task, work)()
    replacement = runtime.get(task['id'])
    assert replacement is not task
    assert replacement['status'] == 'pending'
    assert replacement['events'] == []
    assert task['_activeWorkerCount'] == 0


@pytest.mark.parametrize('use_loop', [False, True])
def test_spawn_rejection_preserves_failure_and_retires_pending_task(runtime, monkeypatch, use_loop):
    task = runtime.create(user_id=1)

    def forbidden():
        pytest.fail('rejected worker must not execute')

    if use_loop:
        pool = ThreadPoolExecutor(max_workers=1)
        pool.shutdown()

        async def drive():
            asyncio.get_running_loop().set_default_executor(pool)
            runtime.spawn(task['id'], forbidden)
            failures = await asyncio.gather(*runtime._bg_tasks, return_exceptions=True)
            assert len(failures) == 1 and isinstance(failures[0], RuntimeError)

        asyncio.run(drive())
    else:
        failure = RuntimeError('cannot start a worker')

        def reject(thread):
            raise failure

        monkeypatch.setattr(runtime_module.threading.Thread, 'start', reject)
        with pytest.raises(RuntimeError) as caught:
            runtime.spawn(task['id'], forbidden)
        assert caught.value is failure
    assert task['status'] == 'error'
    assert [event['type'] for event in task['events']] == ['error']
    assert 'workerActive' not in runtime.poll(task['id'])


@pytest.mark.parametrize('use_loop', [False, True])
def test_start_failure_cannot_settle_replacement_created_during_submission(runtime, monkeypatch, use_loop):
    task = runtime.create(user_id=1)
    failure = RuntimeError('scheduler retired the old carrier')

    def reject(*args):
        assert runtime.discard(task['id']) is task
        runtime.create(user_id=1, task_id=task['id'])
        raise failure

    if use_loop:
        class RejectingExecutor(ThreadPoolExecutor):
            submit = staticmethod(reject)

        async def drive():
            asyncio.get_running_loop().set_default_executor(RejectingExecutor(max_workers=1))
            runtime.spawn(task['id'], lambda: pytest.fail('worker entered'))
            assert await asyncio.gather(*runtime._bg_tasks, return_exceptions=True) == [failure]

        asyncio.run(drive())
    else:
        monkeypatch.setattr(runtime_module.threading.Thread, 'start', reject)
        with pytest.raises(RuntimeError) as caught:
            runtime.spawn(task['id'], lambda: pytest.fail('worker entered'))
        assert caught.value is failure
    replacement = runtime.get(task['id'])
    assert replacement is not task
    assert replacement['status'] == 'pending' and replacement['events'] == []


def test_async_wrapper_rejection_closes_coroutine_and_settles_task(runtime, monkeypatch):
    task = runtime.create(user_id=1)
    failure = RuntimeError('loop rejected task creation')
    pending = []

    def reject(coroutine):
        pending.append(coroutine)
        raise failure

    async def drive():
        monkeypatch.setattr(runtime_module.asyncio, 'ensure_future', reject)
        with pytest.raises(RuntimeError) as caught:
            runtime.spawn(task['id'], lambda: pytest.fail('worker entered'))
        assert caught.value is failure
        monkeypatch.undo()

    asyncio.run(drive())
    assert task['status'] == 'error' and not runtime._bg_tasks
    assert len(pending) == 1 and pending[0].cr_frame is None


@pytest.mark.parametrize('replace_carrier', [False, True])
def test_executor_future_rejection_settles_only_its_original_carrier(runtime, replace_carrier):
    task = runtime.create(user_id=1)
    failure = RuntimeError('executor initializer failed')

    class FailingExecutor(ThreadPoolExecutor):
        def submit(self, fn):
            future = Future()

            def reject():
                if replace_carrier:
                    runtime.discard(task['id'])
                    runtime.create(user_id=1, task_id=task['id'])
                future.set_exception(failure)

            asyncio.get_running_loop().call_soon(reject)
            return future

    async def drive():
        asyncio.get_running_loop().set_default_executor(FailingExecutor(max_workers=1))
        runtime.spawn(task['id'], lambda: pytest.fail('worker must not enter'))
        assert await asyncio.gather(*runtime._bg_tasks, return_exceptions=True) == [failure]

    asyncio.run(drive())
    current = runtime.get(task['id'])
    assert current['status'] == ('pending' if replace_carrier else 'error')
    assert '_activeWorkerCount' not in task
    if replace_carrier:
        assert current is not task and current['events'] == []


def test_inactive_observation_cannot_precede_an_omitted_final_event(runtime, monkeypatch):
    task = runtime.create(user_id=1)
    published, release = threading.Event(), threading.Event()

    def work():
        runtime.finish(task['id'])
        published.set()
        assert release.wait(5)
        runtime.append_event(task['id'], {'type': 'report_meta', 'meta': {'cost': 2}})

    thread = threading.Thread(target=submit(runtime, task, work))
    thread.start()
    original = runtime_module.task_memory_replay_page

    def page_then_exit(carrier, cursor):
        page = original(carrier, cursor)
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        return page

    try:
        assert published.wait(5)
        monkeypatch.setattr(runtime_module, 'task_memory_replay_page', page_then_exit)
        page = runtime.poll(task['id'])
        assert page['events'][-1]['type'] == 'done'
        assert page['workerActive'] is True
        final = runtime.poll(task['id'], page['next_cursor'])
        assert final['workerActive'] is False
        assert final['events'][-1]['type'] == 'report_meta'
    finally:
        release.set()
        thread.join(5)


def test_legacy_and_unmanaged_carriers_do_not_invent_worker_activity(runtime):
    task = runtime.create(user_id=1)
    assert 'workerActive' not in runtime.poll(task['id'])
    runtime.finish(task['id'], terminal_event_fields={'workerActive': True})
    assert 'workerActive' not in runtime.poll(task['id'])
    assert 'workerActive' not in task['events'][-1]


def test_live_replay_schema_declares_optional_worker_observation():
    schema = live_task_replay_response_schema()
    assert schema['properties']['workerActive']['type'] == 'boolean'
    assert 'workerActive' not in schema['required']
