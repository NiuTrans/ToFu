"""Borrow executor Futures to observe real ROOT completion without polling.

Executor ownership, result/exception and scheduling remain unchanged. The
fixture captures only task identity and borrowed Futures, joins every accepted
job on exit, and never cancels a productive worker when its observation expires.
"""
import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import Future
import threading
from unittest.mock import patch


class WorkerCompletion:
    def __init__(self, executor):
        self.executor = executor
        self.loop = asyncio.get_running_loop()
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.capturing = 0
        self.jobs = {}
        self.arrivals = {}
        self.original = executor.submit_task
        self.closed = False

    def submit(self, task_id, function, /, *args, **kwargs):
        with self.changed:
            closed = self.closed
            if not closed:
                self.capturing += 1
        if closed:
            return self.original(task_id, function, *args, **kwargs)
        try:
            future = self.original(task_id, function, *args, **kwargs)
            if not isinstance(future, Future):
                raise TypeError('completion observation requires scheduler Future')
            with self.lock:
                self.jobs[task_id] = future
            self.loop.call_soon_threadsafe(self.publish, task_id, future)
            return future
        finally:
            with self.changed:
                self.capturing -= 1
                self.changed.notify_all()

    def publish(self, task_id, future):
        with self.lock:
            current=self.jobs.get(task_id)
        if self.closed or current is not future:
            return
        arrival = self.arrivals.get(task_id)
        if arrival is not None and not arrival.done():
            arrival.set_result(future)

    async def wait(self, task_id, *, timeout):
        if self.closed or type(task_id) is not str or not task_id:
            raise ValueError('completion observation identity/lifetime invalid')
        with self.lock:
            future = self.jobs.get(task_id)
        async with asyncio.timeout(timeout):
            if future is None:
                arrival = self.arrivals.setdefault(task_id, self.loop.create_future())
                future = await asyncio.shield(arrival)
            # Shield the borrowed Future: observation timeout is not cancellation
            # authority over an admitted task. Exit separately joins the job.
            result = await asyncio.shield(asyncio.wrap_future(future, loop=self.loop))
        with self.lock:
            if self.jobs.get(task_id) is not future:
                raise RuntimeError('completion observation Future changed')
            self.jobs.pop(task_id)
        self.arrivals.pop(task_id, None)
        return result

    async def close(self):
        with self.changed:
            self.closed = True
            capturing = self.capturing > 0
        def join_capture():
            with self.changed:
                self.changed.wait_for(lambda: self.capturing == 0)
        async def drain():
            if capturing:
                await asyncio.to_thread(join_capture)
            with self.lock:
                jobs = tuple(self.jobs.values())
            if jobs:
                await asyncio.gather(*(asyncio.wrap_future(job, loop=self.loop)
                                       for job in jobs), return_exceptions=True)
        work = asyncio.create_task(drain())
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(work)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if work.done():
                        work.result()
                        break
        finally:
            for arrival in self.arrivals.values():
                if not arrival.done():
                    arrival.cancel()
            self.arrivals.clear()
            with self.lock:
                self.jobs.clear()
        if cancelled:
            raise asyncio.CancelledError


@asynccontextmanager
async def observe_workers(executor):
    observation = WorkerCompletion(executor)
    try:
        with patch.object(executor, 'submit_task', observation.submit):
            yield observation
    finally:
        await observation.close()
