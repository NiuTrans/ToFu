"""A task response must not close the test loop before executor retirement."""
import asyncio
import threading

import pytest

from tests.api.test_headless_no_saved_conversation import _run

pytestmark = pytest.mark.unit


def test_headless_loop_drains_executor_work_after_the_response():
    started, release, retired, returned = (threading.Event() for _ in range(4))
    background = []
    errors = []

    def finish_task():
        started.set()
        assert release.wait(30)
        retired.set()

    async def response():
        background.append(asyncio.create_task(asyncio.to_thread(finish_task)))
        assert await asyncio.to_thread(started.wait, 30)
        return 'response delivered before physical retirement'

    def request():
        try:
            _run(response())
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()

    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert started.wait(30)
        assert not returned.wait(0.2), 'The test loop closed while its executor was still running'
    finally:
        release.set()
        thread.join(30)
    assert not thread.is_alive()
    assert not errors
    assert retired.is_set()
    assert all(task.done() and not task.cancelled() for task in background)
