"""Completion observation delegates ownership and joins timed-out work."""
import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
import pytest
from benchmarks.native_completion import observe_workers

pytestmark=pytest.mark.unit


def test_wait_before_submission_preserves_exact_future_result_and_arguments():
    async def run():
        future=Future();seen=[]
        def submit(*args,**kwargs):seen.append((args,kwargs));return future
        executor=SimpleNamespace(submit_task=submit)
        async with observe_workers(executor) as observer:
            waiting=asyncio.create_task(observer.wait('task',timeout=2))
            await asyncio.sleep(0)
            marker=object()
            assert executor.submit_task('task',marker,1,named=2) is future
            future.set_result(marker)
            assert await waiting is marker
            assert seen==[(('task',marker,1),{'named':2})]
            assert observer.jobs=={}
        assert executor.submit_task is submit
    asyncio.run(run())


def test_timeout_does_not_cancel_worker_and_close_waits_until_physical_return():
    async def run():
        future=Future();future.set_running_or_notify_cancel()
        executor=SimpleNamespace(submit_task=lambda *args:future)
        entered=asyncio.Event();release=asyncio.Event()
        async def scope():
            async with observe_workers(executor) as observer:
                executor.submit_task('task',lambda:None)
                with pytest.raises(TimeoutError):await observer.wait('task',timeout=.01)
                assert not future.cancelled()
                entered.set()
        owner=asyncio.create_task(scope());await entered.wait();await asyncio.sleep(.01)
        assert not owner.done()
        future.set_result('done');await owner
    asyncio.run(run())


def test_worker_failure_propagates_and_scope_retires_observation():
    async def run():
        future=Future();failure=RuntimeError('injected worker failure')
        executor=SimpleNamespace(submit_task=lambda *args:future)
        async with observe_workers(executor) as observer:
            executor.submit_task('task',lambda:None);future.set_exception(failure)
            with pytest.raises(RuntimeError) as caught:await observer.wait('task',timeout=1)
            assert caught.value is failure
        assert observer.jobs=={} and observer.arrivals=={}
    asyncio.run(run())


def test_cancelled_scope_close_joins_before_retiring_borrowed_future():
    async def run():
        future=Future();future.set_running_or_notify_cancel()
        executor=SimpleNamespace(submit_task=lambda *args:future)
        entered=asyncio.Event();references=[]
        async def scope():
            async with observe_workers(executor) as observer:
                references.append(observer)
                executor.submit_task('task',lambda:None)
                entered.set()
        owner=asyncio.create_task(scope());await entered.wait();await asyncio.sleep(.01)
        owner.cancel();await asyncio.sleep(.01)
        assert not owner.done() and not future.cancelled()
        future.set_result('done')
        with pytest.raises(asyncio.CancelledError):await owner
        assert references[0].jobs=={} and references[0].arrivals=={}
    asyncio.run(run())


def test_close_joins_submission_in_progress_before_snapshotting_worker_futures():
    import threading
    async def run():
        entered,release=threading.Event(),threading.Event()
        future=Future();future.set_running_or_notify_cancel()
        def submit(*args):
            entered.set();assert release.wait(3);return future
        executor=SimpleNamespace(submit_task=submit)
        observer=None
        async def scope():
            nonlocal observer
            async with observe_workers(executor) as observer:
                await asyncio.to_thread(entered.wait,3)
        owner=asyncio.create_task(scope())
        await asyncio.sleep(0)
        submitting=asyncio.create_task(asyncio.to_thread(executor.submit_task,'task',lambda:None))
        assert await asyncio.to_thread(entered.wait,3)
        await asyncio.sleep(.01)
        assert not owner.done()
        release.set();assert await submitting is future
        await asyncio.sleep(.01);assert not owner.done()
        future.set_result('done');await owner
        assert observer.jobs=={} and observer.capturing==0
    asyncio.run(run())


@pytest.mark.parametrize('selection', ['worker-future', 'invalid', None, []])
def test_different_or_invalid_completion_observers_refuse_comparison(selection):
    from benchmarks.native_application_root import compare
    rows=[{'arm':'python','joined_cleanup':True},
          {'arm':'native','joined_cleanup':True,'completion_observer':selection}]
    with pytest.raises(ValueError,match='completion observer'):
        compare(rows)
