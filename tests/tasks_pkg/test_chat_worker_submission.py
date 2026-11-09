"""Chat worker transport preserves the domain's admission and terminal owner."""

import asyncio
import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import uuid

import pytest

from lib.log import req_id, set_req_id
from lib.tasks_pkg import spawn
from lib.tasks_pkg.manager.runtime import chat_task_runtime

pytestmark = pytest.mark.unit


class InlineExecutor:
    """Enter the worker before submit returns, as a fast real pool can do."""

    def __init__(self):
        self.submissions = 0

    def submit(self, fn):
        self.submissions += 1
        self.task_id = fn._tofu_task_id
        future = Future()
        try:
            future.set_result(fn())
        except BaseException as error:
            future.set_exception(error)
        return future


@pytest.fixture
def domain(monkeypatch):
    calls = {'start_errors': [], 'run_errors': [], 'preserved': []}

    def enter(task, started, lock):
        task['status'] = 'running'
        started.set()
        return True

    monkeypatch.setattr(spawn, '_mark_worker_started', enter)
    monkeypatch.setattr(spawn, '_settle_unscheduled_workspace',
                        lambda task: calls['preserved'].append(task))
    monkeypatch.setattr(spawn, '_finalize_rejected_submission',
                        lambda task, error: calls['start_errors'].append(error))
    monkeypatch.setattr('lib.tasks_pkg.manager.finalize_chat_task_error',
                        lambda task, error: calls['run_errors'].append(error))
    spawn.set_agent_executor(None)
    spawn.set_serving_loop(None)
    yield calls
    spawn.set_agent_executor(None)
    spawn.set_serving_loop(None)


@pytest.mark.parametrize('failure', [None, RuntimeError('runner failed')])
def test_inline_worker_isolates_request_context_and_preserves_future(failure, domain):
    request = contextvars.ContextVar('chat_worker_request', default='no-request')
    task = {'id': 'inline-chat', 'status': 'pending', '_userId': 7,
            '_requestId': 'accepted-request', 'convId': 'accepted-conversation'}
    seen = {}

    def run(carrier):
        seen.update(request=request.get(), correlation=req_id(), carrier=carrier)
        if failure is not None:
            raise failure
        return 'result'

    token = request.set('live-request-resource')
    previous = req_id()
    set_req_id('caller-request')
    try:
        executor = InlineExecutor()
        future = spawn.spawn_task(task, runner=run, executor=executor)
        assert request.get() == 'live-request-resource'
        assert req_id() == 'caller-request'
    finally:
        request.reset(token)
        set_req_id(previous)
    assert seen == {'request': 'no-request', 'correlation': 'accepted-request',
                    'carrier': task}
    assert executor.task_id == task['id']
    assert future.exception() is failure
    if failure is None:
        assert future.result() == 'result'
    assert domain['start_errors'] == []
    assert domain['run_errors'] == ([] if failure is None else [str(failure)])
    assert domain['preserved'] == [task]


@pytest.mark.parametrize('failure', [None, RuntimeError('joined runner failed')])
def test_durable_worker_runs_in_its_owned_thread_until_domain_exit(failure, domain):
    task = {'id': 'durable-chat', 'status': 'pending', '_userId': 7}
    request = contextvars.ContextVar('durable-request', default=None)
    caller = threading.get_ident()
    token = request.set('http-resource')
    seen = []

    def run(carrier):
        assert carrier is task
        assert threading.get_ident() == caller
        assert request.get() is None
        seen.append('entered')
        if failure:
            raise failure
        return 'settled'

    try:
        if failure:
            with pytest.raises(RuntimeError) as caught:
                spawn.run_task_inline(task, runner=run)
            assert caught.value is failure
        else:
            assert spawn.run_task_inline(task, runner=run) == 'settled'
        assert request.get() == 'http-resource'
    finally:
        request.reset(token)
    assert seen == ['entered']
    assert domain['preserved'] == [task]
    assert domain['run_errors'] == ([str(failure)] if failure else [])


@pytest.mark.parametrize('entered', [False, True])
def test_submission_and_admission_failure_keep_start_attribution(monkeypatch, domain, entered):
    error = RuntimeError('worker admission refused')
    task = {'id': 'rejected-chat', 'status': 'pending'}

    def refuse(*args):
        raise error

    if entered:
        monkeypatch.setattr(spawn, '_mark_worker_started', refuse)
        future = spawn.spawn_task(task, runner=lambda _: pytest.fail('runner entered'),
                                  executor=InlineExecutor())
        assert future.exception() is error
    else:
        class RejectingExecutor:
            submit = staticmethod(refuse)
        assert spawn.spawn_task(task, runner=lambda _: pytest.fail('runner entered'),
                                executor=RejectingExecutor()) is None
    assert domain['start_errors'] == [error]
    assert domain['run_errors'] == []


def test_cancelled_unentered_future_does_not_invent_terminal_settlement(domain):
    class CancelledExecutor:
        def submit(self, fn):
            future = Future()
            future.cancel()
            return future

    future = spawn.spawn_task({'id': 'cancelled-chat'},
                              runner=lambda _: pytest.fail('runner entered'),
                              executor=CancelledExecutor())
    assert future.cancelled()
    assert not any(domain.values())


@pytest.mark.parametrize('delayed', [False, True])
def test_executor_future_failure_before_entry_uses_start_settlement(domain, delayed):
    failure = RuntimeError('executor initialization failed')

    class FailsBeforeEntry:
        def submit(self, callback):
            future = Future()
            if delayed:
                asyncio.get_running_loop().call_soon(future.set_exception, failure)
            else:
                future.set_exception(failure)
            return future

    async def drive():
        spawn.set_agent_executor(FailsBeforeEntry())
        spawn.spawn_task({'id': 'failed-initializer', 'status': 'pending'},
                          runner=lambda _: pytest.fail('worker must not enter'))
        await asyncio.gather(*chat_task_runtime._bg_tasks)

    asyncio.run(drive())
    assert domain['start_errors'] == [failure]
    assert domain['run_errors'] == domain['preserved'] == []


def test_entered_worker_settlement_failure_is_not_settled_again(monkeypatch, domain):
    admission_failure = RuntimeError('entry rejected')
    persistence_failure = RuntimeError('terminal receipt unavailable')
    failures = []

    def reject_entry(*args):
        raise admission_failure

    def reject_settlement(task, error):
        failures.append(error)
        raise persistence_failure

    monkeypatch.setattr(spawn, '_mark_worker_started', reject_entry)
    monkeypatch.setattr(spawn, '_finalize_rejected_submission', reject_settlement)

    async def drive():
        spawn.spawn_task({'id': 'entered-failure', 'status': 'pending'},
                          runner=lambda _: pytest.fail('runner must not enter'))
        assert await asyncio.gather(*chat_task_runtime._bg_tasks,
                                     return_exceptions=True) == [persistence_failure]

    asyncio.run(drive())
    assert failures == [admission_failure]


@pytest.mark.parametrize('background', [False, True])
def test_acknowledged_native_stop_retains_cancelled_exception_and_terminal(domain, background):
    failure = asyncio.CancelledError('accepted Stop')
    task = {'id': 'native-stop', 'status': 'pending',
            '_nativePreparedInvocation': object()}

    def run(carrier):
        carrier.update(status='aborted', _nativePreparedInvocation=None)
        raise failure

    if background:
        async def drive():
            spawn.spawn_task(task, runner=run)
            await asyncio.gather(*chat_task_runtime._bg_tasks, return_exceptions=True)
        asyncio.run(drive())
    else:
        future = spawn.spawn_task(task, runner=run, executor=InlineExecutor())
        assert future.exception() is failure
    assert task['status'] == 'aborted'
    assert task['_activeWorkerCount'] == 0
    assert domain['start_errors'] == domain['run_errors'] == []
    assert domain['preserved'] == [task]


def test_fixed_batch_has_one_submission_and_one_entered_worker_per_task(domain):
    executor = InlineExecutor()
    entered = []
    caller_thread = threading.get_ident()

    def run(task):
        entered.append(task['id'])
        assert threading.get_ident() == caller_thread
        assert task['_activeWorkerCount'] == 1

    for index in range(32):
        task = {'id': f'batch-{index}', 'status': 'pending'}
        spawn.spawn_task(task, runner=run, executor=executor).result()
        assert task['_activeWorkerCount'] == 0
    assert executor.submissions == len(entered) == len(set(entered)) == 32
    assert domain['start_errors'] == domain['run_errors'] == []


@pytest.mark.parametrize('reject_during_probe', [False, True])
def test_closed_serving_loop_falls_back_once_and_closes_unsubmitted_coroutine(
        monkeypatch, domain, reject_during_probe):
    from lib.agent_core import task_runtime

    pending, entered = [], []

    class ClosingLoop:
        def is_running(self):
            if reject_during_probe:
                raise RuntimeError('loop closed')
            return True

    def reject(coroutine, loop):
        pending.append(coroutine)
        raise RuntimeError('loop closed during handoff')

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            assert name == 'run_task-loop-closed' and daemon
        def start(self):
            self.target()

    monkeypatch.setattr(task_runtime.asyncio, 'run_coroutine_threadsafe', reject)
    monkeypatch.setattr(task_runtime.threading, 'Thread', ImmediateThread)
    spawn.set_serving_loop(ClosingLoop())
    task = {'id': 'loop-closed', 'status': 'pending'}
    spawn.spawn_task(task, runner=lambda carrier: entered.append(carrier))
    assert entered == [task]
    assert all(coroutine.cr_frame is None for coroutine in pending)
    assert domain['start_errors'] == domain['run_errors'] == []
    assert task['_activeWorkerCount'] == 0


def test_worker_observation_remains_on_original_chat_carrier(domain):
    task = chat_task_runtime.create(user_id=7, task_id='worker-' + uuid.uuid4().hex)
    task_id = task['id']
    try:
        def run(carrier):
            assert carrier is task
            assert chat_task_runtime.poll(task_id)['workerActive'] is True
            chat_task_runtime.discard(task_id)
            replacement = chat_task_runtime.create(user_id=7, task_id=task_id)
            assert replacement is not task
            assert 'workerActive' not in chat_task_runtime.poll(task_id)

        spawn.spawn_task(task, runner=run, executor=InlineExecutor()).result()
        assert task['_activeWorkerCount'] == 0
        assert 'workerActive' not in chat_task_runtime.poll(task_id)
    finally:
        chat_task_runtime.discard(task_id)


def test_background_worker_and_queue_observer_do_not_retain_request_context(monkeypatch, domain):
    request = contextvars.ContextVar('queued_chat_request', default='no-request')
    seen = []
    finished = threading.Event()

    async def observe(*args):
        seen.append(('observer', request.get()))

    def run(task):
        seen.append(('worker', request.get()))
        finished.set()

    monkeypatch.setattr(spawn, '_report_executor_queue', observe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.scheduling_snapshot = lambda _: {}
        spawn.set_agent_executor(executor)

        async def drive():
            token = request.set('live-request-resource')
            try:
                spawn.spawn_task({'id': 'queued-context'}, runner=run)
                # Join the actual submitted callback and queue observer, not a
                # guessed delay after worker entry.
                pending = [job for job in asyncio.all_tasks()
                           if job is not asyncio.current_task()]
                await asyncio.gather(*pending)
            finally:
                request.reset(token)

        asyncio.run(drive())
    assert finished.is_set()
    assert sorted(seen) == [('observer', 'no-request'), ('worker', 'no-request')]
