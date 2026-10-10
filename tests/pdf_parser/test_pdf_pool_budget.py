"""Demand-driven isolation and no-duplicate semantics for classic PDF Futures."""

from __future__ import annotations

from concurrent.futures import BrokenExecutor, Future
import io

import pytest
from werkzeug.datastructures import FileStorage


pytestmark = pytest.mark.unit


class _FuturePool:
    def __init__(self, futures):
        self.futures = list(futures)
        self.calls = []

    def shutdown(self, *, wait, cancel_futures):
        pass

    def submit(self, function, payload, **kwargs):
        self.calls.append((function, payload, kwargs))
        return self.futures.pop(0)


@pytest.fixture
def isolated_pool(monkeypatch):
    from lib.pdf_parser import core
    from lib.pdf_parser import pool
    from lib.pdf_parser import text
    from lib.pdf_parser.admission import _ParseAdmission

    admission = _ParseAdmission()
    monkeypatch.setattr(pool, '_ADMISSION', admission)
    monkeypatch.setattr(core, 'CLASSIC_PDF_ADMISSION', admission)
    monkeypatch.setattr(text, 'CLASSIC_PDF_ADMISSION', admission)
    monkeypatch.setenv('TOFU_PDF_PROCESSES', '1')
    monkeypatch.setenv('TOFU_PDF_PARSE_CAPACITY', '1')
    monkeypatch.setenv('TOFU_PDF_PARSE_TIMEOUT', '30')
    monkeypatch.setenv('TOFU_PDF_WORKER_IDLE_SECONDS', '0')
    monkeypatch.setattr(pool, "_WORKERS", set())
    monkeypatch.setattr(pool, "_POOL_FUTURES", 0)
    yield pool
    pool.shutdown_pdf_pool()


def test_timeout_keeps_exact_ownership_and_unrelated_work_starts(isolated_pool, monkeypatch):
    pending = [Future(), Future()]
    for future in pending:
        assert future.set_running_or_notify_cancel()
    pools = [_FuturePool([future]) for future in pending]
    factory = iter(pools)
    monkeypatch.setattr(isolated_pool, 'ProcessPoolExecutor', lambda **kwargs: next(factory))
    for payload in (b'first', b'second'):
        with pytest.raises(isolated_pool.PdfParseTimeoutError):
            isolated_pool.parse_pdf_pooled(payload, timeout=.01)
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 2
    assert isolated_pool.pdf_pool_metrics()['pool_unfinished'] == 2
    assert all(len(pool.calls) == 1 for pool in pools)
    for future in pending:
        future.set_result({'text':'late result'})
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 0
    assert isolated_pool.pdf_pool_metrics()['pool_unfinished'] == 0


def test_deterministic_worker_error_is_not_parsed_twice(
    isolated_pool,
    monkeypatch,
):
    failed = Future()
    failed.set_exception(ValueError('malformed document'))
    fake_pool = _FuturePool([failed])
    monkeypatch.setattr(isolated_pool, 'ProcessPoolExecutor', lambda **kwargs: fake_pool)
    fallback_calls = []
    monkeypatch.setattr(
        isolated_pool,
        '_parse_pdf_inproc',
        lambda *_args, **_kwargs: fallback_calls.append(True),
    )

    with pytest.raises(ValueError, match='malformed document'):
        isolated_pool.parse_pdf_pooled(b'bad')

    assert len(fake_pool.calls) == 1
    assert fallback_calls == []
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 0
    assert isolated_pool.pdf_pool_metrics()['pool_unfinished'] == 0


@pytest.mark.parametrize('construction_error', [OSError('host failure'), RuntimeError('creation failure')])
def test_construction_failure_before_publication_gets_one_fallback(isolated_pool, monkeypatch, construction_error):
    def construct(**kwargs):
        raise construction_error
    monkeypatch.setattr(isolated_pool, 'ProcessPoolExecutor', construct)
    calls=[]
    def fallback(payload, **kwargs):
        calls.append(payload)
        return {'text':'fallback'}
    monkeypatch.setattr(isolated_pool, '_parse_pdf_inproc', fallback)
    assert isolated_pool.parse_pdf_pooled(b'payload') == {'text':'fallback'}
    assert calls == [b'payload']
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 0


def _pdf_upload():
    return {
        'file': FileStorage(
            stream=io.BytesIO(b'%PDF-1.4\nroute-budget-test'),
            filename='budget.pdf',
            content_type='application/pdf',
        ),
    }





def test_pdf_route_surfaces_caller_timeout_as_504(
    flask_client,
    monkeypatch,
):
    from lib.pdf_parser import pool

    monkeypatch.setattr(
        pool,
        'parse_pdf_pooled',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pool.PdfParseTimeoutError('timed out')),
    )

    response = flask_client.post(
        '/api/pdf/parse', form={}, files=_pdf_upload())

    assert response.status_code == 504
    body = response.get_json()
    assert body['error'] == 'timed out'
    assert body['retryable'] is True


def _stored_paper_pdf(tmp_path, monkeypatch):
    from routes.paper_pkg import _pdf as paper_pdf_routes

    monkeypatch.setattr(paper_pdf_routes, 'PAPER_DIR', str(tmp_path))
    path = tmp_path / 'stored.pdf'
    path.write_bytes(b'%PDF-1.4\nstored-route-budget-test')
    return path.name





def test_paper_reparse_surfaces_timeout_as_retryable_504(
    flask_client,
    monkeypatch,
    tmp_path,
):
    from lib.pdf_parser import pool

    filename = _stored_paper_pdf(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pool,
        'parse_pdf_pooled',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pool.PdfParseTimeoutError('paper timed out')),
    )

    response = flask_client.post(
        '/api/v4/paper/reparse', json={'filename': filename})

    assert response.status_code == 504
    body = response.get_json()
    # See the 503 case above: v4 carries the message as the problem detail;
    # the legacy ``retryable`` extra is not part of the problem document.
    assert body['error']['message'] == 'paper timed out'


def test_idle_pool_generation_retires_without_racing_new_work(monkeypatch):
    from lib.pdf_parser import pool

    shutdown_calls = []
    scheduled = []

    class FakeProcessPool:
        def shutdown(self, *, wait, cancel_futures):
            shutdown_calls.append((wait, cancel_futures))

    class CapturedTimer:
        def __init__(self, interval, function, args=()):
            self.interval = interval
            self.function = function
            self.args = args
            self.daemon = False
            self.cancelled = False

        def start(self):
            scheduled.append(self)

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.function(*self.args)

    fake_pool = FakeProcessPool()
    worker = pool._ParserProcess(fake_pool, busy=False)
    monkeypatch.setattr(pool, '_WORKERS', {worker})
    monkeypatch.setattr(pool, '_POOL_FUTURES', 0)
    monkeypatch.setattr(pool.threading, 'Timer', CapturedTimer)
    monkeypatch.setattr(
        pool, 'classic_pdf_worker_idle_seconds', lambda: 60.0)

    with pool._POOL_LOCK:
        pool._schedule_idle_retirement_locked()

    assert len(scheduled) == 1
    assert scheduled[0].interval == 60.0
    with pool._POOL_LOCK:
        pool._invalidate_idle_retirement_locked()
    scheduled[0].fire()
    assert worker in pool._WORKERS
    assert shutdown_calls == []

    with pool._POOL_LOCK:
        pool._schedule_idle_retirement_locked()
    assert len(scheduled) == 2
    scheduled[1].fire()
    assert not pool._WORKERS
    assert shutdown_calls == [(False, True)]


@pytest.mark.serial
def test_real_spawn_pool_round_trip_is_bounded(monkeypatch):
    from lib.pdf_parser import pool

    pymupdf = pytest.importorskip('pymupdf')
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), 'bounded process pool')
    payload = document.tobytes()
    document.close()

    monkeypatch.setenv('TOFU_PDF_PROCESSES', '1')
    monkeypatch.setenv('TOFU_PDF_PARSE_CAPACITY', '2')
    monkeypatch.setenv('TOFU_PDF_PARSE_TIMEOUT', '30')
    monkeypatch.setenv('TOFU_PDF_WORKER_IDLE_SECONDS', '0')
    pool.shutdown_pdf_pool()
    try:
        result = pool.parse_pdf_pooled(
            payload,
            timeout=float('inf'),
            max_images=0,
            text_mode='fast',
        )
        assert result['totalPages'] == 1
        assert 'bounded process pool' in result['text']
        assert pool.pdf_pool_metrics()['unfinished'] == 0
        assert pool.pdf_pool_metrics()['unfinished_capacity'] == 0
    finally:
        pool.shutdown_pdf_pool()


def _real_blocked_pdf(payload, *, markers, release, **kwargs):
    import os
    import time
    from pathlib import Path
    from lib.pdf_parser.core import _parse_pdf_without_admission
    result = _parse_pdf_without_admission(payload, max_images=0, text_mode='fast')
    Path(markers, str(os.getpid())).write_text('entered')
    deadline = time.monotonic() + 20
    while not Path(release).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('test release did not arrive')
        time.sleep(.005)
    result['worker_pid'] = os.getpid()
    return result


@pytest.mark.serial
def test_real_pdf_burst_starts_beyond_idle_reuse_and_reaps_processes(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    import psutil
    import pymupdf
    from lib.pdf_parser import pool
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72,72),'independent demand PDF')
    payload = document.tobytes()
    document.close()
    markers = tmp_path / 'markers'
    markers.mkdir()
    release = tmp_path / 'release'
    monkeypatch.setenv('TOFU_PDF_PROCESSES','1')
    monkeypatch.setenv('TOFU_PDF_WORKER_IDLE_SECONDS','0')
    monkeypatch.setattr(pool,'_parse_pdf_inproc',_real_blocked_pdf)
    pool.shutdown_pdf_pool()
    futures = []
    try:
        with ThreadPoolExecutor(max_workers=8) as callers:
            for _ in range(8):
                futures.append(callers.submit(pool.parse_pdf_pooled,payload,
                    markers=str(markers),release=str(release),timeout=30))
            try:
                deadline = time.monotonic()+15
                while len(list(markers.iterdir())) != 8:
                    assert time.monotonic()<deadline
                    time.sleep(.01)
                metrics = pool.pdf_pool_metrics()
                assert metrics['pool_unfinished'] == metrics['workers'] == 8
                assert metrics['idle_reuse_workers'] == 1
            finally:
                release.touch()
            results = [future.result(timeout=10) for future in futures]
        assert all('independent demand PDF' in result['text'] for result in results)
        pids = {result['worker_pid'] for result in results}
        assert len(pids) == 8
    finally:
        release.touch()
        pool.shutdown_pdf_pool()
    deadline = time.monotonic()+5
    while any(psutil.pid_exists(pid) for pid in pids):
        assert time.monotonic()<deadline
        time.sleep(.01)
    assert pool.pdf_pool_metrics()['unfinished'] == 0


def test_submission_failure_does_not_start_a_duplicate_inline_parse(isolated_pool, monkeypatch):
    import threading
    entered = threading.Event()
    release = threading.Event()
    class AmbiguousPool:
        def submit(self,*args,**kwargs):
            raise RuntimeError('publication failed')
        def shutdown(self,*,wait,cancel_futures):
            assert wait
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(isolated_pool,'ProcessPoolExecutor',lambda **kwargs: AmbiguousPool())
    monkeypatch.setattr(isolated_pool,'_parse_pdf_inproc',lambda *args,**kwargs: pytest.fail('duplicate parse'))
    try:
        with pytest.raises(RuntimeError,match='publication failed'):
            isolated_pool.parse_pdf_pooled(b'unknown publication')
        assert entered.wait(1)
        assert isolated_pool.pdf_pool_metrics()['unfinished'] == 1
    finally:
        release.set()
    import time
    deadline = time.monotonic()+2
    while isolated_pool.pdf_pool_metrics()['unfinished']:
        assert time.monotonic()<deadline
        time.sleep(.005)


def test_actual_parser_timeout_is_not_mistaken_for_caller_wait_expiry(isolated_pool, monkeypatch):
    completed = Future()
    completed.set_exception(TimeoutError('parser operation failed'))
    monkeypatch.setattr(isolated_pool,'ProcessPoolExecutor',lambda **kwargs: _FuturePool([completed]))
    with pytest.raises(TimeoutError,match='parser operation failed') as failure:
        isolated_pool.parse_pdf_pooled(b'payload')
    assert not isinstance(failure.value,isolated_pool.PdfParseTimeoutError)
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 0


def test_cleanup_join_failure_retains_ownership_until_retry_succeeds(isolated_pool, monkeypatch):
    import threading
    import time
    failed_join = threading.Event()
    release = threading.Event()
    class FailingJoin:
        def __init__(self):
            self.attempts=0
        def submit(self,*args,**kwargs):
            raise RuntimeError('ambiguous publication')
        def shutdown(self,*,wait,cancel_futures):
            if not wait:
                return
            self.attempts+=1
            if self.attempts==1:
                failed_join.set()
                raise OSError('join failed')
            assert release.wait(3)
    executor=FailingJoin()
    monkeypatch.setattr(isolated_pool,'ProcessPoolExecutor',lambda **kwargs:executor)
    try:
        with pytest.raises(RuntimeError,match='ambiguous publication'):
            isolated_pool.parse_pdf_pooled(b'payload')
        assert failed_join.wait(1)
        assert isolated_pool.pdf_pool_metrics()['unfinished']==1
    finally:
        release.set()
    deadline=time.monotonic()+2
    while isolated_pool.pdf_pool_metrics()['unfinished']:
        assert time.monotonic()<deadline
        time.sleep(.005)
    assert executor.attempts>=2
