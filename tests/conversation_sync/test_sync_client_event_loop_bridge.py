"""The sync Quart bridge preserves interrupts and owns its private loop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import gc
import selectors
import signal
import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def alarm_failure():
    if not hasattr(signal, 'SIGALRM'):
        pytest.skip('this platform has no SIGALRM test deadline')
    failure = pytest.fail.Exception('injected test deadline')

    def expired(_signum, _frame):
        raise failure

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        yield failure, expired
    finally:
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize('placement', ['root', 'callback', 'child'])
def test_run_coro_propagates_existing_alarm_from_every_async_boundary(
        placement, alarm_failure, monkeypatch):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    children = []

    async def expire():
        signal.raise_signal(signal.SIGALRM)

    async def operation():
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def finish_probe():
            # Make a broken bridge fail promptly instead of hanging its suite.
            if not completed.done():
                completed.set_result('continued after the deadline')

        if placement == 'root':
            await expire()
        elif placement == 'callback':
            loop.call_soon(signal.raise_signal, signal.SIGALRM)
        else:
            children.append(asyncio.create_task(expire()))
        loop.call_soon(finish_probe)
        return await completed

    def refuse_new_timer(*_args):
        raise AssertionError('the bridge must retain the existing test timer')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(signal, 'setitimer', refuse_new_timer)
            with pytest.raises(pytest.fail.Exception) as caught:
                _run_coro(operation())
        assert caught.value is failure
        assert signal.getsignal(signal.SIGALRM) is installed_handler
    finally:
        for child in children:
            if child.done() and not child.cancelled():
                child.exception()


@pytest.mark.parametrize('entry', ['direct', 'running-loop', 'worker-thread'])
@pytest.mark.parametrize('outcome', ['value', 'error', 'cancel', 'interrupt', 'exit'])
def test_run_coro_preserves_ordinary_outcomes_and_observes_its_closed_root(entry, outcome):
    loops, reports = [], []
    failure = _non_alarm_failure(outcome)
    handler = signal.getsignal(signal.SIGALRM) if hasattr(signal, 'SIGALRM') else None

    async def operation():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        if failure is not None:
            raise failure
        return 'ok'

    if failure is None:
        assert _invoke(entry, operation()) == 'ok'
    else:
        with pytest.raises(type(failure)) as caught:
            _invoke(entry, operation())
        assert caught.value is failure
        del caught
        failure.__traceback__ = failure.__context__ = failure.__cause__ = None
    assert loops[0].is_closed()
    if hasattr(signal, 'SIGALRM'):
        assert signal.getsignal(signal.SIGALRM) is handler
    gc.collect()
    assert not [report for report in reports
                if report['message'] == 'Task exception was never retrieved']


def _invoke(entry, operation):
    from tests.conftest import _run_coro

    if entry == 'running-loop':
        async def outer():
            return _run_coro(operation)
        return asyncio.run(outer())
    if entry == 'worker-thread':
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_run_coro, operation).result()
    return _run_coro(operation)


def _non_alarm_failure(outcome):
    return {
        'error': ValueError('application failure'),
        'cancel': asyncio.CancelledError('stop'),
        'interrupt': KeyboardInterrupt('interrupted'),
        'exit': SystemExit(3),
    }.get(outcome)


def test_existing_helper_preserves_policy_registration_through_runner_cleanup():
    previous = asyncio.get_event_loop_policy()
    caller = threading.get_ident()
    helper_loops, observations, registrations = [], [], []

    class Policy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            loop = super().new_event_loop()
            if threading.get_ident() != caller:
                helper_loops.append(loop)

                def factory(owned_loop, coroutine, context=None):
                    try:
                        current = asyncio.get_event_loop()
                    except RuntimeError:
                        current = None
                    observations.append((owned_loop, current, coroutine.__name__))
                    return asyncio.Task(coroutine, loop=owned_loop, context=context)

                loop.set_task_factory(factory)
            return loop

        def set_event_loop(self, loop):
            super().set_event_loop(loop)
            if threading.get_ident() != caller:
                registrations.append(loop)

    async def operation():
        assert asyncio.get_event_loop() is asyncio.get_running_loop()
        return 'response'

    asyncio.set_event_loop_policy(Policy())
    try:
        assert _invoke('running-loop', operation()) == 'response'
        assert helper_loops and all(loop.is_closed() for loop in helper_loops)
        assert observations and all(owned is current for owned, current, _ in observations)
        assert {'shutdown_asyncgens', 'shutdown_default_executor'} <= {
            name for _, _, name in observations}
        assert registrations[0] is helper_loops[0]
        assert registrations[-1] is None
    finally:
        asyncio.set_event_loop_policy(previous)


@pytest.mark.parametrize('entry', ['direct', 'worker-thread'])
def test_factory_failure_preserves_already_accepted_tasks(entry, monkeypatch):
    loop = asyncio.new_event_loop()
    close = loop.close
    closed, accepted = [], []
    failure = ValueError('task factory failed after accepting work')

    async def background():
        return 'completed'

    async def operation():
        return 'response'

    request = operation()

    def factory(owned_loop, coroutine, context=None):
        if coroutine is request:
            accepted.append(asyncio.Task(background(), loop=owned_loop))
            raise failure
        return asyncio.Task(coroutine, loop=owned_loop, context=context)

    loop.set_task_factory(factory)
    monkeypatch.setattr(asyncio, 'new_event_loop', lambda: loop)
    # Defer only this test's physical close to observe the accepted task after
    # the failed request has handed it off, then drain and close in finally.
    monkeypatch.setattr(loop, 'close', lambda: closed.append(loop))
    try:
        with pytest.raises(ValueError) as caught:
            _invoke(entry, request)
        assert caught.value is failure
        assert closed and all(owned is loop for owned in closed)
        assert len(accepted) == 1 and not accepted[0].cancelled()
        assert loop.run_until_complete(accepted[0]) == 'completed'
    finally:
        request.close()
        loop.set_task_factory(None)
        loop.run_until_complete(asyncio.gather(*accepted, return_exceptions=True))
        close()


@pytest.mark.parametrize('entry', ['direct', 'worker-thread'])
@pytest.mark.parametrize('outcome', ['value', 'error', 'cancel', 'interrupt', 'exit'])
def test_non_alarm_outcomes_keep_accepted_background_work_runnable(entry, outcome, monkeypatch):
    occupied, release = threading.Event(), threading.Event()
    accepted, tasks, loops, closed = [], [], [], []
    executor = ThreadPoolExecutor(max_workers=1)
    failure = _non_alarm_failure(outcome)

    def occupy_worker():
        occupied.set()
        release.wait()

    executor.submit(occupy_worker)
    occupied.wait()
    submit = executor.submit

    def record_submission(*args, **kwargs):
        future = submit(*args, **kwargs)
        accepted.append(future)
        return future

    monkeypatch.setattr(executor, 'submit', record_submission)

    async def operation():
        loop = asyncio.get_running_loop()
        loops.append((loop, loop.close))
        # Record the request's close boundary, then physically close below
        # after this test has drained its accepted child without GC warnings.
        monkeypatch.setattr(loop, 'close', lambda: closed.append(loop))
        queued = asyncio.Event()

        async def background():
            completion = loop.run_in_executor(executor, lambda: 'completed')
            queued.set()
            return await completion

        tasks.append(asyncio.create_task(background()))
        await queued.wait()
        if failure is not None:
            raise failure
        return 'accepted'

    try:
        if failure is None:
            assert _invoke(entry, operation()) == 'accepted'
        else:
            with pytest.raises(type(failure)) as caught:
                _invoke(entry, operation())
            assert caught.value is failure
        assert closed and all(loop is loops[0][0] for loop in closed)
        assert len(accepted) == 1 and not accepted[0].cancelled()
        release.set()
        assert accepted[0].result() == 'completed'
    finally:
        release.set()
        executor.shutdown(wait=True)
        for loop, close in loops:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            close()


@pytest.mark.parametrize('outcome', ['value', 'error'])
def test_alarm_during_handler_restore_cannot_be_lost(outcome, alarm_failure, monkeypatch):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    signal_signal = signal.signal
    restored = []

    def restore(signum, handler):
        if signum == signal.SIGALRM and handler is installed_handler and not restored:
            restored.append(True)
            signal.raise_signal(signal.SIGALRM)
        return signal_signal(signum, handler)

    async def operation():
        if outcome == 'error':
            raise ValueError('application failure before handler restoration')
        return 'response'

    monkeypatch.setattr(signal, 'signal', restore)
    with pytest.raises(pytest.fail.Exception) as caught:
        _run_coro(operation())
    assert caught.value is failure
    assert restored == [True]
    assert signal.getsignal(signal.SIGALRM) is installed_handler


def test_alarm_during_handler_installation_cancels_and_closes_the_owned_root(
        alarm_failure, monkeypatch):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    signal_signal, new_event_loop = signal.signal, asyncio.new_event_loop
    loops, roots, installed = [], [], []

    def create_loop():
        loop = new_event_loop()
        loops.append(loop)
        return loop

    def install(signum, handler):
        previous = signal_signal(signum, handler)
        if signum == signal.SIGALRM and handler is not installed_handler and not installed:
            installed.append(True)
            roots.extend(asyncio.all_tasks(loops[0]))
            signal.raise_signal(signal.SIGALRM)
        return previous

    async def operation():
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(asyncio, 'new_event_loop', create_loop)
    monkeypatch.setattr(signal, 'signal', install)
    try:
        with pytest.raises(pytest.fail.Exception) as caught:
            _run_coro(operation())
        assert caught.value is failure
        assert len(roots) == 1 and roots[0].cancelled()
        assert loops[0].is_closed()
        assert signal.getsignal(signal.SIGALRM) is installed_handler
    finally:
        # A negative control must retire only the loop it deliberately stranded.
        signal_signal(signal.SIGALRM, installed_handler)
        for loop in loops:
            if not loop.is_closed():
                for task in roots:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*roots, return_exceptions=True))
                loop.close()


def test_alarm_in_bridge_prelude_still_closes_the_private_loop(alarm_failure, monkeypatch):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    loop = asyncio.new_event_loop()
    completed = loop.create_future()
    completed.set_result('response')
    get_signal = signal.getsignal
    fired = []

    def interrupt_prelude(signum):
        if signum == signal.SIGALRM and not fired:
            fired.append(True)
            signal.raise_signal(signal.SIGALRM)
        return get_signal(signum)

    monkeypatch.setattr(asyncio, 'new_event_loop', lambda: loop)
    monkeypatch.setattr(signal, 'getsignal', interrupt_prelude)
    try:
        with pytest.raises(pytest.fail.Exception) as caught:
            _run_coro(completed)
        assert caught.value is failure
        assert loop.is_closed()
        assert get_signal(signal.SIGALRM) is installed_handler
    finally:
        loop.close()


@pytest.mark.parametrize(('entry', 'request_fails', 'caller_handles_error'), [
    ('direct', False, True), ('direct', True, False), ('running-loop', True, False),
])
def test_cleanup_failure_keeps_the_original_finally_precedence(
        entry, request_fails, caller_handles_error, monkeypatch):
    cleanup_error = OSError('current loop close failed')
    loops = []

    async def operation():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        close = loop.close

        def failed_close():
            close()
            raise cleanup_error

        monkeypatch.setattr(loop, 'close', failed_close)
        if request_fails:
            raise ValueError('current request failed')
        return 'response'

    def invoke():
        with pytest.raises(OSError) as caught:
            _invoke(entry, operation())
        assert caught.value is cleanup_error

    if caller_handles_error:
        try:
            raise LookupError('caller is handling an unrelated error')
        except LookupError:
            invoke()
    else:
        invoke()
    assert loops[0].is_closed()


@pytest.mark.parametrize('outcome', ['value', 'error', 'cancel', 'interrupt', 'exit'])
def test_existing_helper_thread_settles_resources_for_every_outcome(outcome):
    finalized = []
    release_worker = threading.Event()
    loops, generators, tasks = [], [], []
    failure = _non_alarm_failure(outcome)

    async def stream():
        try:
            yield 'frame'
        finally:
            await asyncio.sleep(0)
            finalized.append('generator')

    async def operation():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        started = asyncio.Event()
        worker_started = asyncio.Event()

        async def child():
            started.set()
            try:
                await loop.create_future()
            finally:
                await asyncio.sleep(0)
                finalized.append('task')
                release_worker.set()

        def worker():
            loop.call_soon_threadsafe(worker_started.set)
            release_worker.wait()
            finalized.append('executor')

        generator = stream()
        generators.append(generator)
        assert await anext(generator) == 'frame'
        tasks.append(asyncio.create_task(child()))
        loop.run_in_executor(None, worker)
        await started.wait()
        await worker_started.wait()
        if outcome != 'value':
            raise failure
        return 'response'

    try:
        if outcome == 'value':
            assert _invoke('running-loop', operation()) == 'response'
        else:
            with pytest.raises(type(failure)) as caught:
                _invoke('running-loop', operation())
            assert caught.value is failure
        assert set(finalized) == {'task', 'generator', 'executor'}
        assert all(loop.is_closed() for loop in loops)
    finally:
        release_worker.set()


@pytest.mark.parametrize(('outcome', 'boundary'), [
    ('signal-return', 'task'), ('signal-error', 'task'), ('signal-return', 'callback'),
])
def test_cleanup_alarm_remains_the_primary_failure_and_restores_handler(
        outcome, boundary, alarm_failure):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    finalized, generators, loops = [], [], []

    async def stream():
        try:
            yield 'frame'
        finally:
            await asyncio.sleep(0)
            try:
                if boundary == 'task':
                    signal.raise_signal(signal.SIGALRM)
                else:
                    loop = asyncio.get_running_loop()
                    completed = loop.create_future()
                    loop.call_soon(signal.raise_signal, signal.SIGALRM)
                    loop.call_soon(lambda: completed.done() or completed.set_result(None))
                    await completed
            finally:
                finalized.append('generator')

    async def operation():
        loops.append(asyncio.get_running_loop())
        generator = stream()
        generators.append(generator)
        await anext(generator)
        loop = asyncio.get_running_loop()
        loop.call_soon(signal.raise_signal, signal.SIGALRM)
        try:
            await loop.create_future()
        except asyncio.CancelledError:
            if outcome == 'signal-error':
                raise ValueError('application failure after interrupted request')
            return 'response after suppressed cancellation'

    with pytest.raises(pytest.fail.Exception) as caught:
        _run_coro(operation())
    assert caught.value is failure
    assert finalized == ['generator']
    assert loops[0].is_closed()
    assert signal.getsignal(signal.SIGALRM) is installed_handler


def test_signal_interrupting_select_joins_the_cancelled_request(alarm_failure, monkeypatch):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    entered, finalized, fired = [], [], []
    select = selectors.DefaultSelector.select

    def interrupted_select(selector, timeout=None):
        if entered and not fired:
            fired.append(True)
            signal.raise_signal(signal.SIGALRM)
        return select(selector, timeout)

    async def operation():
        entered.append(True)
        try:
            await asyncio.get_running_loop().create_future()
        finally:
            await asyncio.sleep(0)
            finalized.append(True)

    monkeypatch.setattr(selectors.DefaultSelector, 'select', interrupted_select)
    with pytest.raises(pytest.fail.Exception) as caught:
        _run_coro(operation())
    assert caught.value is failure
    assert fired == finalized == [True]
    assert signal.getsignal(signal.SIGALRM) is installed_handler


def test_signal_child_failure_is_observed_before_loop_disposal(alarm_failure):
    from tests.conftest import _run_coro

    failure, _ = alarm_failure
    reports = []

    async def expired_child():
        signal.raise_signal(signal.SIGALRM)

    async def operation():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        asyncio.create_task(expired_child())
        await loop.create_future()

    with pytest.raises(pytest.fail.Exception) as caught:
        _run_coro(operation())
    assert caught.value is failure
    del caught
    # The deliberately retained failure must not keep its signal-time frame
    # alive while checking whether disposed Tasks report unobserved failures.
    failure.__traceback__ = failure.__context__ = failure.__cause__ = None
    gc.collect()
    assert not [report for report in reports
                if report['message'] == 'Task exception was never retrieved']


def test_interrupted_helper_thread_is_joined_after_its_async_cleanup(alarm_failure):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    owned, finalized = [], []
    caller = threading.get_ident()

    async def operation():
        loop = asyncio.get_running_loop()
        owned.extend((loop, asyncio.current_task(), threading.current_thread()))
        try:
            signal.pthread_kill(caller, signal.SIGALRM)
            await loop.create_future()
        finally:
            await asyncio.sleep(0)
            finalized.append('request')

    async def outer():
        with pytest.raises(pytest.fail.Exception) as caught:
            _run_coro(operation())
        assert caught.value is failure
        assert finalized == ['request']
        assert owned[0].is_closed() and not owned[2].is_alive()

    try:
        asyncio.run(outer())
        assert signal.getsignal(signal.SIGALRM) is installed_handler
    finally:
        # A broken bridge must not strand this test's own helper thread.
        if owned and not owned[0].is_closed():
            owned[0].call_soon_threadsafe(owned[1].cancel)
            owned[2].join()


def test_nonraising_alarm_handler_keeps_its_existing_behavior(alarm_failure):
    from tests.conftest import _run_coro

    _, installed_handler = alarm_failure
    called = []

    def observed(signum, _frame):
        called.append(signum)

    async def operation():
        signal.raise_signal(signal.SIGALRM)
        return 'response'

    signal.signal(signal.SIGALRM, observed)
    try:
        assert _run_coro(operation()) == 'response'
        assert called == [signal.SIGALRM]
        assert signal.getsignal(signal.SIGALRM) is observed
    finally:
        signal.signal(signal.SIGALRM, installed_handler)


def test_alarm_during_executor_shutdown_waits_for_physical_exit(alarm_failure):
    from tests.conftest import _run_coro

    failure, installed_handler = alarm_failure
    release_worker, worker_done = threading.Event(), threading.Event()
    loops, shutdown_threads = [], []

    def expire_during_join():
        signal.raise_signal(signal.SIGALRM)
        loops[0].call_soon(release_worker.set)

    class ObservedExecutor(ThreadPoolExecutor):
        notified = False

        def shutdown(self, wait=True, *, cancel_futures=False):
            if wait and not self.notified:
                self.notified = True
                shutdown_threads.append(threading.current_thread())
                loops[0].call_soon_threadsafe(expire_during_join)
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    executor = ObservedExecutor(max_workers=1)

    async def operation():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        loop.set_default_executor(executor)
        started = asyncio.Event()

        def worker():
            loop.call_soon_threadsafe(started.set)
            release_worker.wait()
            worker_done.set()

        loop.run_in_executor(None, worker)
        await started.wait()
        signal.raise_signal(signal.SIGALRM)

    try:
        with pytest.raises(pytest.fail.Exception) as caught:
            _run_coro(operation())
        assert caught.value is failure
        assert worker_done.is_set()
        assert all(not thread.is_alive() for thread in shutdown_threads)
        assert loops[0].is_closed()
        assert signal.getsignal(signal.SIGALRM) is installed_handler
    finally:
        # The negative control releases only this test's accepted worker.
        release_worker.set()
        executor.shutdown(wait=True)
        for thread in shutdown_threads:
            thread.join()
