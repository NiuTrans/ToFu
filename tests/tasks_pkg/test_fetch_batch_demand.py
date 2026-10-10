"""Every declared URL retains its occurrence, failure and joined ownership."""
from concurrent.futures import Future
import json
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _item(url):
    return dict(url=url, page_content='evidence:' + url, is_pdf=False,
                raw_chars=40, filtered_chars=40, error_msg=None,
                image_contents=(url,), source_truncated=url.endswith('/31'))


def _execute(monkeypatch, lane, fetch, entries, task=None):
    if lane == 'stream':
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        monkeypatch.setattr('lib.tasks_pkg.handlers.search._core._fetch_url_one', fetch)
        monkeypatch.setattr('lib.tasks_pkg.handlers.search._display._format_fetch_display',
                            lambda item, short: {'url': item['url']})
        accumulator = StreamingToolAccumulator(task or {
            'id': 'fetch-demand', 'aborted': False, 'lastUserQuery': 'question',
        }, project_path='/tmp')
        try:
            result = accumulator._execute_one('fetch_url', {'urls': entries})
            return result, result.display_results
        finally:
            accumulator.close(wait=True)
    from lib.paper.tools import PaperSearchBackend, execute_paper_tool
    backend = PaperSearchBackend(
        web_search_one=lambda *a, **k: None, fetch_url_one=fetch,
        format_search_response=lambda *a, **k: '',
        format_search_display=lambda *a: [],
        format_fetch_display=lambda item, short: {'url': item['url']},
        vertical_header_for_llm=lambda *a: '', vertical_to_sse_payload=lambda *a: None,
    )
    result = execute_paper_tool('fetch_url', json.dumps({'urls': entries}),
                                search_backend=backend)
    return result[0], result[1]


@pytest.mark.parametrize('lane', ['stream', 'paper'])
def test_all_url_occurrences_overlap_and_keep_order(monkeypatch, lane):
    # Includes a repeated URL: equality never authorizes dropping an occurrence.
    urls = [f'https://example.test/{i}' for i in range(32)] + ['https://example.test/0']
    gate = threading.Barrier(len(urls), timeout=5)
    entered = []
    lock = threading.Lock()
    started = time.monotonic()

    def fetch(url, *args, **kwargs):
        with lock:
            entered.append(url)
        gate.wait()
        if url.endswith('/7'):
            raise RuntimeError('injected fetch failure')
        return _item(url)

    entries = [{'url': url} if i % 2 else url for i, url in enumerate(urls)]
    text, rows = _execute(monkeypatch, lane, fetch, ['', {}] + entries)
    print(f'{lane}: 33 joined URL occurrences in {time.monotonic() - started:.6f}s')
    assert sorted(entered) == sorted(urls)
    assert [row['url'] for row in rows] == urls
    assert 'Failed to fetch https://example.test/7.' in text
    assert 'evidence:https://example.test/31' in text
    positions = [text.index(f'Content from {url} ') for url in urls[:7] + urls[8:32]]
    assert positions == sorted(positions)
    assert text.count('Content from https://example.test/0 ') == 2
    if lane == 'stream':
        assert text.cacheable is False
        assert text.image_contents == tuple(url for url in urls if not url.endswith('/7'))
        assert text.result_producer_metadata == {'truncated': True}


@pytest.mark.parametrize('lane', ['stream', 'paper'])
def test_start_failure_keeps_siblings_and_every_failure_position(monkeypatch, lane):
    from lib.observability import InstrumentedThreadPoolExecutor
    original = InstrumentedThreadPoolExecutor.submit
    calls = []
    pools = []

    def submit(pool, fn, *args, **kwargs):
        if pool not in pools:
            pools.append(pool)
        # Each test uses only the URL batch pool; accumulator creates no pool
        # when executing directly.
        if args and args[-1] == 'https://example.test/7':
            raise RuntimeError('injected thread startup failure')
        return original(pool, fn, *args, **kwargs)

    monkeypatch.setattr(InstrumentedThreadPoolExecutor, 'submit', submit)
    urls = [f'https://example.test/{i}' for i in range(32)]

    def fetch(url, *args, **kwargs):
        calls.append(url)
        return _item(url)

    text, rows = _execute(monkeypatch, lane, fetch, urls)
    assert sorted(calls) == sorted(url for url in urls if not url.endswith('/7'))
    assert [row['url'] for row in rows] == urls
    assert 'Failed to fetch https://example.test/7.' in text
    assert 'evidence:https://example.test/31' in text
    assert pools and all(not pool._threads for pool in pools)


def test_batch_submission_failure_has_one_progress_receipt_per_item(monkeypatch):
    from lib.observability import InstrumentedThreadPoolExecutor
    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
    original = InstrumentedThreadPoolExecutor.submit
    receipts = []
    effects = []

    def submit(pool, fn, *args, **kwargs):
        if args[-1] == 7:
            raise RuntimeError('injected startup failure')
        return original(pool, fn, *args, **kwargs)

    monkeypatch.setattr(InstrumentedThreadPoolExecutor, 'submit', submit)
    def work(item):
        effects.append(item)
        return item
    result = run_batch_concurrent(list(range(32)), work, max_workers=1,
        on_item=lambda i, item, result, ok: receipts.append((i, result, ok)))
    assert result == [None if i == 7 else i for i in range(32)]
    assert sorted(effects) == [i for i in range(32) if i != 7]
    assert sorted(receipts) == [(i, None if i == 7 else i, i != 7) for i in range(32)]


def test_stream_batch_checks_stop_before_each_unstarted_url(monkeypatch):
    from lib.observability import InstrumentedThreadPoolExecutor
    task = {'id': 'stop-batch', 'aborted': False}
    jobs = []
    def submit(pool, fn, *args, **kwargs):
        future = Future()
        jobs.append((future, fn, args, kwargs))
        # Hold every job until publication of the whole batch, then stop wins.
        if len(jobs) == 32:
            task['aborted'] = True
            for future, callback, arguments, keywords in jobs:
                future.set_result(callback(*arguments, **keywords))
        return future
    monkeypatch.setattr(InstrumentedThreadPoolExecutor, 'submit', submit)
    def no_effect(*args, **kwargs):
        raise AssertionError('Stop must win before URL effects')
    urls = [f'https://example.test/{i}' for i in range(32)]
    text, rows = _execute(monkeypatch, 'stream', no_effect, urls, task)
    assert [row['url'] for row in rows] == urls
    assert text.count('Task aborted by user.') == 32
    assert text.cacheable is False
