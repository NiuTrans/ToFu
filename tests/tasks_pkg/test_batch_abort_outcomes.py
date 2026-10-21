"""Batch cancellation preserves occurrence results and truthful progress.

Exercise the shared adapter and search/fetch handlers with real demand workers;
external retrieval and event persistence are the only replaced boundaries.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers._adapter import run_batch_concurrent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('inherit_context', [False, True])
def test_pre_cancelled_batch_never_reports_skipped_items_as_success(inherit_context):
    entered = []
    receipts = []
    items = ['duplicate', 'duplicate', 'other']

    results = run_batch_concurrent(
        items, lambda item: entered.append(item), max_workers=1,
        abort=lambda: True, inherit_context=inherit_context,
        on_item=lambda *receipt: receipts.append(receipt))

    assert entered == []
    assert results == [None, None, None]
    assert sorted(receipts) == [
        (0, 'duplicate', None, False),
        (1, 'duplicate', None, False),
        (2, 'other', None, False),
    ]


def test_mid_batch_stop_preserves_completed_and_running_siblings(monkeypatch):
    from lib.observability import InstrumentedThreadPoolExecutor

    original_submit = InstrumentedThreadPoolExecutor.submit
    completed = threading.Event()
    running = threading.Event()
    stopped = threading.Event()
    release = threading.Event()
    entered = []
    receipts = []
    submissions = 0

    def submit(pool, fn, *args, **kwargs):
        nonlocal submissions
        submissions += 1
        if submissions == 3:
            assert completed.wait(5)
            assert running.wait(5)
            stopped.set()
        return original_submit(pool, fn, *args, **kwargs)

    def worker(item):
        entered.append(item)
        if item == 0:
            completed.set()
        elif item == 1:
            running.set()
            assert release.wait(5)
        return item * 10

    def on_item(index, item, result, ok):
        receipts.append((index, item, result, ok))
        if index == 2:
            release.set()

    monkeypatch.setattr(InstrumentedThreadPoolExecutor, 'submit', submit)
    try:
        results = run_batch_concurrent(
            list(range(5)), worker, max_workers=1, abort=stopped.is_set,
            on_item=on_item)
    finally:
        release.set()

    assert sorted(entered) == [0, 1]
    assert results == [0, 10, None, None, None]
    assert sorted(receipts) == [
        (0, 0, 0, True), (1, 1, 10, True),
        (2, 2, None, False), (3, 3, None, False), (4, 4, None, False),
    ]


def test_successful_empty_results_remain_successful():
    receipts = []
    values = [None, False, 0, '', [], {}]
    results = run_batch_concurrent(
        values, lambda value: value, max_workers=1, abort=lambda: False,
        on_item=lambda *receipt: receipts.append(receipt))

    assert results == values
    assert sorted(receipts) == [
        (index, value, value, True) for index, value in enumerate(values)
    ]


def test_broken_abort_observer_does_not_drop_work():
    receipts = []

    def abort():
        raise RuntimeError('injected observer failure')

    results = run_batch_concurrent(
        [1, 2], lambda item: item * 2, max_workers=1, abort=abort,
        on_item=lambda *receipt: receipts.append(receipt))

    assert results == [2, 4]
    assert sorted(receipts) == [(0, 1, 2, True), (1, 2, 4, True)]


def test_progress_observer_failure_does_not_lose_cancelled_occurrences():
    receipts = []

    def on_item(*receipt):
        receipts.append(receipt)
        raise RuntimeError('injected progress failure')

    results = run_batch_concurrent(
        ['first', 'second'], lambda item: item, max_workers=1,
        abort=lambda: True, on_item=on_item)

    assert results == [None, None]
    assert sorted(receipts) == [
        (0, 'first', None, False), (1, 'second', None, False),
    ]


@pytest.mark.parametrize('tool_name', ['web_search', 'fetch_url'])
def test_cancelled_search_and_fetch_progress_marks_each_occurrence_unsuccessful(
        monkeypatch, tool_name):
    from lib.tasks_pkg.executor import _finalize
    from lib.tasks_pkg.handlers.search import _handlers

    events = []
    entered = []

    def retrieve(*args, **kwargs):
        entered.append(args)
        raise AssertionError('cancelled retrieval must not run')

    backend = SimpleNamespace(_web_search_one=retrieve, _fetch_url_one=retrieve)
    monkeypatch.setattr(_handlers, '_load_search_backend',
                        lambda: (backend, lambda *args, **kwargs: ''))
    monkeypatch.setattr(_handlers, 'append_event',
                        lambda task, event: events.append(event))
    monkeypatch.setattr(_finalize, 'append_event',
                        lambda task, event: events.append(event))
    task = {'id': 'cancelled-batch', 'aborted': True, 'lastUserQuery': 'evidence'}
    row = {'roundNum': 1, 'toolCallId': 'batch-1', 'toolName': tool_name}
    if tool_name == 'web_search':
        handler = _handlers._handle_web_search_batch
        entries = ['first query', 'second query']
        arguments = {'queries': entries}
    else:
        handler = _handlers._handle_fetch_url_batch
        entries = ['https://example.test/first', 'https://example.test/second']
        arguments = {'urls': entries}

    call_id, text, is_search = handler(
        task, {}, tool_name, 'batch-1', arguments, entries, 1, row, {}, '/tmp', False)

    assert entered == []
    assert call_id == 'batch-1' and text and is_search
    progress = [event for event in events if event['type'] == 'tool_progress']
    assert sorted(event['batchItem'] for event in progress) == sorted(entries)
    assert [event['batchDone'] for event in progress] == [1, 2]
    assert all(event['batchTotal'] == 2 and event['batchOk'] is False
               for event in progress)
    assert row['_cacheableResult'] is False
