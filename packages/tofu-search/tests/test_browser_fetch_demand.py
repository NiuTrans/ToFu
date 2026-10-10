"""Browser fallback burst admission keeps request-bound provider ownership."""
import contextvars
import threading
import time

import pytest

from tofu_search._worker_executor import DemandThreadPoolExecutor
from tofu_search.fetch import http
from tofu_search.providers import (
    BrowserProvider,
    bound_optional_providers,
    register_browser_provider,
    submit_with_provider_context,
)

pytestmark = pytest.mark.unit


def test_browser_fallback_burst_preserves_identity_and_does_not_discard_work(monkeypatch):
    owner = contextvars.ContextVar('browser-test-owner')
    barrier = threading.Barrier(33)
    release = threading.Event()
    counter_lock = threading.Lock()
    finished = []
    active = 0
    peak = 0
    class BoundBrowser(BrowserProvider):
        def __init__(self, identity):
            self.identity = identity
        def is_connected(self):
            return True
        def fetch_url(self, url, *, max_chars=None, timeout=15):
            nonlocal active, peak
            assert owner.get() == self.identity
            assert max_chars == 128 and timeout == 15
            with counter_lock:
                active += 1
                peak = max(peak, active)
            try:
                if url.endswith('/failure'):
                    raise OSError('provider transport unavailable')
                if not url.endswith('/healthy'):
                    barrier.wait(5)
                    assert release.wait(10)
                return f'owner {self.identity} captured page content'
            finally:
                with counter_lock:
                    active -= 1
                    finished.append(self.identity)
    class TemplateBrowser(BrowserProvider):
        def bind(self):
            return BoundBrowser(owner.get())
    register_browser_provider(TemplateBrowser())
    monkeypatch.setattr(http, '_browser_fallback_stats', {
        'attempts':0, 'skipped':0, 'success':0, 'fail':0, 'last_log':time.time()})
    pool = DemandThreadPoolExecutor(max_workers=1, thread_name_prefix='browser-demand')
    def submit(identity, suffix):
        token = owner.set(identity)
        try:
            with bound_optional_providers():
                return submit_with_provider_context(pool, http.try_browser_fetch,
                    f'https://burst.example/{suffix}', 128)
        finally:
            owner.reset(token)
    futures = []
    try:
        futures = [submit(index, f'blocked-{index}') for index in range(32)]
        barrier.wait(5)
        assert all(not future.done() for future in futures)
        assert submit(100, 'healthy').result(2) == 'owner 100 captured page content'
        assert submit(101, 'failure').result(2) is None
        assert submit(102, 'healthy').result(2) == 'owner 102 captured page content'
        assert active == 32
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert [future.result() for future in futures] == [f'owner {i} captured page content' for i in range(32)]
    assert active == 0 and peak == 33
    assert len(finished) == 35
    assert http._browser_fallback_stats == {
        'attempts':35, 'skipped':0, 'success':34, 'fail':1,
        'last_log':http._browser_fallback_stats['last_log']}
    assert not pool.worker_threads_snapshot()
