"""Finite admission and no-duplicate semantics for classic PDF Futures."""

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
    return pool


def test_timeout_keeps_capacity_until_the_real_future_settles(
    isolated_pool,
    monkeypatch,
):
    pending = Future()
    assert pending.set_running_or_notify_cancel() is True
    fake_pool = _FuturePool([pending])
    monkeypatch.setattr(isolated_pool, '_get_pool', lambda: fake_pool)
    fallback_calls = []
    monkeypatch.setattr(
        isolated_pool,
        '_parse_pdf_inproc',
        lambda *_args, **_kwargs: fallback_calls.append(True),
    )

    with pytest.raises(isolated_pool.PdfParseTimeoutError):
        isolated_pool.parse_pdf_pooled(b'first', timeout=0.01)

    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 1
    assert isolated_pool.pdf_pool_metrics()['pool_unfinished'] == 1
    with pytest.raises(isolated_pool.PdfParseCapacityExceeded):
        isolated_pool.parse_pdf_pooled(b'second', timeout=0.01)
    from lib.pdf_parser.core import parse_pdf
    with pytest.raises(isolated_pool.PdfParseCapacityExceeded):
        parse_pdf(b'direct-call-shares-the-same-budget')
    from lib.pdf_parser.text import extract_pdf_text
    with pytest.raises(isolated_pool.PdfParseCapacityExceeded):
        extract_pdf_text(b'text-call-shares-the-same-budget')
    assert len(fake_pool.calls) == 1
    assert fallback_calls == []

    pending.set_result({'text': 'late but settled'})
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 0
    assert isolated_pool.pdf_pool_metrics()['pool_unfinished'] == 0


def test_deterministic_worker_error_is_not_parsed_twice(
    isolated_pool,
    monkeypatch,
):
    failed = Future()
    failed.set_exception(ValueError('malformed document'))
    fake_pool = _FuturePool([failed])
    monkeypatch.setattr(isolated_pool, '_get_pool', lambda: fake_pool)
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


@pytest.mark.parametrize('submission_error', [
    BrokenExecutor('not accepted'),
    RuntimeError('executor shut down before submit'),
])
def test_pre_submission_pool_failure_gets_one_bounded_fallback(
    isolated_pool,
    monkeypatch,
    submission_error,
):
    class BrokenPool:
        def submit(self, *_args, **_kwargs):
            raise submission_error

    reset_calls = []
    fallback_calls = []
    monkeypatch.setattr(isolated_pool, '_get_pool', BrokenPool)
    monkeypatch.setattr(
        isolated_pool, '_reset_pool', lambda: reset_calls.append(True))

    def fallback(payload, **kwargs):
        fallback_calls.append((payload, kwargs))
        return {'text': 'fallback'}

    monkeypatch.setattr(isolated_pool, '_parse_pdf_inproc', fallback)

    assert isolated_pool.parse_pdf_pooled(
        b'payload', max_images=0) == {'text': 'fallback'}
    assert reset_calls == [True]
    assert fallback_calls == [(b'payload', {'max_images': 0})]
    assert isolated_pool.pdf_pool_metrics()['unfinished'] == 0


def _pdf_upload():
    return {
        'file': FileStorage(
            stream=io.BytesIO(b'%PDF-1.4\nroute-budget-test'),
            filename='budget.pdf',
            content_type='application/pdf',
        ),
    }


def test_pdf_route_surfaces_capacity_as_retryable_503(
    flask_client,
    monkeypatch,
):
    from lib.pdf_parser import pool

    monkeypatch.setattr(
        pool,
        'parse_pdf_pooled',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pool.PdfParseCapacityExceeded('busy')),
    )

    response = flask_client.post(
        '/api/pdf/parse', form={}, files=_pdf_upload())

    assert response.status_code == 503
    assert response.headers['Retry-After'] == '1'
    body = response.get_json()
    assert body['error'] == 'busy'
    assert body['retryable'] is True


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


def test_paper_reparse_surfaces_capacity_as_retryable_503(
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
            pool.PdfParseCapacityExceeded('paper busy')),
    )

    response = flask_client.post(
        '/api/v1/paper/reparse', json={'filename': filename})

    assert response.status_code == 503
    assert response.headers['Retry-After'] == '1'
    body = response.get_json()
    assert body['error'] == 'paper busy'
    assert body['retryable'] is True


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
        '/api/v1/paper/reparse', json={'filename': filename})

    assert response.status_code == 504
    body = response.get_json()
    assert body['error'] == 'paper timed out'
    assert body['retryable'] is True


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
    monkeypatch.setattr(pool, '_POOL', fake_pool)
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
    assert pool._POOL is fake_pool
    assert shutdown_calls == []

    with pool._POOL_LOCK:
        pool._schedule_idle_retirement_locked()
    assert len(scheduled) == 2
    scheduled[1].fire()
    assert pool._POOL is None
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
        assert pool.pdf_pool_metrics()['unfinished_capacity'] == 2
    finally:
        pool.shutdown_pdf_pool()
