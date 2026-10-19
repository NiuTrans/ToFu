"""Native host cleanup control faults cannot escape the serving loop.

Subprocesses contain the deliberately faulty Task boundary in the old owner.
No model, archive loading, native execution or durable storage is involved.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.native_host import NativeHost
from tests.tasks_pkg.test_native_host import image

pytestmark = pytest.mark.unit


PROGRAM = r'''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from lib.app.app_factory import create_base_app
from lib.app.app_lifecycle import add_shutdown_handler
from lib.tasks_pkg.native_host import register_native_host
from tests.tasks_pkg.test_native_host import image

owner, kind, output = sys.argv[1:]
failure = {'interrupt': KeyboardInterrupt, 'exit': SystemExit,
           'cancel': asyncio.CancelledError, 'ordinary': OSError}[kind]('cleanup fault')
events, record = [], {}
app = create_base_app('native-host-close-control')
add_shutdown_handler(app, lambda: events.append('dependency'), name='dependency')
release = image()
host = register_native_host(app, image=release, token_count=True)

def close_owned(name):
    events.append(name)
    if owner == name:
        raise failure

class Client:
    def __init__(self, name):
        self.name = name
    async def close(self):
        await asyncio.sleep(0)
        close_owned(self.name)

host._clients[Client('client')] = None
host._clients[Client('sibling')] = None
host._token_counter = SimpleNamespace(close=lambda: close_owned('token'))
host._flow_scheduler = SimpleNamespace(close=lambda **kwargs: close_owned('flow'))
host._worker_pool = Client('pool')
original_image_close = release.close
async def close_image():
    await original_image_close()
    events.append('image')
release.close = close_image

def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]

async def exercise():
    await app.startup()
    caught_failure = None
    try:
        await app.shutdown()
    except BaseException as caught:
        caught_failure = caught
        record['original_failure'] = leaves(caught) == [failure]
    await asyncio.sleep(0)
    record['loop_progressed'] = True
    record['host_closed'] = host._closed
    record['image_closed'] = release._closed
    record['lifecycle'] = app.extensions['tofu_lifecycle']['status']
    record['remaining_clients'] = len(host._clients)
    record['all_owners_attempted_once'] = sorted(events) == sorted(
        ['client', 'sibling', 'pool', 'token', 'flow', 'image', 'dependency'])
    record['dependency_order'] = events[-2:] == ['image', 'dependency']
    before = list(events)
    try:
        await host.close()
    except BaseException as caught:
        record['stable_failure'] = caught is caught_failure
    record['no_repeated_cleanup'] = events == before

try:
    asyncio.run(exercise())
except BaseException as escaped:
    record['escaped_loop'] = type(escaped).__name__
Path(output).write_text(json.dumps(record))
'''


@pytest.mark.parametrize('owner', ['client', 'token', 'flow'])
@pytest.mark.parametrize('failure', ['interrupt', 'exit', 'cancel', 'ordinary'])
def test_control_failure_preserves_all_host_owners_and_lifespan(tmp_path, owner, failure):
    output = tmp_path / 'result.json'
    python = os.environ.get('TOFU_BENCHMARK_PYTHON') or sys.executable
    result = subprocess.run([python, '-c', PROGRAM, owner, failure, str(output)],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(output.read_text()) == {
        'original_failure': True,
        'loop_progressed': True,
        'host_closed': True,
        'image_closed': True,
        'lifecycle': 'stopped',
        'remaining_clients': int(owner == 'client'),
        'all_owners_attempted_once': True,
        'dependency_order': True,
        'stable_failure': True,
        'no_repeated_cleanup': True,
    }


def test_unstarted_host_close_releases_coroutine_and_preserves_retry(monkeypatch):
    host = NativeHost(image(), token_count=True)
    attempts, unstarted = [], []
    host._token_counter = SimpleNamespace(close=lambda: attempts.append('token'))
    failure = RuntimeError('task creation refused')

    def refuse(operation):
        unstarted.append(operation)
        raise failure

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, 'create_task', refuse)
            with pytest.raises(RuntimeError) as caught:
                await host.close()
            assert caught.value is failure
        assert host._closed and host._closing is None
        assert attempts == [] and not host.image._closed
        assert len(unstarted) == 1 and unstarted[0].cr_frame is None
        await host.close()
        assert attempts == ['token'] and host.image._closed

    asyncio.run(exercise())


def test_repeated_shutdown_cancellation_still_joins_worker_failure():
    host = NativeHost(image(), token_count=True)
    entered, released, completed = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    failure = OSError('worker cleanup failed')

    def close_counter():
        entered.set()
        try:
            assert released.wait(5)
            attempts.append('token')
            raise failure
        finally:
            completed.set()

    host._token_counter = SimpleNamespace(close=close_counter)
    host._flow_scheduler = SimpleNamespace(close=lambda **kwargs: attempts.append('flow'))

    async def exercise():
        closing = asyncio.create_task(host.close())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(2):
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done()
                assert not host.image._closed and attempts == []
            released.set()
            with pytest.raises(ExceptionGroup) as caught:
                await closing
            assert caught.value.exceptions == (failure,)
            assert completed.is_set()
            assert attempts == ['token', 'flow'] and host.image._closed
        finally:
            released.set()
            await asyncio.gather(closing, return_exceptions=True)

    asyncio.run(exercise())


def test_unstarted_worker_cleanup_keeps_failure_and_attempts_other_owners(monkeypatch):
    host = NativeHost(image(), token_count=True)
    attempts, scheduled = [], []
    host._token_counter = SimpleNamespace(close=lambda: attempts.append('token'))
    host._flow_scheduler = SimpleNamespace(close=lambda **kwargs: attempts.append('flow'))
    failure = RuntimeError('worker cleanup task creation refused')

    async def exercise():
        loop = asyncio.get_running_loop()
        create_task = loop.create_task

        def schedule(operation, **options):
            scheduled.append(operation)
            if len(scheduled) == 2:
                raise failure
            return create_task(operation, **options)

        with monkeypatch.context() as patch:
            patch.setattr(loop, 'create_task', schedule)
            with pytest.raises(ExceptionGroup) as caught:
                await host.close()
        assert caught.value.exceptions == (failure,)
        assert scheduled[1].cr_frame is None
        assert attempts == ['flow'] and host.image._closed
        # Repeated close observes the frozen failure; it cannot falsely claim
        # the undispatched token cleanup succeeded or rerun other owners.
        with pytest.raises(ExceptionGroup) as repeated:
            await host.close()
        assert repeated.value is caught.value
        assert attempts == ['flow']

    asyncio.run(exercise())


def test_client_scheduling_failure_joins_started_siblings_and_preserves_ownership(monkeypatch):
    host = NativeHost(image())
    failure = OSError('second client cleanup scheduling refused')
    attempts, operations, tasks = [], [], []

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        class Client:
            def __init__(self, name):
                self.name = name

            async def close(self):
                if self.name == 'first':
                    entered.set()
                    await release.wait()
                attempts.append(self.name)

        clients = [Client(name) for name in ('first', 'unscheduled', 'last')]
        host._clients.update((client, None) for client in clients)
        loop = asyncio.get_running_loop()
        create_task = loop.create_task

        def schedule(operation, **options):
            if operation.cr_code.co_name == '_close_client':
                operations.append(operation)
                if len(operations) == 2:
                    raise failure
                task = create_task(operation, **options)
                tasks.append(task)
                return task
            return create_task(operation, **options)

        with monkeypatch.context() as patch:
            patch.setattr(loop, 'create_task', schedule)
            closing = asyncio.create_task(host.close())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert not closing.done() and not host.image._closed
                release.set()
                with pytest.raises(ExceptionGroup) as caught:
                    await closing
                assert caught.value.exceptions == (failure,)
                assert sorted(attempts) == ['first', 'last'] and host.image._closed
                assert host._clients == {clients[1]: None}
                assert operations[1].cr_frame is None
                with pytest.raises(ExceptionGroup) as repeated:
                    await host.close()
                assert repeated.value is caught.value
            finally:
                release.set()
                await asyncio.gather(closing, *tasks, return_exceptions=True)

    asyncio.run(exercise())
