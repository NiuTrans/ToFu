"""Standalone demand fan-out retains every eligible call and physical owner."""
import asyncio
import contextvars
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest

from tofu_search._worker_executor import DemandThreadPoolExecutor

pytestmark = pytest.mark.unit


def test_persistent_standalone_pool_is_closed_before_interpreter_join(tmp_path):
    package = Path(__file__).resolve().parents[1]
    script = ("import sys,time\n"
              f"sys.path.insert(0,{str(package)!r})\n"
              "from tofu_search._worker_executor import DemandThreadPoolExecutor\n"
              "pool=DemandThreadPoolExecutor(max_workers=2)\n"
              "assert pool.submit(lambda: 7).result(2)==7\n"
              "assert pool.worker_threads_snapshot()\n"
              "pool.submit(time.sleep,0.02)\n")
    result = subprocess.run([sys.executable, '-I', '-c', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_thirty_two_fetches_overlap_beyond_legacy_eight_workers(monkeypatch):
    from tofu_search.fetch import core
    urls = [f'https://example.test/{i}' for i in range(32)]
    gate = threading.Barrier(32, timeout=5)
    pools = []
    class Observe(DemandThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pools.append(self)
    monkeypatch.setattr(core, 'ThreadPoolExecutor', Observe)
    def fetch(url, **kwargs):
        gate.wait()
        return 'source:' + url + 'x' * 60
    monkeypatch.setattr(core, 'fetch_page_content', fetch)
    started = time.monotonic()
    result = core.fetch_urls(urls)
    print(f'32 standalone fetches joined in {time.monotonic() - started:.6f}s')
    assert set(result) == set(urls)
    assert all(result[url].startswith('source:' + url) for url in urls)
    assert len(pools) == 1 and pools[0].worker_threads_snapshot() == ()


def test_filter_calls_overlap_with_legacy_hint_one(monkeypatch):
    from tofu_search.config import SearchConfig
    from tofu_search.fetch import content_filter as cf
    gate = threading.Barrier(32, timeout=5)
    def filter_page(text, **kwargs):
        gate.wait()
        return 'clean:' + text
    monkeypatch.setattr(cf, 'filter_web_content', filter_page)
    monkeypatch.setattr(cf, 'ThreadPoolExecutor',
        lambda **kwargs: DemandThreadPoolExecutor(max_workers=1))
    config = SearchConfig(llm_api_key='synthetic-key', filter_min_chars=0)
    items = [(f'https://example.test/{i}', str(i)) for i in range(32)]
    assert cf.filter_web_contents_batch(items, config=config) == {
        url: 'clean:' + text for url, text in items}


def test_start_failure_withdraws_one_fetch_and_drains_siblings(monkeypatch):
    from tofu_search.fetch import core
    original = threading.Thread.start
    gate = threading.Barrier(31, timeout=5)
    calls = []
    starts = 0
    def start(thread):
        nonlocal starts
        if thread.name.startswith('tofu-demand'):
            starts += 1
            if starts == 8:
                raise OSError('injected physical startup failure')
        return original(thread)
    def fetch(url, **kwargs):
        calls.append(url)
        gate.wait()
        return 'retained:' + url + 'x' * 60
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(core, 'fetch_page_content', fetch)
    urls = [f'https://example.test/{i}' for i in range(32)]
    result = core.fetch_urls(urls)
    expected = set(urls) - {urls[7]}
    assert set(result) == expected
    assert set(calls) == expected


def test_closed_executor_returns_terminal_failure_without_effect():
    pool = DemandThreadPoolExecutor(max_workers=1)
    pool.shutdown()
    effects = []
    future = pool.submit(lambda: effects.append('unstarted'))
    assert future.done()
    with pytest.raises(RuntimeError, match='shutdown'):
        future.result(timeout=0)
    assert effects == [] and pool.worker_threads_snapshot() == ()


def test_url_extraction_retains_all_valid_links_and_only_deduplicates():
    from tofu_search.fetch.core import extract_urls_from_text
    urls = [f'https://example.test/{i}' for i in range(32)]
    assert extract_urls_from_text(' '.join(urls + [urls[0]])) == urls


def test_mcp_health_observes_zero_quota_without_creating_workers(monkeypatch):
    from tofu_search.mcp_server import _bridge as bridge
    monkeypatch.setattr(bridge, '_pool', None)
    monkeypatch.setenv('TOFU_SEARCH_MCP_MAX_CONCURRENCY', '1')
    assert bridge.bridge_snapshot() == {
        'limit': 0, 'in_use': 0, 'idle_reuse_hint': 1, 'resident_threads': 0}
    assert bridge._pool is None


def test_mcp_accepts_32_requests_with_hint_one_and_copies_identity(monkeypatch):
    from tofu_search.mcp_server import _bridge as bridge
    pool = DemandThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(bridge, '_pool', pool)
    owner = contextvars.ContextVar('synthetic_owner')
    gate = threading.Barrier(33, timeout=5)
    def read():
        gate.wait()
        return owner.get()
    async def request(value):
        token = owner.set(value)
        try:
            return await bridge.run_blocking(read)
        finally:
            owner.reset(token)
    async def run():
        tasks = [asyncio.create_task(request(i)) for i in range(32)]
        await asyncio.to_thread(gate.wait)
        assert await asyncio.gather(*tasks) == list(range(32))
    try:
        anyio.run(run)
    finally:
        bridge.shutdown_bridge()
    assert pool.worker_threads_snapshot() == ()


def test_mcp_direct_cancel_waits_for_owned_worker(monkeypatch):
    from tofu_search.mcp_server import _bridge as bridge
    pool = DemandThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(bridge, '_pool', pool)
    entered = threading.Event()
    release = threading.Event()
    def read():
        entered.set()
        assert release.wait(5)
        return 'drained'
    async def run():
        task = asyncio.create_task(bridge.run_blocking(read))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), 'cancel detached an accepted worker'
            task.cancel()  # repeated Stop also retains the same owner
            await asyncio.sleep(0.02)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        anyio.run(run)
    finally:
        bridge.shutdown_bridge()
    assert pool.worker_threads_snapshot() == ()
