"""The admitted fetch batch retains every requested source, including failures."""
import threading

import pytest

from tests.tasks_pkg.test_fetch_http_failure_evidence import (
    fetch_environment as fetch_environment, dispatch_fetch, unavailable_source,
)
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.api


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_batch_larger_than_ten_retains_all_admitted_urls(mode, fetch_environment, observations, monkeypatch, record_property):
    from lib.tasks_pkg.handlers.search import _core
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    original = _core._fetch_url_one
    lock, release = threading.Lock(), threading.Event()
    timer = threading.Timer(.05, release.set)
    active = peak = 0
    started = False
    def observed(*args, **kwargs):
        nonlocal active, peak, started
        with lock:
            active += 1
            peak = max(peak, active)
            if not started:
                started = True
                timer.start()
        try:
            assert release.wait(5), 'source probe was not released'
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(_core, '_fetch_url_one', observed)
    task = recovery_task(_handle_fetch_url, 'read_only', mode)
    try:
        with unavailable_source() as (url, requests):
            urls = [url] + [url + '?ok'] * 12
            row, messages, settled = dispatch_fetch(task, {'urls': [{'url': value} for value in urls]})
    finally:
        release.set()
        if started:
            timer.join()
    assert len(requests) == len(urls)
    assert len(row['results']) == len(urls)
    assert messages[0]['content'].count('recovered-source') == 12
    assert '3600 seconds' in messages[0]['content']
    assert settled[0].execution_status == 'ok' and row['_cacheableResult'] is False
    assert active == 0
    record_property('requested_sources', len(urls))
    record_property('http_requests', len(requests))
    record_property('peak_active_sources', peak)
