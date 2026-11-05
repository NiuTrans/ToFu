"""Transport cancellation cannot escape one exclusive request's ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from tofu_db._request_cancellation import RequestCancellation

pytestmark = pytest.mark.unit


def test_detach_waits_for_interrupt_before_connection_can_be_reused():
    cancellation = RequestCancellation()
    entered, release, detaching, detached = (threading.Event() for _ in range(4))
    calls = []
    def interrupt():
        calls.append('interrupt')
        entered.set()
        assert release.wait(5)
    binding = cancellation.bind(interrupt)
    binding.__enter__()
    def detach():
        detaching.set()
        binding.__exit__(None, None, None)
        detached.set()
    with ThreadPoolExecutor() as executor:
        cancelled = executor.submit(cancellation.cancel)
        assert entered.wait(5)
        finished = executor.submit(detach)
        try:
            assert detaching.wait(5)
            assert not detached.is_set()
        finally:
            release.set()
        cancelled.result(5)
        finished.result(5)
    cancellation.cancel()
    assert calls == ['interrupt']
    with pytest.raises(OSError, match='before dispatch'):
        with cancellation.bind(lambda: pytest.fail('cancelled request borrowed a new connection')):
            pytest.fail('cancelled request dispatched')


def test_native_pool_retires_interrupted_exchange_even_if_response_completed():
    from lib.storage.v2_client import StorageV2Client
    cancellation = RequestCancellation()
    interrupted, releases = [], []
    def request(*args, **kwargs):
        cancellation.cancel()
        return SimpleNamespace(status=0, json=lambda: {'complete': True})
    session = SimpleNamespace(interrupt=lambda: interrupted.append(True), request=request)
    client = object.__new__(StorageV2Client)
    client._pool = SimpleNamespace(acquire=lambda timeout: session,
        release=lambda returned, *, broken: releases.append((returned, broken)))
    result = client._request_once('worker_control.watch', {}, timeout=1,
        command_id=None, deadline_unix_ms=1, cancellation=cancellation)
    assert result == {'complete': True}
    assert interrupted == [True]
    assert releases == [(session, True)]
