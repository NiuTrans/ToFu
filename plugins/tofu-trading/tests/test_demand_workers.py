"""Independent read work starts on demand without changing trade authority."""
import threading
import time
from types import SimpleNamespace

import pytest
from tofu_trading._worker_executor import DemandThreadPoolExecutor

pytestmark = pytest.mark.unit


def test_asset_detail_batch_overlaps_32_reads_and_keeps_siblings(monkeypatch):
    from tofu_trading.trading import info, screening
    gate = threading.Barrier(32, timeout=5)
    codes = [f'code-{i}' for i in range(32)]
    pools = []
    class Observe(DemandThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pools.append(self)
    monkeypatch.setattr(screening, 'ThreadPoolExecutor', Observe)
    def read(code, *, client):
        gate.wait()
        if code == 'code-7':
            raise OSError('injected read failure')
        return {'code': code, 'name': 'synthetic'}
    monkeypatch.setattr(info, 'fetch_asset_info', read)
    started = time.monotonic()
    result = screening.fetch_asset_detail_batch(codes,
        client=SimpleNamespace(check_network=lambda: True))
    print(f'32 trading read workers rendezvous and join in {time.monotonic()-started:.6f}s')
    assert set(result) == set(codes) - {'code-7'}
    assert all(result[code]['code'] == code for code in result)
    assert len(pools) == 1 and pools[0].worker_threads_snapshot() == ()


def test_intel_hint_one_starts_all_queries_with_separate_owned_handles(monkeypatch):
    from tofu_trading import storage
    from tofu_trading.trading import intel_mega_crawler as crawler
    gate = threading.Barrier(32, timeout=5)
    lock = threading.Lock()
    opened, closed = [], []
    def handle(domain, *, owner_user_id):
        assert domain == 'trading' and owner_user_id == 41
        value = SimpleNamespace(owner_user_id=owner_user_id)
        with lock:
            opened.append(value)
        def close():
            with lock:
                closed.append(value)
        value.close = close
        return value
    def crawl(db, category, query, *args, **kwargs):
        assert db.owner_user_id == 41
        gate.wait()
        return int(query)
    monkeypatch.setattr(storage, 'get_thread_db', handle)
    monkeypatch.setattr(crawler, 'crawl_intel_source', crawl)
    monkeypatch.setattr(crawler.time, 'sleep',
        lambda seconds: pytest.fail('completed receipts must not acquire a pacing delay'))
    queries = [str(i) for i in range(32)]
    result = crawler._crawl_category_batch(SimpleNamespace(owner_user_id=41),
        'synthetic', queries, None, None,
        crawler.MegaCrawlConfig(max_workers=1, rate_limit_delay=10))
    assert sorted((row['query'], row['fetched']) for row in result) == sorted(
        (query, int(query)) for query in queries)
    assert len(opened) == len(closed) == 32
    assert {id(value) for value in opened} == {id(value) for value in closed}


def test_start_failure_preserves_every_query_and_closes_accepted_handles(monkeypatch):
    from tofu_trading import storage
    from tofu_trading.trading import intel_mega_crawler as crawler
    original = threading.Thread.start
    starts = 0
    gate = threading.Barrier(31, timeout=5)
    closed = []
    effects = []
    def start(thread):
        nonlocal starts
        if thread.name.startswith('tofu-demand'):
            starts += 1
            if starts == 8:
                raise OSError('injected physical thread failure')
        return original(thread)
    def handle(domain, *, owner_user_id):
        assert owner_user_id == 73
        return SimpleNamespace(close=lambda: closed.append(owner_user_id))
    def crawl(db, category, query, *args, **kwargs):
        effects.append(query)
        gate.wait()
        return 1
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(storage, 'get_thread_db', handle)
    monkeypatch.setattr(crawler, 'crawl_intel_source', crawl)
    queries = [str(i) for i in range(32)]
    result = crawler._crawl_category_batch(SimpleNamespace(owner_user_id=73),
        'synthetic', queries, None, None, crawler.MegaCrawlConfig(max_workers=1))
    assert {row['query'] for row in result} == set(queries)
    failed = [row for row in result if row.get('error')]
    assert failed == [{'query': '7', 'fetched': 0, 'error': 'injected physical thread failure'}]
    assert sorted(effects) == sorted(set(queries) - {'7'})
    assert closed == [73] * 31
