"""Presentation waits for file/freshness release, not merely journal release."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
import threading

import pytest

from lib.project_mod import post_commit

pytestmark = pytest.mark.unit


def test_nested_defer_marks_ready_only_after_outer_release(tmp_path):
    seen = []
    state = {'file_held': True, 'freshness_current': False}

    def callback():
        assert not state['file_held']
        assert state['freshness_current']
        seen.append('released')

    with post_commit.defer_publication():
        ticket = post_commit.register(str(tmp_path), 'file', callback)
        with post_commit.defer_publication():
            post_commit.publish([ticket])
        assert not ticket.ready
        assert seen == []
        state.update(file_held=False, freshness_current=True)
    assert seen == ['released']
    assert ticket.done
    assert post_commit._lanes == {}


def test_other_publisher_cannot_drain_an_unreleased_file_ticket(tmp_path):
    seen = []
    attempted = threading.Event()
    released = False

    def first_callback():
        assert released
        seen.append('first')

    with ThreadPoolExecutor(max_workers=1) as pool:
        with post_commit.defer_publication():
            first = post_commit.register(str(tmp_path), 'file', first_callback)
            post_commit.publish([first])
            second = post_commit.register(str(tmp_path), 'file', lambda: seen.append('second'))

            def other_publisher():
                attempted.set()
                post_commit.publish([second])

            waiting = pool.submit(other_publisher)
            assert attempted.wait(5)
            assert not first.ready
            assert not waiting.done()
            assert seen == []
            released = True
        waiting.result(timeout=5)
    assert seen == ['first', 'second']
    assert post_commit._lanes == {}


def test_exception_releases_already_committed_notifications(tmp_path):
    seen = []
    with pytest.raises(ValueError, match='after commit'):
        with post_commit.defer_publication():
            ticket = post_commit.register(str(tmp_path), 'file', lambda: seen.append('committed'))
            post_commit.publish([ticket])
            raise ValueError('after commit')
    assert seen == ['committed']
    assert post_commit._lanes == {}
    assert not hasattr(post_commit._deferring, 'tickets')


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires POSIX fork')
def test_fork_does_not_replay_parent_deferred_presentation(tmp_path):
    ctx = multiprocessing.get_context('fork')
    seen = []
    receive, send = ctx.Pipe(duplex=False)
    with post_commit.defer_publication():
        parent = post_commit.register(str(tmp_path), 'parent', lambda: seen.append('parent'))
        post_commit.publish([parent])

        def child():
            receive.close()
            child_seen = []
            ticket = post_commit.register(str(tmp_path), 'child', lambda: child_seen.append('child'))
            post_commit.publish([ticket])
            send.send((child_seen, bool(post_commit._lanes), hasattr(post_commit._deferring, 'tickets')))
            send.close()

        worker = ctx.Process(target=child)
        worker.start()
        send.close()
        try:
            assert receive.poll(5)
            assert receive.recv() == (['child'], False, False)
            worker.join(5)
            assert worker.exitcode == 0
            assert seen == []
        finally:
            receive.close()
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    assert seen == ['parent']
    assert post_commit._lanes == {}
